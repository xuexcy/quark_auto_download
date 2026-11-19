# -*- coding: utf-8 -*-
"""MCP 自动命名：MCP-<分享根目录名>，取不到标题回退分享 id。"""

import mcp_server


def test_auto_name_uses_share_title(monkeypatch):
    monkeypatch.setattr(mcp_server, "_share_title", lambda url, pwd="": "Nintendo Switch运动 [版本1.5.0]")
    assert mcp_server._auto_name("https://pan.quark.cn/s/44119f2aeb55") == (
        "MCP-Nintendo Switch运动 [版本1.5.0]"
    )


def test_auto_name_falls_back_to_share_id(monkeypatch):
    monkeypatch.setattr(mcp_server, "_share_title", lambda url, pwd="": "")
    assert mcp_server._auto_name("https://pan.quark.cn/s/44119f2aeb55") == "MCP-44119f2aeb55"


def test_share_title_receives_password(monkeypatch):
    seen = {}

    def fake_title(url, pwd=""):
        seen["url"], seen["pwd"] = url, pwd
        return "某剧"

    monkeypatch.setattr(mcp_server, "_share_title", fake_title)
    assert mcp_server._auto_name("https://pan.quark.cn/s/abc", "1234") == "MCP-某剧"
    assert seen == {"url": "https://pan.quark.cn/s/abc", "pwd": "1234"}


def test_trim_name_collapses_whitespace_and_truncates():
    assert mcp_server._trim_name("  某 剧\n第一季  ") == "某 剧 第一季"
    assert len(mcp_server._trim_name("x" * 100)) == 60
    assert mcp_server._trim_name("...") == ""
    assert mcp_server._trim_name(None) == ""


def test_resolve_share_job_passes_password_when_creating(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_server.jm, "list_jobs", lambda: [])
    monkeypatch.setattr(mcp_server.jm, "share_key", lambda url: "s/abc")
    monkeypatch.setattr(mcp_server, "_auto_name", lambda url, pwd="": "MCP-某剧")

    def fake_add(share_url, share_pwd="", name=""):
        seen.update({"url": share_url, "pwd": share_pwd, "name": name})
        return {"id": "jnew", "name": name, "share_url": share_url}

    monkeypatch.setattr(mcp_server.jm, "add_job", fake_add)
    job, created = mcp_server.resolve_share_job("https://pan.quark.cn/s/abc", "", "1234")
    assert created is True and job["id"] == "jnew"
    assert seen == {"url": "https://pan.quark.cn/s/abc", "pwd": "1234", "name": "MCP-某剧"}
