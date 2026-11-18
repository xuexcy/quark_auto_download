"""调度器 + Worker 流水线。

有序仅限：列举全部文件 → 有序转存 → 有序进入待下载队列(ready)。
下载主 Worker 从 ready 取任务：paused→unpause / active|waiting→接管 / 无→addUri；
可与已有 downloading 并存，不因「已有下载中」而停止取后续任务。
"""

from __future__ import annotations

import os
import threading
import time

import yaml

from error_classify import classify_error
from file_api import ACTIVE_ARIA2_STATUSES, FileApi
from scheduler import Job, PipelineScheduler


class DownloadPipeline:
    # 连续查询 aria2 状态失败的上限：超过即认为任务已丢失（不能无限等）
    STATUS_FAIL_LIMIT = 5
    # 同一文件自动 unpause 的次数上限，避免与外部暂停互相拉扯
    UNPAUSE_LIMIT = 10
    # 清理云端副本失败后最多等多久：清理态文件会阻塞所有转存（plan_* 看到 cleanup
    # 就只返回清理计划），一直失败就等于整个任务卡死，所以超时后放弃删除、保留云端副本。
    CLEANUP_GIVE_UP_SEC = 600

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
        control_file: str = "",
        job_id: str = "",
        state_persist_interval: float = 5.0,
        selection_file: str = "",
        reload_selection=None,
        override_file: str = "",
    ):
        self.file_api = file_api
        self.share_id = share_id
        self.stoken = stoken
        self.poll_interval = max(poll_interval, 1)
        self.log = logger
        self.state_file = state_file
        self.control_file = control_file
        self.job_id = job_id
        # 运行中追加勾选：前端改写 selection.json 后，由 reload_selection() 重新列举并追加队列
        self.selection_file = str(selection_file or "").strip()
        self.reload_selection = reload_selection
        self._selection_stamp = self._file_stamp(self.selection_file)
        self._selection_failed_stamp = None
        # 逐文件暂停/删除：file_override.yaml 由 Web 进程写，本进程只读（声明式暂停 + 一次性重试 + 持久删除）
        self.override_file = str(override_file or "").strip()
        self._override_stamp = self._file_stamp(self.override_file)
        self._applied_paused: set[str] = set()
        self._applied_deleted: set[str] = set()
        # 正在转存的文件不能立刻删（会把转存结果变成孤儿），转存结束再删
        self._deferred_delete: set[str] = set()
        # 已经就地删过网盘副本的路径，启动补删时跳过（避免对同一 fid 删两次）
        self._cloud_purged: set[str] = set()
        self.lock = threading.RLock()
        # 逐文件控制的读-改-写锁：主循环 / 下载 Worker / 重试 Worker 都会调
        # _apply_file_override，不加锁会让同一次删除发两遍 delete_my_fids，
        # 还会让「取消暂停」与「压暂停」交错。
        self._override_lock = threading.Lock()
        # 清理失败（本地无可信副本 / 删后回查仍可见）的起始时间，按路径记
        self._cleanup_failed_since: dict[str, float] = {}
        self.scheduler = PipelineScheduler(file_api, prefer_path_order=strict_download_order)

        pause = max(int(aria2_queue_pause_threshold), 1)
        resume = max(int(aria2_queue_resume_threshold), 0)
        if resume >= pause:
            resume = max(pause - 1, 0)
        self.aria2_queue_pause_threshold = pause
        self.aria2_queue_resume_threshold = resume
        self._aria2_ingest_paused = False
        self._last_aria2_waiting = 0
        # Aria2 连不上时三个 Worker 每 tick 都会失败：同一错误 60 秒只打一条
        self._aria2_error_log: dict[str, dict] = {}
        # state 文件全量重写很贵：默认最多 5s 一次，收尾时强制写
        self.state_persist_interval = max(float(state_persist_interval or 0), 0.0)
        self._state_persisted_at = 0.0

        # none | soft | hard — 用户暂停
        self._user_pause_mode: str | None = None
        self._stop = threading.Event()
        self._worker_errors: dict[str, BaseException] = {}

    @staticmethod
    def _file_stamp(path: str):
        """文件指纹（mtime_ns + size）；不存在返回 None。用来判断前端是否改了勾选。"""
        if not path:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _maybe_reload_selection(self) -> None:
        """勾选文件被改写（运行中追加勾选）时，重新列举分享并把新文件追加进队列。

        只加没跟踪过的路径（scheduler.append_files 负责判重），已完成/在队列中的不动。
        """
        if not self.reload_selection or not self.selection_file:
            return
        stamp = self._file_stamp(self.selection_file)
        if stamp == self._selection_stamp:
            return
        try:
            files = self.reload_selection()
        except Exception as e:
            # 网络/接口抖动时不推进指纹，下个 tick 继续重试；同一次改动只报一次错
            if stamp != self._selection_failed_stamp:
                self.log.error(f"  [勾选] 重新列举勾选文件失败（稍后重试）: {e}")
                self._selection_failed_stamp = stamp
            return
        self._selection_stamp = stamp
        self._selection_failed_stamp = None
        # 用户已删除的文件不再回到队列（deleted 集合持久化在 file_override.yaml）
        files = [f for f in (files or []) if self.file_api.file_path(f) not in self._applied_deleted]
        with self.lock:
            result = self.scheduler.append_files(list(files))
        added = result.get("added") or []
        skipped = result.get("skipped") or []
        if added:
            self.log.info(f"  [勾选] 检测到勾选更新：新增 {len(added)} 个文件进入队列")
            for path in added:
                self.log.info(f"    + {path}")
        else:
            self.log.info("  [勾选] 检测到勾选更新，但没有新文件（都已在队列或已下载）")
        if skipped:
            self.log.info(f"  [勾选] 跳过 {len(skipped)} 个已在队列中的文件")
        self._persist_state(force=True)

    # ---------- 逐文件暂停 / 开始 / 删除（file_override.yaml，Web 进程写、本进程读） ----------

    def _read_file_override(self) -> tuple[set, set, set] | None:
        """读逐文件控制文件，返回 (paused, deleted, retry)；读失败返回 None 让调用方下个 tick 重试。"""
        if not self.override_file or not os.path.isfile(self.override_file):
            return set(), set(), set()
        try:
            with open(self.override_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            self.log.warning(f"  [文件] 读取逐文件控制失败（稍后重试）: {e}")
            return None
        if not isinstance(data, dict):
            self.log.warning("  [文件] 逐文件控制文件结构异常（稍后重试）")
            return None

        def _paths(key: str) -> set[str]:
            raw = data.get(key) or []
            if not isinstance(raw, list):
                return set()
            return {str(item).strip() for item in raw if str(item).strip()}

        return _paths("paused"), _paths("deleted"), _paths("retry")

    def _apply_file_override(self, *, force: bool = False) -> None:
        """文件指纹变了才解析一次；顺序 = 删除 → 恢复 → 取消暂停 → 强制暂停。"""
        with self._override_lock:
            self._apply_file_override_once(force=force)

    def _apply_file_override_once(self, *, force: bool = False) -> None:
        if not self.override_file:
            return
        stamp = self._file_stamp(self.override_file)
        if not force and stamp == self._override_stamp:
            return
        parsed = self._read_file_override()
        if parsed is None:
            return
        paused, deleted, retry = parsed
        self._override_stamp = stamp

        # 1) 删除先做，避免紧接着又去暂停一个马上要消失的文件。
        # 注意这里是「以文件为准」而不是累积：用户在「文件进度」里删掉一个文件、之后又从目录树
        # 重新勾选同一个文件时，Web 会把这个路径从 deleted 里撤掉（见 add_job_selection），
        # 只有跟着撤销缓存，追加勾选才不会被「已删除」永久挡在门外。
        for path in sorted(deleted - self._applied_deleted):
            self._drop_file(path)
        self._applied_deleted = set(deleted)

        # 2) 一次性「开始」意图（幂等：state 已不是 paused/failed 时无副作用）
        for path in sorted(retry):
            self._resume_file(path)

        # 3) 从暂停集合里被移除的 = 取消暂停
        for path in sorted(self._applied_paused - paused):
            self._resume_file(path)
        self._applied_paused = set(paused)

        # 4) 集合内的一律压回 paused
        for path in sorted(self._applied_paused):
            self._enforce_paused(path)

    def _enforce_paused(self, path: str) -> None:
        """把文件压回 paused；正在下载的连带暂停 aria2（保留已下进度）。"""
        with self.lock:
            job = self.scheduler.jobs.get(path)
            # transferring 不在队列里闲着，此刻打断会把已发出的转存变成孤儿副本；
            # 留到转存结束变成 ready 后，下一次 tick 会把它压成 paused（还没开始下载，符合预期）
            if job is None or job.state in ("paused", "done", "failed", "cleanup", "transferring"):
                return
            gid = str(job.gid or "").strip()
            was_downloading = job.state == "downloading"
        if was_downloading and gid:
            try:
                self.file_api.aria2_client.aria2_pause(gid)
            except Exception as e:
                self.log.warning(f"  [文件] 暂停 Aria2 任务失败 gid={gid}（队列内仍会暂停调度）: {e}")
        with self.lock:
            job = self.scheduler.jobs.get(path)
            if job is None or not self.scheduler.mark_user_paused(job):
                return
            aria2_paused = was_downloading and bool(gid)
        self.log.info(
            f"  [文件] 已暂停（用户）: {path}" + (" | Aria2 已暂停，进度保留" if aria2_paused else "")
        )
        self._persist_state(force=True)

    def _resume_file(self, path: str) -> bool:
        with self.lock:
            job = self.scheduler.jobs.get(path)
            if job is None or not self.scheduler.resume_file(job):
                return False
            state = job.state
        self.log.info(f"  [文件] 已恢复（用户）: {path} → {state}")
        self._persist_state(force=True)
        return True

    def _drop_file(self, path: str) -> bool:
        """按用户要求把文件从任务里删掉：先停 aria2、再删云端副本，最后摘出队列。"""
        with self.lock:
            job = self.scheduler.jobs.get(path)
            if job is None:
                return False
            state = job.state
            if state == "transferring":
                if path not in self._deferred_delete:
                    self._deferred_delete.add(path)
                    self.log.info(f"  [文件] 正在转存，转存结束后再删除: {path}")
                return False
            gid = str(job.gid or "").strip()
            fid = str(job.file_item.get("my_fid") or "").strip()
        # my_fid 由 initialize/转存成功时写入；重启或追加勾选后可能还没解析，按目录缓存再查一次
        if not fid and state != "done":
            try:
                fid = self.scheduler.cloud_fid_for(path)
            except Exception as e:
                self.log.warning(f"  [文件] 查询网盘副本失败: {path} | {e}")
                fid = ""
        if gid:
            try:
                self.file_api.aria2_client.aria2_remove(gid)
            except Exception as e:
                self.log.warning(f"  [文件] 移除 Aria2 任务失败 gid={gid}: {e}")
        if fid and state != "done":
            try:
                self.file_api.quark_client.delete_my_fids([fid])
                self._cloud_purged.add(path)
                self.log.info(f"  [文件] 已删除网盘副本: {path}")
            except Exception as e:
                self.log.error(f"  [文件] 删除网盘副本失败（请手动清理）: {path} | {e}")
        with self.lock:
            dropped = self.scheduler.drop_files([path])
        if dropped:
            self.log.info(f"  [文件] 已按用户操作删除并从队列移除: {path}")
            self._persist_state(force=True)
        return bool(dropped)

    def _flush_deferred_deletes(self) -> None:
        """转存结束（不再是 transferring/cleanup）的延后删除，现在执行。"""
        if not self._deferred_delete:
            return
        for path in list(self._deferred_delete):
            with self.lock:
                job = self.scheduler.jobs.get(path)
                busy = bool(job is not None and job.state in ("transferring", "cleanup"))
            if busy:
                continue
            self._deferred_delete.discard(path)
            self._drop_file(path)

    def _purge_deleted_cloud_copies(self) -> None:
        """启动时清一次「用户已删除」文件的网盘副本。

        运行中删除是就地删的；任务当时没在跑的话，删除只写进 override，
        云端那份还占着配额，所以每次启动顺手补一刀。
        """
        for path in sorted(self._applied_deleted - self._cloud_purged):
            try:
                fid = self.scheduler.cloud_fid_for(path)
            except Exception as e:
                self.log.warning(f"  [文件] 查询网盘副本失败: {path} | {e}")
                continue
            if not fid:
                continue
            try:
                self.file_api.quark_client.delete_my_fids([fid])
                self.log.info(f"  [文件] 已清理已删除文件的网盘副本: {path}")
            except Exception as e:
                self.log.error(f"  [文件] 删除网盘副本失败（请手动清理）: {path} | {e}")

    def _tick_file_controls(self) -> None:
        """每个 tick 应用一次逐文件控制（暂停/开始/删除 ≤1s 生效）。"""
        if not self.override_file:
            return
        self._apply_file_override()
        self._flush_deferred_deletes()
        # 自愈：转存/提交可能把刚暂停的文件抢回 ready，这里再压回去
        for path in list(self._applied_paused):
            self._enforce_paused(path)

    def _read_control_command(self) -> str:
        if not self.control_file or not os.path.isfile(self.control_file):
            return "none"
        try:
            with open(self.control_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                return str(data.get("command") or "none").strip().lower()
        except Exception:
            return "none"
        return "none"

    def _apply_user_control(self) -> None:
        """处理 Web 下发的 soft_pause / hard_pause。"""
        cmd = self._read_control_command()
        if cmd == "soft_pause" and self._user_pause_mode != "soft":
            self._user_pause_mode = "soft"
            self.log.warning("▶ 收到软暂停：停止转存/新提交，清除 Aria2 排队，继续跟踪活跃下载")
            self._purge_aria2_for_job(mode="soft")
        elif cmd == "hard_pause" and self._user_pause_mode != "hard":
            self._user_pause_mode = "hard"
            self.log.warning("▶ 收到硬暂停：暂停 Aria2 活跃+排队（保留进度），停止流水线")
            self._purge_aria2_for_job(mode="hard")
            self._stop.set()

    def _purge_aria2_for_job(self, *, mode: str) -> None:
        with self.lock:
            paths = set(self.scheduler.ordered_paths)
        try:
            if mode == "soft":
                # 软暂停：删除排队；活跃继续下载
                result = self.file_api.aria2_client.aria2_remove_by_paths(
                    paths,
                    remove_active=False,
                    remove_waiting=True,
                )
                removed = list(result.get("removed_waiting") or [])
                if removed:
                    with self.lock:
                        self.scheduler.reset_download_to_ready(
                            removed,
                            note="软暂停：排队任务已删除，待下次重新提交/恢复",
                        )
                    self.log.info(f"  软暂停已删除排队任务 {len(removed)} 个")
            else:
                # 硬暂停：pause 活跃+排队，保留进度供下次 unpause
                result = self.file_api.aria2_client.aria2_pause_by_paths(
                    paths,
                    pause_active=True,
                    pause_waiting=True,
                )
                n_active = len(result.get("paused_active") or [])
                n_waiting = len(result.get("paused_waiting") or [])
                self.log.info(
                    f"  硬暂停已暂停 Aria2 任务 active/paused={n_active} waiting={n_waiting}"
                )
        except Exception as e:
            self.log.error(f"  处理本任务 Aria2 暂停/清理失败: {e}")

    def _user_ingest_blocked(self) -> bool:
        return self._user_pause_mode in ("soft", "hard")

    def _soft_pause_should_exit(self) -> bool:
        if self._user_pause_mode != "soft":
            return False
        with self.lock:
            # 软暂停：活跃下载与清理都结束后退出，保留 pending/ready 供下次开始
            busy = self.scheduler.jobs_in("downloading", "cleanup", "transferring")
            return not busy

    def initialize(self, all_files: list[dict]) -> None:
        with self.lock:
            self.scheduler.initialize(all_files, self.log)
        self.log.info(
            f"  Aria2 排队水位: >= {self.aria2_queue_pause_threshold} 暂停转存/提交下载，"
            f"<= {self.aria2_queue_resume_threshold} 恢复（清理不受影响）"
        )
        self._persist_state()
        # 重启后仍然尊重用户操作：暂停的继续暂停，删除的不再下载
        self._apply_file_override(force=True)
        # 上次没在跑的时候删掉的文件，云端副本这时补清一次（占配额）
        self._purge_deleted_cloud_copies()

    def _log_aria2_problem(self, message: str, *, level: str = "error") -> None:
        """Aria2 不可用时限流日志：首次立即打印，同一条 60 秒内只再打一次并带上抑制次数。"""
        now = time.time()
        entry = self._aria2_error_log.get(message)
        emit = self.log.error if level == "error" else self.log.warning
        if entry is None or now - entry["last"] >= 60:
            suppressed = entry["suppressed"] if entry else 0
            suffix = f"（过去 60 秒同错误 {suppressed} 次，已抑制）" if suppressed else ""
            self._aria2_error_log[message] = {"last": now, "suppressed": 0}
            emit(f"{message}{suffix}")
            return
        entry["suppressed"] += 1

    def _refresh_aria2_backpressure(self) -> bool:
        """
        根据 Aria2 waiting 数量更新滞回暂停状态。
        返回 True 表示应暂停转存与提交下载。
        """
        try:
            waiting = self.file_api.aria2_client.aria2_get_waiting_count()
        except Exception as e:
            self._log_aria2_problem(f"  查询 Aria2 排队数量失败，本轮暂不暂停: {e}")
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

    def _persist_state(self, force: bool = False) -> None:
        if not self.state_file:
            return
        # 三个 Worker 每 tick 都调这里：节流，避免同一份 state 被高频全量重写
        now = time.monotonic()
        if not force and self.state_persist_interval > 0:
            if now - self._state_persisted_at < self.state_persist_interval:
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
                self._state_persisted_at = time.monotonic()
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
            transferring_n = counts.get("transferring", 0)
        pause_flag = "暂停中" if self._aria2_ingest_paused else "正常"
        self.log.info(
            f"  [{worker}] pending={counts['pending']} transferring={transferring_n} "
            f"transfer_retry={transfer_retry_n} "
            f"ready(待下载)={counts['ready']} retry={counts['retry']} downloading={counts['downloading']} "
            f"paused={counts.get('paused', 0)} "
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
                self._apply_user_control()
                self._maybe_reload_selection()
                self._tick_file_controls()
                if self._worker_errors:
                    self._stop.set()
                elif self._soft_pause_should_exit():
                    self.log.info("▶ 软暂停：活跃下载与清理已结束，停止流水线")
                    self._stop.set()
                elif not self._has_pending_work() and self._user_pause_mode is None:
                    time.sleep(2)
                    # 收尾前再看一次勾选：刚好在空档追加的文件不会被漏掉
                    self._maybe_reload_selection()
                    self._tick_file_controls()
                    if not self._has_pending_work():
                        self._stop.set()
                for thread in threads:
                    thread.join(timeout=1)
        except KeyboardInterrupt:
            self.log.warning("收到中断信号，正在停止 Worker...")
            self._stop.set()
            for thread in threads:
                thread.join(timeout=self.poll_interval + 5)
            self._persist_state(force=True)
            raise

        self._persist_state(force=True)
        if self._worker_errors:
            name, err = next(iter(self._worker_errors.items()))
            raise RuntimeError(f"{name} 异常退出: {err}") from err
        # 全部任务结束后再扫一遍空目录（硬/软暂停中途退出时跳过全量清理以免拖慢停止）
        if self._user_pause_mode is None:
            self.log.info("▶ 最终清理网盘空文件夹")
            self.file_api.cleanup_empty_dirs()
            self.log.info("▶ 最终清理下载临时目录中的空文件夹")
            self.file_api.cleanup_empty_download_dirs()
        with self.lock:
            return list(self.scheduler.failed_files)

    def verify_completed(self, report_path: str) -> dict:
        """全部任务结束后校验 completed 目录，并写入失败报告。"""
        from verify_download import (
            log_verify_report,
            verify_completed_downloads,
            verify_remote_downloads,
            write_verify_report,
        )

        with self.lock:
            if not self.file_api.local_verifiable:
                # 文件不在本机（远端 aria2，或两个目录都没填由 aria2 决定落点）：
                # 没有文件可校验，只按流水线状态出报告
                report = verify_remote_downloads(self.scheduler)
            else:
                report = verify_completed_downloads(
                    self.scheduler,
                    self.file_api.local_copy_root,
                    resolver=self.file_api.resolve_local_copy,
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
        self._apply_user_control()
        # 清理始终执行，不受 Aria2 排队水位 / 软暂停影响。
        # 清理后同 tick 继续转存：下载高峰时几乎每轮都有 cleanup，若此处 return
        # 会饿死 pending（再叠加全盘容量扫描 ~2min，扫描结束又被新 cleanup 抢走）。
        with self.lock:
            plan = self.scheduler.plan_cleanup()
            jobs = list(plan.jobs) if plan.kind == "cleanup" else []
        if plan.kind == "cleanup":
            self._execute_cleanup(jobs)
            with self.lock:
                if self.scheduler.jobs_in("cleanup"):
                    return

        if self._user_ingest_blocked():
            return

        if self._refresh_aria2_backpressure():
            return

        # 已转存确认不占容量，先推进 ready，避免被全盘容量扫描阻塞数分钟
        with self.lock:
            confirm_plan = self.scheduler.plan_confirm_already_transferred()
        if confirm_plan.kind == "cleanup":
            self._execute_cleanup(list(confirm_plan.jobs))
            with self.lock:
                if self.scheduler.jobs_in("cleanup"):
                    return
        elif confirm_plan.kind == "transfer":
            self._execute_transfer(confirm_plan.jobs, confirm_plan.available_size)
            return

        available = self.file_api.get_available_space()
        with self.lock:
            plan = self.scheduler.plan_transfer(available, source="pending")
            if plan.kind == "idle":
                plan = self.scheduler.plan_transfer(available, source="transfer_retry")

        # 容量扫描期间下载完成会涌入 cleanup：先清再复用刚算/刚修正的容量，勿丢弃重扫
        if plan.kind == "cleanup":
            self._execute_cleanup(list(plan.jobs))
            with self.lock:
                if self.scheduler.jobs_in("cleanup"):
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
            if self.file_api.available_space_low_confidence:
                # 9GB 兜底值不是真实容量：在这里等下去可能永远等不到，直接暴露给用户
                with self.lock:
                    blocked = self.scheduler.jobs.get(plan.blocked_path) if plan.blocked_path else None
                    if blocked is not None and blocked.state not in ("done", "failed"):
                        self.scheduler.mark_failed(
                            blocked,
                            "可用容量为估算值（配额接口与全盘统计都失败），无法判断是否真的装得下；"
                            "请检查 conf/quark.yaml 的 total_space_gb 与 Quark Cookie 是否有效",
                            keep_cloud=True,
                        )
                        self.log.error(
                            f"  [调度] 可用容量为低置信估算值，已停止等待并标记失败: {blocked.path}"
                        )
            return
        if plan.kind != "transfer":
            return

        self._execute_transfer(plan.jobs, plan.available_size)

    def _execute_cleanup(self, jobs: list[Job]) -> None:
        self.log.info(f"  [调度→清理Worker] 批量清理 {len(jobs)} 个文件")
        tasks = [job.cleanup_payload(self.file_api) for job in jobs]
        size_by_path = {task["file_name"]: int(task.get("size") or 0) for task in tasks}
        deleted_names, failed_tasks = self.file_api.cleanup_files_batch(tasks, "已删除 Quark 转存文件")
        failed_paths = {task["file_name"] for task in failed_tasks}
        with self.lock:
            failed_jobs = [job for job in jobs if job.path in failed_paths]
            self.scheduler.apply_cleanup_result(deleted_names, failed_jobs)
            now = time.time()
            for path in deleted_names:
                self._cleanup_failed_since.pop(path, None)
            if failed_jobs:
                for job in failed_jobs:
                    self._cleanup_failed_since.setdefault(job.path, now)
                self.log.warning("  [调度] 存在清理未确认成功的文件，暂停转存")
            # 清理态会挡住所有转存：迟迟删不掉（本地无可信副本 / 权限 / 删除后回查仍可见）
            # 就认输——标记失败并保留云端副本，让后面的文件继续转存，别整任务卡死。
            for path in [p for p, ts in self._cleanup_failed_since.items()
                         if now - ts > self.CLEANUP_GIVE_UP_SEC]:
                job = self.scheduler.jobs.get(path)
                if job is not None and job.state == "cleanup":
                    self.scheduler.mark_failed(
                        job,
                        "清理云端副本多次失败（本地副本缺失或删除后仍可见），"
                        "已放弃删除并保留云端副本，请手动清理网盘",
                        keep_cloud=True,
                    )
                    self.log.error(f"  [调度] 清理超时，放弃删除并保留云端副本: {path}")
                self._cleanup_failed_since.pop(path, None)
        if deleted_names:
            freed = sum(size_by_path.get(path, 0) for path in deleted_names)
            # 增量回补可用容量；无缓存时 no-op，下次 get_available_space 再全盘算
            self.file_api.adjust_available_space_cache(freed)
            # 只清理刚删文件对应的空父目录，避免每轮全盘扫描
            self.file_api.cleanup_empty_dirs(deleted_names)

    def _execute_transfer(self, jobs: list[Job], available_size: int) -> None:
        preexisting_paths = {job.path for job in jobs if job.already_transferred}
        self.log.info(f"  [调度→转存Worker] 本批 {len(jobs)} 个文件")

        def _on_group_transferred(group_items: list[dict]) -> None:
            # 每组转存成功即有序写入待下载队列(ready)；下载 Worker 可与后续转存并行、可多 downloading
            with self.lock:
                advanced = self.scheduler.apply_transfer_success(group_items, preexisting_paths)
            if advanced:
                self.log.info(
                    f"  [调度] 本组转存已有序进入待下载队列(ready) +{advanced}"
                )
                self._persist_state()

        transferred, failed, failure_errors = self.file_api.transfer_file_batch(
            [job.file_item for job in jobs],
            self.share_id,
            self.stoken,
            available_size,
            on_group_transferred=_on_group_transferred,
        )
        # 真实转存占用容量：增量扣减缓存，避免紧接着又全盘列举数分钟
        transferred_cost = sum(
            FileApi.transfer_cost(item)
            for item in transferred
            if self.file_api.file_path(item) not in preexisting_paths
        )
        if transferred_cost:
            self.file_api.adjust_available_space_cache(-transferred_cost)
        max_failures = max(self.file_api.download_submit_max_retries, 1)
        with self.lock:
            # 收尾：处理失败/未完成；成功项可能已在回调中变 ready（幂等）
            self.scheduler.apply_transfer_result(
                transferred,
                failed,
                preexisting_paths,
                max_transfer_failures=max_failures,
                failure_errors=failure_errors,
            )

    def _download_worker_loop(self) -> None:
        self.log.info(
            "▶ 下载主 Worker 启动（从待下载队列 ready 取任务，可并发 downloading；"
            "永久错误跳过，临时错误进重试旁路）"
        )
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

    def _try_submit_job(
        self,
        job: Job,
        *,
        worker_label: str,
        task_infos: dict[str, dict] | None = None,
    ) -> None:
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
            task = self.file_api.submit_download(job.file_item, task_infos=task_infos)
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
            # 锁外提交期间可能被用户暂停/删除：不能把状态硬拉回 downloading，
            # 否则已摘除的文件会重新出现在队列里，而 aria2 留下没人跟踪的任务。
            current = self.scheduler.jobs.get(job.path)
            if current is not None and current.state in ("ready", "retry"):
                self.scheduler.apply_download_started(current, task)
                return
            # 其余情况：把刚提交/接管的任务收掉（暂停的保留进度），再让控制文件下一轮处理
            self._discard_submitted_task(task, pause=bool(current and current.state == "paused"))

    def _discard_submitted_task(self, task: dict, *, pause: bool = False) -> None:
        gid = str(task.get("gid") or "").strip()
        if not gid:
            return
        try:
            if pause:
                self.file_api.aria2_client.aria2_pause(gid)
            else:
                self.file_api.aria2_client.aria2_remove(gid)
        except Exception as e:
            self.log.warning(f"  [下载] 撤销刚提交的 Aria2 任务失败 gid={gid}: {e}")

    def _aria2_task_snapshot(self) -> dict[str, dict] | None:
        """一次性取 Aria2 现有任务（active/waiting/stopped），避免每个文件 3 次 RPC。"""
        try:
            return self.file_api.aria2_client.aria2_get_existing_task_infos_by_name()
        except Exception as e:
            self._log_aria2_problem(f"  读取 Aria2 任务快照失败，回退为逐文件查询: {e}", level="warning")
            return None

    def _download_submit_tick(self) -> None:
        """从待下载队列(ready)取全部候选并逐个处理；不因已有 downloading 而提前停止。"""
        self._apply_user_control()
        # 先落地逐文件控制：避免把用户刚暂停/刚删除的文件又提交给 aria2
        self._tick_file_controls()
        if self._user_ingest_blocked():
            return
        if self._refresh_aria2_backpressure():
            return
        with self.lock:
            candidates = self.scheduler.plan_main_download_candidates()
        task_infos = self._aria2_task_snapshot()
        for job in candidates:
            if self._stop.is_set():
                break
            if self._aria2_ingest_paused:
                break
            self._try_submit_job(job, worker_label="主", task_infos=task_infos)
            # 提交过程中可能积压，每提交若干个可再检查；此处每个任务后轻量使用缓存状态
            if self._last_aria2_waiting + 1 >= self.aria2_queue_pause_threshold:
                # 保守：接近阈值时重新查询
                if self._refresh_aria2_backpressure():
                    break

    def _retry_submit_tick(self) -> None:
        self._apply_user_control()
        self._tick_file_controls()
        if self._user_ingest_blocked():
            return
        if self._refresh_aria2_backpressure():
            return
        with self.lock:
            candidates = self.scheduler.plan_retry_download_candidates()
        task_infos = self._aria2_task_snapshot()
        for job in candidates:
            if self._stop.is_set():
                break
            if self._aria2_ingest_paused:
                break
            self._try_submit_job(job, worker_label="重试", task_infos=task_infos)
            if self._last_aria2_waiting + 1 >= self.aria2_queue_pause_threshold:
                if self._refresh_aria2_backpressure():
                    break

    def _download_poll_tick(self) -> None:
        # 硬暂停 / 已停止时不要再轮询：否则会把刚被 pause 掉的任务重新 unpause 回来，
        # 变成「页面显示已暂停，aria2 还在继续下载」。
        if self._stop.is_set() or self._user_pause_mode == "hard":
            return
        with self.lock:
            jobs = self.scheduler.jobs_in("downloading")
        for job in jobs:
            if self._stop.is_set():
                break
            self._poll_one_download(job)

    def _download_timeout(self) -> int:
        try:
            return max(int(self.file_api.aria2_client.download_timeout or 0), 0)
        except (TypeError, ValueError):
            return 0

    def _poll_one_download(self, job: Job) -> None:
        # 竞态守卫：本 tick 取到 job 之后可能刚被用户暂停/删除，不能再去 unpause aria2
        if job.state != "downloading":
            return
        task = job.download_task or {}
        gid = job.gid or str(task.get("gid", "")).strip()
        if not gid:
            return
        if self._stop.is_set() or self._user_pause_mode == "hard":
            return
        try:
            status = self.file_api.aria2_client.aria2_get_status(gid)
        except Exception as e:
            fails = int(task.get("status_fail_count", 0) or 0) + 1
            task["status_fail_count"] = fails
            job.download_task = task
            self.log.error(
                f"  查询下载状态失败({fails}/{self.STATUS_FAIL_LIMIT}): {job.path} | {e}"
            )
            if fails >= self.STATUS_FAIL_LIMIT:
                # aria2 重启 / 任务被清空后 gid 会失联：不能无限等，也不能删云端副本
                with self.lock:
                    self.scheduler.apply_download_to_cleanup(
                        job,
                        failed=True,
                        keep_cloud=True,
                        note=(
                            f"Aria2 任务状态连续 {fails} 次查询失败（gid={gid}），"
                            "任务可能已被移除；已保留网盘副本，可再次开始"
                        ),
                    )
            return
        task["status_fail_count"] = 0

        state = status.get("status")

        # 下载超时：只统计「真正在下载(active)」的时间。旧实现从提交时刻算，
        # aria2 队列深、或任务被暂停超过 download_timeout 时，一堆还没开始的文件会被判超时。
        timeout = self._download_timeout()
        if state == "active" and not task.get("active_at"):
            task["active_at"] = time.time()
            job.download_task = task
        started_at = float(task.get("active_at") or 0.0)
        if timeout and started_at and state == "active":
            if time.time() - started_at > timeout:
                self.log.error(f"  ⏰ 下载超时（>{timeout} 秒）: {job.path} | state={state}")
                with self.lock:
                    self.scheduler.apply_download_to_cleanup(
                        job,
                        failed=True,
                        keep_cloud=True,
                        note=(
                            f"下载超时（>{timeout} 秒未完成，state={state}）；"
                            "已保留网盘副本，再次开始可续传"
                        ),
                    )
                return

        if state == "complete":
            if not getattr(self.file_api, "local_verifiable", False):
                # 文件不在本机（远端，或没配置目录由 aria2 决定落点）：本地无法校验落盘；
                # aria2 的 totalLength 是唯一防线。
                # 长度不符说明下的是别的东西（同名撞车 / 截断），绝不能删云端副本。
                try:
                    total_bytes = int(status.get("totalLength") or 0)
                except (TypeError, ValueError):
                    total_bytes = 0
                expected = int(job.size or 0)
                if total_bytes > 0 and expected > 0 and total_bytes != expected:
                    self.log.error(
                        f"  ✗ Aria2 报完成但大小不符（{total_bytes} != {expected}），本机无法核对落盘，"
                        f"已保留网盘副本: {job.path}"
                    )
                    with self.lock:
                        self.scheduler.apply_download_to_cleanup(
                            job,
                            failed=True,
                            keep_cloud=True,
                            note=(
                                f"Aria2 报完成但大小不符（{total_bytes} != {expected} 字节），"
                                "可能撞名或被截断；已保留网盘副本"
                            ),
                        )
                    return
            moved = self.file_api.finish_downloaded_file(job.path)
            if not moved:
                # 关键：没真正落盘就绝不能进 cleanup，否则云端副本会被删掉
                self.log.error(
                    f"  ✗ 下载完成但未能移动到目标目录，已保留网盘副本: {job.path}"
                )
                with self.lock:
                    self.scheduler.apply_download_to_cleanup(
                        job,
                        failed=True,
                        keep_cloud=True,
                        note=(
                            "下载完成但移动到目标目录失败（未删除网盘副本）；"
                            "请检查目标目录磁盘空间/权限后再次开始"
                        ),
                    )
                return
            self.log.info(f"  ✓ 下载完成: {job.path}")
            with self.lock:
                self.scheduler.apply_download_to_cleanup(job, failed=False, note="下载完成，待清理")
        elif state == "paused":
            attempts = int(task.get("unpause_count", 0) or 0) + 1
            if attempts > self.UNPAUSE_LIMIT:
                self.log.error(
                    f"  ✗ 任务反复被暂停，放弃本次下载（已恢复 {attempts - 1} 次）: {job.path}"
                )
                with self.lock:
                    self.scheduler.apply_download_to_cleanup(
                        job,
                        failed=True,
                        keep_cloud=True,
                        note=f"任务反复被暂停（已自动恢复 {attempts - 1} 次），已保留网盘副本",
                    )
                return
            task["unpause_count"] = attempts
            job.download_task = task
            try:
                self.file_api.aria2_client.aria2_unpause(gid)
                self.log.info(
                    f"  ↻ 检测到 Aria2 暂停，已恢复（第 {attempts} 次）: {job.path} | gid={gid}"
                )
            except Exception as e:
                self.log.warning(f"  恢复 Aria2 暂停任务失败: {job.path} | {e}")
        elif state == "error":
            retry_count = int(task.get("retry_count", 0))
            # aria2 的 errorMessage 常常只有 "Download aborted."，必须翻译错误码才看得出
            # 是「对方下载目录不可写」这种改配置才能好的错（详见 aria2_api.describe_task_error）
            error_message = self.file_api.aria2_client.describe_task_error(status).strip()
            if classify_error(error_message) == "permanent":
                # 目录不可写这类错误重试 3 次也是同样的结果，只会拖慢队列
                self.log.error(f"  ✗ 下载失败（不可重试）: {job.path} | {error_message}")
                with self.lock:
                    self.scheduler.apply_download_to_cleanup(
                        job,
                        failed=True,
                        keep_cloud=True,
                        note=f"下载失败（不可重试）: {error_message}",
                    )
                return
            if retry_count < self.file_api.aria2_max_retries:
                self.log.warning(
                    f"  ✗ 下载失败，准备重试: {job.path} | "
                    f"{error_message} | 重试 {retry_count + 1}/{self.file_api.aria2_max_retries}"
                )
                try:
                    dl_url = self.file_api.download_url_for(job.file_item)
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
            self.log.warning(f"  ✗ 下载任务已被移除，已保留网盘副本: {job.path}")
            with self.lock:
                self.scheduler.apply_download_to_cleanup(
                    job,
                    failed=True,
                    keep_cloud=True,
                    note="Aria2 任务被移除（网盘副本保留，再次开始会重新提交）",
                )
        elif state in ACTIVE_ARIA2_STATUSES:
            completed = int(status.get("completedLength", 0))
            total = int(status.get("totalLength", 0) or 0) or int(job.size or 0)
            speed = int(status.get("downloadSpeed", 0))
            pct = completed / total * 100 if total else 0
            now = time.time()
            # 每个 tick 都写进任务快照，前端按 downloaded/total 画单文件进度条
            task["completed"] = completed
            task["total"] = total
            task["speed"] = speed
            task["progress"] = round(pct, 1)
            with self.lock:
                job.download_task = task
                # 沿用 Aria2 报的总大小，避免分享接口给 0 时进度条一直不动
                if int(job.size or 0) <= 0 and total > 0:
                    job.size = total
            last_log_at = float(task.get("last_progress_log_at", 0.0))
            if now - last_log_at >= max(self.file_api.download_log_interval, 1):
                self.log.info(f"  ↓ {job.path}  {pct:.1f}%  速度: {speed//1024} KB/s")
                task["last_progress_log_at"] = now
        else:
            self.log.info(f"  ? 下载状态未知: {job.path} | state={state}")
