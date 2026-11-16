"""转存任务轮询：必须真正读出 data.status（曾因漏写赋值导致 NameError）。"""

import logging

import pytest

import quark_api as qa
from error_classify import classify_error


def make_client(task_poll_interval: int = 1, task_timeout: int = 5) -> qa.QuarkClient:
    return qa.QuarkClient(
        cookie="__pus=x",
        request_timeout=5,
        save_to_dir="",
        share_url="",
        share_pwd="",
        logger=logging.getLogger("test-quark-task"),
        task_poll_interval=task_poll_interval,
        task_timeout=task_timeout,
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """轮询里的 sleep 全部跳过，测试不真的等秒。"""
    monkeypatch.setattr(qa.time, "sleep", lambda *_: None)


def test_status_2_returns_save_as_fids(monkeypatch):
    client = make_client()
    monkeypatch.setattr(
        client,
        "quark_get",
        lambda url, params: {
            "status": 200,
            "code": 0,
            "data": {"status": 2, "save_as": {"save_as_top_fids": ["fid-a"]}},
        },
    )
    # 这里是回归点：漏写 status = data_node.get("status") 会抛 NameError
    assert client._wait_task_and_get_fids("task-1") == ["fid-a"]


def test_status_2_falls_back_to_save_as_fids(monkeypatch):
    client = make_client()
    monkeypatch.setattr(
        client,
        "quark_get",
        lambda url, params: {
            "status": 200,
            "code": 0,
            "data": {"status": 2, "save_as": {"save_as_fids": ["fid-b"]}},
        },
    )
    assert client._wait_task_and_get_fids("task-2") == ["fid-b"]


def test_status_3_raises_transfer_failed(monkeypatch):
    client = make_client()
    monkeypatch.setattr(client, "quark_get", lambda url, params: {"status": 200, "code": 0, "data": {"status": 3}})
    with pytest.raises(RuntimeError) as err:
        client._wait_task_and_get_fids("task-3")
    assert "转存任务失败" in str(err.value)


def test_capacity_limit_is_explained(monkeypatch):
    client = make_client()
    monkeypatch.setattr(
        client,
        "quark_get",
        lambda url, params: {
            "status": 400,
            "code": 32003,
            "message": "capacity limit[0]",
            "data": {"remain_capacity": 1658928801},
        },
    )
    with pytest.raises(RuntimeError) as err:
        client._wait_task_and_get_fids("task-4")
    text = str(err.value)
    assert "容量不足" in text and "32003" in text and "1582 MB" in text


def test_missing_status_gives_up_after_limit(monkeypatch):
    client = make_client()
    monkeypatch.setattr(client, "quark_get", lambda url, params: {"status": 200, "code": 0, "data": {}})
    with pytest.raises(RuntimeError) as err:
        client._wait_task_and_get_fids("task-5")
    assert "结构异常" in str(err.value)


def test_capacity_limit_is_temporary_error():
    assert classify_error("查询转存任务状态返回异常: {'code': 32003, 'message': 'capacity limit[0]'}") == "temporary"
