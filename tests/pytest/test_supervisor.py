"""运行环境自检（只读）：cgroup 解析 + 托管判定（不碰真实 systemd）。"""

import os

import pytest

import supervisor as sup


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    sup.reset_cache()
    monkeypatch.delenv("SYSTEMD_EXEC_PID", raising=False)
    yield
    sup.reset_cache()


def test_unit_from_cgroup_systemd_slice():
    text = "0::/system.slice/quark_auto_download.service\n"
    assert sup.unit_from_cgroup(text) == "quark_auto_download"


def test_unit_from_cgroup_rejects_non_service():
    assert sup.unit_from_cgroup("0::/\n") is None
    assert sup.unit_from_cgroup("0::/user.slice/user-1000.slice/session-1.scope\n") is None
    assert sup.unit_from_cgroup("12:pids:/init.scope\n") is None
    assert sup.unit_from_cgroup(None) is None
    assert sup.unit_from_cgroup("garbage") is None


def _fake_show(**kwargs):
    base = {
        "ActiveState": "active",
        "UnitFileState": "enabled",
        "Restart": "always",
        "FragmentPath": "/etc/systemd/system/quark_auto_download.service",
        "MainPID": str(os.getpid()),
    }
    base.update(kwargs)
    return base


def test_runtime_info_systemd(monkeypatch):
    monkeypatch.setattr(sup, "own_unit", lambda: "quark_auto_download")
    monkeypatch.setattr(sup, "_systemctl_show", lambda unit: _fake_show())
    info = sup.runtime_info(refresh=True)
    assert info["supervisor"] == "systemd"
    assert info["unit"] == "quark_auto_download"
    assert info["autostart"] is True
    assert info["restart_policy"] == "always"
    assert info["restart_hint"] == "sudo systemctl restart quark_auto_download"
    assert sup.can_self_restart(info) is True


def test_runtime_info_systemd_disabled_autostart(monkeypatch):
    monkeypatch.setattr(sup, "own_unit", lambda: "quark_auto_download")
    monkeypatch.setattr(
        sup, "_systemctl_show", lambda unit: _fake_show(UnitFileState="disabled", Restart="on-failure")
    )
    info = sup.runtime_info(refresh=True)
    assert info["autostart"] is False
    assert info["restart_policy"] == "on-failure"
    assert sup.can_self_restart(info) is False


def test_runtime_info_pidfile(monkeypatch, tmp_path):
    pid_file = tmp_path / "web.pid"
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(sup, "own_unit", lambda: None)
    monkeypatch.setattr(sup, "PID_FILE", str(pid_file))
    info = sup.runtime_info(refresh=True)
    assert info["supervisor"] == "pidfile"
    assert info["unit"] is None
    assert info["autostart"] is None
    assert info["restart_hint"] is None


def test_runtime_info_unknown_when_pidfile_is_other_process(monkeypatch, tmp_path):
    pid_file = tmp_path / "web.pid"
    pid_file.write_text("999999\n", encoding="utf-8")
    monkeypatch.setattr(sup, "own_unit", lambda: None)
    monkeypatch.setattr(sup, "PID_FILE", str(pid_file))
    assert sup.runtime_info(refresh=True)["supervisor"] == "unknown"


def test_runtime_info_systemd_but_not_main_pid_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(sup, "own_unit", lambda: "quark_auto_download")
    monkeypatch.setattr(sup, "_systemctl_show", lambda unit: _fake_show(MainPID="12345"))
    monkeypatch.setattr(sup, "PID_FILE", str(tmp_path / "nope.pid"))
    assert sup.runtime_info(refresh=True)["supervisor"] == "unknown"


def test_runtime_info_is_cached(monkeypatch):
    calls = {"n": 0}

    def show(unit):
        calls["n"] += 1
        return _fake_show()

    monkeypatch.setattr(sup, "own_unit", lambda: "quark_auto_download")
    monkeypatch.setattr(sup, "_systemctl_show", show)
    sup.runtime_info(refresh=True)
    sup.runtime_info()
    assert calls["n"] == 1
