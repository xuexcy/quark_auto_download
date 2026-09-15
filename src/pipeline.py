"""调度器 + Worker 流水线：调度器决策，Worker 执行转存/清理/下载。"""

from __future__ import annotations

import os
import threading
import time

import yaml

from file_api import ACTIVE_ARIA2_STATUSES, FileApi
from scheduler import Job, PipelineScheduler


class DownloadPipeline:
    def __init__(
        self,
        file_api: FileApi,
        share_id: str,
        stoken: str,
        poll_interval: int,
        logger,
        strict_download_order: bool = True,
        state_file: str = "",
        aria2_queue_pause_threshold: int = 700,
        aria2_queue_resume_threshold: int = 500,
    ):
        self.file_api = file_api
        self.share_id = share_id
        self.stoken = stoken
        self.poll_interval = max(poll_interval, 1)
        self.log = logger
        self.state_file = state_file
        self.lock = threading.RLock()
        self.scheduler = PipelineScheduler(file_api, prefer_path_order=strict_download_order)

        pause = max(int(aria2_queue_pause_threshold), 1)
        resume = max(int(aria2_queue_resume_threshold), 0)
        if resume >= pause:
            resume = max(pause - 1, 0)
        self.aria2_queue_pause_threshold = pause
        self.aria2_queue_resume_threshold = resume
        self._aria2_ingest_paused = False
        self._last_aria2_waiting = 0

        self._stop = threading.Event()
        self._worker_errors: dict[str, BaseException] = {}

    def initialize(self, all_files: list[dict]) -> None:
        with self.lock:
            self.scheduler.initialize(all_files, self.log)
        self.log.info(
            f"  Aria2 排队水位: >= {self.aria2_queue_pause_threshold} 暂停转存/提交下载，"
            f"<= {self.aria2_queue_resume_threshold} 恢复（清理不受影响）"
        )
        self._persist_state()

    def _refresh_aria2_backpressure(self) -> bool:
        """
        根据 Aria2 waiting 数量更新滞回暂停状态。
        返回 True 表示应暂停转存与提交下载。
        """
        try:
            waiting = self.file_api.aria2_client.aria2_get_waiting_count()
        except Exception as e:
            self.log.error(f"  查询 Aria2 排队数量失败，本轮暂不暂停: {e}")
            return self._aria2_ingest_paused

        self._last_aria2_waiting = waiting
        was_paused = self._aria2_ingest_paused
        if not was_paused and waiting >= self.aria2_queue_pause_threshold:
            self._aria2_ingest_paused = True
            self.log.warning(
                f"  [Aria2水位] 排队 {waiting} >= {self.aria2_queue_pause_threshold}，"
                f"暂停转存与提交下载（清理继续）"
            )
        elif was_paused and waiting <= self.aria2_queue_resume_threshold:
            self._aria2_ingest_paused = False
            self.log.info(
                f"  [Aria2水位] 排队 {waiting} <= {self.aria2_queue_resume_threshold}，"
                f"恢复转存与提交下载"
            )
        return self._aria2_ingest_paused

    def _persist_state(self) -> None:
        if not self.state_file:
            return
        # 整段落盘加锁，并用唯一临时文件，避免多 Worker 并发写同一 .tmp 导致 replace 失败
        with self.lock:
            payload = self.scheduler.persist_payload(self.share_id)
            payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            payload["aria2_backpressure"] = {
                "paused": self._aria2_ingest_paused,
                "waiting": self._last_aria2_waiting,
                "pause_threshold": self.aria2_queue_pause_threshold,
                "resume_threshold": self.aria2_queue_resume_threshold,
            }
            try:
                directory = os.path.dirname(self.state_file)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                tmp_path = (
                    f"{self.state_file}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
                )
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write("# quark auto download pipeline state (plain YAML)\n")
                    yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
                os.replace(tmp_path, self.state_file)
            except Exception as e:
                self.log.error(f"写入状态文件失败: {self.state_file} | {e}")
                try:
                    if "tmp_path" in locals() and os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass

    def _has_pending_work(self) -> bool:
        with self.lock:
            return self.scheduler.has_work()

    def _log_counts(self, worker: str) -> None:
        with self.lock:
            counts = self.scheduler.counts()
            main_n = len(self.scheduler.plan_main_download_candidates())
            retry_n = len(self.scheduler.plan_retry_download_candidates())
            transfer_retry_n = counts.get("transfer_retry", 0)
        pause_flag = "暂停中" if self._aria2_ingest_paused else "正常"
        self.log.info(
            f"  [{worker}] pending={counts['pending']} transfer_retry={transfer_retry_n} "
            f"ready={counts['ready']} retry={counts['retry']} downloading={counts['downloading']} "
            f"cleanup={counts['cleanup']} done={counts['done']} failed={counts['failed']} "
            f"| 主下载={main_n} 下载重试={retry_n} | Aria2排队={self._last_aria2_waiting}({pause_flag})"
        )

    def run(self) -> list[str]:
        threads = [
            threading.Thread(target=self._cloud_worker_loop, name="quark-cloud", daemon=True),
            threading.Thread(target=self._download_worker_loop, name="quark-download", daemon=True),
            threading.Thread(target=self._retry_worker_loop, name="quark-retry", daemon=True),
        ]
        for thread in threads:
            thread.start()

        try:
            while any(thread.is_alive() for thread in threads):
                if self._worker_errors:
                    self._stop.set()
                elif not self._has_pending_work():
                    time.sleep(2)
                    if not self._has_pending_work():
                        self._stop.set()
                for thread in threads:
                    thread.join(timeout=1)
        except KeyboardInterrupt:
            self.log.warning("收到中断信号，正在停止 Worker...")
            self._stop.set()
            for thread in threads:
                thread.join(timeout=self.poll_interval + 5)
            self._persist_state()
            raise

        self._persist_state()
        if self._worker_errors:
            name, err = next(iter(self._worker_errors.items()))
            raise RuntimeError(f"{name} 异常退出: {err}") from err
        # 全部任务结束后再扫一遍空目录，避免残留多层空文件夹
        self.log.info("▶ 最终清理网盘空文件夹")
        self.file_api.cleanup_empty_dirs()
        with self.lock:
            return list(self.scheduler.failed_files)

    def verify_completed(self, report_path: str) -> dict:
        """全部任务结束后校验 completed 目录，并写入失败报告。"""
        from verify_download import (
            log_verify_report,
            verify_completed_downloads,
            write_verify_report,
        )

        with self.lock:
            report = verify_completed_downloads(
                self.scheduler,
                self.file_api.download_destination_dir,
            )
        write_verify_report(report, report_path)
        log_verify_report(self.log, report, report_path)
        return report

    def _cloud_worker_loop(self) -> None:
        self.log.info("▶ 网盘 Worker 启动（清理优先；转存失败进旁路，不挡后续）")
        try:
            while not self._stop.is_set():
                self._cloud_worker_tick()
                self._log_counts("网盘")
                self._persist_state()
                if self._stop.wait(self.poll_interval):
                    break
        except BaseException as e:
            self._worker_errors["网盘Worker"] = e
            self.log.exception(f"网盘 Worker 异常: {e}")
            self._stop.set()
        finally:
            self._persist_state()
            self.log.info("▶ 网盘 Worker 结束")

    def _cloud_worker_tick(self) -> None:
        # 清理始终执行，不受 Aria2 排队水位影响
        with self.lock:
            plan = self.scheduler.plan_cleanup()
            jobs = list(plan.jobs) if plan.kind == "cleanup" else []
        if plan.kind == "cleanup":
            self._execute_cleanup(jobs)
            return

        if self._refresh_aria2_backpressure():
            return

        available = self.file_api.get_available_space()
        with self.lock:
            plan = self.scheduler.plan_transfer(available, source="pending")
            if plan.kind == "idle":
                plan = self.scheduler.plan_transfer(available, source="transfer_retry")

        if plan.kind == "cleanup":
            self._execute_cleanup(list(plan.jobs))
            return
        if plan.kind == "wait_capacity":
            self.log.info(
                f"  [调度] 容量不足，等待腾出空间: {plan.blocked_path} "
                f"({plan.blocked_size//1024//1024} MB > {plan.available_size//1024//1024} MB)"
            )
            return
        if plan.kind != "transfer":
            return

        self._execute_transfer(plan.jobs, plan.available_size)

    def _execute_cleanup(self, jobs: list[Job]) -> None:
        self.log.info(f"  [调度→清理Worker] 批量清理 {len(jobs)} 个文件")
        tasks = [job.cleanup_payload(self.file_api) for job in jobs]
        deleted_names, failed_tasks = self.file_api.cleanup_files_batch(tasks, "已删除 Quark 转存文件")
        failed_paths = {task["file_name"] for task in failed_tasks}
        with self.lock:
            failed_jobs = [job for job in jobs if job.path in failed_paths]
            self.scheduler.apply_cleanup_result(deleted_names, failed_jobs)
            if failed_jobs:
                self.log.warning("  [调度] 存在清理未确认成功的文件，暂停转存")
        if deleted_names:
            # 只清理刚删文件对应的空父目录，避免每轮全盘扫描
            self.file_api.cleanup_empty_dirs(deleted_names)

    def _execute_transfer(self, jobs: list[Job], available_size: int) -> None:
        preexisting_paths = {job.path for job in jobs if job.already_transferred}
        self.log.info(f"  [调度→转存Worker] 本批 {len(jobs)} 个文件")
        transferred, failed, failure_errors = self.file_api.transfer_file_batch(
            [job.file_item for job in jobs],
            self.share_id,
            self.stoken,
            available_size,
        )
        max_failures = max(self.file_api.download_submit_max_retries, 1)
        with self.lock:
            self.scheduler.apply_transfer_result(
                transferred,
                failed,
                preexisting_paths,
                max_transfer_failures=max_failures,
                failure_errors=failure_errors,
            )

    def _download_worker_loop(self) -> None:
        self.log.info("▶ 下载主 Worker 启动（路径偏好有序；永久错误跳过，临时错误进重试旁路）")
        try:
            while not self._stop.is_set():
                self._download_submit_tick()
                self._download_poll_tick()
                self._log_counts("下载主")
                self._persist_state()
                if self._stop.wait(self.poll_interval):
                    break
        except BaseException as e:
            self._worker_errors["下载Worker"] = e
            self.log.exception(f"下载主 Worker 异常: {e}")
            self._stop.set()
        finally:
            self._persist_state()
            self.log.info("▶ 下载主 Worker 结束")

    def _retry_worker_loop(self) -> None:
        self.log.info("▶ 下载重试 Worker 启动（仅处理临时失败旁路，不堵塞主队列）")
        try:
            while not self._stop.is_set():
                self._retry_submit_tick()
                self._log_counts("下载重试")
                self._persist_state()
                if self._stop.wait(self.poll_interval):
                    break
        except BaseException as e:
            self._worker_errors["重试Worker"] = e
            self.log.exception(f"下载重试 Worker 异常: {e}")
            self._stop.set()
        finally:
            self._persist_state()
            self.log.info("▶ 下载重试 Worker 结束")

    def _try_submit_job(self, job: Job, *, worker_label: str) -> None:
        max_failures = max(self.file_api.download_submit_max_retries, 1)
        with self.lock:
            current = self.scheduler.jobs.get(job.path)
            if not current or current.state not in ("ready", "retry"):
                return
            expected = "ready" if worker_label == "主" else "retry"
            if current.state != expected:
                return
            job = current
            # 分享元数据 size=0 的文件，Aria2 常因 HTTP 416 失败；直接跳过并记入失败原因
            if int(job.size or 0) <= 0:
                self.log.warning(
                    f"  [下载{worker_label}] 跳过 0 字节文件（分享元数据 size=0，"
                    f"Aria2 易返回 416）: {job.path}"
                )
                self.scheduler.apply_download_to_cleanup(
                    job,
                    failed=True,
                    note=(
                        "跳过下载: 分享文件大小为 0 字节；"
                        "空文件直链常导致 Aria2 HTTP 416 (Range Not Satisfiable)"
                    ),
                )
                return

        try:
            task = self.file_api.submit_download(job.file_item)
        except Exception as e:
            with self.lock:
                result = self.scheduler.apply_submit_failure(job, str(e), max_failures)
            if result == "removed":
                self.log.error(
                    f"  [下载{worker_label}] 提交失败已出局（永久或达上限）: {job.path} | {e}"
                )
            else:
                self.log.warning(
                    f"  [下载{worker_label}] 临时失败，进入重试旁路: {job.path} | "
                    f"{job.submit_fail_count}/{max_failures} | {e}"
                )
            return

        with self.lock:
            self.scheduler.apply_download_started(job, task)

    def _download_submit_tick(self) -> None:
        if self._refresh_aria2_backpressure():
            return
        with self.lock:
            candidates = self.scheduler.plan_main_download_candidates()
        for job in candidates:
            if self._stop.is_set():
                break
            if self._aria2_ingest_paused:
                break
            self._try_submit_job(job, worker_label="主")
            # 提交过程中可能积压，每提交若干个可再检查；此处每个任务后轻量使用缓存状态
            if self._last_aria2_waiting + 1 >= self.aria2_queue_pause_threshold:
                # 保守：接近阈值时重新查询
                if self._refresh_aria2_backpressure():
                    break

    def _retry_submit_tick(self) -> None:
        if self._refresh_aria2_backpressure():
            return
        with self.lock:
            candidates = self.scheduler.plan_retry_download_candidates()
        for job in candidates:
            if self._stop.is_set():
                break
            if self._aria2_ingest_paused:
                break
            self._try_submit_job(job, worker_label="重试")
            if self._last_aria2_waiting + 1 >= self.aria2_queue_pause_threshold:
                if self._refresh_aria2_backpressure():
                    break

    def _download_poll_tick(self) -> None:
        with self.lock:
            jobs = self.scheduler.jobs_in("downloading")
        for job in jobs:
            self._poll_one_download(job)

    def _poll_one_download(self, job: Job) -> None:
        task = job.download_task or {}
        gid = job.gid or str(task.get("gid", "")).strip()
        if not gid:
            return
        try:
            status = self.file_api.aria2_client.aria2_get_status(gid)
        except Exception as e:
            self.log.error(f"  查询下载状态失败: {job.path} | {e}")
            return

        state = status.get("status")
        if state == "complete":
            self.log.info(f"  ✓ 下载完成: {job.path}")
            self.file_api.finish_downloaded_file(job.path)
            with self.lock:
                self.scheduler.apply_download_to_cleanup(job, failed=False, note="下载完成，待清理")
        elif state == "error":
            retry_count = int(task.get("retry_count", 0))
            error_message = str(status.get("errorMessage") or "未知错误").strip()
            if retry_count < self.file_api.aria2_max_retries:
                self.log.warning(
                    f"  ✗ 下载失败，准备重试: {job.path} | "
                    f"{error_message} | 重试 {retry_count + 1}/{self.file_api.aria2_max_retries}"
                )
                try:
                    dl_url = self.file_api.openlist_client.openlist_get_download_url(
                        task.get("openlist_path") or self.file_api.openlist_path(job.path)
                    )
                    new_gid = self.file_api.aria2_client.aria2_add_url(dl_url, job.path)
                    task["retry_count"] = retry_count + 1
                    task["gid"] = new_gid
                    task["start_at"] = time.time()
                    task["last_progress_log_at"] = 0.0
                    task["last_error"] = error_message
                    with self.lock:
                        job.download_task = task
                        job.gid = new_gid
                        job.note = f"下载重试中: {error_message}"
                    self.log.info(f"  ↻ 重试下载任务提交成功: {job.path} | gid={new_gid}")
                except Exception as e:
                    self.log.error(f"  重试提交失败: {job.path} | {e}")
                    with self.lock:
                        self.scheduler.apply_download_to_cleanup(
                            job, failed=True, note=f"重试提交失败: {e}；上次Aria2错误: {error_message}"
                        )
            else:
                self.log.error(
                    f"  ✗ 下载出错且已达最大重试次数: {job.path} | "
                    f"{error_message} | 重试 {retry_count}/{self.file_api.aria2_max_retries}"
                )
                with self.lock:
                    self.scheduler.apply_download_to_cleanup(
                        job,
                        failed=True,
                        note=(
                            f"下载失败达最大重试({retry_count}/{self.file_api.aria2_max_retries}): "
                            f"{error_message}"
                        ),
                    )
        elif state == "removed":
            self.log.warning(f"  ✗ 下载任务已被移除，转入待清理: {job.path}")
            with self.lock:
                self.scheduler.apply_download_to_cleanup(job, failed=True, note="Aria2任务被移除")
        elif state in ACTIVE_ARIA2_STATUSES:
            completed = int(status.get("completedLength", 0))
            total = int(status.get("totalLength", 1))
            speed = int(status.get("downloadSpeed", 0))
            pct = completed / total * 100 if total else 0
            now = time.time()
            last_log_at = float(task.get("last_progress_log_at", 0.0))
            if now - last_log_at >= max(self.file_api.download_log_interval, 1):
                self.log.info(f"  ↓ {job.path}  {pct:.1f}%  速度: {speed//1024} KB/s")
                task["last_progress_log_at"] = now
        else:
            self.log.info(f"  ? 下载状态未知: {job.path} | state={state}")
