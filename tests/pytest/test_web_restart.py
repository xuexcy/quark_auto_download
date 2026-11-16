"""重启接口的三条路径（不真的退出进程、不真的拉起脚本）。"""

import pytest
from fastapi.testclient import TestClient

import web_app


@pytest.fixture()
def client():
    return TestClient(web_app.app, base_url="http://127.0.0.1:8787")


def _info(supervisor, **kwargs):
    base = {
        "supervisor": supervisor,
        "unit": "quark_auto_download",
        "unit_file_state": "enabled",
        "autostart": True,
        "restart_policy": "always",
        "restart_hint": "sudo systemctl restart quark_auto_download",
    }
    base.update(kwargs)
    return base


def test_restart_systemd_self_exit(client, monkeypatch):
    monkeypatch.setattr(web_app.sup, "runtime_info", lambda refresh=False: _info("systemd"))
    called = {}
    monkeypatch.setattr(
        web_app, "_schedule_process_exit", lambda delay=0.8: called.setdefault("delay", delay)
    )
    r = client.post("/api/web/restart", json={})
    assert r.status_code == 200
    assert r.json()["mode"] == "systemd-self-exit"
    assert called["delay"] == 0.8


def test_restart_systemd_refuses_when_restart_is_not_always(client, monkeypatch):
    monkeypatch.setattr(
        web_app.sup, "runtime_info", lambda refresh=False: _info("systemd", restart_policy="on-failure")
    )
    r = client.post("/api/web/restart", json={})
    assert r.status_code == 409
    assert "sudo systemctl restart quark_auto_download" in r.json()["detail"]


def test_restart_uses_script_for_pidfile(client, monkeypatch):
    monkeypatch.setattr(
        web_app.sup,
        "runtime_info",
        lambda refresh=False: _info("pidfile", unit=None, restart_policy=None, restart_hint=None),
    )
    calls = {}

    def fake_popen(*args, **kwargs):
        calls["args"] = args
        return None

    monkeypatch.setattr(web_app.subprocess, "Popen", fake_popen)
    r = client.post("/api/web/restart", json={})
    assert r.status_code == 200
    assert r.json()["mode"] == "restart-script"
    assert "restart.sh" in str(calls["args"])


def test_restart_unknown_supervisor_is_rejected(client, monkeypatch):
    monkeypatch.setattr(
        web_app.sup,
        "runtime_info",
        lambda refresh=False: _info("unknown", unit=None, restart_policy=None, restart_hint=None),
    )
    r = client.post("/api/web/restart", json={})
    assert r.status_code == 409
    assert "无法识别" in r.json()["detail"]
