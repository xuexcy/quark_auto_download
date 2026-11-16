import html
import re
import time

import requests


QUARK_DRIVE_BASE_URL = "https://drive.quark.cn"

# 取直链接口：pc 端域名与主域名同路径，按顺序试（不同账号/Cookie 生效域名不同）
QUARK_DOWNLOAD_URL_BASES = ("https://drive.quark.cn", "https://drive-pc.quark.cn")

# 配额接口候选路径：优先取接口给的真实容量，拿不到再退回"本地递归统计"。
_QUOTA_ENDPOINTS = ("/1/clouddrive/member", "/1/clouddrive/config")

# 只允许十六进制 fid：用于区分"真 fid"与"fid-目录名"这类人类可读写法
_FID_RE = re.compile(r"^[0-9a-fA-F]{16,}$")


def _is_ok(data) -> bool:
    """夸克接口成功判定（历史上有 code==0 与 status==200 两套口径）。

    失败响应既可能只给非 0 的 code，也可能只给非 200 的 status，因此要求
    「有任一成功标记，且没有任何失败标记」。
    """
    if not isinstance(data, dict):
        return False
    code = data.get("code")
    status = data.get("status")
    if code is None and status is None:
        return False
    return code in (0, None) and status in (200, None)


def _payload_brief(data) -> str:
    """错误信息里截断响应体，避免把整段 JSON 写进日志/页面。"""
    text = str(data or "")
    return text if len(text) <= 500 else text[:500] + "…"


def _explain_task_error(data) -> str:
    """把任务查询里的业务错误码翻成人话（目前只识别 32003=网盘容量不足）。"""
    brief = _payload_brief(data)
    code = data.get("code") if isinstance(data, dict) else None
    try:
        code_int = int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        code_int = None
    if code_int != 32003:
        return brief
    remain = 0
    node = data.get("data") if isinstance(data, dict) else None
    if isinstance(node, dict):
        try:
            remain = int(node.get("remain_capacity") or 0)
        except (TypeError, ValueError):
            remain = 0
    hint = f"，接口报可用约 {remain / 1048576:.0f} MB" if remain > 0 else ""
    return (
        "网盘容量不足（32003 capacity limit"
        + hint
        + "）：先删掉网盘里已转存的副本、清空回收站，或等本次下载完成清理后再重试 | "
        + brief
    )


class QuarkDownloadRejected(RuntimeError):
    """夸克明确拒绝下发直链（如 23018 单文件超限）：换域名或重试都没有意义。"""


def _is_download_size_limit(data) -> bool:
    """识别 23018「download file size limit」。"""
    if not isinstance(data, dict):
        return False
    code = data.get("code")
    try:
        if int(code) == 23018:
            return True
    except (TypeError, ValueError):
        pass
    return "download file size limit" in str(data.get("message") or "").lower()


def unescape_html_name(value: str) -> str:
    """解码 Quark 列表里的 HTML 实体，例如 &#39; -> ' 。"""
    text = str(value or "")
    for _ in range(3):
        nxt = html.unescape(text)
        if nxt == text:
            break
        text = nxt
    return text


# 夸克的 file/download 会按 UA 区别对待：浏览器 UA 下普通账号受「单文件大小限制」（400 code=23018），
# 换成官方桌面客户端 UA 就能拿到直链。2026-11-16 实测（同一账号、同一 fid=053d011b…，6.62 GiB）：
#   web UA     → 400 {"code":23018,"message":"download file size limit[<fid>]"}
#   client UA  → 200 + 2198 字符直链（drive.quark.cn / drive-pc.quark.cn 都一样）
# 因此默认带客户端 UA（参考实现 OpenList 的 quark_uc 驱动也是这么做的）。
# 想恢复浏览器 UA 的行为，把 conf/quark.yaml 的 quark_client_ua 改成自定义字符串即可。
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 "
    "Safari/537.36 Channel/pckk_other_ch"
)


