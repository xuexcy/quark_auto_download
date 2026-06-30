import os
import posixpath
import time

from aria2_api import Aria2Client
from openlist_api import OpenListClient
from quark_api import QuarkClient


ACTIVE_ARIA2_STATUSES = ("active", "waiting", "paused")


def normalize_relative_path(path: str) -> str:
    normalized = posixpath.normpath(str(path).replace("\\", "/").strip("/"))
    if normalized in ("", "."):
        return ""
    if normalized.startswith("../") or normalized == "..":
        raise ValueError(f"非法相对路径: {path}")
    return normalized


class FileApi:
    def __init__(
        self,
        quark_client: QuarkClient,
        openlist_client: OpenListClient,
        aria2_client: Aria2Client,
        save_to_fid: str,
        openlist_quark_path: str,
        aria2_download_dir: str,
        download_destination_dir: str,
        aria2_max_retries: int,
        download_log_interval: int,
        logger,
        openlist_refresh_delay: int = 30,
        download_submit_max_retries: int = 10,
        download_submit_retry_interval: int = 30,
    ):
        self.quark_client = quark_client
        self.openlist_client = openlist_client
        self.aria2_client = aria2_client
        self.save_to_fid = save_to_fid
        self.openlist_quark_path = openlist_quark_path.rstrip("/")
        self.aria2_download_dir = aria2_download_dir
        self.download_destination_dir = download_destination_dir
        self.aria2_max_retries = aria2_max_retries
        self.download_log_interval = download_log_interval
        self.openlist_refresh_delay = openlist_refresh_delay
        self.download_submit_max_retries = download_submit_max_retries
        self.download_submit_retry_interval = download_submit_retry_interval
        self.log = logger
        self.cleaned_transferred_names: set[str] = set()

    @staticmethod
    def file_path(file_item: dict) -> str:
        return normalize_relative_path(file_item.get("relative_path") or file_item.get("file_name", ""))

    @staticmethod
    def file_name(file_item: dict) -> str:
        relative_path = FileApi.file_path(file_item)
        return relative_path.rsplit("/", 1)[-1]

    def openlist_path(self, relative_path: str) -> str:
        return f"{self.openlist_quark_path}/{normalize_relative_path(relative_path)}".replace("//", "/")

    def openlist_dir_path(self, relative_dir: str) -> str:
        relative_dir = normalize_relative_path(relative_dir)
        if not relative_dir:
            return self.openlist_quark_path or "/"
        return f"{self.openlist_quark_path}/{relative_dir}".replace("//", "/")

    def get_quark_files_by_name(self) -> dict[str, list[dict]]:
        files_by_name: dict[str, list[dict]] = {}
        for item in self.quark_client.list_my_files_recursive(self.save_to_fid):
            relative_path = self.file_path(item)
            if relative_path:
                files_by_name.setdefault(relative_path, []).append(item)
        return files_by_name

    @staticmethod
    def get_local_downloaded_file_names(download_dir: str) -> set[str]:
        names = set()
        if not os.path.isdir(download_dir):
            return names
        for root, _, files in os.walk(download_dir):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                names.add(normalize_relative_path(os.path.relpath(file_path, download_dir)))
        return names

    def get_local_downloaded_file_names_from_dirs(self) -> set[str]:
        # 只有完成目录里的文件才视为本地已完成；aria2 临时目录可能包含仍在下载的文件。
        if not self.download_destination_dir:
            return set()
        return self.get_local_downloaded_file_names(self.download_destination_dir)

    def refresh_runtime_state(self) -> tuple[dict[str, list[dict]], set[str], dict[str, dict]]:
        try:
            quark_files_by_name = self.get_quark_files_by_name()
        except Exception as e:
            self.log.error(f"读取 Quark 已转存文件列表失败: {e}")
            quark_files_by_name = {}

        try:
            local_downloaded_names = self.get_local_downloaded_file_names_from_dirs()
        except Exception as e:
            self.log.error(f"读取本地下载目录失败: {e}")
            local_downloaded_names = set()

        try:
            aria2_tasks_by_name = self.aria2_client.aria2_get_existing_task_infos_by_name()
        except Exception as e:
            self.log.error(f"读取 Aria2 任务列表失败: {e}")
            aria2_tasks_by_name = {}

        return quark_files_by_name, local_downloaded_names, aria2_tasks_by_name

    def delete_transferred_file(self, openlist_path: str, file_name: str, action: str) -> bool:
        if not openlist_path:
            self.log.error(f"{action}失败（缺少 OpenList 路径，请手动清理）: {file_name}")
            return False
        try:
            if not self.openlist_client.openlist_file_exists(openlist_path):
                self.log.info(f"  → {action}: {file_name} 已不在网盘中，无需清理")
                return True
            self.openlist_client.openlist_delete_file(openlist_path)
            if self.openlist_client.openlist_file_exists(openlist_path):
                self.log.error(f"  {action}失败（删除后仍可见，请手动清理）: {file_name}")
                return False
            self.log.info(f"  → {action}成功，已确认删除: {file_name}")
            return True
        except Exception as e:
            self.log.error(f"  {action}失败（请手动清理）: {file_name} | {e}")
            return False

    def cleanup_transferred_file(self, file_name: str, action: str = "已删除 Quark 转存文件") -> bool:
        return self.delete_transferred_file(self.openlist_path(file_name), file_name, action)

    def cleanup_completed_files(
        self,
        target_files: list[dict],
        quark_files_by_name: dict[str, list[dict]],
        local_downloaded_names: set[str],
        aria2_tasks_by_name: dict[str, dict],
    ) -> tuple[dict[str, list[dict]], bool]:
        deleted_names = []
        deletion_blocked = False
        for file_item in target_files:
            relative_path = self.file_path(file_item)
            if not relative_path or relative_path in self.cleaned_transferred_names:
                continue
            aria2_status = str(aria2_tasks_by_name.get(relative_path, {}).get("status", "")).lower()
            if relative_path not in local_downloaded_names and aria2_status != "complete":
                continue
            self.log.info(f"  ✓ 下载完成: {relative_path}")
            self.finish_downloaded_file(relative_path)
            if self.cleanup_transferred_file(relative_path, "已清理 Quark 已下载完成文件"):
                deleted_names.append(relative_path)
                self.cleaned_transferred_names.add(relative_path)
            else:
                deletion_blocked = True

        if deleted_names:
            self.log.info(f"  已通过 OpenList 清理 {len(deleted_names)} 个文件，释放网盘容量")
            for file_name in deleted_names:
                quark_files_by_name.pop(file_name, None)
        return quark_files_by_name, deletion_blocked

    def get_capacity_limit(self) -> tuple[int | None, int]:
        try:
            quark_available_size = self.quark_client.get_available_space()
        except Exception as e:
            self.log.error(f"查询网盘剩余容量失败: {e}")
            return None, 0
        return quark_available_size, quark_available_size

    def move_file_to_destination(self, relative_path: str) -> bool:
        relative_path = normalize_relative_path(relative_path)
        src_path = os.path.join(self.aria2_download_dir, relative_path)
        if not os.path.isfile(src_path):
            return os.path.isfile(os.path.join(self.download_destination_dir, relative_path))
        dst_path = os.path.join(self.download_destination_dir, relative_path)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        if os.path.abspath(src_path) == os.path.abspath(dst_path):
            return True
        if os.path.exists(dst_path):
            base, ext = os.path.splitext(relative_path.rsplit("/", 1)[-1])
            suffix = time.strftime("%Y%m%d_%H%M%S")
            relative_dir = relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""
            dst_path = os.path.join(self.download_destination_dir, relative_dir, f"{base}_{suffix}{ext}")
        os.replace(src_path, dst_path)
        return True

    def finish_downloaded_file(self, filename: str) -> bool:
        try:
            moved = self.move_file_to_destination(filename)
            if moved:
                self.log.info(f"  → 文件已移动到目标目录: {filename}")
            else:
                self.log.warning(f"  → 未找到待移动文件（可能已在目标目录）: {filename}")
            return moved
        except Exception as e:
            self.log.error(f"  移动下载完成文件失败: {filename} | {e}")
            return False

    def build_download_task(self, file_item: dict, gid: str) -> dict:
        relative_path = self.file_path(file_item)
        return {
            "file_name": relative_path,
            "size": int(file_item.get("size", 0)),
            "openlist_path": self.openlist_path(relative_path),
            "retry_count": 0,
            "start_at": time.time(),
            "last_progress_log_at": 0.0,
            "should_delete": True,
            "gid": gid,
        }

    def attach_existing_downloads(
        self,
        target_files: list[dict],
        aria2_tasks_by_name: dict[str, dict],
        active_downloads: dict[str, dict],
    ) -> int:
        added_size = 0
        active_names = {task["file_name"] for task in active_downloads.values()}
        for file_item in target_files:
            file_name = self.file_path(file_item)
            task_info = aria2_tasks_by_name.get(file_name, {})
            status = str(task_info.get("status", "")).lower()
            gid = str(task_info.get("gid", "")).strip()
            if status not in ACTIVE_ARIA2_STATUSES or not gid or file_name in active_names:
                continue
            task = self.build_download_task(file_item, gid)
            active_downloads[gid] = task
            added_size += int(task["size"])
            active_names.add(file_name)
            self.log.info(f"  接管已有 Aria2 下载任务: {file_name} | gid={gid}")
        return added_size

    def submit_download_for_transferred_file(self, file_item: dict, active_downloads: dict[str, dict]) -> int:
        file_name = self.file_path(file_item)
        self.log.info(f"    开始获取直链并提交 Aria2 下载任务: {file_name}")
        dl_url = self.openlist_client.openlist_get_download_url(self.openlist_path(file_name))
        gid = self.aria2_client.aria2_add_url(dl_url, file_name)
        active_downloads[gid] = self.build_download_task(file_item, gid)
        self.log.info(f"    下载任务提交成功，已开始下载: {file_name} | gid={gid}")
        return int(file_item.get("size", 0))

    def transfer_and_submit_download(
        self,
        file_item: dict,
        share_id: str,
        stoken: str,
        active_downloads: dict[str, dict],
        current_inflight_size: int,
    ) -> tuple[bool, int]:
        file_name = self.file_path(file_item)
        file_size = int(file_item.get("size", 0))
        quark_available_size, capacity_limit = self.get_capacity_limit()
        if quark_available_size is None:
            return False, 0
        if file_size > capacity_limit:
            self.log.info(
                f"  容量不足，等待下载完成腾出空间: {file_name} "
                f"({file_size//1024//1024} MB > {capacity_limit//1024//1024} MB)"
            )
            return False, 0

        self.log.info(f"\n{'─' * 50}")
        self.log.info(
            f"▶ 转存文件: {file_name} ({file_size//1024//1024} MB)，"
            f"当前占用 {current_inflight_size//1024//1024} MB，"
            f"可用 {quark_available_size//1024//1024} MB"
        )
        try:
            relative_dir = file_name.rsplit("/", 1)[0] if "/" in file_name else ""
            if relative_dir:
                self.openlist_client.openlist_ensure_dir(self.openlist_dir_path(relative_dir))
            target_parent_fid = self.quark_client.ensure_my_dir_path(self.save_to_fid, relative_dir)
            self.log.info(f"  目标转存目录 fid: {target_parent_fid}")
            saved_fids = self.quark_client.save_files_to_my_disk(
                [file_item],
                share_id,
                stoken,
                to_pdir_fid=target_parent_fid,
            )
            if not saved_fids:
                raise RuntimeError("转存失败：未获得 fid")
            self.log.info(f"  转存任务提交成功，转存成功: {file_name} | fid={saved_fids[0]}")
        except Exception as e:
            self.log.error(f"  转存失败: {file_name} | {e}")
            return True, 0

        refresh_delay = max(self.openlist_refresh_delay, 0)
        if refresh_delay:
            self.log.info(f"  等待 OpenList 刷新目录（{refresh_delay} 秒）...")
            time.sleep(refresh_delay)
        max_submit_attempts = max(self.download_submit_max_retries, 1)
        retry_interval = max(self.download_submit_retry_interval, 0)
        for attempt in range(1, max_submit_attempts + 1):
            try:
                if attempt == 1:
                    self.log.info(f"  → 提交下载任务: {file_name}")
                else:
                    self.log.info(f"  → 第 {attempt}/{max_submit_attempts} 次重试提交下载任务: {file_name}")
                submitted_size = self.submit_download_for_transferred_file(file_item, active_downloads)
                return True, submitted_size
            except Exception as e:
                self.log.error(
                    f"    获取直链或提交下载失败({attempt}/{max_submit_attempts}): {file_name} | {e}"
                )
                if attempt < max_submit_attempts:
                    if retry_interval:
                        self.log.info(f"    等待 {retry_interval} 秒后重试获取直链并提交下载...")
                        time.sleep(retry_interval)
        self.cleanup_transferred_file(file_name, "回滚删除 Quark 转存文件")
        return True, 0

    def submit_next_pending_file(
        self,
        pending_files: list[dict],
        quark_files_by_name: dict[str, list[dict]],
        local_downloaded_names: set[str],
        aria2_tasks_by_name: dict[str, dict],
        active_downloads: dict[str, dict],
        current_inflight_size: int,
        share_id: str,
        stoken: str,
    ) -> tuple[list[dict], int, bool]:
        if not pending_files:
            return pending_files, 0, False

        pending_files.sort(key=self.file_path)
        active_names = {task["file_name"] for task in active_downloads.values()}

        for index, file_item in enumerate(pending_files):
            file_name = self.file_path(file_item)
            aria2_status = str(aria2_tasks_by_name.get(file_name, {}).get("status", "")).lower()
            if file_name in local_downloaded_names or aria2_status == "complete":
                continue
            if file_name in active_names or aria2_status in ACTIVE_ARIA2_STATUSES:
                continue

            remaining_files = pending_files[:index] + pending_files[index + 1:]
            if file_name in quark_files_by_name:
                self.log.info(f"  发现已转存文件，直接提交下载: {file_name}")
                try:
                    submitted_size = self.submit_download_for_transferred_file(file_item, active_downloads)
                    return remaining_files, submitted_size, True
                except Exception as e:
                    self.log.error(f"  已转存文件提交下载失败: {file_name} | {e}")
                    return remaining_files, 0, True

            try:
                if self.openlist_client.openlist_file_exists(self.openlist_path(file_name)):
                    self.log.info(f"  OpenList 中发现已存在文件，直接提交下载: {file_name}")
                    try:
                        submitted_size = self.submit_download_for_transferred_file(file_item, active_downloads)
                        return remaining_files, submitted_size, True
                    except Exception as e:
                        self.log.error(f"  OpenList 已存在文件提交下载失败: {file_name} | {e}")
                        return remaining_files, 0, True
            except Exception as e:
                self.log.error(f"  检查 OpenList 文件是否存在失败，暂不转存: {file_name} | {e}")
                return pending_files, 0, False

            consumed, submitted_size = self.transfer_and_submit_download(
                file_item,
                share_id,
                stoken,
                active_downloads,
                current_inflight_size,
            )
            if consumed:
                return remaining_files, submitted_size, True
            return pending_files, 0, False

        return [], 0, False

    def process_active_downloads(
        self,
        active_downloads: dict[str, dict],
        failed_files: list[str],
    ) -> tuple[int, bool]:
        released_size = 0
        finalizing_gids = []
        retry_items = []
        deletion_blocked = False

        for gid, task in list(active_downloads.items()):
            file_name = task["file_name"]
            if task.get("awaiting_delete"):
                finalizing_gids.append(gid)
                continue

            try:
                status = self.aria2_client.aria2_get_status(gid)
            except Exception as e:
                self.log.error(f"  查询下载状态失败: {file_name} | {e}")
                continue

            state = status.get("status")
            if state == "complete":
                if not task.get("finished"):
                    self.log.info(f"  ✓ 下载完成: {file_name}")
                    self.finish_downloaded_file(file_name)
                    task["finished"] = True
                task["should_delete"] = True
                task["awaiting_delete"] = True
                finalizing_gids.append(gid)
            elif state == "error":
                retry_count = int(task.get("retry_count", 0))
                if retry_count < self.aria2_max_retries:
                    self.log.warning(
                        f"  ✗ 下载失败，准备重试: {file_name} | "
                        f"{status.get('errorMessage')} | 重试 {retry_count + 1}/{self.aria2_max_retries}"
                    )
                    finalizing_gids.append(gid)
                    task["should_delete"] = False
                    retry_items.append(task)
                else:
                    self.log.error(
                        f"  ✗ 下载出错且已达最大重试次数: {file_name} | "
                        f"{status.get('errorMessage')} | 重试 {retry_count}/{self.aria2_max_retries}"
                    )
                    if not task.get("failure_recorded"):
                        failed_files.append(file_name)
                        task["failure_recorded"] = True
                    task["should_delete"] = True
                    task["awaiting_delete"] = True
                    finalizing_gids.append(gid)
            elif state == "removed":
                self.log.warning(f"  ✗ 下载任务已被移除，清理转存文件: {file_name}")
                if not task.get("failure_recorded"):
                    failed_files.append(file_name)
                    task["failure_recorded"] = True
                task["should_delete"] = True
                task["awaiting_delete"] = True
                finalizing_gids.append(gid)
            elif state in ACTIVE_ARIA2_STATUSES:
                completed = int(status.get("completedLength", 0))
                total = int(status.get("totalLength", 1))
                speed = int(status.get("downloadSpeed", 0))
                pct = completed / total * 100 if total else 0
                now = time.time()
                last_log_at = float(task.get("last_progress_log_at", 0.0))
                if now - last_log_at >= max(self.download_log_interval, 1):
                    self.log.info(f"  ↓ {file_name}  {pct:.1f}%  速度: {speed//1024} KB/s")
                    task["last_progress_log_at"] = now
            else:
                self.log.info(f"  ? 下载状态未知: {file_name} | state={state}")

        for gid in finalizing_gids:
            task = active_downloads.get(gid)
            if not task:
                continue
            if task.get("should_delete", True):
                deleted = self.delete_transferred_file(
                    task.get("openlist_path", ""),
                    task["file_name"],
                    "已删除 Quark 转存文件",
                )
                if not deleted:
                    task["awaiting_delete"] = True
                    deletion_blocked = True
                    continue
                self.cleaned_transferred_names.add(task["file_name"])
            active_downloads.pop(gid, None)
            released_size += int(task["size"])

        for task in retry_items:
            file_name = task["file_name"]
            retry_count = int(task.get("retry_count", 0)) + 1
            try:
                dl_url = self.openlist_client.openlist_get_download_url(task["openlist_path"])
                new_gid = self.aria2_client.aria2_add_url(dl_url, file_name)
                task["retry_count"] = retry_count
                task["start_at"] = time.time()
                task["should_delete"] = True
                task["awaiting_delete"] = False
                task["last_progress_log_at"] = 0.0
                task["gid"] = new_gid
                active_downloads[new_gid] = task
                released_size -= int(task["size"])
                self.log.info(f"  ↻ 重试下载任务提交成功，已开始下载: {file_name} | gid={new_gid}")
            except Exception as e:
                self.log.error(f"  重试提交失败: {file_name} | {e}")
                failed_files.append(file_name)
                if not self.delete_transferred_file(
                    task.get("openlist_path", ""),
                    file_name,
                    "已删除 Quark 转存文件",
                ):
                    task["should_delete"] = True
                    task["awaiting_delete"] = True
                    active_downloads[task["gid"]] = task
                    released_size -= int(task["size"])
                    deletion_blocked = True

        return released_size, deletion_blocked
