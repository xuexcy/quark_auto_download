"""勾选范围过滤：文件 fid / 目录 fid 只收用户勾选的内容；空选择保持全量。"""

import logging

import pytest

from jobs_manager import normalize_selection
from quark_api import QuarkClient


def make_client() -> QuarkClient:
    return QuarkClient(
        cookie="k=v",
        request_timeout=5,
        save_to_dir="",
        share_url="https://pan.quark.cn/s/abc",
        share_pwd="",
        logger=logging.getLogger("test"),
    )


TREE = {
    "rootfid": [
        {"fid": "dira", "file": False, "dir": True, "file_name": "dirA", "size": 0},
        {"fid": "fb", "file": True, "dir": False, "file_name": "b.mp4", "size": 300},
    ],
    "dira": [
        {"fid": "fa1", "file": True, "dir": False, "file_name": "a1.mp4", "size": 100},
        {"fid": "fa2", "file": True, "dir": False, "file_name": "a2.mp4", "size": 200},
        {"fid": "dirsub", "file": False, "dir": True, "file_name": "sub", "size": 0},
    ],
    "dirsub": [
        {"fid": "fs1", "file": True, "dir": False, "file_name": "s1.mp4", "size": 400},
    ],
}


def install_fake(client: QuarkClient) -> list[str]:
    calls: list[str] = []

    def fake_get(url, params=None):
        pdir_fid = (params or {}).get("pdir_fid", "")
        calls.append(pdir_fid)
        return {
            "code": 0,
            "status": 200,
            "data": {"list": [dict(item) for item in TREE.get(pdir_fid, [])]},
        }

    client.quark_get = fake_get  # type: ignore[method-assign]
    return calls


def paths(files) -> list[str]:
    return sorted(item["relative_path"] for item in files)


def test_no_selection_downloads_everything():
    client = make_client()
    calls = install_fake(client)
    files = client.list_share_files("abc", "st", "rootfid")
    assert paths(files) == ["b.mp4", "dirA/a1.mp4", "dirA/a2.mp4", "dirA/sub/s1.mp4"]
    assert len(calls) == 3


def test_selected_files_only():
    client = make_client()
    install_fake(client)
    files = client.list_share_files("abc", "st", "rootfid", selected_file_fids={"fa2"})
    assert paths(files) == ["dirA/a2.mp4"]


def test_selected_dir_includes_whole_subtree():
    client = make_client()
    install_fake(client)
    files = client.list_share_files("abc", "st", "rootfid", selected_dir_fids={"dira"})
    assert paths(files) == ["dirA/a1.mp4", "dirA/a2.mp4", "dirA/sub/s1.mp4"]


def test_deep_selected_dir_reachable_without_ticking_parents():
    """只勾深层子目录（父目录未勾）时，仍必须能列出该子目录里的文件。"""
    client = make_client()
    calls = install_fake(client)
    files = client.list_share_files("abc", "st", "rootfid", selected_dir_fids={"dirsub"})
    assert paths(files) == ["dirA/sub/s1.mp4"]
    # 需要沿路径递归到 dirA 才能发现 dirsub；未勾选目录里的文件仍被过滤掉
    assert calls == ["rootfid", "dira", "dirsub"]


def test_list_share_dir_entries_normalizes_page():
    client = make_client()
    install_fake(client)
    entries = client.list_share_dir_entries("abc", "st", "dira")
    assert [entry["name"] for entry in entries] == ["sub", "a1.mp4", "a2.mp4"]
    assert [entry["is_dir"] for entry in entries] == [True, False, False]
    assert entries[0]["size"] == 0
    assert entries[1]["size"] == 100


@pytest.mark.parametrize(
    "selection, expected",
    [
        (None, {}),
        ({}, {}),
        ({"file_fids": [], "dir_fids": []}, {}),
        ({"file_fids": [" a ", "", "a"], "dir_fids": []}, {"file_fids": ["a"]}),
        ({"file_fids": [], "dir_fids": ["d1", "d1"]}, {"dir_fids": ["d1"]}),
        ({"file_fids": ["f1"], "dir_fids": ["d1"]}, {"file_fids": ["f1"], "dir_fids": ["d1"]}),
    ],
)
def test_normalize_selection(selection, expected):
    assert normalize_selection(selection) == expected
