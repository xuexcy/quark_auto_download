# -*- coding: utf-8 -*-
"""远端 aria2：只提交下载请求 + 查进度，不管理落盘目录。"""

import logging

import pytest

from aria2_api import Aria2Client
from file_api import FileApi
from verify_download import verify_remote_downloads


def make_client(
    remote: bool, calls: list | None = None, download_dir: str = "/dl"
) -> Aria2Client:
    client = Aria2Client(
        host="http://friend:6800/jsonrpc",
        secret="token",
        download_dir=download_dir,
        request_timeout=10,
        download_headers=["Cookie: a=b", "User-Agent: ua"],
        poll_interval=1,
        download_timeout=10,
        logger=logging.getLogger("test"),
        work_dir="j1",
        remote=remote,
    )
    if calls is not None:
        def fake(method, params):
            calls.append((method, params))
            return "gid-1"

        client._aria2_call = fake  # type: ignore[assignment]
    return client


def test_remote_add_url_keeps_relative_path_in_out():
    calls: list = []
    client = make_client(True, calls)
    gid = client.aria2_add_url("https://dl.example/a.mkv", "Show/S01E01.mkv")
    assert gid == "gid-1"
    method, params = calls[0]
    assert method == "aria2.addUri"
    options = params[1]
    # 远端必须带上子目录：只给 basename 时，不同目录下的同名文件会在对方机器互相覆盖
    assert options["out"] == "Show/S01E01.mkv"
    assert "dir" not in options  # 远端：不指定落盘目录
    assert options["header"] == ["Cookie: a=b", "User-Agent: ua"]


def test_local_add_url_still_passes_dir():
    calls: list = []
    client = make_client(False, calls)
    client.aria2_add_url("https://dl.example/a.mkv", "Show/S01E01.mkv")
    options = calls[0][1][1]
    assert options["dir"] == "/dl/j1/Show"
    assert options["out"] == "S01E01.mkv"


def test_no_dir_add_url_omits_dir_and_keeps_subdir():
    """两个目录都没填：不提交 dir（用 aria2 自己的默认目录），但 out 保留子目录防撞名。"""
    calls: list = []
    client = make_client(False, calls, download_dir="")
    assert client.download_dir == ""
    client.aria2_add_url("https://dl.example/a.mkv", "Show/S01E01.mkv")
    options = calls[0][1][1]
    assert "dir" not in options
    assert options["out"] == "Show/S01E01.mkv"


def test_no_dir_never_builds_relative_dir():
    """空根目录不能拼出裸 job_id 的相对路径（那会落到 aria2 的 cwd 里）。"""
    client = make_client(False, download_dir="")
    assert client.download_dir == ""
    client = make_client(False, download_dir="/dl")
    assert client.download_dir == "/dl/j1"


def test_remote_extract_filename_uses_basename():
    client = make_client(True)
    task = {"gid": "g", "files": [{"path": "/home/friend/aria2/dl/S01E01.mkv"}]}
    assert client._extract_task_filename(task) == "S01E01.mkv"


def test_local_extract_filename_requires_own_dir():
    client = make_client(False)
    inside = {"files": [{"path": "/dl/j1/Show/S01E01.mkv"}]}
    outside = {"files": [{"path": "/somewhere/else/S01E01.mkv"}]}
    assert client._extract_task_filename(inside) == "Show/S01E01.mkv"
    assert client._extract_task_filename(outside) == ""


def test_remote_pause_by_paths_matches_basename(monkeypatch):
    client = make_client(True)
    paused: list[str] = []
    monkeypatch.setattr(
        client,
        "aria2_list_tasks",
        lambda **kwargs: [
            {"gid": "g1", "status": "active", "files": [{"path": "/x/y/a.mkv"}]},
            {"gid": "g2", "status": "waiting", "files": [{"path": "/x/y/b.mkv"}]},
        ],
    )
    monkeypatch.setattr(client, "aria2_pause", lambda gid, force=True: paused.append(gid))
    result = client.aria2_pause_by_paths({"Show/a.mkv"})
    assert result["paused_active"] == ["a.mkv"]
    assert paused == ["g1"]


def make_remote_file_api(tmp_path) -> FileApi:
    return FileApi(
        quark_client=object(),
        aria2_client=object(),
        save_to_fid="0",
        aria2_download_dir="",
        download_destination_dir="",
        aria2_max_retries=3,
        download_log_interval=10,
        logger=logging.getLogger("test"),
        work_dir="j1",
        remote_download=True,
    )


def test_remote_file_api_skips_local_work(tmp_path):
    api = make_remote_file_api(tmp_path)
    assert api.finish_downloaded_file("Show/a.mkv") is True
    assert api.move_file_to_destination("Show/a.mkv") is True
    assert api.get_local_downloaded_sizes_from_dirs() == {}
    assert api.local_copy_is_intact("Show/a.mkv", 10) is False
    assert api.cleanup_empty_download_dirs() == []


class _Job:
    def __init__(self, path: str, state: str, note: str = "", size: int = 0):
        self.path = path
        self.state = state
        self.note = note
        self.size = size


class _Scheduler:
    def __init__(self, jobs, failed=()):
        self.jobs = {job.path: job for job in jobs}
        self.ordered_paths = [job.path for job in jobs]
        self.failed_files = list(failed)


def test_verify_remote_reports_by_pipeline_state():
    scheduler = _Scheduler(
        [
            _Job("a.mkv", "cleanup", "下载完成，待清理"),
            _Job("b.mkv", "failed", "下载出错", size=100),
            _Job("c.mkv", "ready"),
        ],
        failed=["b.mkv"],
    )
    report = verify_remote_downloads(scheduler)
    assert report["destination_dir"] == "(远端 aria2)"
    assert report["summary"] == {"total": 3, "ok": 1, "failed": 2}
    failed_paths = {item["path"] for item in report["failures"]}
    assert failed_paths == {"b.mkv", "c.mkv"}
    # 远端报告不引用本机路径
    assert all(item["local_path"] == "(远端 aria2)" for item in report["all_results"])
