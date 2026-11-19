"""防风控 Cookie（web-grey-id）补齐与「红/黄/绿」健康检查。

2026-11-19 的教训：只看 /1/clouddrive/member 会说「Cookie 有效」，
但所有直链都被 CDN 回 412 —— 因为缺 web-grey-id / web-grey-id.sig。
"""

import types

import pytest

import quark_api as qa
import quark_login as ql


class FakeJar:
    """最小 CookieJar 替身：支持 set() 与迭代。"""

    def __init__(self):
        self._items = {}

    def set(self, name, value, domain=""):
        self._items[name] = types.SimpleNamespace(name=name, value=value, domain=domain)

    def add(self, name, value, domain=".quark.cn"):
        self._items[name] = types.SimpleNamespace(name=name, value=value, domain=domain)

    def __iter__(self):
        return iter(self._items.values())


class FakeSession:
    def __init__(self, jar, issued=None, fail=False):
        self.cookies = jar
        self.headers = {}
        self._issued = issued or {}
        self._fail = fail
        self.visited = []

    def get(self, url, **kwargs):
        if self._fail:
            raise RuntimeError("network down")
        self.visited.append(url)
        for name, value in self._issued.items():
            self.cookies.add(name, value)
        return types.SimpleNamespace(status_code=200)


class FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code

    def close(self):
        pass


@pytest.fixture()
def no_session_network(monkeypatch):
    """把 requests.Session 换成假会话，默认访问 pan 时下发两条防风控 Cookie。"""
    jar = FakeJar()
    session = FakeSession(jar, issued={"web-grey-id": "g1", "web-grey-id.sig": "g2"})
    monkeypatch.setattr(ql.requests, "Session", lambda: session)
    return session


def test_warm_cookie_appends_grey_cookies(no_session_network):
    warmed = ql.warm_cookie("__pus=p; __puus=u", "UA/1.0")
    assert "__pus=p" in warmed and "__puus=u" in warmed
    assert "web-grey-id=g1" in warmed and "web-grey-id.sig=g2" in warmed
    # __pus/__puus 必须排在最前，防风控两条紧随其后
    assert warmed.startswith("__pus=p; __puus=u; web-grey-id=g1")
    # 三个预热地址都要走一遍（只访问 pan 首页不够稳）
    assert len(no_session_network.visited) == len(ql.COOKIE_WARM_STEPS)


def test_warm_cookie_keeps_original_when_network_fails(monkeypatch):
    jar = FakeJar()
    monkeypatch.setattr(ql.requests, "Session", lambda: FakeSession(jar, fail=True))
    assert ql.warm_cookie("__pus=p", "UA") == "__pus=p"


def test_warm_cookie_empty_input():
    assert ql.warm_cookie("", "UA") == ""
    assert ql.warm_cookie("   ", "UA") == ""


def test_check_cookie_health_red_when_cookie_invalid(monkeypatch):
    monkeypatch.setattr(ql, "warm_cookie", lambda cookie, *a, **k: cookie)
    monkeypatch.setattr(ql, "validate_cookie", lambda cookie, *a, **k: {"ok": False, "message": "Cookie 无效: require login"})
    health = ql.check_cookie_health("__pus=bad")
    assert health["level"] == "red"
    assert health["ok"] is False
    assert "require login" in health["message"]


class FakeQuarkClient:
    def __init__(self, status):
        self._status = status

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def list_my_dir_entries(self, pdir_fid):
        return [
            {"fid": "dir1", "dir": True, "file_name": "movies"},
            {"fid": "fid-small", "dir": False, "file_name": "a.txt", "size": 1024},
        ]

    def get_download_urls(self, fids):
        return {fids[0]: "https://dl.example/x"}


def _patch_client(monkeypatch, status):
    class _Client(FakeQuarkClient):
        def __init__(self, *a, **k):
            super().__init__(status)

    monkeypatch.setattr(qa, "QuarkClient", _Client)
    monkeypatch.setattr(ql, "warm_cookie", lambda cookie, *a, **k: cookie)
    monkeypatch.setattr(ql, "validate_cookie", lambda cookie, *a, **k: {"ok": True, "nickname": "小明"})
    monkeypatch.setattr(ql.requests, "get", lambda *a, **k: FakeResp(status))


