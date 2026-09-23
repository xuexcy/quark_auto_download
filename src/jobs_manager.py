# -*- coding: utf-8 -*-
"""多分享链接任务存储与进程控制。"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import time
import uuid
from typing import Any

import yaml

from config_loader import BASE_DIR, load_config, resolve_config_path
from log_util import dated_job_log_path, dated_web_log_path

JOBS_INDEX = os.path.join(BASE_DIR, "state", "jobs.yaml")
JOBS_DIR = os.path.join(BASE_DIR, "state", "jobs")
WEB_PID_FILE = os.path.join(BASE_DIR, "state", "web.pid")
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


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _atomic_yaml_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


def _read_yaml(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def job_dir(job_id: str) -> str:
    return os.path.join(JOBS_DIR, job_id)


def job_pid_file(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "job.pid")


def job_state_file(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "pipeline_state.yaml")


def job_control_file(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "control.yaml")


def job_verify_file(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "download_verify_report.yaml")


def job_log_file(job_id: str) -> str:
    """任务下载日志路径：优先用记录中已保存的 log_path，否则兼容旧路径。"""
    job = get_job(job_id)
    if job:
        saved = str(job.get("log_path") or "").strip()
        if saved:
            return saved
    legacy = os.path.join(BASE_DIR, "log", "jobs", job_id, "download.log")
    if os.path.isfile(legacy):
        return legacy
    return ""


def web_log_file() -> str:
    env = os.getenv("QUARK_AUTO_DL_WEB_LOG_FILE", "").strip()
    if env:
        return env
    return dated_web_log_path()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(path: str) -> int | None:
    if not os.path.isfile(path):
        return None
    try:
        pid = int(open(path, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _make_job_id(share_url: str) -> str:
    digest = hashlib.sha1(share_url.strip().encode("utf-8")).hexdigest()[:10]
    return f"j{digest}"


def load_jobs_index() -> list[dict]:
    data = _read_yaml(JOBS_INDEX)
    jobs = data.get("jobs") or []
    return [j for j in jobs if isinstance(j, dict)]


def save_jobs_index(jobs: list[dict]) -> None:
    _atomic_yaml_write(JOBS_INDEX, {"updated_at": _now(), "jobs": jobs})


def get_job(job_id: str) -> dict | None:
    for job in load_jobs_index():
        if str(job.get("id")) == job_id:
            return job
    return None


def upsert_job(job: dict) -> dict:
    jobs = load_jobs_index()
    found = False
    for i, item in enumerate(jobs):
        if item.get("id") == job.get("id"):
            jobs[i] = job
            found = True
            break
    if not found:
        jobs.append(job)
    save_jobs_index(jobs)
    return job


def add_job(share_url: str, share_pwd: str = "", name: str = "") -> dict:
    share_url = str(share_url or "").strip()
    name = str(name or "").strip()
    if not share_url:
        raise ValueError("分享链接不能为空")
    if not name:
        raise ValueError("任务名不能为空")
    if not re.search(r"pan\.quark\.cn/s/", share_url):
        # 宽松校验，仍允许
        pass

    jobs = load_jobs_index()
    for existing in jobs:
        if str(existing.get("name") or "").strip() == name:
            # 同一链接且同名：更新提取码后复用；否则视为重名冲突
            if str(existing.get("share_url", "")).strip() == share_url:
                existing["share_pwd"] = str(share_pwd or "")
                existing["name"] = name
                existing["updated_at"] = _now()
                return upsert_job(existing)
            raise ValueError(f"任务名已存在: {name}")

    # 同一链接复用已有任务（需带上新任务名，且新名不得与其它任务冲突）
    for existing in jobs:
        if str(existing.get("share_url", "")).strip() == share_url:
            existing["share_pwd"] = str(share_pwd or "")
            existing["name"] = name
            existing["updated_at"] = _now()
            return upsert_job(existing)

    job_id = _make_job_id(share_url)
    # 极端冲突时追加短后缀
    if get_job(job_id):
        job_id = f"{job_id}-{uuid.uuid4().hex[:4]}"

    os.makedirs(job_dir(job_id), exist_ok=True)
    job = {
        "id": job_id,
        "name": name,
        "share_url": share_url,
        "share_pwd": str(share_pwd or ""),
        "status": "paused",  # paused | running | soft_pausing
        "created_at": _now(),
        "updated_at": _now(),
        "message": "默认暂停",
    }
    _atomic_yaml_write(job_control_file(job_id), {"command": "none", "updated_at": _now()})
    return upsert_job(job)


def rename_job(job_id: str, name: str) -> dict:
    """修改任务显示名：trim、非空、与其它任务不重名，并写回 jobs.yaml。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("任务名不能为空")
    job = get_job(job_id)
    if not job:
        raise ValueError(f"任务不存在: {job_id}")
    current = str(job.get("name") or "").strip()
    if name == current:
        return job
    for existing in load_jobs_index():
        if existing.get("id") == job_id:
            continue
        if str(existing.get("name") or "").strip() == name:
            raise ValueError(f"任务名已存在: {name}")
    job["name"] = name
    job["updated_at"] = _now()
    return upsert_job(job)


