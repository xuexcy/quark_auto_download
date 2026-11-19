"""两个目录都不填：提交任务不带 dir，本机不校验、不移动、不扫盘。"""

import logging
import os

from file_api import FileApi


def make_api(tmp_path, *, download_dir="", destination_dir="", remote=False) -> FileApi:
    return FileApi(
        quark_client=object(),
        aria2_client=object(),
        save_to_fid="0",
        aria2_download_dir=download_dir,
        download_destination_dir=destination_dir,
        aria2_max_retries=3,
        download_log_interval=10,
        logger=logging.getLogger("test"),
        work_dir="j1",
        remote_download=remote,
    )


class FakeQuark:
    def __init__(self):
        self.deleted: list[list[str]] = []

    def delete_my_fids(self, fids):
        self.deleted.append(list(fids))


def test_no_dir_disables_every_local_check(tmp_path):
    api = make_api(tmp_path)
    assert api.local_verifiable is False
    assert api.download_root == ""
    assert api.get_local_downloaded_sizes_from_dirs() == {}
    assert api.resolve_local_copy("movie/a.mkv") == ""
    assert api.local_copy_is_intact("movie/a.mkv", 10) is False
    # 不知道落点，就没有可移动 / 可清理的东西
    assert api.move_file_to_destination("movie/a.mkv") is True
    assert api.cleanup_empty_download_dirs() == []


def test_finish_downloaded_file_completes_without_dir(tmp_path):
    api = make_api(tmp_path)
    # Aria2 报 complete 就算完成：本机既不能移动也不能核对
    assert api.finish_downloaded_file("movie/a.mkv") is True


def test_cleanup_does_not_require_local_copy_without_dir(tmp_path):
    """本机看不到文件时不能因为「本地副本缺失」就永远不释放网盘空间。"""
    api = make_api(tmp_path)
    api.quark_client = FakeQuark()
    api._parent_fid_for = lambda relative_dir: "pfid"  # type: ignore[assignment]
    listing = [{"a.mkv": {"fid": "f1"}}, {}]  # 第一次列目录 → 删除后回查已空

    def fake_entries(parent_fid, strict=False):
        return listing.pop(0) if listing else {}

    api._entries_by_name = fake_entries  # type: ignore[assignment]
    deleted, failed = api.cleanup_files_batch([{"file_name": "movie/a.mkv", "size": 10}])
    assert deleted == ["movie/a.mkv"]
    assert failed == []
    assert api.quark_client.deleted == [["f1"]]


def test_temp_only_mode_moves_file_up_to_temp_root(tmp_path):
    """只填临时目录：下到 <临时>/<job_id>/，下完挪到 <临时>/ 根下。"""
    root = tmp_path / "dl"
    api = make_api(tmp_path, download_dir=str(root), destination_dir=str(root))
    src = root / "j1" / "movie" / "a.mkv"
    os.makedirs(src.parent, exist_ok=True)
    src.write_text("data", encoding="utf-8")

    assert api.finish_downloaded_file("movie/a.mkv") is True

    assert (root / "movie" / "a.mkv").read_text(encoding="utf-8") == "data"
    assert not src.exists()
