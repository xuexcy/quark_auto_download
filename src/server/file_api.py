import html
import os
import posixpath
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Callable

from aria2_api import Aria2Client
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


def _is_path_under(path: str, root: str) -> bool:
    """判断 path 是否位于 root 之下（不含 root 自身）。"""
    try:
        abs_path = os.path.abspath(path)
        abs_root = os.path.abspath(root)
    except (OSError, ValueError):
        return False
    if abs_path == abs_root:
        return False
    try:
        return os.path.commonpath([abs_path, abs_root]) == abs_root
    except ValueError:
        return False


def prune_empty_local_dirs(root_dir: str) -> list[str]:
    """自底向上删除 root_dir 下的空文件夹，不删除 root_dir 本身。"""
    if not root_dir or not os.path.isdir(root_dir):
        return []
    deleted: list[str] = []
    for current, _dirnames, _filenames in os.walk(root_dir, topdown=False):
        if not _is_path_under(current, root_dir):
            continue
        try:
            os.rmdir(current)
        except OSError:
            continue
        deleted.append(normalize_relative_path(os.path.relpath(current, root_dir)))
    return deleted


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
        aria2_client: Aria2Client,
        save_to_fid: str,
        aria2_download_dir: str,
        download_destination_dir: str,
        aria2_max_retries: int,
        download_log_interval: int,
        logger,
        download_submit_max_retries: int = 10,
        download_submit_retry_interval: int = 30,
        work_dir: str = "",
        remote_download: bool = False,
    ):
        self.quark_client = quark_client
        # 远端 aria2（朋友的机器）：文件不落在本机，本地移动/校验/扫描一律跳过
        self.remote_download = bool(remote_download)
        self.aria2_client = aria2_client
        self.save_to_fid = save_to_fid
        self.aria2_download_dir = aria2_download_dir
        self.download_destination_dir = download_destination_dir
        self.aria2_max_retries = aria2_max_retries
        self.download_log_interval = download_log_interval
        self.download_submit_max_retries = download_submit_max_retries
        self.download_submit_retry_interval = download_submit_retry_interval
        # 每个任务独立的下载子目录（用任务名，任务名唯一），避免多任务同名文件互相覆盖/互相认领
        self.work_dir = str(work_dir or "").strip()
        self.log = logger

    @property
    def download_root(self) -> str:
        """本任务下载中的暂存目录（<临时或完成目录>/<任务名>）；本机不知道落点时为空串。"""
        if not self.local_verifiable:
            return ""
        if self.work_dir:
            return os.path.join(self.aria2_download_dir, self.work_dir)
        return self.aria2_download_dir

    @property
    def local_verifiable(self) -> bool:
        """本机是否知道文件落在哪。

        远端 aria2 的文件在对方机器上；两个目录都没填时由 aria2 自己的默认目录决定——
        这两种情况本机既不能扫描、也不能移动、更不能拿本地副本去决定要不要删云端副本。
        """
        return bool(self.aria2_download_dir) and not self.remote_download

    @property
    def local_copy_root(self) -> str:
        """本地副本所在目录。

        配了「完成后目录」（download_destination_dir）就是它；留空表示下载完不移动，
        文件就留在 <下载临时目录>/<任务名>/<相对路径>，因此本地副本要从这里找。
        """
        return self.download_destination_dir or self.download_root

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

    def _parent_fid_for(self, relative_dir: str, create: bool = False) -> str | None:
        """相对目录 → 网盘 fid：create=True 时缺失即逐级创建，否则缺失返回 None。"""
        relative_dir = normalize_relative_path(relative_dir)
        if not self.save_to_fid:
            return None
        if not relative_dir:
            return self.save_to_fid
        if create:
            return self.quark_client.ensure_my_dir_path(self.save_to_fid, relative_dir)
        return self.quark_client.find_my_dir_fid(self.save_to_fid, relative_dir)

    def _entries_by_name(self, parent_fid: str, strict: bool = False) -> dict[str, dict] | None:
        """列出目录下的文件条目：{文件名: 条目}（不含文件夹）。

        默认读失败记空（调用方按"什么都没有"继续）；
        strict=True 时读失败返回 None，让调用方区分"目录是空的"和"没读到"——
        删除网盘副本后回查必须用 strict，否则列举失败会被误判成"已删干净"。
        """
        try:
            entries = self.quark_client.list_my_dir_entries(parent_fid)
        except Exception as e:
            self.log.warning(f"  读取网盘目录失败: {parent_fid} | {e}")
            return None if strict else {}
        result: dict[str, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("dir"):
                continue
            name = unescape_html_name(str(entry.get("file_name") or "")).strip()
            if name:
                result[name] = entry
        return result

    def resolve_my_fid(self, file_item: dict) -> str:
        """取该文件在网盘里的 fid：优先用转存时回填的 my_fid，否则按目录+文件名回查一次。"""
        cached = str(file_item.get("my_fid") or "").strip()
        if cached:
            return cached
        relative_path = self.file_path(file_item)
        parent_fid = self._parent_fid_for(self.relative_dir(relative_path))
        if not parent_fid:
            return ""
        entry = self._entries_by_name(parent_fid).get(self.file_name(file_item))
        fid = str((entry or {}).get("fid") or "").strip()
        if fid:
            file_item["my_fid"] = fid
        return fid

    def download_url_for(self, file_item: dict) -> str:
        """把网盘文件换成 aria2 可用的下载直链（直链有时效，取完立即提交）。"""
        relative_path = self.file_path(file_item)
        my_fid = self.resolve_my_fid(file_item)
        if not my_fid:
            raise RuntimeError(f"网盘中找不到该文件的转存副本，无法取直链: {relative_path}")
        try:
            urls = self.quark_client.get_download_urls([my_fid])
        except Exception as e:
            # 带上文件路径，页面/日志里才看得出是哪个文件取直链失败
            raise RuntimeError(f"取下载直链失败: {relative_path} | {e}") from e
        dl_url = str(urls.get(my_fid) or "").strip()
        if not dl_url:
            raise RuntimeError(f"未取到下载直链: {relative_path} | fid={my_fid}")
        return dl_url

    def get_quark_files_by_name(self) -> dict[str, list[dict]]:
        files_by_name: dict[str, list[dict]] = {}
        for item in self.quark_client.list_my_files_recursive(self.save_to_fid):
            relative_path = self.file_path(item)
            if relative_path:
                files_by_name.setdefault(relative_path, []).append(item)
        return files_by_name

    @staticmethod
    def get_local_downloaded_file_sizes(download_dir: str) -> dict[str, int]:
        """返回 {相对路径: 字节数}，stat 失败记 -1。

        只比文件名会把截断文件/占位文件当成已完成，所以统一按名字+大小判断。
        """
        sizes: dict[str, int] = {}
        if not os.path.isdir(download_dir):
            return sizes
        for root, _, files in os.walk(download_dir):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                relative_path = normalize_relative_path(os.path.relpath(file_path, download_dir))
                try:
                    sizes[relative_path] = os.path.getsize(file_path)
                except OSError as e:
                    sizes[relative_path] = -1
        return sizes

    @staticmethod
    def get_local_downloaded_file_names(download_dir: str) -> set[str]:
        return set(FileApi.get_local_downloaded_file_sizes(download_dir))

    def get_local_downloaded_sizes_from_dirs(self) -> dict[str, int]:
        if not self.local_verifiable:
            return {}  # 不知道落点：本机没有可扫描的副本，是否已下载只能看 Aria2 状态
        return self.get_local_downloaded_file_sizes(self.local_copy_root)

    def get_local_downloaded_file_names_from_dirs(self) -> set[str]:
        return set(self.get_local_downloaded_sizes_from_dirs())

    def refresh_runtime_state(self) -> tuple[dict[str, list[dict]], dict[str, int], dict[str, dict]]:
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
            local_downloaded_sizes = self.get_local_downloaded_sizes_from_dirs()
            self.log.info(
                f"  本地已完成文件: {len(local_downloaded_sizes)} "
                f"（耗时 {time.monotonic() - local_started:.1f}s）"
            )
        except Exception as e:
            self.log.error(f"读取本地下载目录失败: {e}")
            local_downloaded_sizes = {}

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
        return quark_files_by_name, local_downloaded_sizes, aria2_tasks_by_name

    def get_available_space(self) -> int | None:
        try:
            return self.quark_client.get_available_space()
        except Exception as e:
            self.log.error(f"查询网盘剩余容量失败: {e}")
            return None

    def invalidate_available_space_cache(self) -> None:
        self.quark_client.invalidate_available_space_cache()

    def adjust_available_space_cache(self, delta_bytes: int) -> None:
        self.quark_client.adjust_available_space_cache(delta_bytes)

    @property
    def available_space_low_confidence(self) -> bool:
        """当前可用容量是否只是估算值（配额接口与递归统计都失败）。"""
        return bool(getattr(self.quark_client, "available_space_low_confidence", False))

    def _reserve_destination_path(self, relative_path: str) -> str:
        """原子占用目标路径（O_CREAT|O_EXCL），同名时自动加时间戳后缀，绝不复用/覆盖已有文件。"""
        relative_dir = self.relative_dir(relative_path)
        base_name = relative_path.rsplit("/", 1)[-1]
        stem, ext = os.path.splitext(base_name)
        target_dir = os.path.join(self.download_destination_dir, relative_dir) if relative_dir else self.download_destination_dir
        os.makedirs(target_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        last_error: Exception | None = None
        for index in range(1000):
            if index == 0:
                candidate_name = base_name
            elif index == 1:
                candidate_name = f"{stem}_{stamp}{ext}"
            else:
                candidate_name = f"{stem}_{stamp}_{index}{ext}"
            candidate = os.path.join(target_dir, candidate_name)
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                continue
            except OSError as e:
                last_error = e
                break
            os.close(fd)
            return candidate
        raise RuntimeError(f"目标目录无法创建唯一文件名: {relative_path} | {last_error or '同名文件过多'}")

    def move_file_to_destination(self, relative_path: str) -> bool:
        relative_path = normalize_relative_path(relative_path)
        if not self.local_verifiable:
            return True  # 不知道文件落在哪：本机没有可移动的东西
        src_path = os.path.join(self.download_root, relative_path)
        if not self.download_destination_dir:
            # 没配完成后目录：不移动，文件就留在 <下载临时目录>/<job_id>/ 下
            return os.path.isfile(src_path)
        dst_path = os.path.join(self.download_destination_dir, relative_path)
        if not os.path.isfile(src_path):
            return os.path.isfile(dst_path)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        if os.path.abspath(src_path) == os.path.abspath(dst_path):
            return True
        reserved = self._reserve_destination_path(relative_path)
        if os.path.abspath(reserved) != os.path.abspath(dst_path):
            self.log.warning(
                f"  目标目录已存在同名文件，改名为 {os.path.basename(reserved)}: {relative_path}"
            )
        try:
            # 下载临时目录与目标目录可能跨挂载点：os.replace 会抛 EXDEV，shutil.move 自动退化为复制+删除
            shutil.move(src_path, reserved)
        except Exception:
            try:
                os.unlink(reserved)
            except OSError:
                pass
            raise
        return True

    @staticmethod
    def _local_file_matches(path: str, expected_size: int = 0) -> bool:
        try:
            actual = os.path.getsize(path)
        except OSError:
            return False
        if actual <= 0:
            return False
        expected = int(expected_size or 0)
        return expected <= 0 or actual == expected

    def resolve_local_copy(self, relative_path: str, expected_size: int = 0) -> str:
        """本地可信副本的真实路径；不存在/大小不符返回空串。

        目标目录已有同名文件时，落盘会改名成 name_YYYYmmdd_HHMMSS[_n].ext
        （见 _reserve_destination_path），所以先按原名找，再按改名规则兜底。
        """
        if not self.local_verifiable:
            return ""
        try:
            relative_path = normalize_relative_path(relative_path)
        except ValueError:
            return ""
        exact = os.path.join(self.local_copy_root, relative_path)
        if self._local_file_matches(exact, expected_size):
            return exact
        relative_dir = self.relative_dir(relative_path)
        base_name = relative_path.rsplit("/", 1)[-1]
        target_dir = (
            os.path.join(self.local_copy_root, relative_dir)
            if relative_dir
            else self.local_copy_root
        )
        stem, ext = os.path.splitext(base_name)
        pattern = re.compile(rf"^{re.escape(stem)}_\d{{8}}_\d{{6}}(?:_\d+)?{re.escape(ext)}$")
        try:
            names = sorted(os.listdir(target_dir))
        except OSError:
            return ""
        for name in names:
            if not pattern.match(name):
                continue
            candidate = os.path.join(target_dir, name)
            if self._local_file_matches(candidate, expected_size):
                return candidate
        return ""

    def local_copy_is_intact(self, relative_path: str, expected_size: int = 0) -> bool:
        """本地是否已有可信的副本（目标目录；没配完成后目录则是下载目录，存在且大小匹配）。"""
        if not self.local_verifiable:
            return False  # 不知道落点：本机无法校验，调用方按「无法校验」处理
        return bool(self.resolve_local_copy(relative_path, expected_size))

    def finish_downloaded_file(self, filename: str) -> bool:
        """移动已下载完成的文件；返回 False 表示文件没有真正落到目标目录。"""
        if self.remote_download:
            # 远端：Aria2 报 complete 就算完成，本机既不知道也不该管文件落在哪
            self.log.info(f"  → 远端 Aria2 下载完成（文件在远端，未做本地移动）: {filename}")
            return True
        if not self.local_verifiable:
            # 两个目录都没填：落点由 aria2 自己决定，本机无法移动也无法核对
            self.log.info(f"  → 未配置下载目录，按 Aria2 状态判定完成（本机不校验、不移动）: {filename}")
            return True
        if not self.download_destination_dir:
            # 留空 = 不移动：只要文件在本任务下载目录里就算落盘成功
            try:
                keep_path = os.path.join(self.download_root, normalize_relative_path(filename))
            except ValueError:
                return False
            if os.path.isfile(keep_path):
                self.log.info(f"  → 未配置完成后目录，文件保留在下载目录: {filename}")
                return True
            self.log.warning(f"  → 未找到下载完成的文件: {filename}")
            return False
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

    def _should_prune_download_dir(self) -> bool:
        download_dir = str(self.download_root or "").rstrip("/")
        dest_dir = str(self.download_destination_dir or "").rstrip("/")
        if not download_dir:
            return False
        if dest_dir and os.path.abspath(download_dir) == os.path.abspath(dest_dir):
            return False
        return True

    def cleanup_empty_download_dirs(self) -> list[str]:
        """全部任务结束后，清理本任务下载临时目录中残留的空文件夹。"""
        if not self.local_verifiable:
            return []
        if not self._should_prune_download_dir():
            return []
        root = self.download_root
        try:
            deleted = prune_empty_local_dirs(root)
        except Exception as e:
            self.log.error(f"  清理下载临时空文件夹失败: {e}")
            return []
        # prune_empty_local_dirs 只删 root 里面的空目录，root 自己（任务名那一层）会留下来：
        # 文件全挪走以后它就是个空壳，顺手一起删掉，免得 aria2 下载目录里堆一串空的「任务名」目录。
        # 非空会抛 OSError（还有文件/子目录），那就保留 —— 只删真正空的那一个。
        if self.work_dir and root and os.path.isdir(root):
            try:
                os.rmdir(root)
            except OSError:
                pass
            else:
                deleted.append(normalize_relative_path(os.path.basename(root)))
                self.log.info(f"  已删除本任务的空下载目录: {root}")
        if deleted:
            self.log.info(f"  已清理下载临时空文件夹 {len(deleted)} 个")
            for path in deleted[:20]:
                self.log.info(f"    - {path}")
            if len(deleted) > 20:
                self.log.info(f"    ... 另有 {len(deleted) - 20} 个")
        return deleted

    def build_download_task(self, file_item: dict, gid: str) -> dict:
        relative_path = self.file_path(file_item)
        size = int(file_item.get("size", 0) or 0)
        return {
            "file_item": file_item,
            "file_name": relative_path,
            "size": size,
            "my_fid": str(file_item.get("my_fid") or "").strip(),
            "retry_count": 0,
            "start_at": time.time(),
            "last_progress_log_at": 0.0,
            "gid": gid,
            # 单文件下载进度：前端按 completed/total 画进度条
            "completed": 0,
            "total": size,
            "speed": 0,
            "progress": 0.0,
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

    def _verify_transferred_files(
        self,
        target_parent_fid: str,
        group_files: list[dict],
        saved_fids: list[str],
    ) -> tuple[list[dict], list[dict]]:
        """转存后回查目标目录，确认每个文件真的出现了。

        提交成功不等于转存成功（接口可能只回了 fid 而任务失败），所以按名字核对；
        核对接口本身失败时保持乐观，避免网络抖动把整批判死。

        saved_fids 是本次转存真正写回自己网盘的 fid：目录里可能已经有同名旧文件，
        只按名字认领会把旧文件的 fid 当成新副本，之后清理就会删掉用户原有的文件。
        """
        saved = {str(fid).strip() for fid in (saved_fids or []) if str(fid or "").strip()}
        try:
            entries = self.quark_client.list_my_dir_entries(target_parent_fid)
        except Exception as e:
            self.log.warning(
                f"  转存后回查目标目录失败，按接口返回的 {len(saved)} 个 fid 记成功: {e}"
            )
            return list(group_files), []
        # 名字 → 条目：顺便把 fid 回填到 file_item，后面取直链/删副本都不用再查一次
        entries_by_name: dict[str, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("dir"):
                continue
            name = unescape_html_name(str(entry.get("file_name") or "")).strip()
            if name:
                entries_by_name[name] = entry
        if not entries_by_name:
            self.log.warning("  目标目录回查为空，无法核对，按接口返回记成功")
            return list(group_files), []
        succeeded: list[dict] = []
        missing: list[dict] = []
        for item in group_files:
            entry = entries_by_name.get(self.file_name(item).strip())
            if entry is None:
                missing.append(item)
                continue
            fid = str(entry.get("fid") or "").strip()
            if saved and fid not in saved:
                # 目录里已经有同名文件，但 fid 不是这次转存写回来的 → 很可能是用户原有的文件。
                # 认领它会在清理阶段把用户原文件删掉，所以宁可判「未生效」。
                self.log.error(
                    "  目标目录已有同名文件且不是本次转存结果，按未生效处理（不接管旧 fid）: "
                    f"{self.file_path(item)}"
                )
                missing.append(item)
                continue
            if fid:
                item["my_fid"] = fid
            succeeded.append(item)
        return succeeded, missing

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
                    succeeded, missing = self._verify_transferred_files(
                        target_parent_fid, group_files, saved_fids
                    )
                    self.log.info(
                        f"  转存任务提交成功，确认落盘 {len(succeeded)}/{len(group_files)} 个文件 "
                        f"| fids={saved_fids}"
                    )
                    for file_name in [self.file_path(item) for item in succeeded]:
                        self.log.info(f"    - {file_name}")
                    if succeeded:
                        _mark_and_notify(succeeded)
                    for item in missing:
                        missing_path = self.file_path(item)
                        failed.append(item)
                        failure_errors[missing_path] = "转存后目标目录未出现该文件"
                        self.log.error(f"  转存未生效（目标目录没有该文件）: {missing_path}")
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

    def submit_download(self, file_item: dict, task_infos: dict[str, dict] | None = None) -> dict:
        """
        下载 Worker 从待下载队列取到任务后调用。三种 Aria2 分支（互斥）：
        a. paused → unpause 恢复，记录状态
        b. active/waiting（含人为在 Aria2 里恢复）→ 只接管并记录状态
        c. 无已有任务 → addUri 新建提交，记录状态
        """
        file_name = self.file_path(file_item)
        self.log.info(f"  → 从待下载队列取任务，处理: {file_name}")
        # task_infos 由调用方一次性取好的 aria2 快照，避免每个文件 3 次 RPC
        if task_infos is not None:
            existing = task_infos.get(file_name)
        else:
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
        dl_url = self.download_url_for(file_item)
        gid = self.aria2_client.aria2_add_url(dl_url, file_name)
        task = self.build_download_task(file_item, gid)
        task["submit_action"] = "add"
        self.log.info(f"    下载任务提交成功: {file_name} | gid={gid}")
        return task

    def cleanup_files_batch(self, tasks: list[dict], action: str = "已删除 Quark 转存文件") -> tuple[list[str], list[dict]]:
        """按目录批量清理网盘副本（夸克原生 file/delete）。返回 (成功文件名列表, 失败任务列表)。"""
        if not tasks:
            return [], []

        by_dir: dict[str, list[dict]] = defaultdict(list)
        for task in tasks:
            file_name = normalize_relative_path(task["file_name"])
            by_dir[self.relative_dir(file_name)].append({**task, "file_name": file_name})

        deleted_names: list[str] = []
        failed_tasks: list[dict] = []

        for relative_dir, dir_tasks in by_dir.items():
            try:
                parent_fid = self._parent_fid_for(relative_dir)
                if not parent_fid:
                    # 目录都不在了，说明云端副本早就没有
                    for item in dir_tasks:
                        self.log.info(f"  → {action}: {item['file_name']} 已不在网盘中，无需清理")
                        deleted_names.append(item["file_name"])
                    continue
                # 删除是不可逆的：这里必须 strict，读不到目录时不能假设「文件已经不在了」
                entries = self._entries_by_name(parent_fid, strict=True)
                if entries is None:
                    raise RuntimeError("列举网盘目录失败，无法确认要删的文件")
                targets: list[tuple[dict, str]] = []
                for item in dir_tasks:
                    expected_size = int(item.get("size") or 0)
                    if self.local_verifiable and not self.local_copy_is_intact(
                        item["file_name"], expected_size
                    ):
                        # 删除网盘副本是不可逆操作：本地没有可信副本就绝不动云端
                        self.log.error(
                            f"  {action}跳过（本地目标目录没有可信副本）: {item['file_name']}"
                        )
                        failed_tasks.append({**item, "note": "本地副本缺失或大小不符，已拒绝删除网盘副本"})
                        continue
                    fid = str(item.get("my_fid") or "").strip()
                    if not fid:
                        entry = entries.get(self.file_name(item))
                        fid = str((entry or {}).get("fid") or "").strip()
                    if not fid:
                        self.log.info(f"  → {action}: {item['file_name']} 已不在网盘中，无需清理")
                        deleted_names.append(item["file_name"])
                        continue
                    targets.append((item, fid))
                if not targets:
                    continue
                self.quark_client.delete_my_fids([fid for _item, fid in targets])
                # 删除是异步任务，回查一次确认真的没了，避免「报成功但还在」
                # 回查失败必须算失败：否则会把还占着容量的副本记成「已释放」
                remaining = self._entries_by_name(parent_fid, strict=True)
                if remaining is None:
                    raise RuntimeError("删除后回查目录失败，无法确认是否删干净")
                for item, _fid in targets:
                    if self.file_name(item) in remaining:
                        self.log.error(
                            f"  {action}失败（删除后仍可见，请手动清理）: {item['file_name']}"
                        )
                        failed_tasks.append(item)
                    else:
                        self.log.info(f"  → {action}成功，已确认删除: {item['file_name']}")
                        deleted_names.append(item["file_name"])
            except Exception as e:
                names = ", ".join(item["file_name"] for item in dir_tasks)
                self.log.error(f"  {action}失败（请手动清理）: {names} | {e}")
                deleted_set = set(deleted_names)
                failed_tasks.extend(
                    item for item in dir_tasks if item["file_name"] not in deleted_set
                )

        if deleted_names:
            self.log.info(f"  已清理 {len(deleted_names)} 个网盘副本，释放网盘容量")
        # 同一文件可能既被判本地副本不合格、又在异常分支被重复记，按文件名去重
        unique_failed: list[dict] = []
        seen_failed: set[str] = set()
        for item in failed_tasks:
            key = str(item.get("file_name") or "")
            if key in seen_failed:
                continue
            seen_failed.add(key)
            unique_failed.append(item)
        return deleted_names, unique_failed

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
        return deleted

    def cleanup_transferred_file(self, file_name: str, action: str = "已删除 Quark 转存文件") -> bool:
        deleted, failed = self.cleanup_files_batch(
            [{"file_name": file_name}],
            action,
        )
        return bool(deleted) and not failed
