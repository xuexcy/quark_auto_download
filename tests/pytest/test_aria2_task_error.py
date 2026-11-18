# -*- coding: utf-8 -*-
"""Aria2 任务错误的人话翻译 + 远端下载目录。

真实事故：远端 aria2 的 /downloads 只读，提交成功后 aria2 只回 "Download aborted."
（errorCode=16），页面上表现为「任务已提交但速度一直 0」，一次文件白重试 3 轮。
"""

import logging

import pytest

from aria2_api import Aria2Client, describe_task_error
from error_classify import classify_error


def make_remote_client(remote_dir: str = "") -> Aria2Client:
    return Aria2Client(
        host="https://friend:6800/jsonrpc",
        secret="token",
        download_dir="",
        request_timeout=10,
        download_headers=None,
        poll_interval=1,
        download_timeout=10,
        logger=logging.getLogger("test"),
        remote=True,
        remote_dir=remote_dir,
    )


def test_describe_error_16_explains_unwritable_dir():
    task = {
        "status": "error",
        "errorCode": "16",
        "errorMessage": "Download aborted.",
        "files": [{"path": "/downloads/198 4K.mp4"}],
    }
    text = describe_task_error(task)
    assert "无法在下载目录创建/写入文件" in text
    assert "errorCode=16" in text
    assert "/downloads/198 4K.mp4" in text


def test_describe_error_18_explains_missing_subdir():
    text = describe_task_error({"errorCode": 18, "errorMessage": "Download aborted."})
    assert "无法创建下载子目录" in text


def test_describe_error_unknown_code_keeps_message():
    assert describe_task_error({"errorCode": 0, "errorMessage": "boom"}) == "boom"
    assert describe_task_error({}) == "未知错误"


@pytest.mark.parametrize("code", [16, 18])
def test_write_errors_are_permanent(code):
    text = describe_task_error({"errorCode": code, "errorMessage": "Download aborted."})
    assert classify_error(text) == "permanent"


def test_other_aria2_errors_stay_retryable():
    # 网络类错误必须仍然可重试，别被「不可写」的规则连坐
    assert classify_error(describe_task_error({"errorCode": 6, "errorMessage": "net"})) == "temporary"
    assert classify_error("Download aborted.") == "temporary"


def test_remote_dir_is_passed_as_dir():
    client = make_remote_client("/data/downloads")
    calls: list = []

    def fake(method, params):
        calls.append((method, params))
        return "gid-1"

    client._aria2_call = fake  # type: ignore[assignment]
    client.aria2_add_url("https://dl.example/a.mkv", "Show/S01E01.mkv")
    options = calls[0][1][1]
    assert options["dir"] == "/data/downloads/Show"
    assert options["out"] == "Show/S01E01.mkv"


def test_remote_without_dir_keeps_server_default():
    client = make_remote_client("")
    calls: list = []
    client._aria2_call = lambda method, params: calls.append((method, params)) or "gid-1"  # type: ignore[assignment]
    client.aria2_add_url("https://dl.example/a.mkv", "a.mkv")
    options = calls[0][1][1]
    assert "dir" not in options


def test_remote_dir_absolute_path_join():
    client = make_remote_client("/data/downloads/")
    calls: list = []
    client._aria2_call = lambda method, params: calls.append((method, params)) or "gid-1"  # type: ignore[assignment]
    client.aria2_add_url("https://dl.example/a.mkv", "a.mkv")
    # 尾部斜杠不该出现 "//"
    assert calls[0][1][1]["dir"] == "/data/downloads"


def test_remote_dir_round_trips_through_profiles():
    from aria2_profiles import merge_incoming, options_for, profiles_of

    config = {
        "aria2_profiles": [
            {
                "name": "灰机nas",
                "aria2_host": "https://dl.example:6800/jsonrpc",
                "aria2_secret": "s3cret",
                "aria2_remote": True,
                "aria2_remote_dir": "/data/downloads",
            }
        ],
        "aria2_default": "灰机nas",
    }
    profile = profiles_of(config)[0]
    assert profile["aria2_remote_dir"] == "/data/downloads"
    options = options_for(config, "灰机nas")
    assert options["aria2_remote_dir"] == "/data/downloads"
    assert options["aria2_remote"] is True

    merged = merge_incoming(config, [
        {
            "name": "灰机nas",
            "aria2_host": "https://dl.example:6800/jsonrpc",
            "aria2_secret": "****",
            "aria2_remote": True,
            "aria2_remote_dir": "/data/downloads",
        }
    ])
    # 掩码 secret 沿用旧值，远端目录原样保留
    assert merged[0]["aria2_secret"] == "s3cret"
    assert merged[0]["aria2_remote_dir"] == "/data/downloads"


def test_aria2_target_exposes_remote_fields(monkeypatch):
    """测试接口靠 aria2_target() 取已保存配置，远端标记/目录也要一起给出来。"""
    import jobs_manager

    config = {
        "aria2_profiles": [
            {
                "name": "灰机nas",
                "aria2_host": "https://dl.example:6800/jsonrpc",
                "aria2_secret": "s3cret",
                "aria2_remote": True,
                "aria2_remote_dir": "/var/tmp/quark",
            }
        ],
        "aria2_default": "灰机nas",
    }
    monkeypatch.setattr(jobs_manager, "load_section", lambda name: dict(config))
    target = jobs_manager.aria2_target("灰机nas")
    assert target["remote"] is True
    assert target["remote_dir"] == "/var/tmp/quark"
    assert target["host"] == "https://dl.example:6800/jsonrpc"
