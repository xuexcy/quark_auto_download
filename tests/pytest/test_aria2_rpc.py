# -*- coding: utf-8 -*-
"""Aria2 RPC 地址/令牌配错的诊断：URL 归一化、错误文案与启动前预检。"""

import json
import logging

import pytest

import aria2_api
import error_classify
import jobs_manager
from aria2_api import Aria2Client, Aria2ConfigError, Aria2Error


class FakeResp:
    def __init__(self, status=200, text="", ctype="application/json-rpc", json_data=None):
        self.status_code = status
        self.text = text
        self.headers = {"Content-Type": ctype}
        self._json = json_data

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)


def make_client(host="http://127.0.0.1:6800/jsonrpc", secret="tok"):
    return Aria2Client(
        host=host,
        secret=secret,
        download_dir="/tmp/dl",
        request_timeout=3,
        download_headers=None,
        poll_interval=1,
        download_timeout=10,
        logger=logging.getLogger("test"),
    )


def test_normalize_rpc_url_appends_jsonrpc():
    assert aria2_api.normalize_rpc_url(" http://h:6800/ ") == "http://h:6800/jsonrpc"
    assert aria2_api.normalize_rpc_url("http://h:6800") == "http://h:6800/jsonrpc"
    assert aria2_api.normalize_rpc_url("https://h.example/jsonrpc") == "https://h.example/jsonrpc"
    assert aria2_api.normalize_rpc_url("") == ""
    # 带查询串（部分反代用 ?token=）时只补路径，不动 query
    assert aria2_api.normalize_rpc_url("http://h:6800?x=1") == "http://h:6800/jsonrpc?x=1"


def test_client_normalizes_host_on_init():
    client = make_client(host="http://127.0.0.1:6800")
    assert client.host == "http://127.0.0.1:6800/jsonrpc"


def test_html_response_is_config_error(monkeypatch):
    body = "<html><head><title>501 Not Implemented</title></head></html>"
    monkeypatch.setattr(aria2_api.requests, "post", lambda *a, **k: FakeResp(501, body, "text/html"))
    client = make_client(host="https://dl.example.com")

    with pytest.raises(Aria2ConfigError) as excinfo:
        client.aria2_get_version()
    message = str(excinfo.value)
    assert "不是 JSON-RPC" in message
    assert "6800/jsonrpc" in message
    assert "AriaNg" in message


def test_unauthorized_json_error_is_config_error(monkeypatch):
    body = '{"id":"1","jsonrpc":"2.0","error":{"code":1,"message":"Unauthorized"}}'
    monkeypatch.setattr(
        aria2_api.requests, "post", lambda *a, **k: FakeResp(400, body, "application/json-rpc")
    )
    client = make_client()

    with pytest.raises(Aria2ConfigError) as excinfo:
        client.aria2_get_version()
    assert "令牌（rpc-secret）不对" in str(excinfo.value)


def test_success_returns_result(monkeypatch):
    body = '{"id":"1","jsonrpc":"2.0","result":{"version":"1.36.0"}}'
    monkeypatch.setattr(
        aria2_api.requests, "post", lambda *a, **k: FakeResp(200, body, "application/json-rpc")
    )
    assert make_client().aria2_get_version() == {"version": "1.36.0"}


def test_connection_error_stays_temporary(monkeypatch):
    def boom(*args, **kwargs):
        raise aria2_api.requests.ConnectionError("connection refused")

    monkeypatch.setattr(aria2_api.requests, "post", boom)
    with pytest.raises(Aria2Error) as excinfo:
        make_client().aria2_get_version()
    assert "连接 Aria2 失败" in str(excinfo.value)
    # 网络类错误仍按临时处理，允许稍后重试
    assert error_classify.classify_error(str(excinfo.value)) == "temporary"


@pytest.mark.parametrize(
    "message",
    [
        "Aria2 RPC 地址不对（https://dl.example.com）: HTTP 501 text/html，返回的不是 JSON-RPC",
        "Aria2 RPC 未授权（http://h:6800/jsonrpc）: 令牌（rpc-secret）不对",
    ],
)
def test_rpc_config_errors_are_permanent(message):
    assert error_classify.classify_error(message) == "permanent"


