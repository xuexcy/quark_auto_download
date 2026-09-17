# -*- coding: utf-8 -*-
"""Quark Auto Download Web 服务（多分享链接）。"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config_loader import BASE_DIR
import jobs_manager as jm
from log_util import configure_logging, dated_web_log_path

WEB_DIR = os.path.join(BASE_DIR, "web")
STATIC_DIR = os.path.join(WEB_DIR, "static")
WEB_LOG_PATH = (
    os.getenv("QUARK_AUTO_DL_WEB_LOG_FILE", "").strip() or dated_web_log_path()
)


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

app = FastAPI(title="Quark Auto Download", version="2.0.0")


class ConfigUpdateRequest(BaseModel):
    quark: Optional[dict[str, Any]] = None
    openlist: Optional[dict[str, Any]] = None
    aria2: Optional[dict[str, Any]] = None


class JobCreateRequest(BaseModel):
    share_url: str = Field(..., min_length=1)
    share_pwd: str = ""


class JobPauseRequest(BaseModel):
    mode: str = Field("soft", description="soft | hard")


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
        job = jm.add_job(body.share_url, body.share_pwd)
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
    path = jm.job_verify_file(job_id)
    data = jm._read_yaml(path) if os.path.isfile(path) else None
    return {"report": data}


@app.get("/")
def index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.isfile(index_path):
        raise HTTPException(status_code=404, detail="前端页面不存在")
    return FileResponse(index_path)


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    import uvicorn

    host = os.getenv("QUARK_AUTO_DL_WEB_HOST", "0.0.0.0")
    port = int(os.getenv("QUARK_AUTO_DL_WEB_PORT", "8787"))
    uvicorn.run("web_app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
