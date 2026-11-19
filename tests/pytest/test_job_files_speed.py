# -*- coding: utf-8 -*-
"""GET /api/jobs/{id}/files：附带任务所属 Aria2 的总速度（连不上就降级，不能让进度面板 500）。"""

import pytest
from fastapi import HTTPException

import web_app


def patch_world(monkeypatch, *, status="running", stat=None, error=None, profile="默认"):
    calls = []
    monkeypatch.setattr(
        web_app.jm,
        "get_job",
        lambda job_id: {"id": job_id, "status": status, "aria2_profile": profile},
    )
    monkeypatch.setattr(
        web_app.jm,
        "job_files",
        lambda job_id, offset=0, limit=2000: {"files": [{"path": "a.mkv", "state": "downloading"}], "file_total": 1},
    )

    def fake_stat(job_id, timeout=3):
        calls.append((job_id, timeout))
        if error:
            raise RuntimeError(error)
        return stat or {}

    monkeypatch.setattr(web_app.jm, "aria2_global_stat", fake_stat)
    monkeypatch.setattr(web_app.jm, "aria2_target", lambda name="": {"name": name or "默认"})
    return calls


def test_speed_comes_from_global_stat(monkeypatch):
    calls = patch_world(
        monkeypatch,
        stat={"downloadSpeed": "7340032", "numActive": "2", "numWaiting": "1"},
    )
    data = web_app.api_job_files("j1")
    assert data["files"][0]["path"] == "a.mkv"
    assert data["speed"] == {
        "ok": True,
        "speed": 7340032,
        "num_active": 2,
        "num_waiting": 1,
        "aria2": "默认",
    }
    # 只查一次，且用短超时（Web 端 5 秒轮询不能等 10 秒）
    assert calls == [("j1", 3)]


def test_aria2_failure_degrades(monkeypatch):
    patch_world(monkeypatch, error="Aria2 RPC 地址不对（http://x）")
    data = web_app.api_job_files("j1")
    assert data["speed"]["ok"] is False
    assert data["speed"]["speed"] == 0
    assert "RPC 地址不对" in data["speed"]["message"]
    assert data["files"], "aria2 挂了也要把文件列表返回"


@pytest.mark.parametrize("status", ["done", "failed"])
def test_non_running_job_skips_aria2(monkeypatch, status):
    calls = patch_world(monkeypatch, status=status)
    data = web_app.api_job_files("j1")
    assert data["speed"] == {"ok": False, "speed": 0, "message": "任务未运行"}
    assert calls == []


def test_paused_job_still_reports_speed(monkeypatch):
    # 软暂停时 aria2 里可能还有在传的任务，速度要照常显示
    calls = patch_world(monkeypatch, status="paused", stat={"downloadSpeed": 1024})
    data = web_app.api_job_files("j1")
    assert data["speed"]["ok"] is True
    assert data["speed"]["speed"] == 1024
    assert calls == [("j1", 3)]


def test_missing_job_returns_404(monkeypatch):
    monkeypatch.setattr(web_app.jm, "get_job", lambda job_id: None)
    with pytest.raises(HTTPException) as excinfo:
        web_app.api_job_files("nope")
    assert excinfo.value.status_code == 404
