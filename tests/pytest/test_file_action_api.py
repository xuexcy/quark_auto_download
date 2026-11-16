"""逐文件控制接口 POST /api/jobs/{id}/files/action（不访问外网）。"""

import pytest
from fastapi.testclient import TestClient

import jobs_manager as jm
import web_app


@pytest.fixture()
def client():
    return TestClient(web_app.app, base_url="http://127.0.0.1:8787")


def test_action_requires_existing_job(client, monkeypatch):
    monkeypatch.setattr(jm, "get_job", lambda job_id: None)
    r = client.post("/api/jobs/nope/files/action", json={"op": "pause", "paths": ["a.mp4"]})
    assert r.status_code == 404
    assert r.json()["detail"] == "任务不存在"


def test_action_passes_through_result(client, monkeypatch):
    monkeypatch.setattr(jm, "get_job", lambda job_id: {"id": job_id})
    seen = {}

    def fake_apply(job_id, op, paths, fids):
        seen.update({"job_id": job_id, "op": op, "paths": paths, "fids": fids})
        return {"job_id": job_id, "op": "pause", "applied": "pipeline", "running": True}

    monkeypatch.setattr(jm, "apply_file_control", fake_apply)
    r = client.post(
        "/api/jobs/j1/files/action",
        json={"op": "pause", "paths": ["a/b.mp4"], "fids": ["f1"]},
    )
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "job_id": "j1",
        "op": "pause",
        "applied": "pipeline",
        "running": True,
    }
    assert seen == {"job_id": "j1", "op": "pause", "paths": ["a/b.mp4"], "fids": ["f1"]}


def test_action_maps_value_error_to_400(client, monkeypatch):
    monkeypatch.setattr(jm, "get_job", lambda job_id: {"id": job_id})

    def boom(*args, **kwargs):
        raise ValueError("不支持的操作: explode（只支持 pause / resume / delete）")

    monkeypatch.setattr(jm, "apply_file_control", boom)
    r = client.post("/api/jobs/j1/files/action", json={"op": "explode", "paths": ["a.mp4"]})
    assert r.status_code == 400
    assert "不支持的操作" in r.json()["detail"]
