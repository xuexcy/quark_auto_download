# -*- coding: utf-8 -*-
"""统一日志格式与按日路径。"""

from __future__ import annotations

import logging
import os
import sys
import time

from config_loader import BASE_DIR

LOG_FORMAT = (
    "[%(levelname)s] [%(asctime)s] [%(threadName)s] [%(name)s] "
    "[%(filename)s:%(funcName)s:%(lineno)d]: %(message)s"
)


def _unique_path(path: str) -> str:
    """若 path 已存在，追加 _1 / _2 … 后缀（插在扩展名前）。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    suffix = 1
    while True:
        candidate = f"{root}_{suffix}{ext}"
        if not os.path.exists(candidate):
            return candidate
        suffix += 1


def dated_web_log_path() -> str:
    """生成 `BASE_DIR/log/{YYYY_MM_DD}/web/{HH_MM_SS}.log`，并创建目录。"""
    run_date = time.strftime("%Y_%m_%d")
    run_time = time.strftime("%H_%M_%S")
    log_dir = os.path.join(BASE_DIR, "log", run_date, "web")
    os.makedirs(log_dir, exist_ok=True)
    return _unique_path(os.path.join(log_dir, f"{run_time}.log"))


def dated_job_log_path(job_id: str) -> str:
    """生成 `BASE_DIR/log/{YYYY_MM_DD}/jobs/{job_id}_{HH_MM_SS}.log`，并创建目录。"""
    safe_id = str(job_id or "job").strip() or "job"
    run_date = time.strftime("%Y_%m_%d")
    run_time = time.strftime("%H_%M_%S")
    log_dir = os.path.join(BASE_DIR, "log", run_date, "jobs")
    os.makedirs(log_dir, exist_ok=True)
    return _unique_path(os.path.join(log_dir, f"{safe_id}_{run_time}.log"))


def redirect_stdio_to(log_path: str):
    """将 stdout/stderr 以行缓冲追加到 log_path，返回文件对象。"""
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    log_fp = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_fp
    sys.stderr = log_fp
    return log_fp


def configure_logging(log_path: str | None = None) -> None:
    """配置 root logging：StreamHandler(sys.stdout)、统一格式、captureWarnings。

    若传入 log_path 且当前 stdout/stderr 尚未指向该文件，则先 redirect。
    """
    if log_path:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        already_out = _stdio_points_to(sys.stdout, log_path)
        already_err = _stdio_points_to(sys.stderr, log_path)
        if not (already_out and already_err):
            log_fp = open(log_path, "a", encoding="utf-8", buffering=1)
            if not already_out:
                sys.stdout = log_fp
            if not already_err:
                sys.stderr = log_fp

    root_handler = logging.StreamHandler(sys.stdout)
    root_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[root_handler], force=True)
    logging.captureWarnings(True)


def _stdio_points_to(stream, log_path: str) -> bool:
    try:
        name = getattr(stream, "name", None)
        if not name or not isinstance(name, str):
            return False
        return os.path.realpath(name) == os.path.realpath(log_path)
    except OSError:
        return False
