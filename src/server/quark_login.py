# -*- coding: utf-8 -*-
"""夸克扫码登录：CAS 二维码 → service_ticket → Cookie。

流程（2026-11-16 匿名实测，第 1、2、3 步已跑通；第 4 步换 Cookie 需要真人扫码验证）：

1. GET  /cas/ajax/getTokenForQrcodeLogin          → members.token
2. 把 token 拼成扫码地址，渲染成二维码给手机夸克 App 扫
3. GET  /cas/ajax/getServiceTicketByQrcodeToken   → members.service_ticket
4. 用 service_ticket 走网页版登录链路换 __pus / __puus：
   GET pan.quark.cn/account/info?st=<ticket>&lw=scan → user_login_method_info → auth/pc/flush
   （2026-11-16 实测：/cas/login?code=... 只是登录页，不会发 Cookie）

设计要点：
- 状态全部放内存，进程重启即失效（二维码本来也只有 3 分钟寿命）；
- 二维码渲染用 segno（纯 Python，无 Pillow 依赖），前端直接内联 SVG；
- 换 Cookie 的端点没有公开文档，这里有多个候选，逐个尝试，谁先返回 __pus 就用谁，
  全部失败时把候选与回包摘要带回去，方便真人扫码时定位问题。
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlencode

import requests

try:  # 可选依赖：没装 segno 时退化成只给扫码链接
    import segno  # type: ignore
except Exception:  # pragma: no cover - 环境缺依赖的分支
    segno = None  # type: ignore

CLIENT_ID = "532"
# 与网页版 scanLoginPage 调用里的 uc_biz_str 完全一致
UC_BIZ_STR = "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0"

TOKEN_URL = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin"
POLL_URL = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken"
LOGIN_URL = "https://uop.quark.cn/cas/login"
SCAN_PAGE = "https://su.quark.cn/4_eMHBJ"
MEMBER_URL = "https://pan.quark.cn/1/clouddrive/member"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 状态常量，前端按字符串判断
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"

QR_TTL = 180.0        # 二维码有效期（秒）
SESSION_LIMIT = 32    # 内存里最多保留多少个会话

# service_ticket → Cookie 的登录链路（必须在同一个 requests.Session 里按序走完）。
# "{ticket}" 是占位符，真正发请求时替换成 service_ticket。
EXCHANGE_STEPS: tuple[tuple[str, dict[str, str]], ...] = (
    ("https://uop.quark.cn/", {}),
    ("https://pan.quark.cn/", {}),
    ("https://pan.quark.cn/account/info", {"st": "{ticket}", "lw": "scan"}),
    (
        "https://pan.quark.cn/account/user_login_method_info",
        {"fr": "pc", "platform": "pc"},
    ),
    (
        "https://drive-pc.quark.cn/1/clouddrive/auth/pc/flush",
        {"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
    ),
)

_UNSET = object()


def _new_request_id() -> str:
    """CAS 接口的 request_id，随便一个 uuid 形状的串即可。"""
    return secrets.token_hex(16)


def _short_url(url: str) -> str:
    """诊断用的短 URL：只留 host + path，去掉查询串。"""
    stripped = url.split("://", 1)[-1]
    return stripped.split("?", 1)[0]


def build_scan_content(token: str) -> str:
    """二维码里真正要编码的内容（手机夸克 App 扫这个 URL）。"""
    query = urlencode(
        {
            "token": token,
            "client_id": CLIENT_ID,
            "ssb": "weblogin",
            "uc_param_str": "",
            "uc_biz_str": UC_BIZ_STR,
        }
    )
    return f"{SCAN_PAGE}?{query}"


def mask_cookie(cookie: str) -> str:
    """掩码展示：只留头尾，避免把整串 Cookie 回显到页面/日志里。"""
    text = (cookie or "").strip()
    if not text:
        return ""
    if len(text) <= 24:
        return text[:6] + "…"
    return f"{text[:12]}…{text[-8:]}"


def _cookie_header_from_jar(jar: Any) -> str:
    """把 requests 的 CookieJar 拼成 Cookie 头，__pus/__puus 排前面。

    跳过域名不属于 quark.cn 的 Cookie，以及 _UP_* 这类页面态 Cookie。
    """
    picked: dict[str, str] = {}
    for item in jar:
        domain = str(getattr(item, "domain", "") or "")
        if "quark.cn" not in domain:
            continue
        name = str(getattr(item, "name", "") or "")
        if not name or name.startswith("_UP_"):
            continue
        value = str(getattr(item, "value", "") or "")
        if value:
            picked[name] = value
    if not picked:
        return ""
    priority = ["__pus", "__puus", "__kp", "__uid", "kps", "sign", "vcode"]
    ordered = [n for n in priority if n in picked]
    ordered += [n for n in picked if n not in ordered]
    return "; ".join(f"{name}={picked[name]}" for name in ordered)


def validate_cookie(cookie: str, timeout: float = 10.0) -> dict[str, Any]:
    """用 /1/clouddrive/member 校验 Cookie 是否可用，顺手取昵称。"""
    text = (cookie or "").strip()
    if not text:
        return {"ok": False, "message": "Cookie 为空"}
    headers = {
        "Cookie": text,
        "User-Agent": USER_AGENT,
        "Referer": "https://pan.quark.cn/",
    }
    try:
        resp = requests.get(
            MEMBER_URL,
            params={"pr": "ucpro", "fr": "pc"},
            headers=headers,
            timeout=timeout,
        )
    except Exception as e:  # 网络异常
        return {"ok": False, "message": f"请求失败: {e}"}
    try:
        payload = resp.json()
    except Exception:
        return {"ok": False, "message": f"响应不是 JSON（HTTP {resp.status_code}）"}
    status = payload.get("status")
    # 夸克的成功码有的是 200，有的是 0
    if resp.status_code == 200 and status in (200, 0):
        data = payload.get("data") or {}
        nickname = ""
        if isinstance(data, dict):
            nickname = str(data.get("nickname") or data.get("nick_name") or "")
        return {"ok": True, "message": "Cookie 有效", "nickname": nickname}
    message = str(payload.get("message") or payload.get("code") or "未知错误")
    return {"ok": False, "message": f"Cookie 无效: {message}"}


_SVG_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)


def _with_viewbox(svg: str) -> str:
    """给 segno 的 SVG 补 viewBox。

    segno 的 ``svg_inline`` 只写绝对 ``width``/``height``、没有 ``viewBox``，
    浏览器就按原始像素绘制图案，再由容器的 ``overflow: hidden`` 裁掉四周，
    表现是「二维码显示不全」。改成 viewBox 之后 CSS 的 100% 才真正缩放图案。
    """
    match = _SVG_ROOT_RE.search(svg)
    if not match:
        return svg
    root = match.group(0)
    if "viewbox" in root.lower():
        return svg
    width = re.search(r'\bwidth="([\d.]+)"', root)
    height = re.search(r'\bheight="([\d.]+)"', root)
    if not width or not height:
        return svg
    attrs = re.sub(r'\s(?:width|height)="[^"]*"', "", root)
    attrs = attrs[: -1].rstrip().rstrip("/").rstrip()
    new_root = f'{attrs} viewBox="0 0 {width.group(1)} {height.group(1)}">'
    return svg[: match.start()] + new_root + svg[match.end() :]


def render_qr_svg(content: str, scale: int = 5) -> str:
    """生成内联 SVG；没有 segno 时返回空串，前端会退化成显示链接。"""
    if segno is None:
        return ""
    qr = segno.make(content, error="m")
    svg = qr.svg_inline(scale=scale, border=2, dark="#0f1412", light="#ffffff")
    return _with_viewbox(svg)


class _Session:
    def __init__(self, token: str) -> None:
        self.session_id = secrets.token_urlsafe(16)
        self.token = token
        self.content = build_scan_content(token)
        self.created_at = time.time()
        self.status = STATUS_PENDING
        self.message = "等待扫码"
        self.cookie = ""
        self.nickname = ""
        self.saved = False

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > QR_TTL

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "message": self.message,
            "scan_url": self.content,
            "expires_in": max(0, int(QR_TTL - (time.time() - self.created_at))),
            "cookie_masked": mask_cookie(self.cookie),
            "nickname": self.nickname,
            "saved": self.saved,
        }


class QuarkQrLogin:
    """扫码登录会话管理（进程内，线程安全）。"""

    def __init__(self, logger: logging.Logger | None = None, timeout: float = 10.0) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.timeout = timeout
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    # ---------- 对外接口 ----------

    def start(self) -> dict[str, Any]:
        """申请 token 并渲染二维码。"""
        token = self._fetch_token()
        sess = _Session(token)
        with self._lock:
            self._gc_locked()
            self._sessions[sess.session_id] = sess
        self.logger.info("扫码登录：已生成二维码 session=%s", sess.session_id)
        payload = sess.public()
        payload["qr_svg"] = render_qr_svg(sess.content)
        return payload

    def status(self, session_id: str) -> dict[str, Any] | None:
        """查一次状态；会话不存在返回 None。"""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            return None
        if sess.status in (STATUS_CONFIRMED, STATUS_FAILED):
            return sess.public()
        if sess.expired:
            sess.status = STATUS_EXPIRED
            sess.message = "二维码已过期，请刷新"
            return sess.public()
        self._poll_once(sess)
        return sess.public()

    def cookie_of(self, session_id: str) -> str:
        """取换到的原始 Cookie（仅授权成功时非空），供 Web 层落盘。"""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None or sess.status != STATUS_CONFIRMED:
            return ""
        return sess.cookie

    def mark_saved(self, session_id: str) -> None:
        """标记 Cookie 已写入配置，避免前端每次轮询都重复落盘。"""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess.saved = True

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    # ---------- 内部实现 ----------

    def _gc_locked(self) -> None:
        now = time.time()
        stale = [
            sid
            for sid, sess in self._sessions.items()
            if now - sess.created_at > QR_TTL * 4
        ]
        for sid in stale:
            self._sessions.pop(sid, None)
        if len(self._sessions) > SESSION_LIMIT:
            for sid, _ in sorted(self._sessions.items(), key=lambda kv: kv[1].created_at)[
                : len(self._sessions) - SESSION_LIMIT
            ]:
                self._sessions.pop(sid, None)

    def _fetch_token(self) -> str:
        resp = requests.get(
            TOKEN_URL,
            params={"client_id": CLIENT_ID, "v": "1.2", "request_id": _new_request_id()},
            headers={"User-Agent": USER_AGENT, "Referer": "https://pan.quark.cn/"},
            timeout=self.timeout,
        )
        payload = resp.json()
        if payload.get("status") != 2000000:
            raise RuntimeError(f"申请二维码失败: {payload.get('message') or payload}")
        members = (payload.get("data") or {}).get("members") or {}
        token = str(members.get("token") or "")
        if not token:
            raise RuntimeError("申请二维码失败: 响应缺少 token")
        return token

    def _poll_once(self, sess: _Session) -> None:
        try:
            resp = requests.get(
                POLL_URL,
                params={
                    "client_id": CLIENT_ID,
                    "v": "1.2",
                    "token": sess.token,
                    "request_id": _new_request_id(),
                },
                headers={"User-Agent": USER_AGENT, "Referer": "https://pan.quark.cn/"},
                timeout=self.timeout,
            )
            payload = resp.json()
        except Exception as e:
            sess.message = f"轮询失败，稍后重试: {e}"
            return

        status = payload.get("status")
        if status == 50004001:  # 还没扫 / 还没确认
            sess.message = "等待扫码"
            return
        if status == 50004002:  # token 失效
            sess.status = STATUS_EXPIRED
            sess.message = "二维码已失效，请刷新"
            return
        if status != 2000000:
            sess.message = f"等待确认（{payload.get('message') or status}）"
            return

        members = (payload.get("data") or {}).get("members") or {}
        ticket = str(members.get("service_ticket") or "")
        if not ticket:
            sess.status = STATUS_FAILED
            sess.message = "已确认登录但没拿到 service_ticket"
            return

        sess.message = "已确认，正在换取 Cookie"
        cookie, detail = self._exchange_ticket(ticket)
        if not cookie:
            sess.status = STATUS_FAILED
            sess.message = f"换取 Cookie 失败：{detail}"
            self.logger.warning("扫码登录：换取 Cookie 失败 %s", detail)
            return
        info = validate_cookie(cookie)
        if not info.get("ok"):
            sess.status = STATUS_FAILED
            sess.message = f"换到的 Cookie 校验不通过：{info.get('message')}"
            self.logger.warning("扫码登录：Cookie 校验失败 %s", info.get("message"))
            return
        sess.cookie = cookie
        sess.nickname = str(info.get("nickname") or "")
        sess.status = STATUS_CONFIRMED
        sess.message = "登录成功"
        self.logger.info("扫码登录：成功，昵称=%s", sess.nickname or "(未知)")

    def _exchange_ticket(self, ticket: str) -> tuple[str, str]:
        """用 service_ticket 换 Cookie（网页版扫码登录链路）。

        实测（2026-11-16）：`/cas/login?client_id=532&code=<ticket>` 只会返回登录页 HTML，
        真正发 Cookie 的是 pan 自己的账号链路，且必须在**同一个会话**里按顺序走完：

        1. GET https://uop.quark.cn/                                 建立 uop 会话
        2. GET https://pan.quark.cn/                                 建立 pan 会话
        3. GET https://pan.quark.cn/account/info?st=<ticket>&lw=scan  用 ticket 登录
        4. GET https://pan.quark.cn/account/user_login_method_info   补齐登录方式信息
        5. GET https://drive-pc.quark.cn/1/clouddrive/auth/pc/flush   换成网盘侧 __pus/__puus
        """
        session = requests.Session()
        notes: list[str] = []
        for url, params in EXCHANGE_STEPS:
            query = {k: (ticket if v == "{ticket}" else v) for k, v in params.items()}
            try:
                resp = session.get(
                    url,
                    params=query or None,
                    headers={"User-Agent": USER_AGENT, "Referer": "https://pan.quark.cn/"},
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            except Exception as e:
                notes.append(f"{_short_url(url)} 异常: {e}")
                continue
            notes.append(f"{_short_url(url)} HTTP {resp.status_code}")
        names = {c.name for c in session.cookies}
        cookie = _cookie_header_from_jar(session.cookies)
        if cookie and (names & {"__pus", "__puus"}):
            return cookie, "ok"
        trail = " -> ".join(notes)
        return "", f"未拿到 __pus/__puus（{trail}）；cookies={sorted(names)[:8]}"
