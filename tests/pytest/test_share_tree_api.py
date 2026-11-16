"""分享目录树打标 + 追加勾选接口（不访问外网）。"""

import pytest
from fastapi.testclient import TestClient

import jobs_manager as jm
import web_app


class FakeClient:
    def list_share_dir_entries(self, share_id, stoken, fid):
        return [
            {"fid": "f1", "name": "a.mp4", "is_dir": False, "size": 10, "updated_at": 0},
            {"fid": "d1", "name": "sub", "is_dir": True, "size": 0, "updated_at": 0},
        ]


@pytest.fixture()
def client():
    # base_url 用回环地址：middleware 的 Host 白名单只放行回环/配置过的域名
    return TestClient(web_app.app, base_url="http://127.0.0.1:8787")


def test_tree_requires_existing_job(client, monkeypatch):
    monkeypatch.setattr(jm, "get_job", lambda job_id: None)
    r = client.post("/api/share/tree", json={"job_id": "nope"})
    assert r.status_code == 404
    assert r.json()["detail"] == "任务不存在"


def test_tree_annotates_taken_from_same_share(client, monkeypatch):
    job = {"id": "j1", "name": "A", "share_url": "https://pan.quark.cn/s/abc", "share_pwd": "1"}
    monkeypatch.setattr(jm, "get_job", lambda job_id: job if job_id == "j1" else None)
    monkeypatch.setattr(
        jm,
        "share_usage",
        lambda job_id="": {"f1": {"job_id": "j1", "job_name": "A", "state": "done", "kind": "file"}},
    )
    seen = {}

    def fake_context(url, pwd):
        seen["url"] = url
        seen["pwd"] = pwd
        return FakeClient(), "st", "sid", "root"

    monkeypatch.setattr(web_app, "_share_context", fake_context)
    r = client.post("/api/share/tree", json={"job_id": "j1", "pdir_fid": ""})
    assert r.status_code == 200
    body = r.json()
    entries = {e["fid"]: e for e in body["entries"]}
    assert entries["f1"]["taken"]["state"] == "done"
    assert entries["d1"]["taken"] is None
    # 链接/提取码来自任务，而不是请求体
    assert seen == {"url": "https://pan.quark.cn/s/abc", "pwd": "1"}


def test_add_selection_endpoint_returns_duplicates(client, monkeypatch):
    calls = {}

    def fake_add(job_id, selection, force=False):
        calls["job_id"] = job_id
        calls["selection"] = selection
        calls["force"] = force
        return {
            "job": {"id": job_id, "name": "A", "selection": selection},
            "added": {"files": 1, "dirs": 0},
            "duplicates": [
                {"fid": "f9", "job_id": "j2", "job_name": "B", "state": "done",
                 "kind": "file", "reason": "other_job"}
            ],
            "already_in_job": 2,
        }

    monkeypatch.setattr(jm, "add_job_selection", fake_add)
    monkeypatch.setattr(jm, "refresh_job_runtime", lambda job: job)
    r = client.post(
        "/api/jobs/j1/selection/add",
        json={"file_fids": ["f1"], "dir_fids": [], "force": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == {"files": 1, "dirs": 0}
    assert body["duplicates"][0]["job_name"] == "B"
    assert body["already_in_job"] == 2
    assert calls == {"job_id": "j1", "selection": {"file_fids": ["f1"], "dir_fids": []}, "force": True}


def test_add_selection_endpoint_maps_value_error_to_400(client, monkeypatch):
    def boom(job_id, selection, force=False):
        raise ValueError("没有勾选任何文件或文件夹")

    monkeypatch.setattr(jm, "add_job_selection", boom)
    r = client.post("/api/jobs/j1/selection/add", json={"file_fids": []})
    assert r.status_code == 400
    assert "没有勾选" in r.json()["detail"]

class SizeFakeClient:
    def list_share_dir_entries(self, share_id, stoken, fid):
        return [
            {"fid": "big", "name": "big.mkv", "is_dir": False, "size": 6 * 1024**3, "updated_at": 0},
            {"fid": "small", "name": "s.mp4", "is_dir": False, "size": 100, "updated_at": 0},
            {"fid": "bigdir", "name": "d", "is_dir": True, "size": 9 * 1024**3, "updated_at": 0},
        ]


def _patch_tree(client, monkeypatch, config):
    monkeypatch.setattr(
        web_app, "_share_context", lambda url, pwd: (SizeFakeClient(), "st", "sid", "root")
    )
    if isinstance(config, Exception):
        def boom():
            raise config
        monkeypatch.setattr(web_app, "load_merged_configs", boom)
    else:
        monkeypatch.setattr(web_app, "load_merged_configs", lambda: config)
    r = client.post("/api/share/tree", json={"share_url": "https://pan.quark.cn/s/abc"})
    assert r.status_code == 200
    return r.json()


def test_tree_marks_oversize_files(client, monkeypatch):
    body = _patch_tree(client, monkeypatch, {"quark_cookie": "x", "quark_max_file_size_gb": 5})
    entries = {e["fid"]: e for e in body["entries"]}
    assert body["max_file_size"] == 5 * 1024 ** 3
    assert entries["big"]["oversize"] is True
    assert entries["small"]["oversize"] is False
    # 目录不打标（目录大小是聚合值，单文件限制对它没意义）
    assert entries["bigdir"]["oversize"] is False


def test_tree_oversize_marking_can_be_disabled(client, monkeypatch):
    body = _patch_tree(client, monkeypatch, {"quark_max_file_size_gb": 0})
    assert body["max_file_size"] == 0
    assert all(e["oversize"] is False for e in body["entries"])


def test_tree_oversize_off_by_default(client, monkeypatch):
    # 默认关闭：客户端 UA 下 23018 不再触发，标了反而是假警报
    body = _patch_tree(client, monkeypatch, RuntimeError("boom"))
    assert web_app.DEFAULT_MAX_FILE_SIZE_GB == 0
    assert body["max_file_size"] == 0
    assert all(e["oversize"] is False for e in body["entries"])


def test_remove_selection_endpoint_passes_fids(client, monkeypatch):
    calls = {}

    def fake_remove(job_id, selection):
        calls["job_id"] = job_id
        calls["selection"] = selection
        return {
            "job": {"id": job_id, "name": "A", "selection": {}},
            "removed": {"files": 1, "dirs": 0},
            "state_files_removed": 1,
        }

    monkeypatch.setattr(jm, "remove_job_selection", fake_remove)
    monkeypatch.setattr(jm, "refresh_job_runtime", lambda job: job)
    r = client.post("/api/jobs/j1/selection/remove", json={"file_fids": ["f1"]})
    assert r.status_code == 200
    assert r.json()["removed"] == {"files": 1, "dirs": 0}
    assert r.json()["state_files_removed"] == 1
    assert calls == {"job_id": "j1", "selection": {"file_fids": ["f1"], "dir_fids": []}}


def test_remove_selection_endpoint_rejects_running_job(client, monkeypatch):
    def boom(job_id, selection):
        raise RuntimeError("任务正在运行：先暂停任务，再取消勾选")

    monkeypatch.setattr(jm, "remove_job_selection", boom)
    r = client.post("/api/jobs/j1/selection/remove", json={"file_fids": ["f1"]})
    assert r.status_code == 400
    assert "正在运行" in r.json()["detail"]