def delete_job(job_id: str, *, force_stop: bool = True) -> None:
    job = get_job(job_id)
    if not job:
        raise ValueError(f"任务不存在: {job_id}")
    if force_stop:
        try:
            pause_job(job_id, mode="hard")
        except Exception:
            stop_job_process(job_id)
    jobs = [j for j in load_jobs_index() if j.get("id") != job_id]
    save_jobs_index(jobs)


def _job_file_stats(state: dict) -> dict:
    """从 pipeline_state.files 汇总体积与完成数。

    - total_files / total_size: 分享内全部文件
    - done_files: 本地已有（state 为 done 或 cleanup；cleanup 表示已落盘待清网盘）
    - existing_or_done_size: 上述「本地已有」文件的 size 之和
      （含本次下载完成 + 启动时本地已存在跳过 + Aria2 已完成接管）
    - existing_or_done_files_percent: done_files / total_files，0–100，一位小数；
      total_files 为 0 时为 None（前端显示 —）
    - existing_or_done_percent: existing_or_done_size / total_size，0–100，一位小数；
      total_size 为 0 时为 None（前端显示 —）
    """
    files = state.get("files")
    if not isinstance(files, list):
        files = []

    total_files = 0
    total_size = 0
    done_files = 0
    existing_or_done_size = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        total_files += 1
        size = max(int(item.get("size") or 0), 0)
        total_size += size
        st = str(item.get("state") or "").strip()
        if st in ("done", "cleanup"):
            done_files += 1
            existing_or_done_size += size

    if total_files == 0:
        # 尚未写出 files 时，用 counts 凑总数 / 已完成数（体积仍为 0）
        counts = state.get("counts") if isinstance(state.get("counts"), dict) else {}
        known = (
            "pending",
            "transfer_retry",
            "transferring",
            "ready",
            "retry",
            "downloading",
            "cleanup",
            "done",
            "failed",
        )
        total_files = sum(int(counts.get(k) or 0) for k in known)
        done_files = int(counts.get("done") or 0) + int(counts.get("cleanup") or 0)

    if total_files > 0:
        existing_or_done_files_percent = round(
            100.0 * done_files / total_files, 1
        )
    else:
        existing_or_done_files_percent = None

    if total_size > 0:
        existing_or_done_percent = round(
            100.0 * existing_or_done_size / total_size, 1
        )
    else:
        existing_or_done_percent = None

    return {
        "total_files": total_files,
        "total_size": total_size,
        "done_files": done_files,
        "existing_or_done_size": existing_or_done_size,
        "existing_or_done_files_percent": existing_or_done_files_percent,
        "existing_or_done_percent": existing_or_done_percent,
    }


