"""DownloadPipeline 读取 file_override.yaml：暂停 / 恢复 / 删除（不跑线程、不联网）。"""

import logging

import pytest
import yaml

from pipeline import DownloadPipeline


class FakeAria2:
    def __init__(self):
        self.paused = []
        self.removed = []

    def aria2_pause(self, gid, force=True):
        self.paused.append(gid)
        return True

    def aria2_remove(self, gid, force=True):
        self.removed.append(gid)
        return True


class FakeQuark:
    def __init__(self):
        self.deleted = []

    def delete_my_fids(self, fids):
        self.deleted.append(list(fids))
        return len(list(fids))


class FakeFileApi:
    """只实现 initialize / 逐文件控制用到的接口。"""

    def __init__(self):
        self.aria2_client = FakeAria2()
        self.quark_client = FakeQuark()

    @staticmethod
    def file_sort_key(item):
        return item["relative_path"]

    @staticmethod
    def file_path(item):
        return item["relative_path"]

    def refresh_runtime_state(self):
        return {}, {}, {}

    def finish_downloaded_file(self, path):
        return True

    @staticmethod
    def relative_dir(path):
        head, _, _ = path.rpartition("/")
        return head

    @staticmethod
    def _parent_fid_for(relative_dir, create=False):
        return "fid-root"

    @staticmethod
    def _entries_by_name(parent_fid):
        return {}


def file_item(path, size=100, my_fid=""):
    """my_fid = 转存后「自己网盘里那份」的 fid（删除云端副本用的就是它）。"""
    item = {"relative_path": path, "file_name": path.rsplit("/", 1)[-1], "size": size}
    if my_fid:
        item["my_fid"] = my_fid
    return item


@pytest.fixture()
def build(tmp_path):
    def _build(override=None):
        override_path = tmp_path / "file_override.yaml"
        if override is not None:
            override_path.write_text(yaml.safe_dump(override, allow_unicode=True), encoding="utf-8")
        control_path = tmp_path / "control.yaml"
        control_path.write_text(yaml.safe_dump({"command": "none"}), encoding="utf-8")
        return DownloadPipeline(
            FakeFileApi(),
            "sid",
            "stoken",
            1,
            logging.getLogger("test-pipe"),
            state_file=str(tmp_path / "pipeline_state.yaml"),
            control_file=str(control_path),
            job_id="j1",
            override_file=str(override_path),
        ), override_path

    return _build


def test_paused_declared_before_start_stays_paused(build):
    pipe, _ = build({"paused": ["a.mp4"], "deleted": [], "retry": []})
    pipe.initialize([file_item("a.mp4")])

    assert pipe.scheduler.jobs["a.mp4"].state == "paused"
    assert pipe.scheduler.jobs["a.mp4"].note == "已暂停（用户）"
    assert pipe._applied_paused == {"a.mp4"}
    # 状态落盘时也要进 paused 队列
    payload = pipe.scheduler.persist_payload("sid")
    assert payload["queues"]["paused"] == ["a.mp4"]
    assert payload["files"][0]["state"] == "paused"


def test_retry_entry_resumes_paused_file(build):
    pipe, override_path = build({"paused": ["a.mp4"], "deleted": [], "retry": []})
    pipe.initialize([file_item("a.mp4")])
    assert pipe.scheduler.jobs["a.mp4"].state == "paused"

    override_path.write_text(
        yaml.safe_dump({"paused": [], "deleted": [], "retry": ["a.mp4"]}), encoding="utf-8"
    )
    pipe._tick_file_controls()

    job = pipe.scheduler.jobs["a.mp4"]
    assert job.state == "pending"
    assert job.note == "已恢复（用户）"
    assert pipe._applied_paused == set()


def test_unpause_in_override_resumes_file(build):
    pipe, override_path = build({"paused": ["a.mp4"], "deleted": [], "retry": []})
    pipe.initialize([file_item("a.mp4")])

    override_path.write_text(
        yaml.safe_dump({"paused": [], "deleted": [], "retry": []}), encoding="utf-8"
    )
    pipe._tick_file_controls()
    assert pipe.scheduler.jobs["a.mp4"].state == "pending"


def test_delete_ready_file_removes_from_queue_and_cloud(build):
    pipe, override_path = build()
    pipe.initialize([file_item("a/b.mp4", 100, "mine1")])
    assert "a/b.mp4" in pipe.scheduler.jobs

    override_path.write_text(
        yaml.safe_dump({"paused": [], "deleted": ["a/b.mp4"], "retry": []}), encoding="utf-8"
    )
    pipe._tick_file_controls()

    assert "a/b.mp4" not in pipe.scheduler.jobs
    assert pipe.file_api.quark_client.deleted == [["mine1"]]
    assert pipe._applied_deleted == {"a/b.mp4"}
    # 已删除的文件不会被 reload_selection 重新加回队列
    assert pipe.scheduler.ordered_paths == []


def test_delete_while_transferring_is_deferred(build):
    pipe, override_path = build()
    pipe.initialize([file_item("a/b.mp4", 100, "mine1")])
    job = pipe.scheduler.jobs["a/b.mp4"]
    job.state = "transferring"

    override_path.write_text(
        yaml.safe_dump({"paused": [], "deleted": ["a/b.mp4"], "retry": []}), encoding="utf-8"
    )
    pipe._tick_file_controls()

    # 转存中不能删：否则已发出的转存会变成孤儿云端副本
    assert "a/b.mp4" in pipe.scheduler.jobs
    assert pipe._deferred_delete == {"a/b.mp4"}
    assert pipe.file_api.quark_client.deleted == []

    job.state = "ready"
    pipe._flush_deferred_deletes()
    assert "a/b.mp4" not in pipe.scheduler.jobs
    assert pipe.file_api.quark_client.deleted == [["mine1"]]


def test_pause_enforced_again_after_state_was_reset(build):
    """转存把 paused 抢回 ready 之后，下一次 tick 必须再压回 paused。"""
    pipe, _ = build({"paused": ["a.mp4"], "deleted": [], "retry": []})
    pipe.initialize([file_item("a.mp4")])

    pipe.scheduler.jobs["a.mp4"].state = "ready"  # 模拟其它分支改动状态
    pipe._tick_file_controls()
    assert pipe.scheduler.jobs["a.mp4"].state == "paused"


def test_deleted_file_cloud_copy_purged_on_start(tmp_path):
    """任务没在跑时删的文件：启动时补删云端副本，别白占配额。"""

    class CloudFileApi(FakeFileApi):
        @staticmethod
        def _entries_by_name(parent_fid):
            return {"b.mp4": {"fid": "mine1"}}

    override_path = tmp_path / "file_override.yaml"
    override_path.write_text(
        yaml.safe_dump({"paused": [], "deleted": ["a/b.mp4"], "retry": []}), encoding="utf-8"
    )
    pipe = DownloadPipeline(
        CloudFileApi(),
        "sid",
        "stoken",
        1,
        logging.getLogger("test-pipe"),
        state_file=str(tmp_path / "pipeline_state.yaml"),
        override_file=str(override_path),
    )
    pipe.initialize([file_item("a/b.mp4", 100, "mine1")])

    assert pipe.scheduler.jobs == {}
    assert pipe.file_api.quark_client.deleted == [["mine1"]]


def test_broken_override_file_does_not_break_tick(build):
    pipe, override_path = build()
    pipe.initialize([file_item("a.mp4")])
    override_path.write_text("paused: [\n  - oops\n", encoding="utf-8")

    pipe._tick_file_controls()  # 坏 YAML 只记 warning，不能抛
    assert pipe.scheduler.jobs["a.mp4"].state == "pending"
