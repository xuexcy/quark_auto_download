# -*- coding: utf-8 -*-
"""任务完成通知（webhook）。

Web 服务起一个后台线程，每隔几秒对账任务状态；发现任务从「运行中」变为
「已停止且全部文件处理完」时，向 conf/web.yaml 配置的 notify_webhook_url
POST 一条 JSON（带 Bearer token，如配置了 notify_webhook_token）。

webhook body 示例：
{
  "event": "job_completed",
  "job_id": "...", "name": "...",
  "status": "completed" | "completed_with_failures",
  "total_files": 10, "done_files": 10, "failed_files": [...]
}

重试：失败后按 30s / 60s / 120s 重试三次，避免 agent 端瞬时不可达丢通知。
已发送成功的 job 记在内存 sent 集合；重启后会重发一次（幂等由接收方处理）。
"""

from __future__ import annotations

import logging
import os
import threading
import time

import requests

import jobs_manager as jm
import mcp_server
from config_loader import load_web_settings

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 5.0
_RETRY_DELAYS = (30, 60, 120)


def notify_url() -> str:
    env = os.getenv("QUARK_AUTO_DL_NOTIFY_WEBHOOK_URL", "").strip()
    if env:
        return env
    return str(load_web_settings().get("notify_webhook_url") or "").strip()


def notify_token() -> str:
    env = os.getenv("QUARK_AUTO_DL_NOTIFY_WEBHOOK_TOKEN", "").strip()
    if env:
        return env
    return str(load_web_settings().get("notify_webhook_token") or "").strip()


class CompletionWatcher:
    """轮询任务状态，对「刚完成」的任务发 webhook。线程安全；start() 后台运行。"""

    def __init__(self, poll_interval: float = _POLL_INTERVAL):
        self._poll_interval = poll_interval
        # job_id -> 上次观察到的 running 布尔
        self._prev_running: dict[str, bool] = {}
        self._sent: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="notify-watcher", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            try:
                self.check_once()
            except Exception:
                logger.exception("完成通知对账失败")

    def check_once(self) -> list[dict]:
        """对账一轮，返回本轮发出的通知（测试直接调这个）。"""
        notified: list[dict] = []
        try:
            jobs = jm.list_jobs()
        except Exception:
            return notified
        for job in jobs:
            job_id = str(job.get("id"))
            running = bool(job.get("running"))
            with self._lock:
                prev = self._prev_running.get(job_id)
                self._prev_running[job_id] = running
                # 只在 running -> stopped 的跳变、且尚未发过时触发
                if prev is not True or running or job_id in self._sent:
                    continue
            summary = mcp_server.summarize_job(job)
            if summary["status"] not in ("completed", "completed_with_failures"):
                # 停了但没下完（比如手动暂停/失败中止）不通知完成
                continue
            payload = {
                "event": "job_completed",
                "job_id": summary["job_id"],
                "name": summary["name"],
                "status": summary["status"],
                "total_files": summary["total_files"],
                "done_files": summary["done_files"],
                "failed_files": summary["failed_files"],
            }
            if self._post(payload):
                with self._lock:
                    self._sent.add(job_id)
                notified.append(payload)
        return notified

    def _post(self, payload: dict) -> bool:
        url = notify_url()
        if not url:
            return False  # 未配置 webhook：视为「无处可发」，不再重试
        headers = {"Content-Type": "application/json"}
        token = notify_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt, delay in enumerate((0,) + _RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
                if 200 <= resp.status_code < 300:
                    return True
                logger.warning("webhook 返回 %s (attempt %s)", resp.status_code, attempt + 1)
            except Exception as e:
                logger.warning("webhook 发送失败 (attempt %s): %s", attempt + 1, e)
        logger.error("webhook 重试三次仍失败，放弃: %s", payload.get("job_id"))
        return False


_watcher: CompletionWatcher | None = None


def start_watcher() -> None:
    """在 Web 服务 lifespan 里调用；未配置 webhook 时也启动（对账逻辑无害），
    但不发请求。"""
    global _watcher
    if _watcher is None:
        _watcher = CompletionWatcher()
        _watcher.start()
        if notify_url():
            logger.info("任务完成通知已启用: %s", notify_url())
        else:
            logger.info("未配置 notify_webhook_url，完成通知处于待配置状态")


def stop_watcher() -> None:
    if _watcher is not None:
        _watcher.stop()
