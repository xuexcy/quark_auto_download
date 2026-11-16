"""扫码登录的 Web 接口：申请二维码、轮询、手工 Cookie 校验落盘（不访问外网）。"""

import logging

import pytest
from fastapi.testclient import TestClient

import jobs_manager as jm
import quark_login as ql
import web_app


class FakeResp:
    """requests.get 的最小替身。"""

    def __init__(self, payload, status_code=200, url="https://uop.quark.cn/cas/login"):
        self._payload = payload
        self.status_code = status_code
        self.url = url

    def json(self):
        return self._payload


@pytest.fixture()
def client():
    # base_url 用回环地址：middleware 的 Host 白名单只放行回环/配置过的域名
    return TestClient(web_app.app, base_url="http://127.0.0.1:8787")


def test_qr_endpoints_roundtrip(client, monkeypatch):
    qr = ql.QuarkQrLogin(logger=logging.getLogger("test-qr-api"))
    monkeypatch.setattr(qr, "_fetch_token", lambda: "tok")
    monkeypatch.setattr(web_app, "_QR_LOGIN", qr)
    # 未确认前一律返回「等待扫码」，测试全程不碰真网络
    monkeypatch.setattr(
        ql.requests,
        "get",
        lambda *a, **k: FakeResp({"status": 50004001, "message": "Query result is empty"}),
    )
    started = client.post("/api/quark/login/qr", json={})
    assert started.status_code == 200
    sid = started.json()["session_id"]
    assert started.json()["qr_svg"].startswith("<svg")

    pending = client.get(f"/api/quark/login/qr/{sid}")
    assert pending.status_code == 200
    assert pending.json()["status"] == ql.STATUS_PENDING

    monkeypatch.setattr(
        ql.requests,
        "get",
        lambda *a, **k: FakeResp(
            {"status": 2000000, "data": {"members": {"service_ticket": "T"}}}
        ),
    )
    monkeypatch.setattr(qr, "_exchange_ticket", lambda ticket: ("__pus=x; __puus=y", "ok"))
    # _poll_once 里引用的是 quark_login 模块内的 validate_cookie
    monkeypatch.setattr(ql, "validate_cookie", lambda cookie: {"ok": True, "nickname": "小明"})
    saved = []
    monkeypatch.setattr(web_app, "_save_quark_cookie", lambda cookie: saved.append(cookie) or "掩码")

    done = client.get(f"/api/quark/login/qr/{sid}")
    assert done.status_code == 200
    assert done.json()["status"] == ql.STATUS_CONFIRMED
    assert done.json()["saved"] is True
    assert saved == ["__pus=x; __puus=y"]

    again = client.get(f"/api/quark/login/qr/{sid}")
    assert again.json()["saved"] is True
    assert saved == ["__pus=x; __puus=y"], "确认后的重复轮询不应重复落盘"


def test_qr_status_unknown_session(client):
    res = client.get("/api/quark/login/qr/nope")
    assert res.status_code == 404


def test_manual_cookie_saved_after_validation(client, monkeypatch):
    # 接口里用的是 web_app 命名空间下的 validate_cookie
    monkeypatch.setattr(web_app, "validate_cookie", lambda cookie: {"ok": True, "nickname": "小红"})
    monkeypatch.setattr(web_app, "_save_quark_cookie", lambda cookie: "掩码")
    res = client.post("/api/quark/cookie", json={"cookie": "__pus=abc"})
    assert res.status_code == 200
    assert res.json()["nickname"] == "小红"
    assert res.json()["cookie_masked"] == "掩码"


def test_manual_cookie_rejected_when_invalid(client, monkeypatch):
    monkeypatch.setattr(
        web_app,
        "validate_cookie",
        lambda cookie: {"ok": False, "message": "Cookie 无效: require login"},
    )
    res = client.post("/api/quark/cookie", json={"cookie": "__pus=bad"})
    assert res.status_code == 400
    assert "require login" in res.json()["detail"]


def test_cookie_test_uses_configured_cookie(client, monkeypatch):
    monkeypatch.setattr(web_app, "_quark_cookie", lambda: "__pus=cfg")
    seen = []
    monkeypatch.setattr(
        web_app,
        "validate_cookie",
        lambda cookie: seen.append(cookie) or {"ok": True, "nickname": "小刚"},
    )
    res = client.post("/api/quark/cookie/test", json={})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert seen == ["__pus=cfg"]
