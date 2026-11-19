import os
import posixpath
import time
from urllib.parse import urlsplit, urlunsplit

import requests


class Aria2Error(RuntimeError):
    """Aria2 RPC 调用失败（网络/协议/业务错误）。"""


class Aria2ConfigError(Aria2Error):
    """地址、令牌或协议层面的错误：重试不可能成功，应直接让用户改配置。"""


def normalize_rpc_url(host: str) -> str:
    """把用户填的地址补成 aria2 的 JSON-RPC 端点。

    最常见的错是只填 http://主机:6800（缺 /jsonrpc）：aria2 对任何路径都返回
    JSON-RPC 错误或 404，逐个文件重试也永远失败。这里只补路径、不猜端口——
    端口填错（例如填成 AriaNg 的 443）由启动前预检报错，文案里会给出应有的形状。
    """
    text = str(host or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    path = parts.path or ""
    if path in ("", "/"):
        path = "/jsonrpc"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


# aria2 的错误码决定「换个时间重试有没有用」。只看 errorMessage 没用：
# 远端 aria2 的下载目录不可写时，aria2 只回一句 "Download aborted."（errorCode=16），
# 页面上看起来就是「任务提交成功但速度一直 0」。
_ARIA2_ERROR_HINTS = {
    9: "aria2 所在机器磁盘空间不足",
    13: "目标文件已存在，且 aria2 未允许覆盖",
    16: "aria2 无法在下载目录创建/写入文件（目录只读、不存在，或文件被占用）",
    17: "aria2 文件系统错误（磁盘异常或路径不可用）",
    18: "aria2 无法创建下载子目录（父目录不可写或不存在）",
    19: "下载地址的域名解析失败",
    22: "HTTP 响应异常（直链可能已过期，或对方拦截了请求）",
    24: "HTTP 鉴权失败（直链需要 Cookie/User-Agent）",
}
# 目录写不进去这类错误改配置才能好，重试没有意义（否则同一文件白白重试 3 轮）
PERMANENT_ARIA2_ERROR_CODES = (16, 18)


def plan_download_dirs(download_dir: str, destination_dir: str, *, remote: bool = False) -> dict[str, str]:
    """决定这次任务怎么用目录（远端一律留空）。

    两个目录都可选，组合出三种本机模式：
    1. 有临时目录：临时目录只当「下载中的暂存区」（提交时带 <任务名>）；下完挪到最终位置——
       配了完成目录就挪过去，没配就从 `<临时>/<任务名>/` 挪到 `<临时>/` 根下（最终目录里看不到任务名那一层）
    2. 没有临时目录、有完成目录：直接下到 `<完成>/<任务名>/`，下完在同一目录内挪一层去掉任务名
    3. 两个都没有：提交任务时不指定 dir，交给 aria2 自己的默认目录；本机不知道落点，所以不校验、不移动

    返回 `{"download_dir", "destination_dir", "mode"}`，mode ∈ temp / dest / none / remote。
    download_dir 是「暂存根目录」（Aria2Client/FileApi 会再拼任务名子目录），destination_dir 是最终落点。
    """
    temp = str(download_dir or "").strip()
    dest = str(destination_dir or "").strip()
    if remote:
        return {"download_dir": "", "destination_dir": "", "mode": "remote"}
    if temp:
        return {"download_dir": temp, "destination_dir": dest or temp, "mode": "temp"}
    if dest:
        return {"download_dir": dest, "destination_dir": dest, "mode": "dest"}
    return {"download_dir": "", "destination_dir": "", "mode": "none"}


def describe_task_error(task: dict) -> str:
    """把 aria2 的任务错误翻译成人话；看不懂的错误码原样返回 errorMessage。"""
    if not isinstance(task, dict):
        return "未知错误"
    message = str(task.get("errorMessage") or "").strip() or "未知错误"
    try:
        code = int(task.get("errorCode") or 0)
    except (TypeError, ValueError):
        code = 0
    hint = _ARIA2_ERROR_HINTS.get(code, "")
    if not hint:
        return message
    files = task.get("files")
    path = ""
    if isinstance(files, list) and files and isinstance(files[0], dict):
        path = str(files[0].get("path") or "").strip()
    target = f"；目标路径: {path}" if path else ""
    detail = f"{hint}（aria2 errorCode={code}: {message}）{target}"
    if "412" in message:
        # 412 = CDN 主动拒绝直链：Cookie 少了 web-grey-id / web-grey-id.sig 这类防风控字段，
        # 接口照样 200（所以「测试 Cookie 有效」看不出来），换直链也成功，只有真去下载才 412
        detail += (
            "；412 = 直链被夸克 CDN 拒绝，通常是 Cookie 缺少防风控字段（web-grey-id / web-grey-id.sig），"
            "到设置页重新保存 Cookie（会自动补齐）或重新扫码后再试"
        )
    return detail


class Aria2Client:
    def __init__(
        self,
        host: str,
        secret: str,
        download_dir: str,
        request_timeout: int,
        download_headers: list[str] | None,
        poll_interval: int,
        download_timeout: int,
        logger,
        work_dir: str = "",
        remote: bool = False,
        remote_dir: str = "",
    ):
        self.host = normalize_rpc_url(host)
        self.secret = secret
        # 远端 aria2（朋友提供的 RPC）：只提交下载请求 + 查进度，不知道也不该管对方的落盘目录
        self.remote = bool(remote)
        # 远端目录：可选。对方 aria2 的默认目录常常不可写（实测 /downloads 只读 → errorCode=16），
        # 填了就用它当 dir；留空则完全交给对方配置。
        self.remote_dir = str(remote_dir or "").strip().rstrip("/")
        # 下载根目录；若指定 work_dir（任务名子目录），则该任务独占 <root>/<任务名>，
        # 避免多任务同名文件互相覆盖（任务名唯一，见 job_paths.work_dir_name）
        self._base_download_dir = str(download_dir or "").rstrip("/")
        self.work_dir = str(work_dir or "").strip()
        # 直链下载请求头（Cookie/Referer/UA）：夸克直链校验来源，缺失会 403
        self.download_headers = [str(h) for h in (download_headers or []) if str(h).strip()]
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval
        self.download_timeout = download_timeout
        self.log = logger
        self._id = 0

    @property
    def download_dir(self) -> str:
        # 没有暂存根目录（远端 / 两个目录都没填）时返回空：提交任务时不带 dir，
        # 交给 aria2 自己的默认目录 —— 绝对不能拼出相对路径（会落到 aria2 的 cwd）。
        if not self._base_download_dir:
            return ""
        if self.work_dir:
            return f"{self._base_download_dir}/{self.work_dir}"
        return self._base_download_dir

    def _aria2_call(self, method: str, params: list):
        self._id += 1
        secret_param = f"token:{self.secret}" if self.secret else ""
        full_params = ([secret_param] + params) if secret_param else params
        payload = {"jsonrpc": "2.0", "id": str(self._id), "method": method, "params": full_params}

        start_time = time.time()
        try:
            r = requests.post(self.host, json=payload, timeout=self.request_timeout)
        except requests.RequestException as e:
            raise Aria2Error(f"连接 Aria2 失败（{self.host}）: {e}") from e
        elapsed = time.time() - start_time

        # 先解析再判状态码：aria2 的令牌错误是 HTTP 400 + JSON-RPC error，
        # 先 raise_for_status() 只会得到 "400 Client Error"，看不出是 rpc-secret 不对。
        content_type = (r.headers.get("Content-Type") or "").lower()
        text = (r.text or "").strip()
        data = None
        if "json" in content_type or text.startswith("{"):
            try:
                data = r.json()
            except ValueError:
                data = None
        if not isinstance(data, dict):
            # 典型场景：把 AriaNg 网页地址、静态文件站或 501 的目录当成 RPC 地址
            snippet = text[:120].replace("\n", " ")
            raise Aria2ConfigError(
                f"Aria2 RPC 地址不对（{self.host}）: HTTP {r.status_code}"
                f" {content_type or '未知 Content-Type'}，返回的不是 JSON-RPC。"
                f"RPC 地址形如 http://主机:6800/jsonrpc；"
                f"AriaNg 网页地址（网页控制台）不能当 RPC 用。响应片段: {snippet}"
            )

        error = data.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = str(error.get("message") or "")
            if code in (1, "1") or "unauthorized" in message.lower():
                raise Aria2ConfigError(
                    f"Aria2 RPC 未授权（{self.host}）: 令牌（rpc-secret）不对，或对方没配 secret"
                )
            raise Aria2Error(f"Aria2 RPC 错误（{method}）: {error}")
        if r.status_code >= 400:
            raise Aria2Error(f"Aria2 RPC HTTP {r.status_code}（{self.host}）: {text[:120]}")
        if "result" not in data:
            raise Aria2Error(f"Aria2 RPC 响应缺少 result（{self.host}）: {text[:120]}")

        # 记录请求耗时
        self.log.debug(f"Aria2 API 请求耗时: {method} - {elapsed:.3f}s")
        return data["result"]

    @staticmethod
    def _normalize_relative_path(path: str) -> str:
        normalized = posixpath.normpath(str(path).replace("\\", "/").strip("/"))
        if normalized in ("", "."):
            return ""
        if normalized.startswith("../") or normalized == "..":
            raise ValueError(f"非法相对路径: {path}")
        return normalized

    def aria2_add_url(self, url: str, filename: str) -> str:
        relative_path = self._normalize_relative_path(filename)
        relative_dir = relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""
        output_name = relative_path.rsplit("/", 1)[-1]
        if self.remote:
            # 远端：默认不指定 dir（落在对方 aria2 自己的目录）；配置里填了「远端下载目录」
            # 就用它，否则对方默认目录不可写时每个文件都只会得到 errorCode=16。
            # 无论哪种都要保留子目录，否则不同子目录下的同名文件会在对方机器上互相覆盖。
            options = {"out": relative_path}
            if self.remote_dir:
                options["dir"] = (
                    posixpath.join(self.remote_dir, relative_dir) if relative_dir else self.remote_dir
                )
        elif self.download_dir:
            options = {"dir": os.path.join(self.download_dir, relative_dir), "out": output_name}
        else:
            # 两个目录都没填：不指定 dir（用 aria2 自己的默认目录），但保留子目录，
            # 否则不同子目录下的同名文件会互相覆盖。
            options = {"out": relative_path}
        if self.download_headers:
            options["header"] = list(self.download_headers)
        gid = self._aria2_call("aria2.addUri", [[url], options])
        return gid

    def aria2_get_status(self, gid: str) -> dict:
        return self._aria2_call("aria2.tellStatus", [gid])

    def describe_task_error(self, task: dict) -> str:
        """tellStatus 的 error 结果 → 能照着修的说明（见 describe_task_error）。"""
        return describe_task_error(task)

    def _match_key(self, path: str) -> str:
        """按路径对齐 Aria2 任务时用的键：完整相对路径（远端也带子目录）。"""
        return self._normalize_relative_path(path)

    @staticmethod
    def _path_matches(rel: str, wanted: set[str]) -> bool:
        """判断 aria2 任务还原出来的相对路径是否在目标集合里。

        兼容历史任务：早期远端提交只给 out=文件名，还原出来就是裸文件名，
        这类键没有子目录，只能按文件名比一次（可能撞名，仅作兜底）。
        """
        if rel in wanted:
            return True
        if "/" in rel:
            return False
        return any(key.rsplit("/", 1)[-1] == rel for key in wanted)

    def _extract_task_filename(self, task: dict) -> str:
        """把 aria2 任务的文件绝对路径还原为相对路径；不属于本任务下载目录的返回空串。"""
        if not isinstance(task, dict):
            return ""
        files = task.get("files", [])
        if isinstance(files, list) and files:
            first = files[0] if isinstance(files[0], dict) else {}
            path = str(first.get("path", "")).strip()
            if path:
                normalized_path = path.replace("\\", "/").rstrip("/")
                task_dir = str(task.get("dir") or "").replace("\\", "/").rstrip("/")
                # 提交时 dir = 根目录/<子目录>、out = 文件名，所以 tellStatus 的 dir 是「子目录」，
                # 直接用 task_dir 剥只剩文件名，和提交时用的完整相对路径对不上 → 重启后
                # 会把在下的任务当成新任务再提交一遍（重复下载、多个 .1 副本）。
                # 因此先按「根目录」还原：本机用 download_dir，远端用 remote_dir。
                base = self.remote_dir if self.remote else self.download_dir
                normalized_base = str(base or "").replace("\\", "/").rstrip("/")
                if normalized_base and normalized_path.startswith(f"{normalized_base}/"):
                    return normalized_path[len(normalized_base) + 1 :]
                if normalized_base and normalized_path == normalized_base:
                    return ""
                # 兜底：老任务 / 换了根目录的任务，按 task_dir 还原（可能只剩文件名）
                if task_dir and normalized_path.startswith(f"{task_dir}/"):
                    return normalized_path[len(task_dir) + 1 :]
                if self.remote:
                    # 远端拿不到 dir（老任务/对方省略）：退回文件名，兼容历史任务
                    return normalized_path.rsplit("/", 1)[-1] if normalized_path else ""
                # 没配根目录（用 aria2 默认目录）且 tellStatus 没给 dir：无从还原，宁可不匹配，
                # 也不要拿绝对路径去和别人撞键
                return ""
        bittorrent = task.get("bittorrent", {})
        info = bittorrent.get("info", {}) if isinstance(bittorrent, dict) else {}
        name = str(info.get("name", "")).strip()
        return name

    def aria2_get_global_stat(self) -> dict:
        """返回 aria2.getGlobalStat 结果（含 numActive/numWaiting/numStopped）。"""
        result = self._aria2_call("aria2.getGlobalStat", [])
        return result if isinstance(result, dict) else {}

    def aria2_get_version(self) -> dict:
        """返回 aria2.getVersion 结果（version/enabledFeatures），设置页「测试」用。"""
        result = self._aria2_call("aria2.getVersion", [])
        return result if isinstance(result, dict) else {}

    def aria2_get_global_option(self) -> dict:
        """返回 aria2.getGlobalOption 结果；设置页用它的 dir 说明文件会落到哪。"""
        result = self._aria2_call("aria2.getGlobalOption", [])
        return result if isinstance(result, dict) else {}

    def aria2_get_waiting_count(self) -> int:
        """排队中（waiting）任务数量。"""
        stat = self.aria2_get_global_stat()
        try:
            return int(stat.get("numWaiting", 0))
        except (TypeError, ValueError):
            return 0

    def aria2_get_existing_tasks_by_name(self) -> dict[str, str]:
        """
        返回 filename -> status 的映射。
        status 可能是 active/waiting/paused/error/complete/removed。
        """
        return {
            name: task_info["status"]
            for name, task_info in self.aria2_get_existing_task_infos_by_name().items()
        }

    @staticmethod
    def _task_status_priority(status: str) -> int:
        if status in ("active", "waiting", "paused"):
            return 4
        if status == "complete":
            return 3
        if status == "error":
            return 2
        if status == "removed":
            return 1
        return 0

    def aria2_get_existing_task_infos_by_name(self) -> dict[str, dict]:
        """
        返回 filename -> {gid, status} 的映射。
        保留 gid 方便接管已经存在的下载任务并继续监控。
        """
        status_by_name: dict[str, str] = {}
        info_by_name: dict[str, dict] = {}
        methods = (
            ("aria2.tellActive", []),
            ("aria2.tellWaiting", [0, 1000]),
            ("aria2.tellStopped", [0, 1000]),
        )
        for method, params in methods:
            tasks = self._aria2_call(method, params)
            if not isinstance(tasks, list):
                continue
            for task in tasks:
                fname = self._extract_task_filename(task)
                if not fname:
                    continue
                task_status = str(task.get("status", "")).strip() or "unknown"
                old_status = status_by_name.get(fname)
                if (
                    old_status is None
                    or self._task_status_priority(task_status) > self._task_status_priority(old_status)
                ):
                    status_by_name[fname] = task_status
                    info_by_name[fname] = {
                        "gid": str(task.get("gid", "")).strip(),
                        "status": task_status,
                    }
        if self.remote:
            # 远端老任务是按裸文件名提交的：补一个文件名别名，避免每次都被当成新任务重复提交。
            # 仅在完整路径键不存在时补，新任务仍以完整相对路径为准。
            for key in [k for k in info_by_name if "/" in k]:
                info_by_name.setdefault(key.rsplit("/", 1)[-1], info_by_name[key])
        return info_by_name

    def aria2_wait_done(self, gid: str, filename: str) -> bool:
        deadline = time.time() + self.download_timeout
        while time.time() < deadline:
            status = self.aria2_get_status(gid)
            state = status.get("status")
            if state == "complete":
                self.log.info(f"  ✓ 下载完成: {filename}")
                return True
            if state == "error":
                self.log.error(f"  ✗ 下载出错: {filename} | {status.get('errorMessage')}")
                return False
            completed = int(status.get("completedLength", 0))
            total = int(status.get("totalLength", 1))
            speed = int(status.get("downloadSpeed", 0))
            pct = completed / total * 100 if total else 0
            self.log.info(f"  ↓ {filename}  {pct:.1f}%  速度: {speed//1024} KB/s")
            time.sleep(self.poll_interval)
        self.log.warning(f"  ⏰ 下载超时: {filename}")
        return False

    def aria2_remove(self, gid: str, *, force: bool = True) -> None:
        """移除 Aria2 任务。"""
        method = "aria2.forceRemove" if force else "aria2.remove"
        try:
            self._aria2_call(method, [gid])
        except Exception as e:
            # 任务可能已结束
            self.log.warning(f"  Aria2 移除任务失败 gid={gid}: {e}")

    def aria2_pause(self, gid: str, *, force: bool = True) -> None:
        method = "aria2.forcePause" if force else "aria2.pause"
        self._aria2_call(method, [gid])

    def aria2_unpause(self, gid: str) -> None:
        self._aria2_call("aria2.unpause", [gid])

    def aria2_find_task_by_path(self, relative_path: str) -> dict | None:
        """按相对路径查找本任务下载目录下的 active/waiting/paused 任务。

        只做完整相对路径精确匹配：不同目录下的同名文件（如 cover.jpg）不会被误认领。
        """
        wanted = self._match_key(relative_path)
        if not wanted:
            return None
        infos = self.aria2_get_existing_task_infos_by_name()
        found = infos.get(wanted)
        if found is None and "/" in wanted:
            # 历史任务可能只按文件名提交
            found = infos.get(wanted.rsplit("/", 1)[-1])
        return found

    def aria2_list_tasks(self, *, include_stopped: bool = False) -> list[dict]:
        tasks: list[dict] = []
        methods = [
            ("aria2.tellActive", []),
            ("aria2.tellWaiting", [0, 5000]),
        ]
        if include_stopped:
            methods.append(("aria2.tellStopped", [0, 5000]))
        for method, params in methods:
            result = self._aria2_call(method, params)
            if isinstance(result, list):
                tasks.extend(result)
        return tasks

    def aria2_remove_by_paths(
        self,
        paths: set[str],
        *,
        remove_active: bool,
        remove_waiting: bool,
    ) -> dict[str, list[str]]:
        """
        按相对路径移除 Aria2 任务。
        返回 {"removed_waiting": [...], "removed_active": [...]}。
        """
        wanted = {self._match_key(p) for p in paths if p}
        removed_waiting: list[str] = []
        removed_active: list[str] = []
        if not wanted:
            return {"removed_waiting": removed_waiting, "removed_active": removed_active}

        for task in self.aria2_list_tasks(include_stopped=False):
            status = str(task.get("status", "")).lower()
            gid = str(task.get("gid", "")).strip()
            name = self._extract_task_filename(task)
            if not gid or not name:
                continue
            rel = self._normalize_relative_path(name)
            if not self._path_matches(rel, wanted):
                continue
            if status == "waiting" and remove_waiting:
                self.aria2_remove(gid, force=True)
                removed_waiting.append(rel)
            elif status in ("active", "paused") and remove_active:
                self.aria2_remove(gid, force=True)
                removed_active.append(rel)
        return {"removed_waiting": removed_waiting, "removed_active": removed_active}

    def aria2_pause_by_paths(
        self,
        paths: set[str],
        *,
        pause_active: bool = True,
        pause_waiting: bool = True,
    ) -> dict[str, list[str]]:
        """按相对路径暂停 Aria2 任务（保留进度，便于下次 unpause）。"""
        wanted = {self._match_key(p) for p in paths if p}
        paused_waiting: list[str] = []
        paused_active: list[str] = []
        if not wanted:
            return {"paused_waiting": paused_waiting, "paused_active": paused_active}

        for task in self.aria2_list_tasks(include_stopped=False):
            status = str(task.get("status", "")).lower()
            gid = str(task.get("gid", "")).strip()
            name = self._extract_task_filename(task)
            if not gid or not name:
                continue
            rel = self._normalize_relative_path(name)
            if not self._path_matches(rel, wanted):
                continue
            if status == "paused":
                if pause_waiting or pause_active:
                    # 已是暂停，记入对应列表便于统计
                    paused_active.append(rel)
                continue
            try:
                if status == "waiting" and pause_waiting:
                    self.aria2_pause(gid, force=True)
                    paused_waiting.append(rel)
                elif status == "active" and pause_active:
                    self.aria2_pause(gid, force=True)
                    paused_active.append(rel)
            except Exception as e:
                self.log.warning(f"  Aria2 暂停失败 {rel} gid={gid}: {e}")
        return {"paused_waiting": paused_waiting, "paused_active": paused_active}
