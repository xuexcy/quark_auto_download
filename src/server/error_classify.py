"""错误分级：永久错误立即出局，临时错误进入重试旁路。

标记一律用正则 + 边界匹配：旧实现是裸子串，"401" 会命中 size=1401，
"500" 会命中 "5000000 bytes"，把可重试的错误误判成永久错误。
"""

from __future__ import annotations

import re
from typing import Literal


ErrorKind = Literal["permanent", "temporary"]


_PERMANENT_PATTERNS = (
    r"未找到文件",
    r"\bnot\s+found\b",
    r"\bobject\s+not\s+found\b",
    r"不存在",
    r"非法相对路径",
    r"\billegal\b",
    r"\bpermission\s+denied\b",
    r"\bforbidden\b",
    r"\bunauthorized\b",
    r"转存失败：未获得\s*fid",
    # 夸克 23018：单文件超过账号可下载上限，重试无用
    r"\b23018\b",
    r"download\s+file\s+size\s+limit",
    r"\bhttp\s*(?:error\s*)?(?:status\s*)?[=:]?\s*(?:401|403|404)\b",
    r"\bstatus(?:_code)?\s*[=:]\s*(?:401|403|404)\b",
)

_TEMPORARY_PATTERNS = (
    r"\btimeout\b",
    r"\btimed\s*out\b",
    r"\btime-out\b",
    r"\bconnection\b",
    r"\btemporarily\b",
    r"connection\s+reset",
    r"connection\s+aborted",
    r"broken\s+pipe",
    r"too\s+many\s+requests",
    # 夸克 32003：网盘容量不足/转存被容量拦下，腾出空间后可重试
    r"\b32003\b",
    r"capacity\s+limit",
    r"容量不足",
    r"服务繁忙",
    r"\beof\b",
    r"\bhttp\s*(?:error\s*)?(?:status\s*)?[=:]?\s*(?:429|5\d{2})\b",
    r"\bstatus(?:_code)?\s*[=:]\s*(?:429|5\d{2})\b",
)

_PERMANENT_RE = tuple(re.compile(pattern) for pattern in _PERMANENT_PATTERNS)
_TEMPORARY_RE = tuple(re.compile(pattern) for pattern in _TEMPORARY_PATTERNS)


def classify_error(error: BaseException | str) -> ErrorKind:
    """
    分类提交/转存错误。
    - permanent: 重试无意义（如文件名对不上），应立即跳过/出局
    - temporary: 可稍后重试（超时、网络、短暂未就绪）

    数字类标记（401/403/429/5xx）必须带边界或 HTTP 上下文，避免误命中 size 数值。
    """
    message = str(error or "").lower()
    if not message:
        return "temporary"
    for pattern in _PERMANENT_RE:
        if pattern.search(message):
            return "permanent"
    for pattern in _TEMPORARY_RE:
        if pattern.search(message):
            return "temporary"
    # 未知错误按临时处理，有次数上限，避免永久堵死；也不会单次卡很久
    return "temporary"
