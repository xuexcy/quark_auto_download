import requests


class OpenListClient:
    def __init__(self, host: str, token: str, request_timeout: int):
        self.host = host.rstrip("/")
        self.token = token
        self.request_timeout = request_timeout

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = self.token
        return headers

    @staticmethod
    def _split_file_path(path: str) -> tuple[str, str]:
        normalized_path = "/" + str(path).strip("/")
        dir_path, file_name = normalized_path.rsplit("/", 1)
        if not file_name:
            raise ValueError(f"OpenList 删除路径不是文件: {path}")
        return dir_path or "/", file_name

    @staticmethod
    def _normalize_dir_path(path: str) -> str:
        path = "/" + str(path).replace("\\", "/").strip("/")
        return path.rstrip("/") or "/"

    @staticmethod
    def _check_response(r, action: str, path: str) -> dict:
        r.raise_for_status()
        data = r.json()
        code = data.get("code")
        if code not in (None, 200):
            raise RuntimeError(f"OpenList {action} 失败: {path} | {data}")
        return data

    def openlist_list(self, path: str) -> list[dict]:
        r = requests.post(
            f"{self.host}/api/fs/list",
            headers=self._headers(),
            json={"path": path, "page": 1, "per_page": 0, "refresh": True},
            timeout=self.request_timeout,
        )

        data = self._check_response(r, "list", path)
        return data.get("data", {}).get("content", []) or []

    def openlist_get_download_url(self, path: str) -> str:
        dir_path, file_name = self._split_file_path(path)
        entries = self.openlist_list(dir_path)
        entry_names = [str(entry.get("name", "")) for entry in entries]
        if file_name not in entry_names:
            visible_names = ", ".join(entry_names[:20]) or "空"
            raise RuntimeError(
                f"OpenList get 失败: {path} | 父目录已刷新但未找到文件 `{file_name}`，"
                f"当前可见条目: {visible_names}"
            )

        r = requests.post(
            f"{self.host}/api/fs/get",
            headers=self._headers(),
            json={"path": path},
            timeout=self.request_timeout,
        )

        data = self._check_response(r, "get", path)
        data_node = data.get("data")
        if not isinstance(data_node, dict) or not data_node.get("raw_url"):
            raise RuntimeError(f"OpenList get 未返回 raw_url: {path} | {data}")
        return data_node["raw_url"]

    def openlist_file_exists(self, path: str) -> bool:
        """检查文件是否存在。父目录不存在时返回 False，其他 OpenList 错误继续抛出。"""
        dir_path, file_name = self._split_file_path(path)
        try:
            entries = self.openlist_list(dir_path)
        except RuntimeError as e:
            message = str(e).lower()
            if "not found" in message or "object not found" in message or "不存在" in message:
                return False
            raise
        for entry in entries:
            if str(entry.get("name", "")) != file_name:
                continue
            if bool(entry.get("is_dir", False)):
                return False
            return True
        return False

    def openlist_mkdir(self, path: str) -> bool:
        """创建目录。"""
        path = self._normalize_dir_path(path)
        r = requests.post(
            f"{self.host}/api/fs/mkdir",
            headers=self._headers(),
            json={"path": path},
            timeout=self.request_timeout,
        )

        self._check_response(r, "mkdir", path)
        return True

    def openlist_ensure_dir(self, path: str) -> bool:
        """逐级创建目录，目录已存在时跳过。"""
        path = self._normalize_dir_path(path)
        if path == "/":
            return True
        current = ""
        for part in [p for p in path.strip("/").split("/") if p]:
            current = f"{current}/{part}"
            try:
                self.openlist_mkdir(current)
            except RuntimeError as e:
                message = str(e)
                if "already" in message.lower() or "exist" in message.lower() or "存在" in message:
                    continue
                raise
        return True

    def openlist_delete_file(self, path: str) -> bool:
        """删除文件"""
        dir_path, file_name = self._split_file_path(path)
        entries = self.openlist_list(dir_path)
        entry_names = [str(entry.get("name", "")) for entry in entries]
        if file_name not in entry_names:
            visible_names = ", ".join(entry_names[:20]) or "空"
            raise RuntimeError(
                f"OpenList remove 失败: {path} | 父目录已刷新但未找到文件 `{file_name}`，"
                f"当前可见条目: {visible_names}"
            )
        r = requests.post(
            f"{self.host}/api/fs/remove",
            headers=self._headers(),
            json={"names": [file_name], "dir": dir_path},
            timeout=self.request_timeout,
        )

        self._check_response(r, "remove", path)
        return True
