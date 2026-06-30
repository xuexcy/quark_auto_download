import os
import posixpath
import time

import requests


class Aria2Client:
    def __init__(
        self,
        host: str,
        secret: str,
        download_dir: str,
        request_timeout: int,
        openlist_token: str,
        poll_interval: int,
        download_timeout: int,
        logger,
    ):
        self.host = host
        self.secret = secret
        self.download_dir = download_dir
        self.request_timeout = request_timeout
        self.openlist_token = openlist_token
        self.poll_interval = poll_interval
        self.download_timeout = download_timeout
        self.log = logger
        self._id = 0

    def _aria2_call(self, method: str, params: list):
        import time
        self._id += 1
        secret_param = f"token:{self.secret}" if self.secret else ""
        full_params = ([secret_param] + params) if secret_param else params
        payload = {"jsonrpc": "2.0", "id": str(self._id), "method": method, "params": full_params}

        start_time = time.time()
        r = requests.post(self.host, json=payload, timeout=self.request_timeout)
        elapsed = time.time() - start_time

        r.raise_for_status()
        result = r.json()
        if "error" in result:
            raise RuntimeError(f"Aria2 RPC 错误: {result['error']}")

        # 记录请求耗时
        self.log.debug(f"Aria2 API 请求耗时: {method} - {elapsed:.3f}s")
        return result["result"]

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
        options = {"dir": os.path.join(self.download_dir, relative_dir), "out": output_name}
        if self.openlist_token:
            options["header"] = [f"Authorization: {self.openlist_token}"]
        gid = self._aria2_call("aria2.addUri", [[url], options])
        return gid

    def aria2_get_status(self, gid: str) -> dict:
        return self._aria2_call("aria2.tellStatus", [gid])

    def _extract_task_filename(self, task: dict) -> str:
        if not isinstance(task, dict):
            return ""
        files = task.get("files", [])
        if isinstance(files, list) and files:
            first = files[0] if isinstance(files[0], dict) else {}
            path = str(first.get("path", "")).strip()
            if path:
                normalized_path = path.replace("\\", "/").rstrip("/")
                normalized_download_dir = self.download_dir.replace("\\", "/").rstrip("/")
                if normalized_path == normalized_download_dir:
                    return ""
                if normalized_path.startswith(f"{normalized_download_dir}/"):
                    return normalized_path[len(normalized_download_dir) + 1 :]
                return normalized_path.split("/")[-1]
        bittorrent = task.get("bittorrent", {})
        info = bittorrent.get("info", {}) if isinstance(bittorrent, dict) else {}
        name = str(info.get("name", "")).strip()
        return name

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