class QuarkClient:
    def __init__(
        self,
        cookie: str,
        request_timeout: int,
        save_to_dir: str,
        share_url: str,
        share_pwd: str,
        logger,
        task_poll_interval: int = 3,
        task_timeout: int = 180,
        total_space_bytes: int | None = None,
        space_cache_ttl: float = 1800.0,
        user_agent: str = "",
    ):
        self.request_timeout = request_timeout
        self.task_poll_interval = task_poll_interval
        self.task_timeout = task_timeout
        # 网盘总容量：由配置传入（默认 10GB），用于估算剩余可用空间
        self.total_space_bytes = int(total_space_bytes or 10 * 1024 * 1024 * 1024)
        self.save_to_dir = str(save_to_dir or "").replace("\\", "/").strip("/")
        self.save_to_fid = ""
        self.share_url = share_url
        self.share_pwd = share_pwd
        self.log = logger
        self.user_agent = str(user_agent or "").strip() or DESKTOP_USER_AGENT
        self.headers = {
            "Cookie": cookie,
            "User-Agent": self.user_agent,
            "Referer": "https://pan.quark.cn/",
            "Content-Type": "application/json",
        }
        # get_available_space 需全盘递归（常 >2min），缓存避免每轮 poll 卡死；
        # 清理/转存用 adjust 增量修正；TTL 默认 30min，避免每轮轮询都重扫整盘。
        # 元素为 (缓存时刻, 可用字节, 是否低置信估算)
        self._available_space_cache: tuple[float, int, bool] | None = None
        self._available_space_cache_ttl = max(float(space_cache_ttl or 0), 60.0)

    def quark_get(self, url: str, params: dict = None) -> dict:
        import time
        start_time = time.time()
        r = requests.get(url, headers=self.headers, params=params, timeout=self.request_timeout)
        elapsed = time.time() - start_time
        self.log.debug(f"Quark API 请求耗时: GET {url} - {elapsed:.3f}s")

        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"Quark GET 失败: {url} | status={r.status_code} | body={r.text[:500]}") from e
        try:
            result = r.json()
        except ValueError as e:
            raise RuntimeError(
                f"Quark GET 返回非 JSON 内容: {url} | status={r.status_code} | body={r.text[:500]}"
            ) from e
        if not isinstance(result, dict):
            raise RuntimeError(
                f"Quark GET 返回非对象 JSON: {url} | status={r.status_code} | body={r.text[:500]}"
            )
        return result

    def quark_post(self, url: str, payload: dict) -> dict:
        import time
        start_time = time.time()
        r = requests.post(url, headers=self.headers, json=payload, timeout=self.request_timeout)
        elapsed = time.time() - start_time
        self.log.debug(f"Quark API 请求耗时: POST {url} - {elapsed:.3f}s")

        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"Quark POST 失败: {url} | status={r.status_code} | body={r.text[:500]}") from e
        return r.json()

    def quark_post_with_params(self, url: str, params: dict, payload: dict) -> dict:
        import time
        start_time = time.time()
        r = requests.post(
            url,
            headers=self.headers,
            params=params,
            json=payload,
            timeout=self.request_timeout,
        )
        elapsed = time.time() - start_time
        self.log.debug(f"Quark API 请求耗时: POST {url} - {elapsed:.3f}s")

        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(
                f"Quark POST 失败: {url} | params={params} | status={r.status_code} | body={r.text[:500]}"
            ) from e
        return r.json()

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_my_fid(fid: str) -> str:
        """规整 fid。

        配置里可能写成 `<fid>-<目录名>`，Quark 接口只接受纯 fid；但 fid 本身
        也可能含 `-`，所以只有「前半段确实是 16 位以上十六进制」时才截断，
        避免把合法 fid 静默切坏（切坏会指到错误目录甚至新建到别处）。
        """
        text = str(fid or "").strip()
        if "/" in text:
            text = text.rstrip("/").rsplit("/", 1)[-1]
        head, sep, _tail = text.partition("-")
        if sep and _FID_RE.match(head):
            return head
        return text

    def resolve_save_to_dir(self) -> str:
        """把配置的转存目录路径解析为 Quark fid，目录不存在则创建。"""
        self.save_to_fid = self.ensure_my_dir_path("0", self.save_to_dir)
        self.log.info(f"转存目录: /{self.save_to_dir or ''}  fid={self.save_to_fid}")
        return self.save_to_fid

    def invalidate_available_space_cache(self) -> None:
        """转存/清理后容量变化，丢弃缓存。"""
        self._available_space_cache = None

    def adjust_available_space_cache(self, delta_bytes: int) -> None:
        """按已知增减增量更新缓存（清理释放 / 转存占用），避免再次全盘列举。"""
        cached = self._available_space_cache
        if cached is None:
            return
        _cached_at, cached_size, low_confidence = cached
        new_size = max(0, int(cached_size) + int(delta_bytes))
        # 刷新时间戳，让刚修正过的容量在 TTL 内可复用
        self._available_space_cache = (time.monotonic(), new_size, low_confidence)

    @property
    def available_space_low_confidence(self) -> bool:
        """当前缓存的可用容量是否是「算不出来时的估算值」。"""
        cached = self._available_space_cache
        return bool(cached[2]) if cached else False

    def _fetch_quota_from_api(self) -> tuple[int, int] | None:
        """尝试用配额接口取 (总容量, 已用容量)；接口不可用/字段缺失时返回 None。"""
        for path in _QUOTA_ENDPOINTS:
            try:
                data = self.quark_get(
                    f"{QUARK_DRIVE_BASE_URL}{path}",
                    {"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
                )
            except Exception as e:
                self.log.debug(f"读取配额接口 {path} 失败: {e}")
                continue
            node = data.get("data") if isinstance(data, dict) else None
            if not isinstance(node, dict):
                continue
            total = self._to_int(node.get("total_capacity", node.get("total_size")), 0)
            used = self._to_int(node.get("use_capacity", node.get("used_size")), 0)
            if total > 0:
                return total, used
        return None

    def get_available_space(self) -> int:
        """获取网盘可用空间（字节）。

        三级策略（前一级成功就不走下一级）：
        1. 配额接口：1 次请求拿到 total/use，最准也最省；
        2. 递归统计：接口不可用时兜底，常 >2min，结果按 TTL（默认 30min）缓存；
        3. 估算 9GB：连统计也失败时使用，并标记低置信（调度器不会据此无限等容量）。
        """
        now = time.monotonic()
        cached = self._available_space_cache
        if cached is not None:
            cached_at, cached_size, _low = cached
            if now - cached_at <= self._available_space_cache_ttl:
                return cached_size

        quota = self._fetch_quota_from_api()
        if quota is not None:
            total, used = quota
            available_size = max(total - used, 0)
            self.log.info(
                f"  网盘可用空间: {available_size // 1024 // 1024} MB "
                f"（配额接口：已用 {used // 1024 // 1024} MB / 总 {total // 1024 // 1024} MB）"
            )
            self._available_space_cache = (time.monotonic(), available_size, False)
            return available_size

        try:
            self.log.info("  配额接口不可用，改为递归统计网盘占用（文件多时较慢）…")
            started = time.monotonic()
            all_files = self.list_my_files_recursive("0")
            used_size = sum(self._to_int(file.get("size", 0)) for file in all_files)
            total_size = max(int(self.total_space_bytes), 0)
            if used_size >= total_size:
                self.log.warning(
                    f"  配置的网盘总容量({total_size // 1024 // 1024} MB) 不大于已用"
                    f"({used_size // 1024 // 1024} MB)：可用空间按 0 计，"
                    f"请检查 conf/quark.yaml 的 total_space_gb 是否与实际账号一致"
                )
            available_size = max(total_size - used_size, 0)
            elapsed = time.monotonic() - started
            self.log.info(
                f"  网盘可用空间: {available_size // 1024 // 1024} MB "
                f"（已用 {used_size // 1024 // 1024} MB / "
                f"{len(all_files)} 个文件，耗时 {elapsed:.1f}s）"
            )
            self._available_space_cache = (time.monotonic(), available_size, False)
            return available_size
        except Exception as e:
            fallback = 9 * 1024 * 1024 * 1024
            self.log.warning(
                f"计算可用空间失败，暂按 {fallback // 1024 // 1024 // 1024}GB 估算"
                f"（低置信，仅供本轮调度参考）: {e}"
            )
            self._available_space_cache = (time.monotonic(), fallback, True)
            return fallback

    def parse_share_url(self) -> tuple[str, str]:
        """解析分享链接，返回 (share_id, folder_fid)。

        支持带锚点子目录的链接，例如：
        https://pan.quark.cn/s/xxx#/list/share/<folder_fid>
        folder_fid 为空表示分享根目录。
        """
        raw = str(self.share_url or "").strip()
        if "/s/" not in raw:
            raise RuntimeError(f"无效的分享链接: {raw}")

        path_part, _, fragment = raw.partition("#")
        tail = path_part.split("/s/", 1)[-1].split("?")[0].strip("/")
        segments = [seg for seg in tail.split("/") if seg]
        share_id = segments[0] if segments else ""
        if not share_id:
            raise RuntimeError(f"无法从分享链接解析 share_id: {raw}")

        # 新形态子目录链接：https://pan.quark.cn/s/<pwd_id>/<fid>-<名称>
        folder_fid = ""
        if len(segments) > 1:
            match = re.match(r"^([0-9a-fA-F]{16,32})(?:-.*)?$", segments[1])
            if match:
                folder_fid = match.group(1)
        # 兼容 #/list/share/<fid> 以及 #list/share/<fid>
        marker = "/list/share"
        frag = fragment.lstrip("/")
        if marker in f"/{frag}":
            after = f"/{frag}".split(marker, 1)[1].strip("/")
            if after:
                folder_fid = after.split("/")[0].split("?")[0].strip()
        return share_id, folder_fid

    def get_share_token(self) -> tuple[str, str, str]:
        share_id, _ = self.parse_share_url()
        data = self.quark_post_with_params(
            f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/share/sharepage/token",
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            payload={"pwd_id": share_id, "passcode": self.share_pwd},
        )
        if not _is_ok(data):
            raise RuntimeError(f"获取 stoken 失败: {_payload_brief(data)}")
        stoken = data["data"]["stoken"]
        info = self.quark_get(
            f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/share/sharepage/detail",
            {
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "ver": "2",
                "pwd_id": share_id,
                "stoken": stoken,
                "pdir_fid": "0",
                "force": "0",
                "_page": "1",
                "_size": "50",
                "_fetch_banner": "1",
                "_fetch_share": "1",
                "_fetch_total": "1",
                "_sort": "file_type:asc,file_name:asc",
            },
        )
        self.log.info(f"分享信息: {info.get('message', 'ok')}")
        if not _is_ok(info):
            raise RuntimeError(f"获取分享详情失败: {_payload_brief(info)}")
        fid_list = (info.get("data") or {}).get("share", {}).get("fid_list", [])
        root_fid = fid_list[0] if fid_list else "0"
        return stoken, share_id, root_fid

    def list_share_files(
        self,
        share_id: str,
        stoken: str,
        pdir_fid: str,
        relative_dir: str = "",
        _stats: dict | None = None,
        selected_file_fids: set[str] | None = None,
        selected_dir_fids: set[str] | None = None,
        _inherited: bool = False,
    ) -> list[dict]:
        """递归分页列举分享目录。串行请求、无 sleep；深目录时主要耗时在 Quark API 往返。

        selected_file_fids / selected_dir_fids 非空时只收「用户勾选的文件」和
        「用户勾选的目录（整棵子树）」；两者都为空表示全量（保持旧行为）。
        """
        selection_active = bool(selected_file_fids) or bool(selected_dir_fids)
        selected_file_fids = selected_file_fids or set()
        selected_dir_fids = selected_dir_fids or set()
        inside_selected = _inherited or (pdir_fid in selected_dir_fids)
        files = []
        page = 1
        relative_dir = relative_dir.strip("/")
        root_call = _stats is None
        if root_call:
            _stats = {"requests": 0, "dirs": 0, "files": 0, "started": time.monotonic()}
        _stats["dirs"] += 1
        dir_label = relative_dir or "(根)"
        while True:
            started = time.monotonic()
            data = self.quark_get(
                f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/share/sharepage/detail",
                {
                    "pr": "ucpro",
                    "fr": "pc",
                    "uc_param_str": "",
                    "ver": "2",
                    "pwd_id": share_id,
                    "stoken": stoken,
                    "pdir_fid": pdir_fid,
                    "force": "0",
                    "_page": str(page),
                    "_size": "50",
                    "_fetch_banner": "0",
                    "_fetch_share": "0",
                    "_fetch_total": "1",
                    "_sort": "file_type:asc,file_name:asc",
                },
            )
            req_elapsed = time.monotonic() - started
            _stats["requests"] += 1
            items = data.get("data", {}).get("list", [])
            if not items:
                break
            page_files = 0
            page_dirs = 0
            child_dirs: list[tuple[str, str]] = []
            for item in items:
                if item.get("file_name"):
                    item["file_name"] = unescape_html_name(item["file_name"])
                if item.get("file"):
                    if (
                        selection_active
                        and not inside_selected
                        and item.get("fid") not in selected_file_fids
                    ):
                        continue
                    relative_path = "/".join(
                        part for part in (relative_dir, item.get("file_name", "")) if part
                    )
                    item["relative_path"] = relative_path
                    files.append(item)
                    page_files += 1
                    _stats["files"] += 1
                elif item.get("dir"):
                    page_dirs += 1
                    next_relative_dir = "/".join(
                        part for part in (relative_dir, item.get("file_name", "")) if part
                    )
                    child_selected = inside_selected or (item["fid"] in selected_dir_fids)
                    # 注意：不能因为本目录未勾选就跳过递归——用户可能在树里只勾了更深一层的
                    # 子目录，跳过会导致整棵子树漏掉。这里只做「本身不在选中子树里」的路径标记，
                    # 是否收文件由子调用里的 inside_selected 判定。
                    child_dirs.append((item["fid"], next_relative_dir, child_selected))
            self.log.info(
                f"  列举分享: {dir_label}  page={page}  "
                f"本页文件={page_files} 本页子目录={page_dirs}  "
                f"请求耗时 {req_elapsed:.1f}s  "
                f"累计请求={_stats['requests']} 累计文件={_stats['files']}"
            )
            for child_fid, child_rel, child_selected in child_dirs:
                files.extend(
                    self.list_share_files(
                        share_id,
                        stoken,
                        child_fid,
                        child_rel,
                        _stats=_stats,
                        selected_file_fids=selected_file_fids,
                        selected_dir_fids=selected_dir_fids,
                        _inherited=child_selected,
                    )
                )
            if len(items) < 50:
                break
            page += 1
        if root_call:
            total_elapsed = time.monotonic() - _stats["started"]
            self.log.info(
                f"  分享列表列举完成: 文件={len(files)} 目录数≈{_stats['dirs']} "
                f"API请求={_stats['requests']} 总耗时 {total_elapsed:.1f}s "
                f"（均约 {total_elapsed / max(_stats['requests'], 1):.1f}s/请求，串行无本地磁盘检测）"
            )
        return files

    def list_share_dir_entries(
        self,
        share_id: str,
        stoken: str,
        pdir_fid: str,
        page_size: int = 100,
        max_pages: int = 30,
    ) -> list[dict]:
        """单层列举分享目录（不递归），供前端目录树逐层展开。

        返回 [{fid, name, is_dir, size, updated_at}]，目录在前、同名按名称升序。
        """
        entries: list[dict] = []
        page = 1
        while page <= max_pages:
            data = self.quark_get(
                f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/share/sharepage/detail",
                {
                    "pr": "ucpro",
                    "fr": "pc",
                    "uc_param_str": "",
                    "ver": "2",
                    "pwd_id": share_id,
                    "stoken": stoken,
                    "pdir_fid": pdir_fid,
                    "force": "0",
                    "_page": str(page),
                    "_size": str(page_size),
                    "_fetch_banner": "0",
                    "_fetch_share": "0",
                    "_fetch_total": "1",
                    "_sort": "file_type:asc,file_name:asc",
                },
            )
            if not _is_ok(data):
                raise RuntimeError(f"列举分享目录失败: {_payload_brief(data)}")
            node = data.get("data") or {}
            items = node.get("list") or []
            for item in items:
                name = unescape_html_name(str(item.get("file_name") or ""))
                fid = str(item.get("fid") or "")
                if not name or not fid:
                    continue
                entries.append(
                    {
                        "fid": fid,
                        "name": name,
                        "is_dir": bool(item.get("dir")),
                        "size": self._to_int(item.get("size"), 0),
                        "updated_at": self._to_int(item.get("updated_at"), 0),
                    }
                )
            total = self._to_int((node.get("metadata") or {}).get("_total"), 0)
            if len(items) < page_size or (total and page * page_size >= total):
                break
            page += 1
        entries.sort(key=lambda e: (0 if e["is_dir"] else 1, e["name"]))
        return entries

    def list_my_dir_entries(self, pdir_fid: str) -> list[dict]:
        """列出自己网盘目录下的直系条目（文件+目录）。"""
        entries = []
        page = 1
        pdir_fid = self._normalize_my_fid(pdir_fid)
        while True:
            data = self.quark_get(
                f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/file/sort",
                {
                    "pr": "ucpro",
                    "fr": "pc",
                    "uc_param_str": "",
                    "pdir_fid": pdir_fid,
                    "_page": str(page),
                    "_size": "100",
                    "_fetch_total": "1",
                    "_sort": "file_type:asc,file_name:asc",
                },
            )
            if not _is_ok(data):
                raise RuntimeError(f"读取网盘目录失败: {_payload_brief(data)}")
            items = data.get("data", {}).get("list", [])
            if not items:
                break
            for item in items:
                if item.get("file_name"):
                    item["file_name"] = unescape_html_name(item["file_name"])
            entries.extend(items)
            if len(items) < 100:
                break
            page += 1
        return entries

    def list_my_files_recursive(
        self,
        pdir_fid: str,
        relative_dir: str = "",
        _stats: dict | None = None,
    ) -> list[dict]:
        """递归列出自己网盘目录中的文件（不含目录）。"""
        files = []
        pdir_fid = self._normalize_my_fid(pdir_fid)
        relative_dir = relative_dir.strip("/")
        root_call = _stats is None
        if root_call:
            _stats = {"dirs": 0, "files": 0, "started": time.monotonic()}
        _stats["dirs"] += 1
        dir_label = relative_dir or "(根)"
        started = time.monotonic()
        entries = self.list_my_dir_entries(pdir_fid)
        req_elapsed = time.monotonic() - started
        page_files = 0
        page_dirs = 0
        child_dirs: list[tuple[str, str]] = []
        for item in entries:
            if item.get("file"):
                relative_path = "/".join(
                    part for part in (relative_dir, item.get("file_name", "")) if part
                )
                item["relative_path"] = relative_path
                files.append(item)
                page_files += 1
                _stats["files"] += 1
            elif item.get("dir"):
                page_dirs += 1
                next_relative_dir = "/".join(
                    part for part in (relative_dir, item.get("file_name", "")) if part
                )
                child_dirs.append((item.get("fid", ""), next_relative_dir))
        # 全盘算容量时避免每层刷 INFO；慢请求 / 前两层 / 每 10 个目录仍可见
        if (
            root_call
            or req_elapsed >= 1.0
            or _stats["dirs"] <= 2
            or _stats["dirs"] % 10 == 0
        ):
            self.log.info(
                f"  列举网盘: {dir_label}  "
                f"本层文件={page_files} 本层子目录={page_dirs}  "
                f"列举耗时 {req_elapsed:.1f}s  "
                f"累计目录={_stats['dirs']} 累计文件={_stats['files']}"
            )
        for child_fid, child_rel in child_dirs:
            files.extend(self.list_my_files_recursive(child_fid, child_rel, _stats=_stats))
        if root_call:
            total_elapsed = time.monotonic() - _stats["started"]
            self.log.info(
                f"  网盘递归列举完成: 文件={len(files)} 目录数≈{_stats['dirs']} "
                f"总耗时 {total_elapsed:.1f}s"
            )
        return files

    def create_my_dir(self, pdir_fid: str, dir_name: str) -> str:
        pdir_fid = self._normalize_my_fid(pdir_fid)
        data = self.quark_post_with_params(
            f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/file",
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            payload={
                "pdir_fid": pdir_fid,
                "file_name": dir_name,
                "dir_path": "",
                "dir_init_lock": False,
            },
        )
        if not _is_ok(data):
            raise RuntimeError(f"创建网盘目录失败: {dir_name} | {_payload_brief(data)}")
        fid = data.get("data", {}).get("fid")
        if not fid:
            raise RuntimeError(f"创建网盘目录未返回 fid: {dir_name} | {data}")
        return fid

    def ensure_my_dir_path(self, root_fid: str, relative_dir: str) -> str:
        current_fid = self._normalize_my_fid(root_fid)
        for part in [p for p in relative_dir.replace("\\", "/").strip("/").split("/") if p]:
            entries = self.list_my_dir_entries(current_fid)
            next_dir = None
            for entry in entries:
                if entry.get("dir") and entry.get("file_name") == part:
                    next_dir = entry
                    break
                if entry.get("file") and entry.get("file_name") == part:
                    raise RuntimeError(f"目标路径中存在同名文件，无法创建目录: {relative_dir}")
            if next_dir:
                current_fid = next_dir["fid"]
            else:
                current_fid = self.create_my_dir(current_fid, part)
        return self._normalize_my_fid(current_fid)

    @staticmethod
    def _extract_fid_token(item: dict) -> str:
        for key in ("fid_token", "share_fid_token", "file_token", "share_file_token"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def save_files_to_my_disk(
        self,
        file_items: list[dict],
        share_id: str,
        stoken: str,
        to_pdir_fid: str | None = None,
    ) -> list[str]:
        fid_list = [item["fid"] for item in file_items]
        fid_token_list = [self._extract_fid_token(item) for item in file_items]
        missing_token_count = sum(1 for token in fid_token_list if not token)
        if missing_token_count:
            self.log.warning(
                f"转存条目中有 {missing_token_count}/{len(fid_token_list)} 个文件未提取到 token，可能导致转存失败"
            )
        payload = {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": self._normalize_my_fid(to_pdir_fid or self.save_to_fid),
            "pwd_id": share_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "copy",
        }
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": "__dt"}
        save_url = f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/share/sharepage/save"
        data = self.quark_post_with_params(save_url, params=params, payload=payload)

        # 部分老接口不认 fid_token_list：失败时去掉 token 再试一次
        if not _is_ok(data) and any(fid_token_list):
            self.log.warning(
                f"转存接口拒绝带 fid_token_list 的请求，去掉 token 重试一次: {_payload_brief(data)}"
            )
            payload_without_tokens = dict(payload)
            payload_without_tokens["fid_token_list"] = []
            data = self.quark_post_with_params(
                save_url,
                params=params,
                payload=payload_without_tokens,
            )

        if not _is_ok(data):
            raise RuntimeError(f"转存失败: {_payload_brief(data)}")
        node = data.get("data")
        if not isinstance(node, dict):
            raise RuntimeError(f"转存响应缺少 data 字段: {_payload_brief(data)}")
        task_id = str(node.get("task_id") or "").strip()
        if task_id:
            return self._wait_task_and_get_fids(task_id)
        return [
            str(item.get("fid") or "")
            for item in (node.get("list") or [])
            if isinstance(item, dict) and item.get("fid")
        ]

    # 连续 N 次拿到"非 ok / 缺字段"的响应就快速失败，不耗满 task_timeout
    TASK_ABNORMAL_LIMIT = 5

    def _wait_task_and_get_fids(self, task_id: str) -> list[str]:
        task_params = {"task_id": task_id, "retry_index": 0, "pr": "ucpro", "fr": "pc", "uc_param_str": "__dt"}
        deadline = time.time() + max(self.task_timeout, 1)
        abnormal = 0
        while time.time() < deadline:
            time.sleep(max(self.task_poll_interval, 1))
            data = self.quark_get(f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/task", task_params)

            code = data.get("code")
            message = str(data.get("message", "")).lower()
            if code not in (None, 0):
                raise RuntimeError(f"查询转存任务状态返回异常: {_explain_task_error(data)}")
            if message and message not in ("ok", "success"):
                abnormal += 1
                self.log.warning(
                    f"任务查询 message 非 ok（{abnormal}/{self.TASK_ABNORMAL_LIMIT}）: "
                    f"code={code}, message={data.get('message')}"
                )
                if abnormal >= self.TASK_ABNORMAL_LIMIT:
                    raise RuntimeError(
                        f"转存任务连续 {abnormal} 次返回异常状态，放弃等待: {_payload_brief(data)}"
                    )
                continue

            data_node = data.get("data", {}) if isinstance(data, dict) else {}
            status = data_node.get("status") if isinstance(data_node, dict) else None
            if status is None:
                abnormal += 1
                self.log.warning(
                    f"任务返回结构异常（{abnormal}/{self.TASK_ABNORMAL_LIMIT}），等待重试: "
                    f"{_payload_brief(data)}"
                )
                if abnormal >= self.TASK_ABNORMAL_LIMIT:
                    raise RuntimeError(
                        f"转存任务连续 {abnormal} 次返回结构异常，放弃等待: {_payload_brief(data)}"
                    )
                continue
            abnormal = 0

            if status == 2:
                save_as = data_node.get("save_as", {})
                save_list = save_as.get("save_as_top_fids", []) or save_as.get("save_as_fids", [])
                return save_list
            if status == 3:
                raise RuntimeError(f"转存任务失败: {data}")
        raise TimeoutError(f"等待转存任务超时（{self.task_timeout} 秒）")

    def _wait_task_done(self, task_id: str) -> None:
        """等待通用任务完成（删除等，不要求返回 fid）。"""
        task_params = {"task_id": task_id, "retry_index": 0, "pr": "ucpro", "fr": "pc", "uc_param_str": "__dt"}
        deadline = time.time() + max(self.task_timeout, 1)
        while time.time() < deadline:
            time.sleep(max(self.task_poll_interval, 1))
            data = self.quark_get(f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/task", task_params)
            code = data.get("code")
            if code not in (None, 0):
                raise RuntimeError(f"查询任务状态返回异常: {_explain_task_error(data)}")
            data_node = data.get("data", {}) if isinstance(data, dict) else {}
            if not isinstance(data_node, dict):
                continue
            status = data_node.get("status")
            if status == 2:
                return
            if status == 3:
                raise RuntimeError(f"任务失败: {data}")
        raise TimeoutError(f"等待任务超时（{self.task_timeout} 秒）: {task_id}")

    def download_headers(self) -> list[str]:
        """下载器（aria2）请求直链时要带的头：夸克对直链同样校验 Cookie/Referer/UA。"""
        return [
            f"Cookie: {self.headers.get('Cookie', '')}",
            f"Referer: {self.headers.get('Referer', 'https://pan.quark.cn/')}",
            f"User-Agent: {self.headers.get('User-Agent', '')}",
        ]

    def get_download_urls(self, fids: list[str]) -> dict[str, str]:
        """批量把网盘文件 fid 换成下载直链，返回 {fid: download_url}。

        分享里的文件必须先转存成自己的 fid 才有直链；接口是 /1/clouddrive/file/download，
        直链带时效，拿到后要尽快交给下载器。
        """
        wanted = [self._normalize_my_fid(fid) for fid in fids]
        wanted = [fid for fid in wanted if fid]
        if not wanted:
            return {}
        last_error: Exception | None = None
        for base in QUARK_DOWNLOAD_URL_BASES:
            url = f"{base}/1/clouddrive/file/download"
            try:
                data = self.quark_post_with_params(
                    url, {"pr": "ucpro", "fr": "pc"}, {"fids": wanted}
                )
            except Exception as e:
                last_error = e
                continue
            if not _is_ok(data):
                # 23018 = 单文件大小超过当前账号可下载上限：换 base / 重试都没用，直接抛明确错误
                if _is_download_size_limit(data):
                    raise QuarkDownloadRejected(
                        "夸克拒绝下发下载直链（23018 单文件大小超限）："
                        "该文件超过当前账号可下载的大小上限（也可能是 quark_client_ua 被改成了浏览器 UA），"
                        f"fid={wanted[0]} | ua={self.user_agent[:40]}… | {_payload_brief(data)}"
                    )
                last_error = RuntimeError(_payload_brief(data))
                continue
            urls: dict[str, str] = {}
            items = data.get("data") or []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    fid = self._normalize_my_fid(item.get("fid", ""))
                    dl_url = str(item.get("download_url") or "").strip()
                    if fid and dl_url:
                        urls[fid] = dl_url
            if urls:
                return urls
            last_error = RuntimeError("接口未返回 download_url")
        raise RuntimeError(f"获取下载直链失败: {last_error or '未知错误'}")

    def delete_my_fids(self, fid_list: list[str]) -> None:
        """删除自己网盘中的文件/文件夹（进回收站）。"""
        fids = [self._normalize_my_fid(fid) for fid in fid_list if str(fid or "").strip()]
        if not fids:
            return
        data = self.quark_post_with_params(
            f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/file/delete",
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            payload={"action_type": 2, "filelist": fids, "exclude_fids": []},
        )
        if not _is_ok(data):
            raise RuntimeError(f"删除网盘文件失败: {_payload_brief(data)}")
        task_id = ""
        data_node = data.get("data")
        if isinstance(data_node, dict):
            task_id = str(data_node.get("task_id") or "").strip()
        if task_id:
            self._wait_task_done(task_id)

    def find_my_dir_fid(self, root_fid: str, relative_dir: str) -> str | None:
        """解析已存在目录的 fid；任一层不存在则返回 None（不会创建目录）。"""
        current_fid = self._normalize_my_fid(root_fid)
        parts = [p for p in str(relative_dir or "").replace("\\", "/").strip("/").split("/") if p]
        if not parts:
            return current_fid
        for part in parts:
            entries = self.list_my_dir_entries(current_fid)
            next_fid = None
            for entry in entries:
                if entry.get("dir") and entry.get("file_name") == part:
                    next_fid = self._normalize_my_fid(entry.get("fid", ""))
                    break
            if not next_fid:
                return None
            current_fid = next_fid
        return current_fid

    def prune_empty_parent_dirs(self, relative_paths: list[str], root_fid: str | None = None) -> list[str]:
        """针对已删文件路径，自底向上清理变空的父目录。"""
        root_fid = self._normalize_my_fid(root_fid or self.save_to_fid)
        parents: set[str] = set()
        for path in relative_paths:
            parts = [p for p in str(path or "").replace("\\", "/").strip("/").split("/") if p]
            for depth in range(len(parts) - 1, 0, -1):
                parents.add("/".join(parts[:depth]))
        ordered = sorted(parents, key=lambda p: p.count("/"), reverse=True)
        deleted: list[str] = []
        for rel in ordered:
            fid = self.find_my_dir_fid(root_fid, rel)
            if not fid or fid == root_fid:
                continue
            try:
                if self.list_my_dir_entries(fid):
                    continue
                self.delete_my_fids([fid])
                deleted.append(rel)
            except Exception as e:
                self.log.warning(f"清理空父目录失败: /{rel} | {e}")
        return deleted

    def prune_empty_dirs(self, root_fid: str) -> list[str]:
        """
        自底向上删除 root_fid 下的空文件夹。
        不删除 root_fid 本身；返回已删除目录的相对路径列表。
        """
        root_fid = self._normalize_my_fid(root_fid)
        if not root_fid:
            return []
        deleted: list[str] = []

        def _walk(fid: str, relative_dir: str) -> None:
            entries = self.list_my_dir_entries(fid)
            for entry in entries:
                if not entry.get("dir"):
                    continue
                name = str(entry.get("file_name") or "").strip()
                child_fid = str(entry.get("fid") or "").strip()
                if not name or not child_fid:
                    continue
                child_rel = "/".join(part for part in (relative_dir, name) if part)
                _walk(child_fid, child_rel)

            if fid == root_fid:
                return
            # 子目录处理完后重新检查是否为空
            if self.list_my_dir_entries(fid):
                return
            self.delete_my_fids([fid])
            deleted.append(relative_dir)

        _walk(root_fid, "")
        return deleted

