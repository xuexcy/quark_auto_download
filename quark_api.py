import time

import requests


QUARK_DRIVE_BASE_URL = "https://drive.quark.cn"


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

    def get_available_space(self) -> int:
        """获取网盘可用空间（字节）。通过计算所有文件占用容量得到。"""
        try:
            # 获取所有文件
            all_files = self.list_my_files_recursive("0")
            # 累计占用容量
            used_size = sum(self._to_int(file.get("size", 0)) for file in all_files)
            # 总容量10G
            total_size = 10 * 1024 * 1024 * 1024
            # 剩余容量
            available_size = total_size - used_size
            if available_size < 0:
                available_size = 0
            return available_size
        except Exception as e:
            self.log.warning(f"计算可用空间失败，使用默认9GB: {e}")
            # 出错时返回默认9GB
            return 9 * 1024 * 1024 * 1024

    def get_share_token(self) -> tuple[str, str, str]:
        share_id = self.share_url.split("/s/")[-1].split("#")[0].split("?")[0].strip("/")
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

    def list_share_dir_entries(self, share_id: str, stoken: str, pdir_fid: str) -> list[dict]:
        entries = []
        page = 1
        while True:
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
            items = data.get("data", {}).get("list", [])
            if not items:
                break
            entries.extend(items)
            if len(items) < 50:
                break
            page += 1
        return entries

    def resolve_sub_dir_fid(self, share_id: str, stoken: str, root_fid: str, sub_dir: str) -> str:
        sub_dir = (sub_dir or "").strip().strip("/")
        if not sub_dir:
            return root_fid
        current_fid = root_fid
        for part in [p for p in sub_dir.split("/") if p]:
            entries = self.list_share_dir_entries(share_id, stoken, current_fid)
            next_dir = None
            for entry in entries:
                if entry.get("dir") and entry.get("file_name") == part:
                    next_dir = entry
                    break
            if not next_dir:
                raise RuntimeError(f"分享子目录不存在: {sub_dir}（缺少目录 `{part}`）")
            current_fid = next_dir["fid"]
        return current_fid

    def list_share_files(self, share_id: str, stoken: str, pdir_fid: str, relative_dir: str = "") -> list[dict]:
        files = []
        page = 1
        relative_dir = relative_dir.strip("/")
        while True:
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
            items = data.get("data", {}).get("list", [])
            if not items:
                break
            for item in items:
                if item.get("file"):
                    relative_path = "/".join(
                        part for part in (relative_dir, item.get("file_name", "")) if part
                    )
                    item["relative_path"] = relative_path
                    files.append(item)
                elif item.get("dir"):
                    next_relative_dir = "/".join(
                        part for part in (relative_dir, item.get("file_name", "")) if part
                    )
                    files.extend(self.list_share_files(share_id, stoken, item["fid"], next_relative_dir))
            if len(items) < 50:
                break
            page += 1
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
            entries.extend(items)
            if len(items) < 100:
                break
            page += 1
        return entries

    def list_my_files_recursive(self, pdir_fid: str, relative_dir: str = "") -> list[dict]:
        """递归列出自己网盘目录中的文件（不含目录）。"""
        files = []
        pdir_fid = self._normalize_my_fid(pdir_fid)
        relative_dir = relative_dir.strip("/")
        for item in self.list_my_dir_entries(pdir_fid):
            if item.get("file"):
                relative_path = "/".join(
                    part for part in (relative_dir, item.get("file_name", "")) if part
                )
                item["relative_path"] = relative_path
                files.append(item)
            elif item.get("dir"):
                next_relative_dir = "/".join(
                    part for part in (relative_dir, item.get("file_name", "")) if part
                )
                files.extend(self.list_my_files_recursive(item.get("fid", ""), next_relative_dir))
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

