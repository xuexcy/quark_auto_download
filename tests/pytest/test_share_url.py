"""分享链接解析：普通链接、带参链接、锚点子目录、新形态子目录链接。"""

import logging

import pytest

from quark_api import QuarkClient


def make_client(url: str, pwd: str = "") -> QuarkClient:
    return QuarkClient(
        cookie="k=v",
        request_timeout=5,
        save_to_dir="",
        share_url=url,
        share_pwd=pwd,
        logger=logging.getLogger("test"),
    )


@pytest.mark.parametrize(
    "url, expected_id, expected_fid",
    [
        ("https://pan.quark.cn/s/abcdef0123456789", "abcdef0123456789", ""),
        ("https://pan.quark.cn/s/abc123?entry=share", "abc123", ""),
        (
            "https://pan.quark.cn/s/abc123#/list/share/0123456789abcdef",
            "abc123",
            "0123456789abcdef",
        ),
        (
            "https://pan.quark.cn/s/abc123/0123456789abcdef0123-%E7%94%B5%E5%BD%B1",
            "abc123",
            "0123456789abcdef0123",
        ),
        (
            "https://pan.quark.cn/s/abc123/0123456789abcdef0123",
            "abc123",
            "0123456789abcdef0123",
        ),
    ],
)
def test_parse_share_url(url, expected_id, expected_fid):
    share_id, folder_fid = make_client(url).parse_share_url()
    assert share_id == expected_id
    assert folder_fid == expected_fid


def test_parse_share_url_rejects_invalid():
    with pytest.raises(RuntimeError):
        make_client("https://pan.quark.cn/web/share").parse_share_url()


def test_parse_share_url_fragment_overrides_subdir():
    url = "https://pan.quark.cn/s/abc123/0123456789abcdef0123-xx#/list/share/ffffffffffffffff"
    _, folder_fid = make_client(url).parse_share_url()
    assert folder_fid == "ffffffffffffffff"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0123456789abcdef0123", "0123456789abcdef0123"),
        ("0", "0"),
        # fid-名称 形态，只有前半段确实是 fid 时才截断
        ("0123456789abcdef0123-目录名", "0123456789abcdef0123"),
        ("abc-def", "abc-def"),
        ("我的-目录", "我的-目录"),
        ("dir/sub/0123456789abcdef0123", "0123456789abcdef0123"),
    ],
)
def test_normalize_my_fid(raw, expected):
    assert make_client("https://pan.quark.cn/s/abc")._normalize_my_fid(raw) == expected
