import html
import os
import posixpath
import time
from collections import defaultdict
from collections.abc import Callable

from aria2_api import Aria2Client
from openlist_api import OpenListClient
from quark_api import QuarkClient


ACTIVE_ARIA2_STATUSES = ("active", "waiting", "paused")


def unescape_html_name(value: str) -> str:
    """解码 HTML 实体，例如 Bird&#39;s -> Bird's。"""
    text = str(value or "")
    for _ in range(3):
        nxt = html.unescape(text)
        if nxt == text:
            break
        text = nxt
    return text


def normalize_relative_path(path: str) -> str:
    raw = str(path).replace("\\", "/").strip("/")
    parts = [unescape_html_name(part) for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"非法相对路径: {path}")
    normalized = posixpath.normpath("/".join(parts)) if parts else ""
    if normalized in ("", "."):
        return ""
    if normalized.startswith("../") or normalized == "..":
        raise ValueError(f"非法相对路径: {path}")
    return normalized


def full_path_sort_key(path: str) -> tuple:
    """按完整相对路径排序：目录层级优先，再比文件名（不是只比 basename）。"""
    normalized = normalize_relative_path(path)
    if not normalized:
        return ()
    # 分段比较，保证 dir/a 与 dir2/b 按层级语义排序，而不是只看最后一段文件名
    return tuple(part.casefold() for part in normalized.split("/"))


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
        self.download_submit_max_retries = download_submit_max_retries
        self.download_submit_retry_interval = download_submit_retry_interval
        self.log = logger

    @staticmethod
    def file_path(file_item: dict) -> str:
        # 优先用带目录的 relative_path，保证排序/下载顺序包含完整路径
        return normalize_relative_path(file_item.get("relative_path") or file_item.get("file_name", ""))

    @staticmethod
    def file_sort_key(file_item: dict) -> tuple:
        return full_path_sort_key(FileApi.file_path(file_item))

    @staticmethod
    def file_name(file_item: dict) -> str:
        relative_path = FileApi.file_path(file_item)
        return relative_path.rsplit("/", 1)[-1]

    @staticmethod
    def format_size(size: int) -> str:
        """人类可读大小：B / KB / MB / GB。"""
        n = max(int(size or 0), 0)
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB".replace(".0 ", " ")
        if n < 1024 * 1024 * 1024:
            return f"{n / (1024 * 1024):.1f} MB".replace(".0 ", " ")
        return f"{n / (1024 * 1024 * 1024):.2f} GB".replace(".00 ", " ")

    @staticmethod
    def relative_dir(file_name: str) -> str:
        return file_name.rsplit("/", 1)[0] if "/" in file_name else ""

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
        if not self.download_destination_dir:
            return set()
        return self.get_local_downloaded_file_names(self.download_destination_dir)

    def refresh_runtime_state(self) -> tuple[dict[str, list[dict]], set[str], dict[str, dict]]:
        self.log.info("▶ 刷新运行时状态：列举转存目录 / 本地已完成 / Aria2 任务…")
        started = time.monotonic()
        try:
            quark_files_by_name = self.get_quark_files_by_name()
            self.log.info(
                f"  转存目录已有文件: {sum(len(v) for v in quark_files_by_name.values())} "
                f"（耗时 {time.monotonic() - started:.1f}s）"
            )
        except Exception as e:
            self.log.error(f"读取 Quark 已转存文件列表失败: {e}")
            quark_files_by_name = {}

        try:
            local_started = time.monotonic()
            local_downloaded_names = self.get_local_downloaded_file_names_from_dirs()
            self.log.info(
                f"  本地已完成文件: {len(local_downloaded_names)} "
                f"（耗时 {time.monotonic() - local_started:.1f}s）"
            )
        except Exception as e:
            self.log.error(f"读取本地下载目录失败: {e}")
            local_downloaded_names = set()

        try:
            aria2_started = time.monotonic()
            aria2_tasks_by_name = self.aria2_client.aria2_get_existing_task_infos_by_name()
            self.log.info(
                f"  Aria2 现有任务: {len(aria2_tasks_by_name)} "
                f"（耗时 {time.monotonic() - aria2_started:.1f}s）"
            )
        except Exception as e:
            self.log.error(f"读取 Aria2 任务列表失败: {e}")
            aria2_tasks_by_name = {}

        self.log.info(f"  运行时状态刷新完成，总耗时 {time.monotonic() - started:.1f}s")
        return quark_files_by_name, local_downloaded_names, aria2_tasks_by_name

    def get_available_space(self) -> int | None:
        try:
            return self.quark_client.get_available_space()
        except Exception as e:
            self.log.error(f"查询网盘剩余容量失败: {e}")
            return None

    def invalidate_available_space_cache(self) -> None:
        self.quark_client.invalidate_available_space_cache()

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
            relative_dir = self.relative_dir(relative_path)
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
            "file_item": file_item,
            "file_name": relative_path,
            "size": int(file_item.get("size", 0)),
            "openlist_path": self.openlist_path(relative_path),
            "retry_count": 0,
            "start_at": time.time(),
            "last_progress_log_at": 0.0,
            "gid": gid,
        }

    @staticmethod
    def transfer_cost(file_item: dict) -> int:
        """已转存文件不再占用转存容量预算。"""
        if file_item.get("_already_transferred"):
            return 0
        return int(file_item.get("size", 0))

    @staticmethod
    def take_consecutive_by_capacity(files: list[dict], capacity_limit: int) -> tuple[list[dict], list[dict]]:
        """严格有序：按队列从头连续装入，遇装不下的文件即停止，不跳过。"""
        packed: list[dict] = []
        remaining_capacity = capacity_limit
        for index, file_item in enumerate(files):
            file_size = FileApi.transfer_cost(file_item)
            if file_size > remaining_capacity:
                return packed, files[index:]
            packed.append(file_item)
            remaining_capacity -= file_size
        return packed, []

    @staticmethod
    def pack_files_by_capacity(files: list[dict], capacity_limit: int) -> tuple[list[dict], list[dict]]:
        """非严格有序：可跳过当前装不下的文件，优先填满容量。"""
        packed: list[dict] = []
        deferred: list[dict] = []
        remaining_capacity = capacity_limit
        for file_item in files:
            file_size = FileApi.transfer_cost(file_item)
            if file_size > remaining_capacity:
                deferred.append(file_item)
                continue
            packed.append(file_item)
            remaining_capacity -= file_size
        return packed, deferred

    def transfer_file_batch(
        self,
        files: list[dict],
        share_id: str,
        stoken: str,
        quark_available_size: int,
        on_group_transferred: Callable[[list[dict]], None] | None = None,
    ) -> tuple[list[dict], list[dict], dict[str, str]]:
        """按目录批量转存。已转存文件跳过 API，仍按输入顺序计入成功列表。

        on_group_transferred: 每成功一组（或启动时已转存跳过）即回调，便于流水线提前 ready→下载。
        """
        if not files:
            return [], [], {}

        already_ready = [item for item in files if item.get("_already_transferred")]
        need_transfer = [item for item in files if not item.get("_already_transferred")]
        for item in already_ready:
            self.log.info(f"  已转存，跳过重复转存: {self.file_path(item)}")

        transferred_names: set[str] = {self.file_path(item) for item in already_ready}
        failed: list[dict] = []
        failure_errors: dict[str, str] = {}

        def _mark_and_notify(group_items: list[dict]) -> None:
            for item in group_items:
                item["_already_transferred"] = True
                transferred_names.add(self.file_path(item))
            if on_group_transferred and group_items:
                on_group_transferred(list(group_items))

        if already_ready:
            _mark_and_notify(already_ready)

        if need_transfer:
            batch_size = sum(int(item.get("size", 0)) for item in need_transfer)
            self.log.info(f"\n{'─' * 50}")
            self.log.info(
                f"▶ 按容量批量转存 {len(need_transfer)} 个文件（{batch_size//1024//1024} MB），"
                f"另有 {len(already_ready)} 个已转存跳过，可用 {quark_available_size//1024//1024} MB"
            )

            groups: dict[str, list[dict]] = {}
            for file_item in need_transfer:
                groups.setdefault(self.relative_dir(self.file_path(file_item)), []).append(file_item)

            for relative_dir in sorted(groups):
                group_files = groups[relative_dir]
                group_names = [self.file_path(item) for item in group_files]
                try:
                    if relative_dir:
                        self.openlist_client.openlist_ensure_dir(self.openlist_dir_path(relative_dir))
                    target_parent_fid = self.quark_client.ensure_my_dir_path(self.save_to_fid, relative_dir)
                    self.log.info(
                        f"  目标转存目录 /{relative_dir} fid: {target_parent_fid}，"
                        f"本批 {len(group_files)} 个文件"
                    )
                    saved_fids = self.quark_client.save_files_to_my_disk(
                        group_files,
                        share_id,
                        stoken,
                        to_pdir_fid=target_parent_fid,
                    )
                    if not saved_fids:
                        raise RuntimeError("转存失败：未获得 fid")
                    self.log.info(
                        f"  转存任务提交成功，转存成功 {len(group_files)} 个文件 | fids={saved_fids}"
                    )
                    for file_name in group_names:
                        self.log.info(f"    - {file_name}")
                    _mark_and_notify(group_files)
                except Exception as e:
                    self.log.error(f"  转存失败: {', '.join(group_names)} | {e}")
                    failed.extend(group_files)
                    for file_name in group_names:
                        failure_errors[file_name] = str(e)

        transferred = [item for item in files if self.file_path(item) in transferred_names]
        for item in transferred:
            item["_already_transferred"] = True
        return transferred, failed, failure_errors

    @staticmethod
    def submit_backoff_seconds(attempt: int, base_interval: int, cap: int = 60) -> int:
        """保留兼容；当前提交下载改为单次尝试，不再使用退避。"""
        base_interval = max(base_interval, 1)
        delay = base_interval * (2 ** max(attempt - 1, 0))
        return min(cap, delay)

    def submit_download(self, file_item: dict) -> dict:
        """
        下载 Worker 从待下载队列取到任务后调用。三种 Aria2 分支（互斥）：
        a. paused → unpause 恢复，记录状态
        b. active/waiting（含人为在 Aria2 里恢复）→ 只接管并记录状态
        c. 无已有任务 → addUri 新建提交，记录状态
        """
        file_name = self.file_path(file_item)
        self.log.info(f"  → 从待下载队列取任务，处理: {file_name}")
        existing = self.aria2_client.aria2_find_task_by_path(file_name)
        if existing:
            gid = str(existing.get("gid") or "").strip()
            status = str(existing.get("status") or "").lower()
            if gid and status == "paused":
                self.log.info(
                    f"  [Aria2分支=恢复] paused→unpause: {file_name} | gid={gid}"
                )
                self.aria2_client.aria2_unpause(gid)
                task = self.build_download_task(file_item, gid)
                task["submit_action"] = "unpause"
                self.log.info(f"    已恢复下载: {file_name} | gid={gid}")
                return task
            if gid and status in ("active", "waiting"):
                self.log.info(
                    f"  [Aria2分支=接管] {status}→接管记录: {file_name} | gid={gid}"
                )
                task = self.build_download_task(file_item, gid)
                task["submit_action"] = "adopt"
                return task

        self.log.info(f"  [Aria2分支=新建] 无已有任务→addUri: {file_name}")
        self.log.info(f"    开始获取直链并提交 Aria2 下载任务: {file_name}")
        dl_url = self.openlist_client.openlist_get_download_url(self.openlist_path(file_name))
        gid = self.aria2_client.aria2_add_url(dl_url, file_name)
        task = self.build_download_task(file_item, gid)
        task["submit_action"] = "add"
        self.log.info(f"    下载任务提交成功: {file_name} | gid={gid}")
        return task

    def cleanup_files_batch(self, tasks: list[dict], action: str = "已删除 Quark 转存文件") -> tuple[list[str], list[dict]]:
        """按目录批量清理网盘文件。返回 (成功文件名列表, 失败任务列表)。"""
        if not tasks:
            return [], []

        by_dir: dict[str, list[dict]] = defaultdict(list)
        for task in tasks:
            file_name = task["file_name"]
            openlist_path = task.get("openlist_path") or self.openlist_path(file_name)
            dir_path, base_name = self.openlist_client._split_file_path(openlist_path)
            by_dir[dir_path].append({**task, "openlist_path": openlist_path, "_base_name": base_name})

        deleted_names: list[str] = []
        failed_tasks: list[dict] = []

        for dir_path, dir_tasks in by_dir.items():
            file_names = [item["file_name"] for item in dir_tasks]
            try:
                existing = []
                missing = []
                for item in dir_tasks:
                    if self.openlist_client.openlist_file_exists(item["openlist_path"]):
                        existing.append(item)
                    else:
                        missing.append(item)
                for item in missing:
                    self.log.info(f"  → {action}: {item['file_name']} 已不在网盘中，无需清理")
                    deleted_names.append(item["file_name"])
                if existing:
                    existing_names = [item["_base_name"] for item in existing]
                    self.openlist_client.openlist_delete_files_in_dir(dir_path, existing_names)
                    for item in existing:
                        if self.openlist_client.openlist_file_exists(item["openlist_path"]):
                            self.log.error(
                                f"  {action}失败（删除后仍可见，请手动清理）: {item['file_name']}"
                            )
                            failed_tasks.append(item)
                        else:
                            self.log.info(f"  → {action}成功，已确认删除: {item['file_name']}")
                            deleted_names.append(item["file_name"])
            except Exception as e:
                self.log.error(f"  {action}失败（请手动清理）: {', '.join(file_names)} | {e}")
                deleted_set = set(deleted_names)
                failed_tasks.extend(
                    item for item in dir_tasks if item["file_name"] not in deleted_set
                )

        if deleted_names:
            self.log.info(f"  已通过 OpenList 清理 {len(deleted_names)} 个文件，释放网盘容量")
        return deleted_names, failed_tasks

    def cleanup_empty_dirs(self, relative_paths: list[str] | None = None) -> list[str]:
        """
        清理转存根目录下的空文件夹（不删除转存根本身）。
        relative_paths 有值时只清理这些文件的空父目录；否则全量扫描。
        """
        deleted: list[str] = []
        try:
            if relative_paths:
                deleted = self.quark_client.prune_empty_parent_dirs(relative_paths, self.save_to_fid)
            else:
                deleted = self.quark_client.prune_empty_dirs(self.save_to_fid)
            if deleted:
                self.log.info(f"  已清理网盘空文件夹 {len(deleted)} 个")
                for path in deleted[:20]:
                    self.log.info(f"    - /{path}")
                if len(deleted) > 20:
                    self.log.info(f"    ... 另有 {len(deleted) - 20} 个")
        except Exception as e:
            self.log.error(f"  清理网盘空文件夹失败: {e}")

        # 全量清理时再同步 OpenList，避免每轮批量删文件都扫挂载树
        if relative_paths is None:
            try:
                openlist_removed = self.openlist_client.openlist_prune_empty_dirs(
                    self.openlist_quark_path
                )
                if openlist_removed:
                    self.log.info(f"  已同步清理 OpenList 空目录 {len(openlist_removed)} 个")
            except Exception as e:
                self.log.warning(f"  同步清理 OpenList 空目录失败（可忽略）: {e}")
        return deleted

    def cleanup_transferred_file(self, file_name: str, action: str = "已删除 Quark 转存文件") -> bool:
        deleted, failed = self.cleanup_files_batch(
            [{"file_name": file_name, "openlist_path": self.openlist_path(file_name)}],
            action,
        )
        return bool(deleted) and not failed
