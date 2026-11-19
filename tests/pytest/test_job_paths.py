"""下载子目录用任务名（不再是内部 job id），收尾时连空的「任务名」目录一起删掉。"""

import logging
import os

from aria2_api import Aria2Client
from file_api import FileApi
from job_paths import work_dir_name


def make_api(root, *, work_dir="脱口秀", destination_dir=""):
    return FileApi(
        quark_client=object(),
        aria2_client=object(),
        save_to_fid="0",
        aria2_download_dir=str(root / "dl"),
        download_destination_dir=destination_dir,
        aria2_max_retries=3,
        download_log_interval=10,
        logger=logging.getLogger("test"),
        work_dir=work_dir,
    )


def test_work_dir_name_keeps_chinese_and_replaces_unsafe_chars():
    # 中文、空格、括号都保留，只把路径分隔符等换成下划线
    assert work_dir_name("脱口秀") == "脱口秀"
    assert work_dir_name("Show/第01期: 上?\u0001") == "Show_第01期_ 上__"
    assert work_dir_name("  a   b  ") == "a b"


def test_work_dir_name_falls_back_when_unusable():
    # 清洗后为空（只剩点/空格）或者就是 . / ..，退回 job id，绝不让下载目录跑出上两级
    assert work_dir_name("", "j1") == "j1"
    assert work_dir_name("   ", "j1") == "j1"
    assert work_dir_name("..", "j1") == "j1"
    assert work_dir_name("./", "j1") == "j1"


def test_work_dir_name_truncates_long_name():
    name = work_dir_name("剧" * 200)
    assert len(name) == 80
    assert name == "剧" * 80


def test_aria2_download_dir_uses_task_name():
    client = Aria2Client(
        host="http://127.0.0.1:6800/jsonrpc",
        secret="",
        download_dir="/dl",
        request_timeout=10,
        download_headers=None,
        poll_interval=1,
        download_timeout=10,
        logger=logging.getLogger("test"),
        work_dir="脱口秀",
    )
    assert client.download_dir == "/dl/脱口秀"


def test_file_api_download_root_uses_task_name(tmp_path):
    api = make_api(tmp_path)
    assert api.download_root == os.path.join(str(tmp_path / "dl"), "脱口秀")


def test_cleanup_removes_empty_task_dir(tmp_path):
    """收尾时把 <临时>/<任务名> 这个空壳也删掉（以前只删它里面的空子目录）。"""
    api = make_api(tmp_path)
    task_dir = tmp_path / "dl" / "脱口秀"
    nested = task_dir / "S01" / "sub"
    nested.mkdir(parents=True)

    deleted = api.cleanup_empty_download_dirs()

    assert not task_dir.exists()
    assert "脱口秀" in deleted  # 任务目录自己也算清理项，便于日志统计


def test_cleanup_keeps_task_dir_when_files_remain(tmp_path):
    api = make_api(tmp_path)
    task_dir = tmp_path / "dl" / "脱口秀"
    (task_dir / "S01").mkdir(parents=True)
    (task_dir / "S01" / "a.mkv").write_bytes(b"x")

    deleted = api.cleanup_empty_download_dirs()

    assert task_dir.is_dir()
    assert deleted == []


def test_cleanup_removes_task_dir_after_files_moved_out(tmp_path):
    """临时目录模式：文件挪到 <临时>/ 根下以后，剩下的 <临时>/<任务名>/ 应该被删掉。"""
    api = make_api(tmp_path)
    task_dir = tmp_path / "dl" / "脱口秀"
    (task_dir / "S01").mkdir(parents=True)
    (tmp_path / "dl" / "S01").mkdir(parents=True)
    (tmp_path / "dl" / "S01" / "a.mkv").write_bytes(b"x")  # 已经挪到根下

    api.cleanup_empty_download_dirs()

    assert not task_dir.exists()
    assert (tmp_path / "dl" / "S01" / "a.mkv").is_file()
