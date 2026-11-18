"""运行中追加勾选：调度器追加队列、勾选文件原子落盘、流水线侦测改动后重新列举。

对应需求（2026-11-16）：任务正在下载时，在「分享目录」里继续勾选文件并点「下载所选」，
新文件要排队，当前批次下完接着下。
"""

import contextlib
import json
import logging

import jobs_manager
import yaml
from pipeline import DownloadPipeline
from scheduler import PipelineScheduler


class FakeFileApi:
    """只实现调度器/流水线用到的接口，避免真实网络与磁盘依赖。"""

    def __init__(self, local_sizes=None, cloud_entries=None):
        self.local_sizes = dict(local_sizes or {})
        self.cloud_entries = dict(cloud_entries or {})

    @staticmethod
    def file_sort_key(file_item):
        return file_item["relative_path"]

    @staticmethod
    def file_path(file_item):
        return file_item["relative_path"]

    @staticmethod
    def relative_dir(relative_path):
        head, _, _ = relative_path.rpartition("/")
        return head

    def refresh_runtime_state(self):
        return {}, dict(self.local_sizes), {}

    def get_local_downloaded_sizes_from_dirs(self):
        return dict(self.local_sizes)

    @staticmethod
    def _parent_fid_for(relative_dir, create=False):
        return "fid-dir"

    def _entries_by_name(self, parent_fid):
        return {name: {"fid": fid} for name, fid in self.cloud_entries.items()}


def file_item(path, size):
    return {"relative_path": path, "file_name": path.rsplit("/", 1)[-1], "size": size}


def make_scheduler(api):
    return PipelineScheduler(api)


def test_append_files_only_adds_unknown_paths():
    api = FakeFileApi()
    scheduler = make_scheduler(api)
    scheduler.initialize([file_item("a.mp4", 10)], logging.getLogger("test"))
    scheduler.jobs["a.mp4"].state = "done"

    result = scheduler.append_files([file_item("a.mp4", 10), file_item("b.mp4", 20)])

    assert result["added"] == ["b.mp4"]
    assert result["skipped"] == ["a.mp4"]
    # 已完成的不许被追加覆盖回 pending
    assert scheduler.jobs["a.mp4"].state == "done"
    assert scheduler.jobs["b.mp4"].state == "pending"
    assert scheduler.ordered_paths == ["a.mp4", "b.mp4"]


def test_append_files_skips_file_already_on_disk():
    api = FakeFileApi(local_sizes={"b.mp4": 20})
    scheduler = make_scheduler(api)
    scheduler.initialize([], logging.getLogger("test"))

    scheduler.append_files([file_item("b.mp4", 20)])

    assert scheduler.jobs["b.mp4"].state == "done"


def test_append_files_reuses_cloud_copy():
    api = FakeFileApi(cloud_entries={"b.mp4": "fid-b"})
    scheduler = make_scheduler(api)
    scheduler.initialize([], logging.getLogger("test"))

    scheduler.append_files([file_item("b.mp4", 20)])

    job = scheduler.jobs["b.mp4"]
    assert job.state == "pending"
    assert job.already_transferred is True
    assert job.file_item["my_fid"] == "fid-b"


def test_pipeline_reloads_selection_when_file_changes(tmp_path):
    api = FakeFileApi()
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"file_fids": ["f1"]}), encoding="utf-8")
    state = tmp_path / "state.yaml"
    calls = []

    def reload_selection():
        calls.append(1)
        return [file_item("b.mp4", 20)]

    pipeline = DownloadPipeline(
        file_api=api,
        share_id="sid",
        stoken="st",
        poll_interval=1,
        logger=logging.getLogger("test"),
        state_file=str(state),
        selection_file=str(selection),
        reload_selection=reload_selection,
    )
    pipeline.initialize([file_item("a.mp4", 10)])

    # 文件没变：不重新列举（每个 tick 都调，不能打接口）
    pipeline._maybe_reload_selection()
    assert calls == []

    selection.write_text(json.dumps({"file_fids": ["f1", "f2"]}), encoding="utf-8")
    pipeline._maybe_reload_selection()

    assert calls == [1]
    assert set(pipeline.scheduler.jobs) == {"a.mp4", "b.mp4"}
    persisted = yaml.safe_load(state.read_text(encoding="utf-8"))
    assert [item["path"] for item in persisted["files"]] == ["a.mp4", "b.mp4"]