def refresh_job_runtime(job: dict) -> dict:
    """根据 PID / 控制文件刷新运行态。

    以进程是否存活为准：无 pid 或 pid 已死时不得视为运行中，
    并将 jobs.yaml 中过期的 running/soft_pausing 写回 paused。
    """
    job_id = str(job.get("id"))
    pid = _read_pid(job_pid_file(job_id))
    alive = bool(pid and _pid_alive(pid))
    if pid and not alive:
        try:
            os.remove(job_pid_file(job_id))
        except OSError:
            pass
        pid = None

    # 无 pid / 进程已死：不得继续显示运行中（含 stop.sh 杀进程后 pid 文件已删）
    if not alive and job.get("status") in ("running", "soft_pausing"):
        prev = job.get("status")
        job["status"] = "paused"
        job["message"] = "软暂停完成" if prev == "soft_pausing" else "进程已退出"
        job["updated_at"] = _now()
        upsert_job(job)

    control = _read_yaml(job_control_file(job_id))
    state = _read_yaml(job_state_file(job_id))
    stats = _job_file_stats(state)
    return {
        **job,
        "pid": pid,
        "running": alive,
        "control": control.get("command") or "none",
        "counts": state.get("counts") or {},
        "stats": stats,
        "total_files": stats["total_files"],
        "total_size": stats["total_size"],
        "done_files": stats["done_files"],
        "existing_or_done_size": stats["existing_or_done_size"],
        "existing_or_done_files_percent": stats["existing_or_done_files_percent"],
        "existing_or_done_percent": stats["existing_or_done_percent"],
        "pipeline_updated_at": state.get("updated_at"),
        "failed_files": state.get("failed_files") or [],
        "aria2_backpressure": state.get("aria2_backpressure") or {},
        "log_path": str(job.get("log_path") or "").strip() or job_log_file(job_id),
    }


def list_jobs() -> list[dict]:
    return [refresh_job_runtime(dict(j)) for j in load_jobs_index()]


def reconcile_all_jobs() -> list[dict]:
    """启动时对账：按进程存活情况纠正过期 running 状态。"""
    return list_jobs()


def write_control(job_id: str, command: str) -> None:
    _atomic_yaml_write(
        job_control_file(job_id),
        {"command": command, "updated_at": _now()},
    )


