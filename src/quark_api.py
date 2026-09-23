import html
import time

import requests


QUARK_DRIVE_BASE_URL = "https://drive.quark.cn"


def unescape_html_name(value: str) -> str:
    """解码 Quark 列表里的 HTML 实体，例如 &#39; -> ' 。"""
    text = str(value or "")
    for _ in range(3):
        nxt = html.unescape(text)
        if nxt == text:
            break
        text = nxt
    return text


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
    ):
        self.request_timeout = request_timeout
        self.task_poll_interval = task_poll_interval
        self.task_timeout = task_timeout
        self.save_to_dir = str(save_to_dir or "").replace("\\", "/").strip("/")
        self.save_to_fid = ""
        self.share_url = share_url
        self.share_pwd = share_pwd
        self.log = logger
        self.headers = {
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://pan.quark.cn/",
            "Content-Type": "application/json",
        }
        # get_available_space 需全盘递归（常 >2min），缓存避免每轮 poll 卡死；
        # 清理/转存用 adjust 增量修正，TTL 需覆盖数轮 poll + 一次慢扫描窗口
        self._available_space_cache: tuple[float, int] | None = None
        self._available_space_cache_ttl = 180.0

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
        """配置里可能把 fid 和目录名写成 `fid-name`，Quark 接口只接受纯 fid。"""
        fid = str(fid or "").strip()
        if "/" in fid:
            fid = fid.rstrip("/").rsplit("/", 1)[-1]
        if "-" in fid:
            return fid.split("-", 1)[0]
        return fid

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
        _cached_at, cached_size = cached
        new_size = max(0, int(cached_size) + int(delta_bytes))
        # 刷新时间戳，让刚修正过的容量在 TTL 内可复用
        self._available_space_cache = (time.monotonic(), new_size)

    def get_available_space(self) -> int:
        """获取网盘可用空间（字节）。通过计算所有文件占用容量得到。

        注意：当前实现会递归列出整个网盘，文件多时可能耗时数分钟；
        结果短时缓存，避免网盘 Worker 每轮都阻塞。
        """
        now = time.monotonic()
        cached = self._available_space_cache
        if cached is not None:
            cached_at, cached_size = cached
            if now - cached_at <= self._available_space_cache_ttl:
                return cached_size

        try:
            self.log.info("  开始计算网盘可用空间（全盘递归列举，文件多时较慢）…")
            started = time.monotonic()
            all_files = self.list_my_files_recursive("0")
            used_size = sum(self._to_int(file.get("size", 0)) for file in all_files)
            # 总容量10G
            total_size = 10 * 1024 * 1024 * 1024
            available_size = total_size - used_size
            if available_size < 0:
                available_size = 0
            elapsed = time.monotonic() - started
            self.log.info(
                f"  网盘可用空间: {available_size // 1024 // 1024} MB "
                f"（已用 {used_size // 1024 // 1024} MB / "
                f"{len(all_files)} 个文件，耗时 {elapsed:.1f}s）"
            )
            self._available_space_cache = (time.monotonic(), available_size)
            return available_size
        except Exception as e:
            self.log.warning(f"计算可用空间失败，使用默认9GB: {e}")
            fallback = 9 * 1024 * 1024 * 1024
            self._available_space_cache = (time.monotonic(), fallback)
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
        share_id = path_part.split("/s/")[-1].split("?")[0].strip("/")
        if not share_id:
            raise RuntimeError(f"无法从分享链接解析 share_id: {raw}")

        folder_fid = ""
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
        if data.get("code") != 0:
            raise RuntimeError(f"获取 stoken 失败: {data}")
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
        if info.get("code") != 0:
            raise RuntimeError(f"获取分享详情失败: {info}")
        fid_list = info["data"]["share"].get("fid_list", [])
        root_fid = fid_list[0] if fid_list else "0"
        return stoken, share_id, root_fid

    def list_share_files(
        self,
        share_id: str,
        stoken: str,
        pdir_fid: str,
        relative_dir: str = "",
        _stats: dict | None = None,
    ) -> list[dict]:
        """递归分页列举分享目录。串行请求、无 sleep；深目录时主要耗时在 Quark API 往返。"""
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
                    child_dirs.append((item["fid"], next_relative_dir))
            self.log.info(
                f"  列举分享: {dir_label}  page={page}  "
                f"本页文件={page_files} 本页子目录={page_dirs}  "
                f"请求耗时 {req_elapsed:.1f}s  "
                f"累计请求={_stats['requests']} 累计文件={_stats['files']}"
            )
            for child_fid, child_rel in child_dirs:
                files.extend(
                    self.list_share_files(
                        share_id, stoken, child_fid, child_rel, _stats=_stats
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
            if data.get("code") not in (0, None):
                raise RuntimeError(f"读取网盘目录失败: {data}")
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
        if data.get("code") not in (0, None) and data.get("status") != 200:
            raise RuntimeError(f"创建网盘目录失败: {dir_name} | {data}")
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

        if (not data or (data.get("status") != 200 and data.get("code") != 0)) and any(fid_token_list):
            payload_without_tokens = dict(payload)
            payload_without_tokens["fid_token_list"] = []
            data = self.quark_post_with_params(
                save_url,
                params=params,
                payload=payload_without_tokens,
            )

        if data.get("status") != 200 and data.get("code") != 0:
            raise RuntimeError(f"转存失败: {data}")
        task_id = data["data"].get("task_id", "")
        if task_id:
            return self._wait_task_and_get_fids(task_id)
        return [item["fid"] for item in data["data"].get("list", [])]

    def _wait_task_and_get_fids(self, task_id: str) -> list[str]:
        task_params = {"task_id": task_id, "retry_index": 0, "pr": "ucpro", "fr": "pc", "uc_param_str": "__dt"}
        deadline = time.time() + max(self.task_timeout, 1)
        while time.time() < deadline:
            time.sleep(max(self.task_poll_interval, 1))
            data = self.quark_get(f"{QUARK_DRIVE_BASE_URL}/1/clouddrive/task", task_params)

            code = data.get("code")
            message = str(data.get("message", "")).lower()
            if code not in (None, 0):
                raise RuntimeError(f"查询转存任务状态返回异常: {data}")
            if message and message not in ("ok", "success"):
                self.log.warning(f"任务查询 message 非 ok，继续等待: code={code}, message={data.get('message')}")
                continue

            data_node = data.get("data", {}) if isinstance(data, dict) else {}
            if not isinstance(data_node, dict):
                self.log.warning(f"任务返回 data 结构异常，等待重试: {str(data)[:300]}")
                continue

            status = data_node.get("status")
            if status is None:
                self.log.warning(f"任务返回缺少 data.status，等待重试: {str(data)[:300]}")
                continue

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
                raise RuntimeError(f"查询任务状态返回异常: {data}")
            data_node = data.get("data", {}) if isinstance(data, dict) else {}
            if not isinstance(data_node, dict):
                continue
            status = data_node.get("status")
            if status == 2:
                return
            if status == 3:
                raise RuntimeError(f"任务失败: {data}")
        raise TimeoutError(f"等待任务超时（{self.task_timeout} 秒）: {task_id}")

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
        if data.get("code") not in (0, None) and data.get("status") != 200:
            raise RuntimeError(f"删除网盘文件失败: {data}")
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

