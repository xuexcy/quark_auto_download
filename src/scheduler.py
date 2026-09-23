"""流水线调度器。

权威有序范围（仅此）：
  分享链接列举全部文件 →【有序：全部文件】→ 有序转存 →【有序进入待下载队列】
其中「待下载队列」= 状态 ready。

strict_download_order / prefer_path_order 只约束上述转存与进入 ready 的路径偏好；
不限制下载 Worker 一次只下一个，也不因已有 downloading 而停止从 ready 取后续任务。
下载并发交给 Aria2；失败跳过不堵后续。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from error_classify import classify_error
from file_api import FileApi, full_path_sort_key


JobState = Literal[
    "pending",
    "transfer_retry",
    "transferring",
    "ready",  # 待下载队列：转存完成后有序进入；下载 Worker 从此取任务
    "retry",
    "downloading",
    "cleanup",
    "done",
    "failed",
]


@dataclass
class Job:
    path: str
    size: int
    file_item: dict
    state: JobState = "pending"
    already_transferred: bool = False
    gid: str = ""
    note: str = ""
    download_task: dict | None = None
    submit_fail_count: int = 0
    transfer_fail_count: int = 0

    def cleanup_payload(self, file_api: FileApi) -> dict:
        return {
            "file_item": self.file_item,
            "file_name": self.path,
            "size": self.size,
            "openlist_path": file_api.openlist_path(self.path),
        }


@dataclass
class CloudPlan:
    kind: Literal["cleanup", "transfer", "wait_capacity", "idle"]
    jobs: list[Job] = field(default_factory=list)
    available_size: int = 0
    blocked_path: str = ""
    blocked_size: int = 0
    source: str = "pending"  # pending | transfer_retry


class PipelineScheduler:
    """状态机。有序仅限转存→进入待下载队列(ready)；下载从 ready 取任务可并发。"""

    def __init__(self, file_api: FileApi, prefer_path_order: bool = True):
        self.file_api = file_api
        self.prefer_path_order = bool(prefer_path_order)
        # 兼容配置名 strict_download_order：语义=转存+进入待下载队列有序，非串行下载
        self.strict_download_order = self.prefer_path_order
        self.ordered_paths: list[str] = []
        self.jobs: dict[str, Job] = {}
        self.failed_files: list[str] = []

    def initialize(self, all_files: list[dict], logger) -> None:
        ordered = sorted(all_files, key=self.file_api.file_sort_key)
        quark_files_by_name, local_downloaded_names, aria2_tasks_by_name = self.file_api.refresh_runtime_state()
        skipped = []

        for file_item in ordered:
            path = self.file_api.file_path(file_item)
            size = int(file_item.get("size", 0))
            aria2_info = aria2_tasks_by_name.get(path, {})
            aria2_status = str(aria2_info.get("status", "")).lower()
            gid = str(aria2_info.get("gid", "")).strip()

            self.ordered_paths.append(path)
            job = Job(path=path, size=size, file_item=file_item)
            self.jobs[path] = job

            if path in local_downloaded_names:
                skipped.append(f"{path} (本地已存在)")
                if path in quark_files_by_name:
                    job.state = "cleanup"
                    job.already_transferred = True
                    job.note = "本地已存在，待清理网盘"
                else:
                    job.state = "done"
                    job.note = "本地已存在"
                continue

            if aria2_status == "complete":
                skipped.append(f"{path} (Aria2已完成)")
                self.file_api.finish_downloaded_file(path)
                job.state = "cleanup"
                job.already_transferred = True
                job.note = "Aria2已完成"
                continue

            # 暂停任务：不在启动时批量 unpause；放入待下载队列(ready)，由下载 Worker 取到后再 unpause
            if aria2_status == "paused" and gid:
                job.already_transferred = True
                job.state = "ready"
                job.gid = gid
                job.note = "存在Aria2暂停任务，已入待下载队列，待 Worker 取到后恢复"
                logger.info(
                    f"  发现 Aria2 暂停任务，已入待下载队列(ready)，待下载 Worker 恢复: "
                    f"{path} | gid={gid}"
                )
                continue

            # 已在 Aria2 下载中/排队：启动时直接接管跟踪（等价于 Worker 的 active/waiting 分支）
            if aria2_status in ("active", "waiting") and gid:
                job.download_task = self.file_api.build_download_task(file_item, gid)
                job.state = "downloading"
                job.already_transferred = True
                job.gid = gid
                job.note = "接管已有Aria2任务"
                logger.info(
                    f"  [Aria2分支=接管] 启动时接管已有任务({aria2_status}): {path} | gid={gid}"
                )
                continue

            already_ready = path in quark_files_by_name
            if not already_ready:
                try:
                    already_ready = self.file_api.openlist_client.openlist_file_exists(
                        self.file_api.openlist_path(path)
                    )
                except Exception as e:
                    logger.error(f"  检查 OpenList 文件是否存在失败，放入待转存: {path} | {e}")
                    already_ready = False

            if already_ready:
                job.file_item = dict(file_item)
                job.file_item["_already_transferred"] = True
                job.already_transferred = True
                job.state = "pending"
                job.note = "已转存，跳过重复转存，按偏好序进入待下载队列"
                logger.info(
                    f"  发现已转存文件，跳过重复转存，按偏好序进入待下载队列: {path}"
                )
            else:
                job.state = "pending"

        mode = (
            "转存+入待下载队列有序（失败跳过；下载可并发）"
            if self.prefer_path_order
            else "转存可插队填容量；下载可并发"
        )
        counts = self.counts()
        logger.info(
            f"▶ 调度器初始化完成（{mode}）: "
            f"pending={counts['pending']} ready(待下载队列)={counts['ready']} "
            f"retry={counts['retry']} transfer_retry={counts['transfer_retry']} "
            f"downloading={counts['downloading']} cleanup={counts['cleanup']} "
            f"done={counts['done']} 跳过={len(skipped)}"
        )
        logger.info("  偏好处理顺序（完整相对路径）:")
        for index, path in enumerate(self.ordered_paths, start=1):
            logger.info(f"    {index}. {path}")
        for item in skipped:
            logger.info(f"    - {item}")

    def counts(self) -> dict[str, int]:
        result = {
            "pending": 0,
            "transfer_retry": 0,
            "transferring": 0,
            "ready": 0,
            "retry": 0,
            "downloading": 0,
            "cleanup": 0,
            "done": 0,
            "failed": 0,
        }
        for job in self.jobs.values():
            if job.state in result:
                result[job.state] += 1
        return result

    def has_work(self) -> bool:
        return any(job.state not in ("done", "failed") for job in self.jobs.values())

    def jobs_in(self, *states: JobState) -> list[Job]:
        return [self.jobs[path] for path in self.ordered_paths if self.jobs[path].state in states]

    def mark_failed(self, job: Job, note: str) -> None:
        if job.path not in self.failed_files:
            self.failed_files.append(job.path)
        if job.already_transferred:
            job.state = "cleanup"
            job.note = note
        else:
            job.state = "failed"
            job.note = note
            job.download_task = None

    def plan_cleanup(self) -> CloudPlan:
        cleanup_jobs = self.jobs_in("cleanup")
        if cleanup_jobs:
            return CloudPlan(kind="cleanup", jobs=cleanup_jobs)
        return CloudPlan(kind="idle")

    def _pack_transfer_jobs(self, pool: list[Job], available_size: int) -> CloudPlan:
        if not pool:
            return CloudPlan(kind="idle")
        items = [job.file_item for job in pool]
        # 有序仅为转存偏好：按路径尽量靠前装入；装不下队头时跳过填后面（避免大文件永久挡住）
        # 进入待下载队列的有序由「按 path 规划的转存批次 + 成功即写 ready」保证；此处不限制下载并发
        if self.prefer_path_order:
            packed_items, deferred = FileApi.take_consecutive_by_capacity(items, available_size)
            if not packed_items:
                # 队头过大：跳过队头本轮，尝试后面能装下的
                packed_items, deferred = FileApi.pack_files_by_capacity(items, available_size)
                if not packed_items:
                    front = pool[0]
                    return CloudPlan(
                        kind="wait_capacity",
                        blocked_path=front.path,
                        blocked_size=FileApi.transfer_cost(front.file_item),
                        available_size=available_size,
                    )
        else:
            packed_items, deferred = FileApi.pack_files_by_capacity(items, available_size)
            if not packed_items:
                front = pool[0]
                return CloudPlan(
                    kind="wait_capacity",
                    blocked_path=front.path,
                    blocked_size=FileApi.transfer_cost(front.file_item),
                    available_size=available_size,
                )

        packed_paths = {self.file_api.file_path(item) for item in packed_items}
        packed_jobs = [job for job in pool if job.path in packed_paths]
        packed_jobs.sort(key=lambda job: full_path_sort_key(job.path))
        for job in packed_jobs:
            job.state = "transferring"
            job.note = "转存中" if not job.already_transferred else "确认已转存"
        return CloudPlan(kind="transfer", jobs=packed_jobs, available_size=available_size)

    def plan_confirm_already_transferred(self) -> CloudPlan:
        """已转存文件不占容量预算，无需等 get_available_space，避免全盘列举阻塞下载开工。"""
        cleanup_jobs = self.jobs_in("cleanup")
        if cleanup_jobs:
            return CloudPlan(kind="cleanup", jobs=cleanup_jobs)
        pool = [job for job in self.jobs_in("pending") if job.already_transferred]
        if not pool:
            return CloudPlan(kind="idle")
        pool.sort(key=lambda job: full_path_sort_key(job.path))
        for job in pool:
            job.state = "transferring"
            job.note = "确认已转存"
        return CloudPlan(kind="transfer", jobs=pool, available_size=0, source="pending")

    def plan_transfer(self, available_size: int | None, *, source: str = "pending") -> CloudPlan:
        """有 cleanup 时不转存。source=pending 优先；transfer_retry 单独再跑，避免失败项挡主队列。"""
        cleanup_jobs = self.jobs_in("cleanup")
        if cleanup_jobs:
            return CloudPlan(kind="cleanup", jobs=cleanup_jobs)
        if available_size is None:
            return CloudPlan(kind="idle")

        state: JobState = "pending" if source == "pending" else "transfer_retry"
        pool = self.jobs_in(state)
        # 真正转存才走容量打包；已转存由 plan_confirm_already_transferred 先行处理
        if source == "pending":
            pool = [job for job in pool if not job.already_transferred]
        plan = self._pack_transfer_jobs(pool, available_size)
        plan.source = source
        return plan

    def apply_cleanup_result(self, deleted_names: list[str], failed_jobs: list[Job]) -> None:
        for path in deleted_names:
            job = self.jobs.get(path)
            if not job:
                continue
            if path in self.failed_files:
                job.state = "failed"
                job.note = job.note or "已清理网盘文件"
            else:
                job.state = "done"
                job.note = "已清理网盘文件"
            job.download_task = None
        for job in failed_jobs:
            stored = self.jobs.get(job.path)
            if stored:
                stored.state = "cleanup"
                stored.note = "清理未确认成功"

    def apply_transfer_success(
        self,
        transferred: list[dict],
        preexisting_paths: set[str],
    ) -> int:
        """将已成功转存的 transferring 任务推进为 ready（不碰其余仍在转存中的任务）。"""
        if not transferred:
            return 0
        transferred_map = {self.file_api.file_path(item): item for item in transferred}
        advanced = 0
        for job in list(self.jobs_in("transferring")):
            item = transferred_map.get(job.path)
            if item is None:
                continue
            job.state = "ready"
            job.already_transferred = True
            job.file_item = item
            job.transfer_fail_count = 0
            job.note = (
                "已转存，已进入待下载队列"
                if job.path in preexisting_paths
                else "转存完成，已进入待下载队列"
            )
            advanced += 1
        return advanced

    def apply_transfer_result(
        self,
        transferred: list[dict],
        failed: list[dict],
        preexisting_paths: set[str],
        max_transfer_failures: int,
        failure_errors: dict[str, str] | None = None,
    ) -> None:
        """收尾：成功→ready；明确失败按规则出局/旁路；其余仍 transferring 的视为未完成旁路。"""
        self.apply_transfer_success(transferred, preexisting_paths)
        failed_paths = {self.file_api.file_path(item) for item in failed}
        failure_errors = failure_errors or {}
        max_transfer_failures = max(max_transfer_failures, 1)

        for job in list(self.jobs_in("transferring")):
            if job.path not in failed_paths:
                job.state = "transfer_retry"
                job.note = "转存未完成，进入转存重试旁路"
                continue

            error = failure_errors.get(job.path, "转存失败")
            kind = classify_error(error)
            job.transfer_fail_count = int(job.transfer_fail_count or 0) + 1
            if kind == "permanent" or job.transfer_fail_count >= max_transfer_failures:
                self.mark_failed(
                    job,
                    note=(
                        f"转存永久失败: {error}"
                        if kind == "permanent"
                        else f"转存失败达上限({job.transfer_fail_count}/{max_transfer_failures}): {error}"
                    ),
                )
            else:
                job.state = "transfer_retry"
                job.note = f"转存临时失败 {job.transfer_fail_count}/{max_transfer_failures}，旁路重试: {error}"

    def plan_main_download_candidates(self) -> list[Job]:
        """
        主下载候选：返回待下载队列(ready)全部任务（按 ordered_paths 序仅便于日志可读）。

        重要：不因已有 downloading 而截断或等待；Worker 应对本列表逐个取任务并提交/恢复/接管，
        允许多个 downloading 并存。strict_download_order 不作用于此列表的「可否并发」。
        retry 走重试旁路，不占本列表。
        """
        return self.jobs_in("ready")

    def plan_retry_download_candidates(self) -> list[Job]:
        return self.jobs_in("retry")

    def plan_download_candidates(self) -> list[Job]:
        return self.plan_main_download_candidates()

    def apply_download_started(self, job: Job, task: dict) -> None:
        job.state = "downloading"
        job.already_transferred = True
        job.download_task = task
        job.gid = str(task.get("gid", "") or "")
        job.submit_fail_count = 0
        action = str(task.get("submit_action") or "").strip()
        if action == "unpause":
            job.note = "已从待下载队列恢复(paused→unpause)"
        elif action == "adopt":
            job.note = "已从待下载队列接管(active/waiting)"
        elif action == "add":
            job.note = "已从待下载队列新建提交(addUri)"
        else:
            job.note = "下载中"

    def apply_submit_failure(self, job: Job, error: str, max_failures: int) -> str:
        """
        下载提交失败：
        - 永久错误 → 立即出局
        - 临时错误 → 进 retry 旁路；超限出局
        返回: retry | removed
        """
        kind = classify_error(error)
        max_failures = max(max_failures, 1)
        if kind == "permanent":
            self.mark_failed(job, note=f"下载提交永久失败（跳过）: {error}")
            return "removed"

        job.submit_fail_count = int(job.submit_fail_count or 0) + 1
        if job.submit_fail_count >= max_failures:
            self.mark_failed(
                job,
                note=f"下载提交临时失败达上限({job.submit_fail_count}/{max_failures}): {error}",
            )
            return "removed"

        job.state = "retry"
        job.note = f"下载提交临时失败，进入重试旁路 {job.submit_fail_count}/{max_failures}: {error}"
        return "retry"

    def reset_download_to_ready(self, paths: list[str], *, note: str) -> None:
        """Aria2 任务被移除后，将 downloading 退回待下载队列(ready)，便于下次再取。"""
        for path in paths:
            job = self.jobs.get(path)
            if not job:
                continue
            if job.state == "downloading":
                job.state = "ready"
                job.gid = ""
                job.download_task = None
                job.note = note

    def apply_download_to_cleanup(self, job: Job, *, failed: bool, note: str) -> None:
        if failed:
            self.mark_failed(job, note=note)
        else:
            job.state = "cleanup"
            job.note = note

    def persist_payload(self, share_id: str) -> dict:
        counts = self.counts()
        files = []
        for path in self.ordered_paths:
            job = self.jobs[path]
            files.append(
                {
                    "path": job.path,
                    "size": job.size,
                    "state": job.state,
                    "already_transferred": job.already_transferred,
                    "gid": job.gid,
                    "submit_fail_count": int(job.submit_fail_count or 0),
                    "transfer_fail_count": int(job.transfer_fail_count or 0),
                    "note": job.note,
                }
            )
        return {
            "share_id": share_id,
            "prefer_path_order": self.prefer_path_order,
            "strict_download_order": self.prefer_path_order,
            "architecture": "scheduler+workers+error-classify",
            "queues": {
                "main_download": [j.path for j in self.plan_main_download_candidates()],
                "retry_download": [j.path for j in self.plan_retry_download_candidates()],
                "pending_transfer": [j.path for j in self.jobs_in("pending")],
                "transfer_retry": [j.path for j in self.jobs_in("transfer_retry")],
            },
            "counts": counts,
            "failed_files": list(self.failed_files),
            "files": files,
        }
