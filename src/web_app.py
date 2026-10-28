# -*- coding: utf-8 -*-
"""Quark Auto Download Web 服务（多分享链接）。

安全约定：
- 默认不需要登录；在 conf/web.yaml 配置 web_password（或设置环境变量
  QUARK_AUTO_DL_WEB_PASSWORD）后，所有接口/页面都要求 HTTP Basic 认证。
- 非 GET 请求校验 Origin/Referer 与 Host 同源，避免跨站请求触发启停/重启。
- 不暴露 /docs、/redoc、/openapi.json。
- /api/jobs 不下发 share_pwd（提取码），只下发 share_pwd_set。
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import subprocess
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config_loader import BASE_DIR, load_web_settings
import jobs_manager as jm
from log_util import configure_logging, dated_web_log_path

WEB_DIR = os.path.join(BASE_DIR, "web")
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
    return hmac.compare_digest(username, WEB_USERNAME) and hmac.compare_digest(
        password, WEB_PASSWORD
    )


def _request_is_same_origin(request: Request) -> bool:
    """浏览器跨站请求一定带 Origin（或 Referer）；非浏览器工具两者都没有。"""
    host = request.headers.get("host", "").lower()
    if not host:
        return True
    for header_name in ("origin", "referer"):
        value = request.headers.get(header_name)
        if not value:
            continue
        netloc = urlsplit(value).netloc
        if not netloc:
            return False
        return netloc.lower() == host
    return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log = logging.getLogger(__name__)
    if not AUTH_ENABLED:
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


app = FastAPI(
    title="Quark Auto Download",
    version="2.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def _security_guard(request: Request, call_next):
    path = request.url.path
    if AUTH_ENABLED and path not in AUTH_EXEMPT_PATHS and not _check_basic_auth(request):
        return Response(
            content="需要登录",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Quark Auto Download"'},
            media_type="text/plain; charset=utf-8",
        )
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _request_is_same_origin(request):
        return JSONResponse({"detail": "跨站请求已被拒绝"}, status_code=403)
    return await call_next(request)


class ConfigUpdateRequest(BaseModel):
    quark: Optional[dict[str, Any]] = None
    openlist: Optional[dict[str, Any]] = None
    aria2: Optional[dict[str, Any]] = None


class JobCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, description="任务显示名，必填且不可与已有任务重复")
    share_url: str = Field(..., min_length=1)
    share_pwd: str = ""


class JobPauseRequest(BaseModel):
    mode: str = Field("soft", description="soft | hard")


class JobRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, description="新任务显示名，trim 后非空且不可与其它任务重复")


@app.get("/api/health")
def health():
    return {"ok": True}


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


@app.get("/api/jobs")
def api_list_jobs():
    return {"jobs": jm.list_jobs()}


@app.post("/api/jobs")
def api_create_job(body: JobCreateRequest):
    try:
        job = jm.add_job(body.share_url, body.share_pwd, name=body.name)
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


@app.post("/api/web/restart")
def api_web_restart():
    """后台调度 bin/restart.sh（先 sleep 再 stop+start），避免当前进程自杀导致响应发不出去。"""
    if not os.path.isfile(RESTART_SCRIPT):
        raise HTTPException(status_code=500, detail=f"找不到重启脚本: {RESTART_SCRIPT}")
    try:
        # 独立会话 + 短暂延迟：先把本请求响应写回客户端，再由脚本杀旧 PID 并拉起新进程
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
    return {"ok": True, "message": "正在重启…请稍后刷新"}


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

    host = os.getenv("QUARK_AUTO_DL_WEB_HOST", "0.0.0.0")
    port = int(os.getenv("QUARK_AUTO_DL_WEB_PORT", "8787"))
    uvicorn.run("web_app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
