"""调度器状态机：本地大小判定、Aria2 complete 未落盘必须保留网盘副本。"""

import logging

from scheduler import PipelineScheduler


class FakeFileApi:
    """只实现 scheduler 用到的接口，避免真实网络/磁盘依赖。"""

    def __init__(self, local_sizes=None, aria2_tasks=None, finish_ok=True):
        self.local_sizes = dict(local_sizes or {})
        self.aria2_tasks = dict(aria2_tasks or {})
        self.finish_ok = finish_ok
        self.finish_calls = []

    # --- 路径 / 排序 ---
    @staticmethod
    def file_sort_key(file_item):
        return file_item["relative_path"]

    @staticmethod
    def file_path(file_item):
        return file_item["relative_path"]

    def refresh_runtime_state(self):
        return {}, dict(self.local_sizes), dict(self.aria2_tasks)

    # --- 落盘 ---
    def finish_downloaded_file(self, relative_path):
        self.finish_calls.append(relative_path)
        return self.finish_ok

    # --- 网盘目录（判"是否已转存"用，测试里视为网盘为空） ---
    @staticmethod
    def relative_dir(relative_path):
        head, _, _ = relative_path.rpartition("/")
        return head

    @staticmethod
    def _parent_fid_for(relative_dir, create=False):
        return "fid-root"

    @staticmethod
    def _entries_by_name(parent_fid):
        return {}


def make_scheduler(file_api) -> PipelineScheduler:
    return PipelineScheduler(file_api)


def file_item(path, size):
    return {"relative_path": path, "file_name": path.rsplit("/", 1)[-1], "size": size}


def test_local_size_matches():
    assert PipelineScheduler._local_size_matches(100, 100) is True
    assert PipelineScheduler._local_size_matches(99, 100) is False
    assert PipelineScheduler._local_size_matches(-1, 100) is False
    assert PipelineScheduler._local_size_matches(None, 100) is False
    assert PipelineScheduler._local_size_matches(0, 100) is False
    assert PipelineScheduler._local_size_matches(10, 0) is True


def test_local_file_with_wrong_size_is_redownloaded():
    api = FakeFileApi(local_sizes={"a.mp4": 5})
    scheduler = make_scheduler(api)
    scheduler.initialize([file_item("a.mp4", 100)], logging.getLogger("test"))
    assert scheduler.jobs["a.mp4"].state == "pending"


def test_local_file_with_matching_size_is_done():
    api = FakeFileApi(local_sizes={"a.mp4": 100})
    scheduler = make_scheduler(api)
    scheduler.initialize([file_item("a.mp4", 100)], logging.getLogger("test"))
    assert scheduler.jobs["a.mp4"].state == "done"


def test_aria2_complete_without_local_file_keeps_cloud_copy():
    api = FakeFileApi(aria2_tasks={"a.mp4": {"gid": "g1", "status": "complete"}}, finish_ok=False)
    scheduler = make_scheduler(api)
    scheduler.initialize([file_item("a.mp4", 100)], logging.getLogger("test"))

    assert api.finish_calls == ["a.mp4"]
    job = scheduler.jobs["a.mp4"]
    assert job.state == "failed"
    assert "a.mp4" in scheduler.failed_files
    assert "未落到目标目录" in job.note


def test_aria2_complete_with_local_file_goes_to_cleanup():
    api = FakeFileApi(aria2_tasks={"a.mp4": {"gid": "g1", "status": "complete"}}, finish_ok=True)
    scheduler = make_scheduler(api)
    scheduler.initialize([file_item("a.mp4", 100)], logging.getLogger("test"))
    job = scheduler.jobs["a.mp4"]
    assert job.state == "cleanup"
    assert job.already_transferred is True
