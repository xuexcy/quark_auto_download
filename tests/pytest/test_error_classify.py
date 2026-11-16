"""错误分级：数字类标记必须有边界/HTTP 上下文，不能被 size 数值误命中。"""

import pytest

from error_classify import classify_error


@pytest.mark.parametrize(
    "message",
    [
        "size=1401 bytes",
        "5000000 bytes",
        "文件大小 1401 与远端不一致",
        "",
        "未知异常",
    ],
)
def test_size_numbers_are_not_permanent(message):
    assert classify_error(message) == "temporary"


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 401 Unauthorized",
        "status=403",
        "status_code: 404",
        "permission denied",
        "对象不存在",
        "转存失败：未获得 fid",
        # 夸克 23018：单文件超过账号可下载上限，重试无用
        "夸克拒绝下发下载直链（23018 单文件大小超限）：fid=abc",
        "download file size limit[abc]",
    ],
)
def test_permanent_markers(message):
    assert classify_error(message) == "permanent"


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 500",
        "status=429",
        "read timeout",
        "connection reset by peer",
        "服务繁忙",
    ],
)
def test_temporary_markers(message):
    assert classify_error(message) == "temporary"


def test_exception_input_is_supported():
    assert classify_error(RuntimeError("permission denied")) == "permanent"
