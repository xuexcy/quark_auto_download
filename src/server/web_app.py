# -*- coding: utf-8 -*-
"""Quark Auto Download Web 服务（多分享链接）。

安全约定：
- 默认监听 127.0.0.1（只有本机能访问）；用环境变量 QUARK_AUTO_DL_WEB_HOST 改成
  0.0.0.0/局域网地址时，若未配置密码会拒绝启动（除非设置
  QUARK_AUTO_DL_WEB_ALLOW_INSECURE=1）。
- 默认不需要登录；在 conf/web.yaml 配置 web_password（或设置环境变量
  QUARK_AUTO_DL_WEB_PASSWORD）后，所有接口/页面都要求 HTTP Basic 认证。
- 所有请求都校验 Host 白名单（默认 localhost/127.0.0.1/::1 + 监听地址；可用
  QUARK_AUTO_DL_WEB_ALLOWED_HOSTS 追加），防 DNS rebinding。
- 非 GET 请求校验 Origin/Referer 与 Host 同源，避免跨站请求触发启停/重启。
- 不暴露 /docs、/redoc、/openapi.json。
- /api/jobs 不下发 share_pwd（提取码），只下发 share_pwd_set。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config_loader import BASE_DIR, load_merged_configs, load_web_settings
import jobs_manager as jm
from log_util import configure_logging, dated_web_log_path
from quark_api import QuarkClient
from quark_login import QuarkQrLogin, mask_cookie, validate_cookie
import supervisor as sup

WEB_DIR = os.path.join(BASE_DIR, "src", "web")
STATIC_DIR = os.path.join(WEB_DIR, "static")
RESTART_SCRIPT = os.path.join(BASE_DIR, "bin", "restart.sh")
WEB_LOG_PATH = (
    os.getenv("QUARK_AUTO_DL_WEB_LOG_FILE", "").strip() or dated_web_log_path()
)

WEB_SETTINGS = load_web_settings()
WEB_USERNAME = (
    os.getenv("QUARK_AUTO_DL_WEB_USER", "").strip()
    or str(WEB_SETTINGS.get("web_username") or "").strip()
    or "admin"
)
WEB_PASSWORD = (
    os.getenv("QUARK_AUTO_DL_WEB_PASSWORD", "").strip()
    or str(WEB_SETTINGS.get("web_password") or "").strip()
)
AUTH_ENABLED = bool(WEB_PASSWORD)
AUTH_EXEMPT_PATHS = {"/api/health"}

# 监听地址：默认只听回环，想给局域网/公网用必须显式设置并自行负责认证
WEB_HOST = os.getenv("QUARK_AUTO_DL_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
WEB_PORT = int(os.getenv("QUARK_AUTO_DL_WEB_PORT", "8787"))

# Host 白名单：防 DNS rebinding（攻击者把域名解析到 127.0.0.1 后让浏览器以"同源"身份打本服务）
ALLOWED_HOSTS = {
    h.strip().lower()
    for h in os.getenv("QUARK_AUTO_DL_WEB_ALLOWED_HOSTS", "").split(",")
    if h.strip()
} | {"localhost", "127.0.0.1", "::1"}
if WEB_HOST not in ("0.0.0.0", "::", ""):
    ALLOWED_HOSTS.add(WEB_HOST.lower())


def _is_loopback_host(hostname: str) -> bool:
    if hostname in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _hostname_of(host_header: str) -> str:
    """从 Host 头里取主机名（去端口、去 IPv6 方括号）。"""
    try:
        return (urlsplit(f"//{host_header}").hostname or "").strip().lower()
    except ValueError:
        return ""


def _host_allowed(host_header: str) -> bool:
    hostname = _hostname_of(host_header)
    if not hostname:
        return False
    return hostname in ALLOWED_HOSTS or _is_loopback_host(hostname)


def _setup_web_logging() -> None:
    """无论 start.sh 还是直接 uvicorn，stdout/stderr/logging 都进 web 日志文件。"""
    configure_logging(WEB_LOG_PATH)
    root_handler = logging.getLogger().handlers[0] if logging.getLogger().handlers else None
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_log = logging.getLogger(name)
        uv_log.handlers.clear()
        if root_handler is not None:
            uv_log.addHandler(root_handler)
        uv_log.setLevel(logging.INFO)
        uv_log.propagate = False


_setup_web_logging()


def _check_basic_auth(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, param = header.partition(" ")
    if scheme.lower() != "basic" or not param:
        return False
    try:
        decoded = base64.b64decode(param).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return False
    username, _, password = decoded.partition(":")
    # 必须用 bytes 比较：compare_digest 对非 ASCII 的 str 直接抛 TypeError，
    # 会让整个站点 500（中文/emoji 口令是很正常的需求）。
    return hmac.compare_digest(
        username.encode("utf-8"), WEB_USERNAME.encode("utf-8")
    ) and hmac.compare_digest(
        password.encode("utf-8"), WEB_PASSWORD.encode("utf-8")
    )


def _netloc_parts(value: str, default_scheme: str = "") -> tuple[str, str]:
    """把 Host / Origin 归一成 (hostname, port)；缺端口时按 scheme 补默认端口。"""
    text = str(value or "").strip()
    if not text:
        return ("", "")
    parts = urlsplit(text if "//" in text else f"//{text}")
    host = (parts.hostname or "").lower()
    if not host:
        return ("", "")
    port = parts.port
    if port is None:
        scheme = (parts.scheme or default_scheme).lower()
        if scheme == "https":
            port = 443
        elif scheme == "http":
            port = 80
    return (host, "" if port is None else str(port))


def _request_is_same_origin(request: Request) -> bool:
    """浏览器跨站请求一定带 Origin（或 Referer）；非浏览器工具两者都没有。

    没有 Host 头的一律拒绝：HTTP/1.1 必须带 Host，缺失只能是手工构造的请求。
    反向代理常把 Host 改写成 host:port、而 Origin 省略默认端口，所以按
    (hostname, port) 归一后比较，而不是逐字比 netloc。
    """
    host = request.headers.get("host", "")
    if not host:
        return False
    host_name, host_port = _netloc_parts(host)
    if not host_name:
        return False
    for header_name in ("origin", "referer"):
        value = request.headers.get(header_name)
        if not value:
            continue
        origin_name, origin_port = _netloc_parts(value)
        if not origin_name:
            return False
        if origin_name != host_name:
            return False
        # Host 没写端口（反代常见）时只比主机名；两边都有端口才要求一致
        return not (host_port and origin_port) or host_port == origin_port
    return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log = logging.getLogger(__name__)
    if not AUTH_ENABLED:
        if not (_is_loopback_host(WEB_HOST) or WEB_HOST.startswith("127.")):
            if os.getenv("QUARK_AUTO_DL_WEB_ALLOW_INSECURE", "").strip() not in ("1", "true", "yes"):
                raise RuntimeError(
                    f"拒绝以无密码方式监听 {WEB_HOST}:{WEB_PORT}：这会把 Cookie/配置管理接口"
                    "暴露给网络上的任何人。请二选一：①设置 conf/web.yaml 的 web_password"
                    "（或环境变量 QUARK_AUTO_DL_WEB_PASSWORD）；②确实要无密码 + 非回环监听，"
                    "设置 QUARK_AUTO_DL_WEB_ALLOW_INSECURE=1。"
                )
        log.warning(
            "Web 未启用登录（conf/web.yaml 的 web_password 为空 / 环境变量 "
            "QUARK_AUTO_DL_WEB_PASSWORD 未设置）：任何能访问本端口的人都可以管理下载任务、"
            "查看/修改 Cookie 等配置。公网或不可信内网请务必设置密码。"
        )
    else:
        log.info("Web 已启用 HTTP Basic 认证，用户名: %s", WEB_USERNAME)
    # 启动时对账：清掉 stop.sh / 进程死后残留的 running 状态，避免页面闪「运行中」
    try:
        jm.list_jobs()
    except Exception:
        log.exception("启动时任务状态对账失败")
    yield


APP_VERSION = "2.8.16"
CODE_ROOT = os.path.dirname(os.path.abspath(__file__))
PROCESS_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
PROCESS_START_TS = time.time()


app = FastAPI(
    title="Quark Auto Download",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def _security_guard(request: Request, call_next):
    path = request.url.path
    if not _host_allowed(request.headers.get("host", "")):
        return JSONResponse(
            {
                "detail": (
                    "Host 不在白名单内，已拒绝请求。若确实需要用该域名/地址访问，"
                    "请设置环境变量 QUARK_AUTO_DL_WEB_ALLOWED_HOSTS=<host1>,<host2> 后重启。"
                )
            },
            status_code=400,
        )
    if AUTH_ENABLED and path not in AUTH_EXEMPT_PATHS and not _check_basic_auth(request):
        return Response(
            content="需要登录",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Quark Auto Download"'},
            media_type="text/plain; charset=utf-8",
        )
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _request_is_same_origin(request):
        return JSONResponse({"detail": "跨站请求已被拒绝"}, status_code=403)
    response = await call_next(request)
    # 页面与 API 都不缓存：部署新版本后刷新即生效。
    # 之前只给 HTML 加 no-store，/api/health 这类 JSON 仍可能被浏览器按启发式规则缓存，
    # 结果页面是新的、版本接口却回旧 JSON，看起来像「后端还是旧版本」。
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


# 分享目录浏览：stoken 缓存 15 分钟，键为 (分享链接, 提取码) 的哈希，不落库
_SHARE_SESSION_CACHE: dict[str, tuple[float, str, str, str]] = {}
_SHARE_SESSION_TTL = 900.0
_SHARE_SESSION_MAX = 64


# 扫码登录会话（进程内，内存态；二维码 3 分钟过期）
_QR_LOGIN = QuarkQrLogin(logging.getLogger(__name__))


def _quark_cookie() -> str:
    try:
        config = load_merged_configs()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取配置失败: {e}") from e
    cookie = str(config.get("quark_cookie") or "").strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="尚未配置夸克 Cookie（设置页 → 夸克网盘）")
    return cookie


# 单文件下载上限打标：默认关闭。
# 23018「download file size limit」实测是 **按 UA 区分** 的（浏览器 UA 才拦，官方客户端 UA 直接给直链），
# 项目默认已改用客户端 UA（见 quark_api.DESKTOP_USER_AGENT），所以正常情况下不会再触发。
# 若你改回浏览器 UA（quark_client_ua），可以把 quark_max_file_size_gb 设成一个数字（如 5）提前提示。
DEFAULT_MAX_FILE_SIZE_GB = 0.0


def _max_download_file_size() -> int:
    """返回单文件下载上限（字节）。0 表示不打标。"""
    try:
        config = load_merged_configs()
    except Exception:
        config = {}
    raw = config.get("quark_max_file_size_gb", DEFAULT_MAX_FILE_SIZE_GB)
    try:
        gb = float(raw)
    except (TypeError, ValueError):
        gb = DEFAULT_MAX_FILE_SIZE_GB
    if gb <= 0:
        return 0
    return int(gb * 1024 * 1024 * 1024)


def _quark_user_agent() -> str:
    """请求 UA：配置了 quark_client_ua 就用它，否则用 QuarkClient 默认的桌面客户端 UA。"""
    try:
        config = load_merged_configs()
    except Exception:
        return ""
    return str(config.get("quark_client_ua") or "").strip()


def _share_context(share_url: str, share_pwd: str) -> tuple[QuarkClient, str, str, str]:
    """返回 (client, stoken, share_id, 浏览根 fid)。浏览根优先取链接里的子目录。"""
    key = hashlib.sha1(f"{share_url}\n{share_pwd}".encode("utf-8")).hexdigest()
    now = time.monotonic()
    hit = _SHARE_SESSION_CACHE.get(key)
    if hit and now - hit[0] < _SHARE_SESSION_TTL:
        stoken, share_id, base_fid = hit[1], hit[2], hit[3]
    else:
        stoken = share_id = base_fid = ""
    client = QuarkClient(
        cookie=_quark_cookie(),
        request_timeout=15,
        save_to_dir="",
        share_url=share_url,
        share_pwd=share_pwd,
        logger=logging.getLogger(__name__),
        user_agent=_quark_user_agent(),
    )
    if not stoken:
        stoken, share_id, root_fid = client.get_share_token()
        _, url_folder_fid = client.parse_share_url()
        base_fid = url_folder_fid or root_fid
        if len(_SHARE_SESSION_CACHE) >= _SHARE_SESSION_MAX:
            _SHARE_SESSION_CACHE.clear()
        _SHARE_SESSION_CACHE[key] = (now, stoken, share_id, base_fid)
    return client, stoken, share_id, base_fid


class ShareTreeRequest(BaseModel):
    share_url: str = Field("", description="给 job_id 时可省略，直接沿用任务上保存的链接")
    share_pwd: str = ""
    # 为空表示分享根目录（或链接里带的子目录）
    pdir_fid: str = ""
    # 指定任务时：用任务上的分享链接/提取码，并给每个条目标注「是否已在任务中」
    job_id: str = ""


class CookieRequest(BaseModel):
    cookie: str = Field(..., min_length=1, description="完整 Cookie 串，如 __pus=...; __puus=...")


class JobSelectionRequest(BaseModel):
    file_fids: list[str] = Field(default_factory=list)
    dir_fids: list[str] = Field(default_factory=list)


class JobSelectionAddRequest(JobSelectionRequest):
    # 跨任务重复时是否也强制加进来（默认拦截，提示「已经添加过了」）
    force: bool = False


class JobSelectionRemoveRequest(JobSelectionRequest):
    """取消勾选：这些 fid 会从任务勾选里移除，对应的单文件状态也一并清掉。"""


class JobFileActionRequest(BaseModel):
    """逐文件操作：op=pause 暂停（进行中的同时暂停 aria2）、resume 开始、delete 从任务里删除。"""

    op: str = Field(..., description="pause | resume | delete")
    # 相对路径（与 pipeline_state.files[].path / aria2 out 一致）
    paths: list[str] = Field(default_factory=list)
    # 文件 fid：删除时一并从任务勾选里摘掉，前端目录树可以重新勾选
    fids: list[str] = Field(default_factory=list)


class ConfigUpdateRequest(BaseModel):
    quark: Optional[dict[str, Any]] = None
    aria2: Optional[dict[str, Any]] = None


class Aria2TestRequest(BaseModel):
    """测试一套 Aria2 配置是否连得上；secret 是掩码时用已保存的值。"""

    name: str = ""
    host: str = ""
    secret: str = ""
    # 远端配置：把「远端下载目录」一起带回来，测试时好说明文件会落到哪。
    # 留 None = 请求里没带，按已保存的同名配置补齐（host 也对得上时）。
    remote: bool | None = None
    remote_dir: str | None = None


class JobCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, description="任务显示名，必填且不可与已有任务重复")
    share_url: str = Field(..., min_length=1)
    share_pwd: str = ""
    # 前端目录树勾选结果；两项都空 = 下载整个分享目录（旧行为）
    selection: Optional[dict[str, list[str]]] = None
    # 用哪套 Aria2 配置（conf/aria2.yaml 的 aria2_profiles），空 = 默认配置
    aria2_profile: str = ""


class JobPauseRequest(BaseModel):
    mode: str = Field("soft", description="soft | hard")


class JobRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, description="新任务显示名，trim 后非空且不可与其它任务重复")


@app.get("/api/health")
def health():
    """健康检查 + 版本自证：返回进程 PID、代码目录、启动时间，
    便于区分「页面是旧缓存」和「后端跑的是旧代码」。

    另外只读地报告「谁在托管这个进程 / 开机自启是否开启」（见 supervisor.py）：
    页面只展示，不提供开关——修改 systemd 配置属于运维操作，不该由 Web 接口代劳。
    """
    runtime = sup.runtime_info()
    return {
        "ok": True,
        "version": APP_VERSION,
        "pid": os.getpid(),
        "started_at": PROCESS_STARTED_AT,
        "uptime_seconds": int(time.time() - PROCESS_START_TS),
        "code_root": CODE_ROOT,
        "python": sys.version.split()[0],
        "supervisor": runtime["supervisor"],
        "unit": runtime["unit"],
        "unit_file_state": runtime["unit_file_state"],
        "autostart": runtime["autostart"],
        "restart_policy": runtime["restart_policy"],
        "restart_hint": runtime["restart_hint"],
    }


@app.get("/api/config")
def api_get_config():
    return jm.get_all_configs(mask_secrets=True)


@app.put("/api/config")
def api_put_config(body: ConfigUpdateRequest):
    try:
        saved = jm.save_all_configs(body.model_dump(exclude_none=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "saved": saved, "config": jm.get_all_configs(mask_secrets=True)}


@app.get("/api/aria2/profiles")
def api_aria2_profiles():
    """全部 Aria2 配置（secret 掩码）+ 默认配置名，供设置页与新增任务下拉使用。"""
    return jm.list_aria2_profiles()


@app.post("/api/aria2/test")
def api_aria2_test(body: Aria2TestRequest):
    """连通性测试：设置页每张配置卡片上的「测试」按钮。"""
    host = (body.host or "").strip()
    secret = body.secret or ""
    name = (body.name or "").strip()
    saved = jm.aria2_target(name)
    if not secret or "*" in secret:
        # 页面上的 secret 是掩码（或刚加的卡片还没填）→ 用已保存的令牌。
        # 但此时 host 只能沿用已保存的配置：否则请求体随便给个地址，
        # 就会把明文令牌发到那台机器上（等于绕过配置接口的掩码）。
        if host and saved["host"] and host.rstrip("/") != saved["host"].rstrip("/"):
            raise HTTPException(
                status_code=400,
                detail="换 RPC 地址时必须重新填写令牌（不能用已保存的令牌测试陌生地址）",
            )
        host = host or saved["host"]
        secret = saved["secret"]
    if not host:
        raise HTTPException(status_code=400, detail="请先填写 RPC Host")
    if urlsplit(host).scheme.lower() not in ("http", "https"):
        raise HTTPException(status_code=400, detail="RPC Host 必须以 http:// 或 https:// 开头")
    # 用 curl / 脚本调这个接口时通常只给 name，远端标记与远端目录要沿用已保存的配置
    same_profile_host = bool(saved["host"]) and host.rstrip("/") == saved["host"].rstrip("/")
    if body.remote is None:
        remote = bool(saved.get("remote")) if same_profile_host else False
    else:
        remote = bool(body.remote)
    if body.remote_dir is None:
        remote_dir = str(saved.get("remote_dir") or "") if same_profile_host else ""
    else:
        remote_dir = str(body.remote_dir or "")
    try:
        info = jm.test_aria2_connection(
            host,
            secret,
            remote=remote,
            remote_dir=remote_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"连接失败: {e}") from e
    return {"ok": True, **info}


def _save_quark_cookie(cookie: str) -> str:
    """写入 conf/quark.yaml，返回掩码后的展示串。"""
    text = (cookie or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Cookie 为空")
    try:
        jm.save_section("quark", {"quark_cookie": text})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"保存配置失败: {e}") from e
    return mask_cookie(text)


@app.post("/api/quark/login/qr")
def api_quark_qr_start():
    """申请二维码：返回 session_id + 内联 SVG，前端自行轮询状态。"""
    try:
        payload = _QR_LOGIN.start()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"申请二维码失败: {e}") from e
    return payload


@app.get("/api/quark/login/qr/{session_id}")
def api_quark_qr_status(session_id: str):
    """轮询扫码状态；确认成功后自动写入配置（只写一次）。"""
    payload = _QR_LOGIN.status(session_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="扫码会话不存在或已过期，请重新生成二维码")
    if payload.get("status") == "confirmed" and not payload.get("saved"):
        cookie = _QR_LOGIN.cookie_of(session_id)
        if cookie:
            payload["cookie_masked"] = _save_quark_cookie(cookie)
            _QR_LOGIN.mark_saved(session_id)
            payload["saved"] = True
    return payload


@app.post("/api/quark/cookie")
def api_quark_cookie(body: CookieRequest):
    """手工粘贴 Cookie：先校验再落盘，避免存进去一个死 Cookie。"""
    info = validate_cookie(body.cookie)
    if not info.get("ok"):
        raise HTTPException(status_code=400, detail=str(info.get("message") or "Cookie 无效"))
    masked = _save_quark_cookie(body.cookie)
    return {
        "ok": True,
        "cookie_masked": masked,
        "nickname": info.get("nickname") or "",
        "message": info.get("message") or "Cookie 已保存",
    }


@app.post("/api/quark/cookie/test")
def api_quark_cookie_test():
    """校验当前配置里的 Cookie 是否还有效。"""
    info = validate_cookie(_quark_cookie())
    return info


@app.get("/api/jobs")
def api_list_jobs():
    return {"jobs": jm.list_jobs()}


@app.post("/api/share/tree")
def api_share_tree(body: ShareTreeRequest):
    """浏览分享目录（单层），给前端目录树逐层展开用。

    带 job_id 时：链接/提取码取任务上的，并给每个条目补 `taken` 字段
    （同一分享下已被勾选或下载过的文件 → 前端打标 + 禁止重复勾选）。
    """
    job = None
    if body.job_id.strip():
        job = jm.get_job(body.job_id.strip())
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        share_url = str(job.get("share_url") or "").strip()
        share_pwd = str(job.get("share_pwd") or "")
    else:
        share_url = body.share_url.strip()
        share_pwd = (body.share_pwd or "").strip()
    if "/s/" not in share_url:
        raise HTTPException(status_code=400, detail="分享链接格式不正确（应包含 /s/）")
    try:
        client, stoken, share_id, base_fid = _share_context(share_url, share_pwd)
        target_fid = (body.pdir_fid or "").strip() or base_fid
        entries = client.list_share_dir_entries(share_id, stoken, target_fid)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if job is not None:
        usage = jm.share_usage(str(job.get("id")))
        for entry in entries:
            fid = str(entry.get("fid") or "")
            entry["taken"] = usage.get(fid)

    limit = _max_download_file_size()
    for entry in entries:
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            # 分享接口偶尔把 size 给成字符串/空值，别让整个目录树 500
            size = 0
        entry["oversize"] = bool(limit and not entry.get("is_dir") and size >= limit)
    return {
        "share_id": share_id,
        "root_fid": base_fid,
        "pdir_fid": target_fid,
        "is_root": target_fid == base_fid,
        "max_file_size": limit,
        "entries": entries,
    }


@app.post("/api/jobs/{job_id}/selection/add")
def api_add_job_selection(job_id: str, body: JobSelectionAddRequest):
    """追加勾选（可反复调用）：只加新 fid，重复的返回 duplicates 供前端提示。"""
    try:
        result = jm.add_job_selection(
            job_id,
            {"file_fids": body.file_fids, "dir_fids": body.dir_fids},
            force=body.force,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "job": jm.refresh_job_runtime(result["job"]),
        "added": result["added"],
        "duplicates": result["duplicates"],
        "already_in_job": result["already_in_job"],
    }


@app.post("/api/jobs/{job_id}/selection/remove")
def api_remove_job_selection(job_id: str, body: JobSelectionRemoveRequest):
    """取消勾选（可从目录树或「文件进度」里删）：删掉后可以重新勾选。"""
    try:
        result = jm.remove_job_selection(
            job_id,
            {"file_fids": body.file_fids, "dir_fids": body.dir_fids},
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "job": jm.refresh_job_runtime(result["job"]),
        "removed": result["removed"],
        "state_files_removed": result["state_files_removed"],
    }


@app.put("/api/jobs/{job_id}/selection")
def api_set_job_selection(job_id: str, body: JobSelectionRequest):
    try:
        job = jm.set_job_selection(job_id, body.model_dump())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"job": jm.refresh_job_runtime(job)}


@app.post("/api/jobs")
def api_create_job(body: JobCreateRequest):
    try:
        job = jm.add_job(
            body.share_url,
            body.share_pwd,
            name=body.name,
            selection=body.selection,
            aria2_profile=body.aria2_profile,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"job": jm.refresh_job_runtime(job)}


@app.patch("/api/jobs/{job_id}")
def api_rename_job(job_id: str, body: JobRenameRequest):
    try:
        job = jm.rename_job(job_id, body.name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"job": jm.refresh_job_runtime(job)}


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: str):
    try:
        jm.delete_job(job_id, force_stop=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@app.post("/api/jobs/{job_id}/start")
def api_start_job(job_id: str):
    try:
        job = jm.start_job(job_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"job": job}


@app.post("/api/jobs/{job_id}/pause")
def api_pause_job(job_id: str, body: JobPauseRequest | None = None):
    mode = (body.mode if body else "soft") or "soft"
    try:
        job = jm.pause_job(job_id, mode=mode)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"job": job}


@app.get("/api/jobs/{job_id}/files")
def api_job_files(job_id: str, offset: int = 0, limit: int = 2000):
    """单文件下载进度（前端 5s 轮询，只读 pipeline_state，不触发 aria2 查询）。"""
    if not jm.get_job(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return jm.job_files(job_id, offset=offset, limit=limit)


@app.post("/api/jobs/{job_id}/files/action")
def api_job_file_action(job_id: str, body: JobFileActionRequest):
    """单文件 暂停 / 开始 / 删除（运行中由下载子进程在 ≤1s 内应用）。"""
    if not jm.get_job(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        result = jm.apply_file_control(job_id, body.op, body.paths, body.fids)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **result}


@app.get("/api/jobs/{job_id}/logs")
def api_job_logs(job_id: str, lines: int = 200):
    if not jm.get_job(job_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    path = jm.job_log_file(job_id)
    content = jm.tail_file(path, lines=lines)
    return {
        "path": path,
        "exists": os.path.isfile(path),
        "content": content if content else ("(暂无日志，开始任务后会写入此文件)" if not os.path.isfile(path) else "(日志为空)"),
    }


@app.get("/api/jobs/{job_id}/report")
def api_job_report(job_id: str):
    if not jm.get_job(job_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    path = jm.job_verify_file(job_id)
    data = jm._read_yaml(path) if os.path.isfile(path) else None
    return {"report": data}


def _schedule_process_exit(delay: float = 0.8) -> None:
    """先把响应写回客户端，再让进程退出（交给 systemd 的 Restart=always 拉起）。"""

    def _exit() -> None:
        time.sleep(delay)
        os._exit(1)

    threading.Thread(target=_exit, name="self-restart", daemon=True).start()


@app.post("/api/web/restart")
def api_web_restart():
    """重启 Web 服务。三条互斥路径，都不调用 sudo、不修改系统设置：

    1. systemd 托管且 `Restart=always`：直接退出进程，几秒后由 systemd 拉起
       （0.8 秒后再退，保证响应能写回）。Restart 不是 always 时返回 409 + 手工命令，
       否则「重启」会变成「关机」；
    2. 脚本托管（`bin/start.sh` 写的 state/web.pid 命中本进程）：后台调度 bin/restart.sh；
    3. 其它（容器 / 手动 uvicorn）：返回 409 + 手工命令，不乱动进程。
    """
    info = sup.runtime_info(refresh=True)
    if info["supervisor"] == "systemd":
        if not sup.can_self_restart(info):
            unit = info.get("unit") or "<unit>"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"当前由 systemd 托管（unit {unit}），但 Restart 策略是 "
                    f"{info.get('restart_policy') or '未设置'}，进程退出后不会被拉起；"
                    f"请手动执行：sudo systemctl restart {unit}"
                ),
            )
        _schedule_process_exit()
        return {
            "ok": True,
            "mode": "systemd-self-exit",
            "message": "进程即将退出，systemd 会在约 5 秒后拉起，请稍后刷新",
        }
    if info["supervisor"] == "pidfile":
        if not os.path.isfile(RESTART_SCRIPT):
            raise HTTPException(status_code=500, detail=f"找不到重启脚本: {RESTART_SCRIPT}")
        try:
            # 独立会话 + 短暂延迟：先把响应写回客户端，再由脚本杀旧 PID 并拉起新进程
            subprocess.Popen(
                ["/bin/sh", "-c", f"sleep 1; exec '{RESTART_SCRIPT}'"],
                cwd=BASE_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"无法调度重启: {e}") from e
        return {"ok": True, "mode": "restart-script", "message": "正在重启…请稍后刷新"}
    raise HTTPException(
        status_code=409,
        detail=(
            "无法识别本服务的启动方式（既不是 systemd 托管，也没有 state/web.pid 指向本进程），"
            "不会自动重启。" + (f"请手动执行：{info['restart_hint']}" if info.get("restart_hint") else "请手动重启进程")
        ),
    )


@app.get("/")
def index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.isfile(index_path):
        raise HTTPException(status_code=404, detail="前端页面不存在")
    return FileResponse(index_path)


@app.get("/settings")
def settings_page():
    settings_path = os.path.join(STATIC_DIR, "settings.html")
    if not os.path.isfile(settings_path):
        raise HTTPException(status_code=404, detail="设置页面不存在")
    return FileResponse(settings_path)


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    import uvicorn

    uvicorn.run("web_app:app", host=WEB_HOST, port=WEB_PORT, reload=False)


if __name__ == "__main__":
    main()