def test_check_cookie_health_green_when_cdn_allows(monkeypatch):
    _patch_client(monkeypatch, 206)
    health = ql.check_cookie_health("__pus=cfg")
    assert health["level"] == "green"
    assert health["cdn_status"] == 206
    assert health["sample"] == "a.txt"


def test_check_cookie_health_yellow_when_cdn_returns_412(monkeypatch):
    _patch_client(monkeypatch, 412)
    health = ql.check_cookie_health("__pus=cfg")
    assert health["level"] == "yellow"
    assert health["ok"] is True
    assert "412" in health["message"]
    assert "web-grey-id" in health["message"]


def test_check_cookie_health_yellow_when_no_probe_file(monkeypatch):
    class _Empty(FakeQuarkClient):
        def __init__(self, *a, **k):
            super().__init__(0)

        def list_my_dir_entries(self, pdir_fid):
            return []

    monkeypatch.setattr(qa, "QuarkClient", _Empty)
    monkeypatch.setattr(ql, "warm_cookie", lambda cookie, *a, **k: cookie)
    monkeypatch.setattr(ql, "validate_cookie", lambda cookie, *a, **k: {"ok": True, "nickname": ""})
    health = ql.check_cookie_health("__pus=cfg")
    assert health["level"] == "yellow"
    assert "找不到可试下载的小文件" in health["message"]


def test_check_cookie_health_finds_probe_deep_in_subdirs(monkeypatch):
    """文件都埋在子目录里时也要能找到样本（NAS 上根目录只有几个大目录）。"""

    class _Deep(FakeQuarkClient):
        def __init__(self, *a, **k):
            super().__init__(0)

        def list_my_dir_entries(self, pdir_fid):
            if pdir_fid == "0":
                return [{"fid": "d1", "dir": True, "file_name": "movies"}]
            if pdir_fid == "d1":
                return [{"fid": "d2", "dir": True, "file_name": "S01"}]
            return [{"fid": "fid-3g", "dir": False, "file_name": "big.mkv", "size": 3 * 1024 ** 3}]

    monkeypatch.setattr(qa, "QuarkClient", _Deep)
    monkeypatch.setattr(ql, "warm_cookie", lambda cookie, *a, **k: cookie)
    monkeypatch.setattr(ql, "validate_cookie", lambda cookie, *a, **k: {"ok": True, "nickname": ""})
    monkeypatch.setattr(ql.requests, "get", lambda *a, **k: FakeResp(206))
    health = ql.check_cookie_health("__pus=cfg")
    assert health["level"] == "green"
    assert health["sample"] == "big.mkv"


def test_save_quark_cookie_persists_warmed_cookie(monkeypatch):
    """设置页保存 Cookie 时先补齐防风控字段，落盘的是补好的那一串。"""
    import web_app

    saved = {}
    monkeypatch.setattr(web_app.jm, "save_section", lambda name, payload: saved.update(payload))
    monkeypatch.setattr(web_app, "warm_cookie", lambda cookie, *a, **k: cookie + "; web-grey-id=g1")
    masked = web_app._save_quark_cookie("__pus=p")
    assert saved["quark_cookie"] == "__pus=p; web-grey-id=g1"
    assert masked.endswith("…") or "…" in masked


def test_save_quark_cookie_survives_warm_failure(monkeypatch):
    """补 Cookie 抛错时仍要用原串落盘，不能因为补不上就存不进去。"""
    import web_app

    saved = {}
    monkeypatch.setattr(web_app.jm, "save_section", lambda name, payload: saved.update(payload))

    def boom(cookie, *a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(web_app, "warm_cookie", boom)
    web_app._save_quark_cookie("__pus=p")
    assert saved["quark_cookie"] == "__pus=p"
