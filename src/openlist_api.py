import html

import requests


def unescape_html_name(value: str) -> str:
    text = str(value or "")
    for _ in range(3):
        nxt = html.unescape(text)
        if nxt == text:
            break
        text = nxt
    return text


def names_match(left: str, right: str) -> bool:
    return unescape_html_name(left) == unescape_html_name(right)


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
        normalized_path = "/" + unescape_html_name(str(path)).replace("\\", "/").strip("/")
        dir_path, file_name = normalized_path.rsplit("/", 1)
        file_name = unescape_html_name(file_name)
        if not file_name:
            raise ValueError(f"OpenList 删除路径不是文件: {path}")
        return dir_path or "/", file_name

    @staticmethod
    def _normalize_dir_path(path: str) -> str:
        path = "/" + unescape_html_name(str(path)).replace("\\", "/").strip("/")
        return path.rstrip("/") or "/"

    def _resolve_entry_name(self, entries: list[dict], file_name: str) -> dict | None:
        """按解码后的名字匹配条目，避免 &#39; 与 ' 对不上。"""
        wanted = unescape_html_name(file_name)
        exact = None
        decoded_match = None
        for entry in entries:
            name = str(entry.get("name", ""))
            if name == file_name:
                exact = entry
                break
            if unescape_html_name(name) == wanted:
                decoded_match = entry
        return exact or decoded_match

    def openlist_get_download_url(self, path: str) -> str:
        dir_path, file_name = self._split_file_path(path)
        entries = self.openlist_list(dir_path)
        entry = self._resolve_entry_name(entries, file_name)
        if entry is None:
            entry_names = [str(item.get("name", "")) for item in entries]
            visible_names = ", ".join(entry_names[:20]) or "空"
            raise RuntimeError(
                f"OpenList get 失败: {path} | 父目录已刷新但未找到文件 `{file_name}`，"
                f"当前可见条目: {visible_names}"
            )
        actual_name = str(entry.get("name", file_name))
        actual_path = f"{dir_path.rstrip('/')}/{actual_name}"

        r = requests.post(
            f"{self.host}/api/fs/get",
            headers=self._headers(),
            json={"path": actual_path},
            timeout=self.request_timeout,
        )

        data = self._check_response(r, "get", actual_path)
        data_node = data.get("data")
        if not isinstance(data_node, dict) or not data_node.get("raw_url"):
            raise RuntimeError(f"OpenList get 未返回 raw_url: {actual_path} | {data}")
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
        entry = self._resolve_entry_name(entries, file_name)
        if entry is None:
            return False
        return not bool(entry.get("is_dir", False))

    def openlist_delete_files_in_dir(self, dir_path: str, file_names: list[str]) -> bool:
        """同一目录下批量删除文件。"""
        if not file_names:
            return True
        dir_path = self._normalize_dir_path(dir_path)
        entries = self.openlist_list(dir_path)
        resolved = []
        missing = []
        for name in file_names:
            entry = self._resolve_entry_name(entries, name)
            if entry is None:
                missing.append(name)
            else:
                resolved.append(str(entry.get("name", name)))
        if missing:
            entry_names = {str(entry.get("name", "")) for entry in entries}
            visible_names = ", ".join(sorted(entry_names)[:20]) or "空"
            raise RuntimeError(
                f"OpenList remove 失败: {dir_path} | 父目录已刷新但未找到文件 "
                f"`{', '.join(missing)}`，当前可见条目: {visible_names}"
            )
        r = requests.post(
            f"{self.host}/api/fs/remove",
            headers=self._headers(),
            json={"names": resolved, "dir": dir_path},
            timeout=self.request_timeout,
        )
        self._check_response(r, "remove", dir_path)
        return True

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
        """删除单个文件。"""
        dir_path, file_name = self._split_file_path(path)
        return self.openlist_delete_files_in_dir(dir_path, [file_name])

    def openlist_prune_empty_dirs(self, root_path: str) -> list[str]:
        """
        自底向上删除 root_path 下的空目录（不删除 root 本身）。
        主要用于与 Quark 侧清理后同步 OpenList 视图。
        """
        root = self._normalize_dir_path(root_path)
        removed: list[str] = []

        def _walk(path: str) -> None:
            try:
                entries = self.openlist_list(path)
            except RuntimeError as e:
                message = str(e).lower()
                if "not found" in message or "object not found" in message or "不存在" in message:
                    return
                raise
            for entry in entries:
                if not entry.get("is_dir"):
                    continue
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                child = f"{path.rstrip('/')}/{name}"
                _walk(child)
            if path == root:
                return
            try:
                entries = self.openlist_list(path)
            except RuntimeError:
                return
            if entries:
                return
            parent = self._normalize_dir_path(path.rsplit("/", 1)[0] or "/")
            name = path.rsplit("/", 1)[-1]
            if not name:
                return
            try:
                self.openlist_delete_files_in_dir(parent, [name])
                removed.append(path)
            except Exception:
                # OpenList 可能因缓存与 Quark 不一致，忽略单次失败
                return

        _walk(root)
        return removed
