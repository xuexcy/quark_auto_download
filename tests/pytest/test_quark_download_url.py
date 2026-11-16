"""取直链：23018（单文件超限）必须立刻抛明确错误，不换域名重试。"""

import logging

import pytest

import quark_api as qa


def make_client() -> qa.QuarkClient:
    return qa.QuarkClient(
        cookie="__pus=x",
        request_timeout=5,
        save_to_dir="",
        share_url="",
        share_pwd="",
        logger=logging.getLogger("test-quark-download"),
    )


def test_size_limit_raises_rejected_without_retrying_other_base(monkeypatch):
    client = make_client()
    calls = []

    def fake_post(url, params, payload):
        calls.append(url)
        return {"status": 400, "code": 23018, "message": "download file size limit[fid1]"}

    monkeypatch.setattr(client, "quark_post_with_params", fake_post)
    with pytest.raises(qa.QuarkDownloadRejected) as err:
        client.get_download_urls(["a" * 32])
    assert "23018" in str(err.value)
    assert len(calls) == 1


def test_other_business_error_falls_back_to_next_base(monkeypatch):
    client = make_client()
    calls = []

    def fake_post(url, params, payload):
        calls.append(url)
        if len(calls) == 1:
            return {"status": 400, "code": 41006, "message": "share missing"}
        return {"status": 200, "code": 0, "data": [{"fid": "a" * 32, "download_url": "https://dl/x"}]}

    monkeypatch.setattr(client, "quark_post_with_params", fake_post)
    urls = client.get_download_urls(["a" * 32])
    assert urls == {"a" * 32: "https://dl/x"}
    assert len(calls) == 2

def test_default_user_agent_is_desktop_client():
    client = make_client()
    # 浏览器 UA 下 file/download 会返回 23018；默认必须带桌面客户端 UA
    assert client.user_agent == qa.DESKTOP_USER_AGENT
    assert client.headers["User-Agent"] == qa.DESKTOP_USER_AGENT
    assert "quark-cloud-drive" in client.headers["User-Agent"]


def test_user_agent_can_be_overridden():
    client = qa.QuarkClient(
        cookie="__pus=x",
        request_timeout=5,
        save_to_dir="",
        share_url="",
        share_pwd="",
        logger=logging.getLogger("test-quark-download"),
        user_agent="  Mozilla/5.0 (custom)  ",
    )
    assert client.user_agent == "Mozilla/5.0 (custom)"
    assert client.headers["User-Agent"] == "Mozilla/5.0 (custom)"


def test_download_headers_reuse_client_user_agent():
    client = make_client()
    headers = client.download_headers()
    assert f"User-Agent: {qa.DESKTOP_USER_AGENT}" in headers
    assert any(line.startswith("Cookie: ") for line in headers)
    assert any(line.startswith("Referer: ") for line in headers)