def test_pipeline_reload_failure_keeps_queue_and_retries(tmp_path):
    api = FakeFileApi()
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"file_fids": ["f1"]}), encoding="utf-8")
    attempts = []

    def reload_selection():
        attempts.append(1)
        raise RuntimeError("接口抖动")

    pipeline = DownloadPipeline(
        file_api=api,
        share_id="sid",
        stoken="st",
        poll_interval=1,
        logger=logging.getLogger("test"),
        selection_file=str(selection),
        reload_selection=reload_selection,
    )
    pipeline.initialize([file_item("a.mp4", 10)])

    selection.write_text(json.dumps({"file_fids": ["f1", "f2"]}), encoding="utf-8")
    # 失败不能抛到 run()，也不能把队列搞坏
    pipeline._maybe_reload_selection()
    pipeline._maybe_reload_selection()

    assert attempts == [1, 1]  # 没推进指纹 → 下个 tick 继续重试
    assert set(pipeline.scheduler.jobs) == {"a.mp4"}


def _patch_jobs_world(monkeypatch, tmp_path, jobs):
    monkeypatch.setattr(jobs_manager, "load_jobs_index", lambda: [dict(j) for j in jobs])
    monkeypatch.setattr(
        jobs_manager,
        "save_jobs_index",
        lambda items: jobs.__setitem__(slice(None), [dict(i) for i in items]),
    )
    monkeypatch.setattr(jobs_manager, "_index_lock", lambda: contextlib.nullcontext())
    monkeypatch.setattr(jobs_manager, "job_dir", lambda job_id: str(tmp_path / job_id))
    monkeypatch.setattr(
        jobs_manager,
        "job_selection_file",
        lambda job_id: str(tmp_path / job_id / "selection.json"),
    )
    monkeypatch.setattr(jobs_manager, "share_usage", lambda job_id="": {})
    monkeypatch.setattr(jobs_manager, "upsert_job", lambda job: jobs.__setitem__(0, dict(job)) or job)


def test_add_job_selection_writes_selection_file(tmp_path, monkeypatch):
    jobs = [{"id": "j1", "name": "任务一", "share_url": "https://pan.quark.cn/s/abc"}]
    _patch_jobs_world(monkeypatch, tmp_path, jobs)
    monkeypatch.setattr(jobs_manager, "get_job", lambda job_id: jobs[0] if jobs[0]["id"] == job_id else None)

    jobs_manager.add_job_selection("j1", {"file_fids": ["f1"]})
    jobs_manager.add_job_selection("j1", {"file_fids": ["f2"], "dir_fids": ["d1"]})

    path = tmp_path / "j1" / "selection.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"file_fids": ["f1", "f2"], "dir_fids": ["d1"]}


def test_share_usage_skips_files_deleted_in_progress(tmp_path, monkeypatch):
    """删掉的文件即使还在 pipeline_state 里，也不能再算「已在任务中」（否则重新勾选被挡）。"""
    jobs = [{"id": "j1", "name": "任务一", "share_url": "https://pan.quark.cn/s/abc",
             "selection": {"file_fids": ["f2"]}}]
    # 注意：不能用 _patch_jobs_world，它会把 share_usage 本身替换成空实现。
    monkeypatch.setattr(jobs_manager, "load_jobs_index", lambda: jobs)
    monkeypatch.setattr(jobs_manager, "job_state_file", lambda job_id: str(tmp_path / "state.yaml"))
    monkeypatch.setattr(jobs_manager, "read_file_override", lambda job_id: {"deleted": {"a.mp4"}})
    (tmp_path / "state.yaml").write_text(
        "files:\n"
        "- {path: a.mp4, fid: f1, state: downloading}\n"
        "- {path: b.mp4, fid: f2, state: done}\n",
        encoding="utf-8",
    )

    usage = jobs_manager.share_usage("j1")
    assert "f1" not in usage            # 已删除
    assert usage["f2"]["state"] == "done"
