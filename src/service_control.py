# -*- coding: utf-8 -*-
"""下载任务进程控制与配置读写（供 Web / CLI 共用）。"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

import yaml

from config_loader import BASE_DIR, CONF_DIR, load_config, resolve_config_path
from log_util import dated_job_log_path

PID_FILE = os.path.join(BASE_DIR, "state", "quark_main.pid")
STATE_FILE = os.path.join(BASE_DIR, "state", "pipeline_state.yaml")
VERIFY_REPORT_FILE = os.path.join(BASE_DIR, "state", "download_verify_report.yaml")
MAIN_SCRIPT = os.path.join(BASE_DIR, "src", "quark_main.py")

SECRET_KEYS = {
    "quark": {"quark_cookie"},
    "openlist": {"openlist_token"},
    "aria2": {"aria2_secret"},
}


def _python_bin() -> str:
    env = os.getenv("QUARK_AUTO_DL_PYTHON", "").strip()
    if env and os.path.isfile(env):
        return env
    candidate = os.path.join(os.path.dirname(BASE_DIR), "bin", "python3")
    if os.path.isfile(candidate):
        return candidate
    return "python3"


def _read_pid() -> int | None:
    if not os.path.isfile(PID_FILE):
        return None
    try:
        raw = open(PID_FILE, encoding="utf-8").read().strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_job_status() -> dict[str, Any]:
    pid = _read_pid()
    running = bool(pid and _pid_alive(pid))
    if pid and not running:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        pid = None

    state: dict[str, Any] = {}
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                state = loaded
        except Exception:
            state = {}

    quark = {}
    try:
        quark = load_section("quark")
    except Exception:
        pass

    return {
        "running": running,
        "pid": pid,
        "pid_file": PID_FILE,
        "share_url": str(quark.get("share_url") or ""),
        "share_pwd": str(quark.get("share_pwd") or ""),
        "updated_at": state.get("updated_at"),
        "counts": state.get("counts") or {},
        "aria2_backpressure": state.get("aria2_backpressure") or {},
        "failed_files": state.get("failed_files") or [],
    }


def start_job() -> dict[str, Any]:
    status = get_job_status()
    if status["running"]:
        raise RuntimeError(f"任务已在运行中，PID={status['pid']}")

    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    python_bin = _python_bin()
    if not os.path.isfile(MAIN_SCRIPT):
        raise FileNotFoundError(f"找不到入口脚本: {MAIN_SCRIPT}")

    run_out = dated_job_log_path("quark_main")
    out = open(run_out, "a", encoding="utf-8")
    try:
        out.write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        out.flush()
        proc = subprocess.Popen(
            [python_bin, MAIN_SCRIPT],
            cwd=BASE_DIR,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        out.close()  # 父进程关闭；子进程已继承 fd
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    return {
        "running": True,
        "pid": proc.pid,
        "message": f"已启动，PID={proc.pid}",
        "log_path": run_out,
    }


def stop_job(timeout_sec: int = 30) -> dict[str, Any]:
    pid = _read_pid()
    if not pid:
        return {"running": False, "pid": None, "message": "当前没有运行中的任务"}
    if not _pid_alive(pid):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return {"running": False, "pid": None, "message": f"进程不存在，已清理 PID={pid}"}

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + max(timeout_sec, 1)
    while time.time() < deadline:
        if not _pid_alive(pid):
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            return {"running": False, "pid": pid, "message": f"已停止，PID={pid}"}
        time.sleep(0.5)

    os.kill(pid, signal.SIGKILL)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return {"running": False, "pid": pid, "message": f"已强制停止，PID={pid}"}


def config_path(basename: str) -> str:
    return resolve_config_path("", basename, conf_dir=CONF_DIR)


def load_section(basename: str) -> dict:
    path = config_path(basename)
    if not os.path.exists(path):
        return {}
    return load_config(path)


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "*" * (len(text) - 8) + text[-4:]


def get_all_configs(*, mask_secrets: bool = True) -> dict[str, Any]:
    sections: dict[str, dict] = {}
    for basename in ("quark", "openlist", "aria2"):
        data = dict(load_section(basename))
        if mask_secrets:
            for key in SECRET_KEYS.get(basename, set()):
                if key in data and data[key]:
                    data[key] = _mask_secret(data[key])
                    data[f"{key}__masked"] = True
        sections[basename] = data
    return {
        "quark": sections["quark"],
        "openlist": sections["openlist"],
        "aria2": sections["aria2"],
        "paths": {
            "quark": config_path("quark"),
            "openlist": config_path("openlist"),
            "aria2": config_path("aria2"),
        },
    }


def _is_masked_placeholder(value: Any) -> bool:
    text = str(value or "")
    return "*" in text and not text.startswith("http")


def save_section(basename: str, updates: dict, *, keep_secrets_if_masked: bool = True) -> str:
    path = config_path(basename)
    current = load_section(basename) if os.path.exists(path) else {}
    merged = dict(current)
    secret_keys = SECRET_KEYS.get(basename, set())

    for key, value in updates.items():
        if key.endswith("__masked"):
            continue
        if keep_secrets_if_masked and key in secret_keys:
            if value is None or value == "" or _is_masked_placeholder(value):
                continue
        merged[key] = value

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"# {basename} config (managed by web UI)\n")
        yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)
    return path


def save_all_configs(payload: dict) -> dict[str, str]:
    saved = {}
    for basename in ("quark", "openlist", "aria2"):
        section = payload.get(basename)
        if isinstance(section, dict):
            saved[basename] = save_section(basename, section)
    return saved


def update_share(share_url: str, share_pwd: str | None = None) -> str:
    updates: dict[str, Any] = {"share_url": str(share_url or "").strip()}
    if not updates["share_url"]:
        raise ValueError("分享链接不能为空")
    if share_pwd is not None:
        updates["share_pwd"] = str(share_pwd)
    return save_section("quark", updates)


def tail_file(path: str, lines: int = 80) -> str:
    if not os.path.isfile(path):
        return ""
    lines = max(1, min(int(lines), 2000))
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = 4096
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
        text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def latest_log_path() -> str | None:
    log_root = os.path.join(BASE_DIR, "log")
    if not os.path.isdir(log_root):
        return None
    candidates: list[str] = []
    for root, _, files in os.walk(log_root):
        for name in files:
            if not name.endswith(".log"):
                continue
            # 新路径 jobs/<id>_HH_MM_SS.log，或旧 quark_main.*.log / run.out
            if (
                name.startswith("quark_main_")
                or name.startswith("quark_main.")
                or "/jobs/" in root.replace("\\", "/")
            ):
                candidates.append(os.path.join(root, name))
    legacy = os.path.join(BASE_DIR, "log", "run.out")
    if not candidates:
        return legacy if os.path.isfile(legacy) else None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def get_verify_report() -> dict[str, Any] | None:
    if not os.path.isfile(VERIFY_REPORT_FILE):
        return None
    try:
        with open(VERIFY_REPORT_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