def test_start_job_fails_before_spawn_when_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_manager, "JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(jobs_manager, "JOBS_INDEX", str(tmp_path / "jobs.yaml"))
    job = {
        "id": "j1",
        "name": "测试任务",
        "share_url": "https://pan.quark.cn/s/abc",
        "selection": {"file_fids": ["f1"]},
        "selection_mode": "explicit",
        "status": "paused",
        "log_path": "",
    }
    jobs_manager.save_jobs_index([job])
    monkeypatch.setattr(jobs_manager, "refresh_job_runtime", lambda job, persist=True: {"running": False, "pid": None})
    monkeypatch.setattr(
        jobs_manager,
        "load_section",
        lambda name: {"aria2_profiles": [{"name": "坏配置", "aria2_host": "https://dl.example.com"}]},
    )

    def bad_probe(name, timeout=5):
        raise Aria2ConfigError("Aria2 RPC 地址不对（https://dl.example.com）: 返回的不是 JSON-RPC")

    monkeypatch.setattr(jobs_manager, "probe_aria2_profile", bad_probe)

    def no_spawn(*args, **kwargs):
        raise AssertionError("预检失败时不应启动子进程")

    monkeypatch.setattr(jobs_manager.subprocess, "Popen", no_spawn)

    with pytest.raises(ValueError) as excinfo:
        jobs_manager.start_job("j1")
    message = str(excinfo.value)
    assert "坏配置" in message
    assert "连不上" in message
    assert "不是 JSON-RPC" in message

# ---------- 两个目录都可选：plan_download_dirs ----------
# job_id 只当「下载中的暂存区」，最终落点由 destination_dir 决定；两个都不填时提交任务
# 不带 dir，交给 aria2 自己的默认目录（本机不校验、不移动），所以 plan 不碰 RPC。

def test_plan_keeps_temp_and_dest():
    plan = aria2_api.plan_download_dirs("/tmp/dl", "/done")
    assert plan == {"download_dir": "/tmp/dl", "destination_dir": "/done", "mode": "temp"}


def test_plan_temp_only_moves_up_to_temp_root():
    """只填临时目录：下到 <临时>/<job_id>/，下完挪到 <临时>/ 根下（最终看不到 job_id）。"""
    plan = aria2_api.plan_download_dirs("/tmp/dl", "  ")
    assert plan == {"download_dir": "/tmp/dl", "destination_dir": "/tmp/dl", "mode": "temp"}


def test_plan_dest_only_downloads_into_dest():
    plan = aria2_api.plan_download_dirs("", "/done")
    assert plan == {"download_dir": "/done", "destination_dir": "/done", "mode": "dest"}


def test_plan_neither_submits_no_dir():
    plan = aria2_api.plan_download_dirs("  ", "")
    assert plan == {"download_dir": "", "destination_dir": "", "mode": "none"}


def test_plan_remote_ignores_both_dirs():
    plan = aria2_api.plan_download_dirs("/tmp/dl", "/done", remote=True)
    assert plan == {"download_dir": "", "destination_dir": "", "mode": "remote"}


def test_plan_does_not_touch_aria2(monkeypatch):
    """plan 纯函数：以前会为了「都空」去读 getGlobalOption，现在不该再连 RPC。"""
    def boom(*args, **kwargs):
        raise AssertionError("plan_download_dirs 不该连 Aria2")

    monkeypatch.setattr(aria2_api, "Aria2Client", boom)
    assert aria2_api.plan_download_dirs("", "")["mode"] == "none"


def test_probe_does_not_read_global_option(monkeypatch):
    """预检只验证连通性：目录不再参与，两个都不填也能启动。"""
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def aria2_get_version(self):
            return {"version": "1.36.0"}

        def aria2_get_global_option(self):
            raise AssertionError("预检不该读取 aria2 默认目录")

    monkeypatch.setattr(
        jobs_manager,
        "load_section",
        lambda name: {"aria2_profiles": [{"name": "本机", "aria2_host": "http://a"}]},
    )
    monkeypatch.setattr(aria2_api, "Aria2Client", FakeClient)
    assert jobs_manager.probe_aria2_profile("本机")["version"] == "1.36.0"
