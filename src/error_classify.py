"""错误分级：永久错误立即出局，临时错误进入重试旁路。"""

from __future__ import annotations

from typing import Literal


ErrorKind = Literal["permanent", "temporary"]


_PERMANENT_MARKERS = (
    "未找到文件",
    "not found",
    "object not found",
    "不存在",
    "非法相对路径",
    "illegal",
    "permission denied",
    "forbidden",
    "unauthorized",
    "401",
    "403",
    "转存失败：未获得 fid",
)

_TEMPORARY_MARKERS = (
    "timeout",
    "timed out",
    "time-out",
    "connection",
    "temporarily",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "429",
    "500",
    "502",
    "503",
    "504",
    "服务繁忙",
    "too many requests",
    "eof",
)


def classify_error(error: BaseException | str) -> ErrorKind:
    """
    分类提交/转存错误。
    - permanent: 重试无意义（如文件名对不上），应立即跳过/出局
    - temporary: 可稍后重试（超时、网络、短暂未就绪）
    """
    message = str(error or "").lower()
    for marker in _PERMANENT_MARKERS:
        if marker.lower() in message:
            return "permanent"
    for marker in _TEMPORARY_MARKERS:
        if marker.lower() in message:
            return "temporary"
    # 未知错误按临时处理，有次数上限，避免永久堵死；也不会单次卡很久
    return "temporary"
