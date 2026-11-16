"""扫码登录：二维码内容、Cookie 组装、会话状态机（全离线，不真连夸克）。"""

import logging
import time

import pytest
import requests.cookies

import quark_login as ql


class FakeResp:
    """requests.get 的最小替身。"""

    def __init__(self, payload, status_code=200, url="https://uop.quark.cn/cas/login"):
        self._payload = payload
        self.status_code = status_code
        self.url = url

    def json(self):
        return self._payload


def make_jar(items):
    jar = requests.cookies.RequestsCookieJar()
    for name, value, domain in items:
        jar.set(name, value, domain=domain, path="/")
    return jar


def make_login(**kwargs) -> ql.QuarkQrLogin:
    return ql.QuarkQrLogin(logger=logging.getLogger("test-quark-login"), **kwargs)


def test_build_scan_content_carries_token_and_biz():
    content = ql.build_scan_content("staAAA")
    assert content.startswith("https://su.quark.cn/4_eMHBJ?")
    assert "token=staAAA" in content
    assert "client_id=532" in content
    assert "ssb=weblogin" in content
    # uc_biz_str 里的管道符必须被 urlencode，否则手机端解析不了
    assert "%7C" in content
    assert "|" not in content.split("?", 1)[1]


def test_mask_cookie_hides_middle():
    masked = ql.mask_cookie("__pus=0123456789abcdef; __puus=zyxw")
    assert masked.startswith("__pus=01234")
    assert "6789abcdef" not in masked
    assert ql.mask_cookie("") == ""
    assert ql.mask_cookie("short=1") == "short=…"


def test_cookie_header_prefers_pus_and_drops_noise():
    jar = make_jar(
        [
            ("other", "c", ".pan.quark.cn"),
            ("__puus", "b", ".pan.quark.cn"),
            ("__pus", "a", ".quark.cn"),
            ("_UP_28A_52_", "noise", ".quark.cn"),
            ("CNZZDATA", "x", ".cnzz.com"),
        ]
    )
    header = ql._cookie_header_from_jar(jar)
    assert header.startswith("__pus=a")
    assert "__puus=b" in header
    assert "_UP_" not in header
    assert "CNZZDATA" not in header


def test_start_requires_token(monkeypatch):
    client = make_login()

    def boom():
        raise RuntimeError("申请二维码失败: Token Not Found")

    monkeypatch.setattr(client, "_fetch_token", boom)
    with pytest.raises(RuntimeError):
        client.start()


def test_session_full_flow(monkeypatch):
    client = make_login()
    monkeypatch.setattr(client, "_fetch_token", lambda: "tok123")
    monkeypatch.setattr(ql, "render_qr_svg", lambda content, scale=5: "<svg/>")
    started = client.start()
    assert started["status"] == ql.STATUS_PENDING
    assert started["qr_svg"] == "<svg/>"
    sid = started["session_id"]

    # 1) 还没扫码
    monkeypatch.setattr(
        ql.requests,
        "get",
        lambda *a, **k: FakeResp({"status": 50004001, "message": "Query result is empty"}),
    )
    pending = client.status(sid)
    assert pending["status"] == ql.STATUS_PENDING
    assert pending["cookie_masked"] == ""

    # 2) 手机确认 → 换 Cookie → 校验通过
    monkeypatch.setattr(
        ql.requests,
        "get",
        lambda *a, **k: FakeResp(
            {"status": 2000000, "data": {"members": {"service_ticket": "TICKET"}}}
        ),
    )
    monkeypatch.setattr(client, "_exchange_ticket", lambda ticket: ("__pus=x; __puus=y", "ok"))
    monkeypatch.setattr(ql, "validate_cookie", lambda cookie: {"ok": True, "nickname": "小明"})
    done = client.status(sid)
    assert done["status"] == ql.STATUS_CONFIRMED
    assert done["nickname"] == "小明"
    assert done["saved"] is False
    assert client.cookie_of(sid) == "__pus=x; __puus=y"

    # 3) 落盘标记只生效一次
    client.mark_saved(sid)
    assert client.status(sid)["saved"] is True


def test_session_fails_when_exchange_returns_nothing(monkeypatch):
    client = make_login()
    monkeypatch.setattr(client, "_fetch_token", lambda: "tok")
    monkeypatch.setattr(ql, "render_qr_svg", lambda content, scale=5: "")
    sid = client.start()["session_id"]
    monkeypatch.setattr(
        ql.requests,
        "get",
        lambda *a, **k: FakeResp(
            {"status": 2000000, "data": {"members": {"service_ticket": "T"}}}
        ),
    )
    monkeypatch.setattr(client, "_exchange_ticket", lambda ticket: ("", "cookies=[]"))
    failed = client.status(sid)
    assert failed["status"] == ql.STATUS_FAILED
    assert "换取 Cookie 失败" in failed["message"]
    assert client.cookie_of(sid) == ""


def test_session_expires(monkeypatch):
    client = make_login()
    monkeypatch.setattr(client, "_fetch_token", lambda: "tok")
    monkeypatch.setattr(ql, "render_qr_svg", lambda content, scale=5: "")
    sid = client.start()["session_id"]
    client._sessions[sid].created_at = time.time() - ql.QR_TTL - 5
    expired = client.status(sid)
    assert expired["status"] == ql.STATUS_EXPIRED
    assert "过期" in expired["message"]


def test_status_unknown_session_returns_none():
    assert make_login().status("nope") is None


def test_render_qr_svg_is_inline_svg():
    svg = ql.render_qr_svg(ql.build_scan_content("tok"))
    assert svg.startswith("<svg") or svg == ""
    if svg:
        assert "</svg>" in svg
        assert "viewBox=\"0 0 " in svg


def test_render_qr_svg_is_scaleable():
    """必须带 viewBox 且不写死像素宽高，否则容器会裁掉图案（显示不全）。"""
    svg = ql.render_qr_svg(ql.build_scan_content("tok"))
    if not svg:
        return
    root = ql._SVG_ROOT_RE.search(svg).group(0)
    assert "viewBox=" in root
    assert "width=" not in root.replace("viewBox=", "")
    assert "height=" not in root

class FakeSession:
    """requests.Session 的最小替身：Cookie 由构造时给的 jar 决定，并记录调用顺序。"""

    def __init__(self, jar):
        self.cookies = jar
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        return FakeResp({}, url=url)


def test_exchange_ticket_walks_web_login_chain(monkeypatch):
    """实测链路：同一个会话按 EXCHANGE_STEPS 走完，ticket 放进 account/info 的 st。"""
    sess = FakeSession(make_jar([("__pus", "p", ".quark.cn")]))
    monkeypatch.setattr(ql.requests, "Session", lambda: sess)
    cookie, detail = make_login()._exchange_ticket("TICK")
    assert detail == "ok"
    assert cookie.startswith("__pus=p")
    assert [c[0] for c in sess.calls] == [u for u, _ in ql.EXCHANGE_STEPS]
    assert sess.calls[2][1] == {"st": "TICK", "lw": "scan"}


def test_exchange_ticket_reports_trail_without_pus_cookie(monkeypatch):
    sess = FakeSession(make_jar([("_UP_28A_52_", "noise", ".quark.cn")]))
    monkeypatch.setattr(ql.requests, "Session", lambda: sess)
    cookie, detail = make_login()._exchange_ticket("TICK")
    assert cookie == ""
    assert "未拿到 __pus/__puus" in detail
    assert "pan.quark.cn/account/info" in detail
