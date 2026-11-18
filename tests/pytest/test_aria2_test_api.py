# -*- coding: utf-8 -*-
"""POST /api/aria2/test：不许把已保存的令牌发到请求体里给的陌生地址。"""

import pytest
from fastapi import HTTPException

import web_app


SAVED = {"name": "默认", "host": "http://192.168.1.2:6800/jsonrpc", "secret": "tok"}


def test_masked_secret_rejects_foreign_host(monkeypatch):
    monkeypatch.setattr(web_app.jm, "aria2_target", lambda name="": dict(SAVED))
    body = web_app.Aria2TestRequest(name="默认", host="http://evil.example:6800/jsonrpc", secret="********")
    with pytest.raises(HTTPException) as excinfo:
        web_app.api_aria2_test(body)
    assert excinfo.value.status_code == 400
    assert "重新填写令牌" in excinfo.value.detail


def test_masked_secret_allows_same_host(monkeypatch):
    monkeypatch.setattr(web_app.jm, "aria2_target", lambda name="": dict(SAVED))
    monkeypatch.setattr(
        web_app.jm,
        "test_aria2_connection",
        lambda host, secret="", timeout=8, **kwargs: {"host": host, "version": "1.36.0"},
    )
    body = web_app.Aria2TestRequest(name="默认", host="http://192.168.1.2:6800/jsonrpc", secret="********")
    out = web_app.api_aria2_test(body)
    assert out["ok"] is True
    assert out["host"] == "http://192.168.1.2:6800/jsonrpc"


def test_blank_host_falls_back_to_saved(monkeypatch):
    monkeypatch.setattr(web_app.jm, "aria2_target", lambda name="": dict(SAVED))
    seen = {}

    def fake_test(host, secret="", timeout=8, **kwargs):
        seen["host"] = host
        seen["secret"] = secret
        return {"host": host, "version": "1.36.0"}

    monkeypatch.setattr(web_app.jm, "test_aria2_connection", fake_test)
    web_app.api_aria2_test(web_app.Aria2TestRequest(name="默认", host="", secret=""))
    assert seen == {"host": SAVED["host"], "secret": SAVED["secret"]}


def test_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setattr(web_app.jm, "aria2_target", lambda name="": dict(SAVED))
    body = web_app.Aria2TestRequest(name="默认", host="ftp://x.example/jsonrpc", secret="tok")
    with pytest.raises(HTTPException) as excinfo:
        web_app.api_aria2_test(body)
    assert excinfo.value.status_code == 400
    assert "http://" in excinfo.value.detail


def test_saved_remote_dir_is_reused_when_body_omits_it(monkeypatch):
    """只给 name 调接口（脚本/curl）时，远端标记与远端目录用已保存的配置。"""
    monkeypatch.setattr(
        web_app.jm,
        "aria2_target",
        lambda name="": {**SAVED, "remote": True, "remote_dir": "/var/tmp/quark"},
    )
    seen = {}

    def fake_test(host, secret="", timeout=8, **kwargs):
        seen.update(kwargs)
        seen["host"] = host
        return {"host": host, "version": "1.36.0", "dir": kwargs.get("remote_dir")}

    monkeypatch.setattr(web_app.jm, "test_aria2_connection", fake_test)
    out = web_app.api_aria2_test(web_app.Aria2TestRequest(name="灰机nas", host="", secret=""))
    assert seen["remote"] is True
    assert seen["remote_dir"] == "/var/tmp/quark"
    assert out["dir"] == "/var/tmp/quark"


def test_body_remote_fields_win_over_saved(monkeypatch):
    monkeypatch.setattr(
        web_app.jm,
        "aria2_target",
        lambda name="": {**SAVED, "remote": True, "remote_dir": "/var/tmp/quark"},
    )
    seen = {}

    def fake_test(host, secret="", timeout=8, **kwargs):
        seen.update(kwargs)
        return {"host": host, "version": "1.36.0"}

    monkeypatch.setattr(web_app.jm, "test_aria2_connection", fake_test)
    web_app.api_aria2_test(
        web_app.Aria2TestRequest(name="灰机nas", host="", secret="", remote=False, remote_dir="/data")
    )
    assert seen["remote"] is False
    assert seen["remote_dir"] == "/data"


def test_foreign_host_does_not_inherit_saved_remote_dir(monkeypatch):
    """换地址时不要顺手把「对方的远端目录」套到陌生机器上。"""
    monkeypatch.setattr(
        web_app.jm,
        "aria2_target",
        lambda name="": {**SAVED, "remote": True, "remote_dir": "/var/tmp/quark"},
    )
    seen = {}

    def fake_test(host, secret="", timeout=8, **kwargs):
        seen.update(kwargs)
        return {"host": host, "version": "1.36.0"}

    monkeypatch.setattr(web_app.jm, "test_aria2_connection", fake_test)
    web_app.api_aria2_test(web_app.Aria2TestRequest(name="灰机nas", host="http://other:6800/jsonrpc", secret="tok"))
    assert seen["remote"] is False
    assert seen["remote_dir"] == ""