def start_job(job_id: str) -> dict:
    job = get_job(job_id)
    if not job:
        raise ValueError(f"任务不存在: {job_id}")
    runtime = refresh_job_runtime(job)
    if runtime["running"]:
        raise RuntimeError(f"任务已在运行中，PID={runtime['pid']}")

    os.makedirs(job_dir(job_id), exist_ok=True)
    write_control(job_id, "none")

    log_path = dated_job_log_path(job_id)
    job["log_path"] = log_path
    job["updated_at"] = _now()
    upsert_job(job)

    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(BASE_DIR, "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["QUARK_AUTO_DL_JOB_ID"] = job_id
    env["QUARK_AUTO_DL_SHARE_URL"] = str(job.get("share_url") or "")
    env["QUARK_AUTO_DL_SHARE_PWD"] = str(job.get("share_pwd") or "")
    env["QUARK_AUTO_DL_STATE_FILE"] = job_state_file(job_id)
    env["QUARK_AUTO_DL_CONTROL_FILE"] = job_control_file(job_id)
    env["QUARK_AUTO_DL_VERIFY_REPORT_FILE"] = job_verify_file(job_id)
    env["QUARK_AUTO_DL_JOB_LOG_FILE"] = log_path

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    out = open(log_path, "a", encoding="utf-8")
    try:
        out.write(f"\n===== start {_now()} job={job_id} share={job.get('share_url', '')} =====\n")
        out.flush()
        proc = subprocess.Popen(
            [_python_bin(), MAIN_SCRIPT],
            cwd=BASE_DIR,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    finally:
        out.close()  # 父进程关闭；子进程已继承 fd
    with open(job_pid_file(job_id), "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    job["status"] = "running"
    job["message"] = "运行中"
    job["updated_at"] = _now()
    upsert_job(job)
    return refresh_job_runtime(job)


def stop_job_process(job_id: str, timeout_sec: int = 20) -> None:
    pid = _read_pid(job_pid_file(job_id))
    if not pid:
        return
    if not _pid_alive(pid):
        try:
            os.remove(job_pid_file(job_id))
        except OSError:
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.3)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.remove(job_pid_file(job_id))
    except OSError:
        pass


def _job_paths_from_state(job_id: str) -> set[str]:
    state = _read_yaml(job_state_file(job_id))
    paths: set[str] = set()
    for item in state.get("files") or []:
        if isinstance(item, dict) and item.get("path"):
            paths.add(str(item["path"]))
    for p in state.get("failed_files") or []:
        paths.add(str(p))
    return paths


def _aria2_client_from_config():
    from aria2_api import Aria2Client
    import logging

    try:
        aria2 = load_config(resolve_config_path("", "aria2"))
    except Exception as e:
        raise RuntimeError(f"读取 aria2 配置失败: {e}") from e
    openlist = {}
    try:
        openlist = load_config(resolve_config_path("", "openlist"))
    except Exception:
        pass

    logger = logging.getLogger("jobs_manager")
    return Aria2Client(
        host=str(aria2.get("aria2_host") or ""),
        secret=str(aria2.get("aria2_secret") or ""),
        download_dir=str(aria2.get("aria2_download_dir") or "/tmp"),
        request_timeout=int(aria2.get("request_timeout") or 10),
        openlist_token=str(openlist.get("openlist_token") or ""),
        poll_interval=int(aria2.get("poll_interval") or 30),
        download_timeout=int(aria2.get("download_timeout") or 7200),
        logger=logger,
    )


def pause_job(job_id: str, mode: str = "soft") -> dict:
    """
    mode=soft: 删排队，活跃继续，跟踪至完成后进程退出
    mode=hard: 暂停活跃+排队（Aria2 pause，保留进度），尽快停止进程
    """
    mode = "hard" if mode == "hard" else "soft"
    job = get_job(job_id)
    if not job:
        raise ValueError(f"任务不存在: {job_id}")

    runtime = refresh_job_runtime(job)
    write_control(job_id, "hard_pause" if mode == "hard" else "soft_pause")

    aria2_result: dict = {}
    paths = _job_paths_from_state(job_id)
    try:
        client = _aria2_client_from_config()
        if paths:
            if mode == "hard":
                aria2_result = client.aria2_pause_by_paths(
                    paths, pause_active=True, pause_waiting=True
                )
            else:
                aria2_result = client.aria2_remove_by_paths(
                    paths, remove_active=False, remove_waiting=True
                )
    except Exception as e:
        job["message"] = f"暂停已下发，但 Aria2 处理失败: {e}"

    if mode == "hard":
        stop_job_process(job_id, timeout_sec=15)
        job["status"] = "paused"
        job["message"] = (
            f"硬暂停完成：已暂停排队 {len(aria2_result.get('paused_waiting', []))}，"
            f"已暂停下载中 {len(aria2_result.get('paused_active', []))}（可下次恢复）"
        )
    else:
        if runtime["running"]:
            job["status"] = "soft_pausing"
            job["message"] = (
                f"软暂停中：已清排队 {len(aria2_result.get('removed_waiting', []))}，"
                f"等待活跃任务完成"
            )
        else:
            job["status"] = "paused"
            job["message"] = "已暂停（进程未运行）"
    job["updated_at"] = _now()
    upsert_job(job)
    result = refresh_job_runtime(job)
    result["aria2_result"] = aria2_result
    return result


def mark_job_paused_if_exited(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    runtime = refresh_job_runtime(job)
    if not runtime["running"] and job.get("status") != "paused":
        job["status"] = "paused"
        job["updated_at"] = _now()
        if job.get("status") == "soft_pausing" or runtime.get("control") == "soft_pause":
            job["message"] = "软暂停完成"
        upsert_job(job)


# ---------- 全局配置（高级设置） ----------

def config_path(basename: str) -> str:
    return resolve_config_path("", basename)


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
        "paths": {k: config_path(k) for k in ("quark", "openlist", "aria2")},
    }


def _is_masked_placeholder(value: Any) -> bool:
    text = str(value or "")
    return "*" in text and not str(text).startswith("http")


def save_section(basename: str, updates: dict) -> str:
    path = config_path(basename)
    current = load_section(basename) if os.path.exists(path) else {}
    merged = dict(current)
    secret_keys = SECRET_KEYS.get(basename, set())
    for key, value in updates.items():
        if key.endswith("__masked"):
            continue
        if key in secret_keys and (value is None or value == "" or _is_masked_placeholder(value)):
            continue
        # 分享链接改由 jobs 管理，高级配置不再覆盖
        if basename == "quark" and key in ("share_url", "share_pwd"):
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


def tail_file(path: str, lines: int = 80) -> str:
    if not path or not os.path.isfile(path):
        return ""
    lines = max(1, min(int(lines), 2000))
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(4096, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])
