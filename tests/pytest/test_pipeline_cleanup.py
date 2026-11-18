# -*- coding: utf-8 -*-
"""清理一直失败时必须认输：否则 cleanup 闸门会把整个任务卡死。"""

import logging
import time

from pipeline import DownloadPipeline
from scheduler import Job


class FakeFileApi:
    def __init__(self, fail=True):
        self.fail = fail
        self.cleaned = []
        self.freed = 0

    @staticmethod
    def file_sort_key(item):
        return item["relative_path"]

    @staticmethod
    def file_path(item):
        return item["relative_path"]

    def cleanup_files_batch(self, tasks, log_label):
        if self.fail:
            return [], [dict(task) for task in tasks]
        self.cleaned.extend(task["file_name"] for task in tasks)
        return [task["file_name"] for task in tasks], []

    def adjust_available_space_cache(self, delta):
        self.freed += int(delta)

    def cleanup_empty_dirs(self, deleted_names):
        return None


def build(tmp_path, fail=True):
    api = FakeFileApi(fail=fail)
    pipe = DownloadPipeline(
        file_api=api,
        share_id="s1",
        stoken="t1",
        poll_interval=60,
        logger=logging.getLogger("test"),
        state_file=str(tmp_path / "state.yaml"),
    )
    return pipe, api


def add_cleanup_job(pipe, path="Show/a.mkv", size=100):
    item = {"relative_path": path, "file_name": path.rsplit("/", 1)[-1], "size": size, "my_fid": "fid-1"}
    job = Job(path=path, size=size, file_item=item, state="cleanup", already_transferred=True)
    pipe.scheduler.jobs[path] = job
    pipe.scheduler.ordered_paths.append(path)
    return job


def test_cleanup_failure_gives_up_and_unblocks(tmp_path):
    pipe, _ = build(tmp_path)
    job = add_cleanup_job(pipe)
    pipe._execute_cleanup([job])
    assert job.state == "cleanup"  # 第一次失败：先记时间，继续等（可能是接口抖动）
    assert job.path in pipe._cleanup_failed_since

    # 超过认输窗口仍失败 → 标失败并保留云端副本，cleanup 闸门不再挡住后续转存
    pipe._cleanup_failed_since[job.path] = time.time() - pipe.CLEANUP_GIVE_UP_SEC - 1
    pipe._execute_cleanup([job])
    assert job.state == "failed"
    assert "已放弃删除并保留云端副本" in job.note
    assert job.path not in pipe._cleanup_failed_since
    assert not pipe.scheduler.jobs_in("cleanup")


def test_cleanup_success_clears_state(tmp_path):
    pipe, api = build(tmp_path, fail=False)
    job = add_cleanup_job(pipe)
    pipe._cleanup_failed_since[job.path] = time.time() - 10
    pipe._execute_cleanup([job])
    assert job.state == "done"
    assert job.path not in pipe._cleanup_failed_since
    assert api.freed == 100
