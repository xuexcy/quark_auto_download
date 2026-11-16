"""落盘：同名文件绝不被覆盖；占位文件正确回收；本地副本完整性判定。"""

import logging
import os

from file_api import FileApi


def make_file_api(tmp_path) -> FileApi:
    return FileApi(
        quark_client=object(),
        aria2_client=object(),
        save_to_fid="0",
        aria2_download_dir=str(tmp_path / "dl"),
        download_destination_dir=str(tmp_path / "dst"),
        aria2_max_retries=3,
        download_log_interval=10,
        logger=logging.getLogger("test"),
        job_id="j1",
    )


def write(path, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def test_move_into_free_path(tmp_path):
    api = make_file_api(tmp_path)
    write(os.path.join(api.download_root, "movie", "a.mp4"), "new")
    assert api.move_file_to_destination("movie/a.mp4") is True
    dst = tmp_path / "dst" / "movie" / "a.mp4"
    assert dst.read_text(encoding="utf-8") == "new"
    assert not os.path.exists(os.path.join(api.download_root, "movie", "a.mp4"))


def test_move_never_overwrites_existing_file(tmp_path):
    api = make_file_api(tmp_path)
    write(os.path.join(api.download_root, "movie", "a.mp4"), "new")
    dst = write(str(tmp_path / "dst" / "movie" / "a.mp4"), "old")

    assert api.move_file_to_destination("movie/a.mp4") is True

    # 原文件内容不能被改动
    with open(dst, encoding="utf-8") as handle:
        assert handle.read() == "old"
    # 新文件改名落盘，内容保留
    siblings = sorted(os.listdir(os.path.join(tmp_path, "dst", "movie")))
    assert len(siblings) == 2
    renamed = [name for name in siblings if name != "a.mp4"]
    with open(os.path.join(tmp_path, "dst", "movie", renamed[0]), encoding="utf-8") as handle:
        assert handle.read() == "new"


def test_move_missing_source_reports_destination(tmp_path):
    api = make_file_api(tmp_path)
    assert api.move_file_to_destination("movie/missing.mp4") is False
    write(str(tmp_path / "dst" / "movie" / "b.mp4"), "done")
    assert api.move_file_to_destination("movie/b.mp4") is True


def test_move_rejects_parent_traversal(tmp_path):
    api = make_file_api(tmp_path)
    try:
        api.move_file_to_destination("../escape.mp4")
    except ValueError:
        pass
    else:
        raise AssertionError("应当拒绝 .. 相对路径")


def test_local_copy_is_intact(tmp_path):
    api = make_file_api(tmp_path)
    path = write(str(tmp_path / "dst" / "movie" / "c.mp4"), "0123456789")
    assert api.local_copy_is_intact("movie/c.mp4", os.path.getsize(path)) is True
    assert api.local_copy_is_intact("movie/c.mp4", 999) is False
    assert api.local_copy_is_intact("movie/c.mp4", 0) is True
    assert api.local_copy_is_intact("movie/missing.mp4", 10) is False
