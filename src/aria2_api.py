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

    def aria2_get_global_stat(self) -> dict:
        """返回 aria2.getGlobalStat 结果（含 numActive/numWaiting/numStopped）。"""
        result = self._aria2_call("aria2.getGlobalStat", [])
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
        """按相对路径查找 active/waiting/paused 任务，供下载 Worker 三分支（恢复/接管/新建）判断。"""
        wanted = self._normalize_relative_path(relative_path)
        if not wanted:
            return None
        infos = self.aria2_get_existing_task_infos_by_name()
        info = infos.get(wanted)
        if info:
            return info
        # 兼容仅 basename 的情况
        base = wanted.rsplit("/", 1)[-1]
        for name, task_info in infos.items():
            if name == base or name.endswith("/" + base):
                return task_info
        return None

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
        wanted = {self._normalize_relative_path(p) for p in paths if p}
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
            if rel not in wanted:
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
        wanted = {self._normalize_relative_path(p) for p in paths if p}
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
            if rel not in wanted:
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
