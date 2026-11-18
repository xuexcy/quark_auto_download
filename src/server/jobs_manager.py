# -*- coding: utf-8 -*-
"""多分享链接任务存储与进程控制。

并发约定：
- `state/jobs.yaml` 的读-改-写统一走 `_index_lock()`（线程 RLock + 文件 flock），
  Web 进程与 quark_main 子进程之间同样互斥，避免丢更新。
- 启动单个任务走 `_job_start_lock()`（非阻塞独占锁），避免连点导致同一任务起两个进程。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import yaml

from config_loader import (
    BASE_DIR,
    allowed_roots,
    load_config,
    load_web_settings,
    resolve_config_path,
)
import aria2_profiles as aria2cfg
from log_util import dated_job_log_path, dated_web_log_path

JOBS_INDEX = os.path.join(BASE_DIR, "state", "jobs.yaml")
JOBS_DIR = os.path.join(BASE_DIR, "state", "jobs")
WEB_PID_FILE = os.path.join(BASE_DIR, "state", "web.pid")
MAIN_SCRIPT = os.path.join(BASE_DIR, "src", "server", "quark_main.py")
INDEX_LOCK_FILE = os.path.join(BASE_DIR, "state", "jobs.yaml.lock")

SECRET_KEYS = {
    "quark": {"quark_cookie"},
    "aria2": {"aria2_secret"},
}

def _python_bin() -> str:
    """任务子进程使用的解释器：优先项目内 venv，保证与 Web 使用同一套依赖。"""
    env = os.getenv("QUARK_AUTO_DL_PYTHON", "").strip()
    if env and os.path.isfile(env):
        return env
    for candidate in (
        os.path.join(BASE_DIR, "py_venv", "bin", "python3"),
        os.path.join(BASE_DIR, "venv", "bin", "python3"),
        os.path.join(os.path.dirname(BASE_DIR), "bin", "python3"),  # 兼容旧布局（venv 在项目上一级）
    ):
        if os.path.isfile(candidate):
            return candidate
    return "python3"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _write_private_text(path: str, text: str) -> None:
    """以 0600 权限写文件（配置里含 Cookie / Token / 提取码，不能让同机其他用户读）。

    os.open 的 mode 只在创建时生效：文件已存在且是 0644 时不会收紧，
    所以必须再 fchmod 一次。
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        raise


def _atomic_yaml_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
            # 不 fsync 的话断电后可能留下 0 字节的 jobs.yaml（rename 已可见但内容未落盘）
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        # 序列化/写盘中途失败时别把 .tmp 留在 state/ 里
        _remove_quietly(tmp)
    try:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _read_yaml(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        # 单个坏文件不能让 /api/jobs 整体 500（任务列表、暂停、删除全都得还能用）
        logging.getLogger("jobs_manager").warning(
            "读取 YAML 失败，按空处理: %s | %s", path, e
        )
        return {}
    return data if isinstance(data, dict) else {}


# ---------- 锁 ----------

_index_lock_guard = threading.RLock()
_index_lock_depth = 0
_index_lock_handle = None


@contextmanager
def _index_lock() -> Iterator[None]:
    """jobs.yaml 读改写锁：同进程线程间用 RLock，跨进程用 flock。"""
    global _index_lock_depth, _index_lock_handle
    with _index_lock_guard:
        if _index_lock_depth == 0:
            os.makedirs(os.path.dirname(INDEX_LOCK_FILE) or ".", exist_ok=True)
            handle = open(INDEX_LOCK_FILE, "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _index_lock_handle = handle
        _index_lock_depth += 1
        try:
            yield
        finally:
            _index_lock_depth -= 1
            if _index_lock_depth == 0:
                handle = _index_lock_handle
                _index_lock_handle = None
                if handle is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()


@contextmanager
def _job_start_lock(job_id: str) -> Iterator[None]:
    """单个任务的启动锁：非阻塞，抢不到说明另一个请求正在启动它。"""
    os.makedirs(job_dir(job_id), exist_ok=True)
    lock_path = os.path.join(job_dir(job_id), ".start.lock")
    handle = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RuntimeError("该任务正在启动中，请稍后刷新页面") from e
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


# ---------- 进程判断 ----------

def _proc_cmdline(pid: int) -> str:
    """读 /proc/<pid>/cmdline；非 Linux 或读不到时返回空串。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # 排除僵尸：进程已退出但父进程还没回收时，kill(pid, 0) 仍然成功
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read()
    except OSError:
        return True
    try:
        state = stat[stat.rindex(b")") + 2 :][:1]
    except ValueError:
        return True
    return state not in (b"Z", b"X")


def _pid_is_job_process(pid: int) -> bool:
    """确认 pid 确实是本项目的 quark_main 进程，避免 PID 复用后误杀无关进程。"""
    cmdline = _proc_cmdline(pid)
    if not cmdline:
        return True  # 无法读取（非 Linux / 权限不足）时保持旧行为
    return "quark_main.py" in cmdline


def _read_pid(path: str) -> int | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def job_dir(job_id: str) -> str:
    return os.path.join(JOBS_DIR, job_id)


def job_pid_file(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "job.pid")


def job_state_file(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "pipeline_state.yaml")


def job_control_file(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "control.yaml")


def job_selection_file(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "selection.json")


def job_override_file(job_id: str) -> str:
    """逐文件控制文件（暂停/删除/重试）：Web 进程写，下载子进程读。"""
    return os.path.join(job_dir(job_id), "file_override.yaml")


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


def upsert_job(job: dict, *, only_keys: tuple[str, ...] | list[str] | None = None) -> dict:
    """按 id 合并写入：只覆盖传入的字段，避免把并发写入的 log_path/status 整条冲掉。

    only_keys 用于「锁外拿到整条快照、只想改其中几个字段」的调用方（子进程退出、
    refresh 回写），避免拿旧快照把 Web 刚写进去的 selection 覆盖回去。
    """
    incoming = {k: v for k, v in job.items() if v is not None}
    if only_keys is not None:
        allowed = set(only_keys)
        incoming = {k: v for k, v in incoming.items() if k in allowed or k == "id"}
    with _index_lock():
        jobs = load_jobs_index()
        for i, item in enumerate(jobs):
            if item.get("id") == incoming.get("id"):
                merged = dict(item)
                merged.update(incoming)
                merged["updated_at"] = incoming.get("updated_at") or _now()
                jobs[i] = merged
                save_jobs_index(jobs)
                return merged
        job = dict(incoming)
        job.setdefault("created_at", _now())
        job.setdefault("updated_at", _now())
        jobs.append(job)
        save_jobs_index(jobs)
    return job


def normalize_selection(selection: dict | None) -> dict:
    """规范前端传来的勾选结果：{"file_fids": [...], "dir_fids": [...]}。

    空值 / 两项都空 = 不限制（下载整个分享目录，保持旧行为）。
    """
    if not isinstance(selection, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key in ("file_fids", "dir_fids"):
        seen: list[str] = []
        for value in selection.get(key) or []:
            text = str(value or "").strip()
            if text and text not in seen:
                seen.append(text)
        if seen:
            result[key] = seen[:5000]
    return result


def add_job(
    share_url: str,
    share_pwd: str = "",
    name: str = "",
    selection: dict | None = None,
    aria2_profile: str = "",
) -> dict:
    share_url = str(share_url or "").strip()
    name = str(name or "").strip()
    selection = normalize_selection(selection)
    profile = str(aria2_profile or "").strip()
    if not share_url:
        raise ValueError("分享链接不能为空")
    if not name:
        raise ValueError("任务名不能为空")
    if profile:
        try:
            names = aria2cfg.profile_names(load_section("aria2"))
        except Exception:
            names = []
        if profile not in names:
            raise ValueError(f"Aria2 配置不存在: {profile}")

    with _index_lock():
        jobs = load_jobs_index()
        # 只拦重名，不再按分享链接复用任务：2.4.2 起勾选是「追加」语义，
        # 复用会覆盖掉旧任务已勾选/已下载的记录（用户可建多个任务下同一分享的不同文件，
        # 重复文件由 share_usage 在勾选时拦下）。
        for other in jobs:
            if str(other.get("name") or "").strip() == name:
                raise ValueError(f"任务名已存在: {name}")

        job_id = _make_job_id(share_url)
        # 极端冲突时追加短后缀（含"目录已存在但索引里没有"的孤儿目录）
        if get_job(job_id) or os.path.isdir(job_dir(job_id)):
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
            "selection": selection,
            # explicit = 勾选范围由用户决定（哪怕为空也不等于「下载全部」）
            "selection_mode": "explicit" if selection else "",
            # 用哪套 Aria2 配置（conf/aria2.yaml 的 aria2_profiles）；空 = 默认配置
            "aria2_profile": profile,
        }
        _atomic_yaml_write(job_control_file(job_id), {"command": "none", "updated_at": _now()})
        jobs.append(job)
        save_jobs_index(jobs)
        return job


def set_job_selection(job_id: str, selection: dict | None) -> dict:
    """更新任务的勾选范围（下次「开始」生效）。"""
    normalized = normalize_selection(selection)
    with _index_lock():
        job = get_job(job_id)
        if not job:
            raise ValueError(f"任务不存在: {job_id}")
        job["selection"] = normalized
        job["selection_mode"] = "explicit"
        job["updated_at"] = _now()
        return upsert_job(job)


_SHARE_ID_RE = re.compile(r"/s/([0-9A-Za-z_-]+)")


def share_key(share_url: str) -> str:
    """分享链接归一化：能取到 /s/<id> 就按 id 比，否则退回整串（去掉 query）。"""
    text = str(share_url or "").strip()
    match = _SHARE_ID_RE.search(text)
    if match:
        return "s/" + match.group(1)
    return text.split("?", 1)[0].rstrip("/")


def share_usage(job_id: str = "") -> dict[str, dict]:
    """同一分享下已被「勾选」或「下载过」的 fid -> 归属信息。

    前端目录树用它给文件打标（已下载 / 已在任务中），可重复勾选时用它拦截重复添加。
    fid 来自 pipeline_state.files[].fid（2.4.2 起写入）；老任务没有 fid 就只靠 selection 判断。
    """
    jobs = load_jobs_index()
    target = next((j for j in jobs if str(j.get("id")) == job_id), None)
    key = share_key(target.get("share_url")) if target else ""
    usage: dict[str, dict] = {}

    for job in jobs:
        jid = str(job.get("id") or "")
        if not jid:
            continue
        if key and share_key(job.get("share_url")) != key:
            continue
        name = str(job.get("name") or jid)
        selection = normalize_selection(job.get("selection"))
        for fid in selection.get("file_fids") or []:
            usage.setdefault(
                fid, {"job_id": jid, "job_name": name, "state": "selected", "kind": "file"}
            )
        for fid in selection.get("dir_fids") or []:
            usage.setdefault(
                fid, {"job_id": jid, "job_name": name, "state": "selected", "kind": "dir"}
            )
        # 用户在「文件进度」里删掉的文件仍留在 pipeline_state 里（要等子进程下一拍才清），
        # 但它已经不算「已在任务中」了：否则目录树会显示「已勾选」、重新勾选又被当成重复项挡掉。
        deleted_paths = read_file_override(jid)["deleted"]
        state = _read_yaml(job_state_file(jid))
        for item in state.get("files") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("path") or "").strip() in deleted_paths:
                continue
            fid = str(item.get("fid") or "").strip()
            if not fid:
                continue
            entry = usage.get(fid)
            if entry is None or entry.get("state") == "selected":
                usage[fid] = {
                    "job_id": jid,
                    "job_name": name,
                    "state": str(item.get("state") or ""),
                    "kind": "file",
                }
    return usage


def add_job_selection(job_id: str, selection: dict | None, force: bool = False) -> dict:
    """把勾选结果**追加**到任务上（可反复追加），并拦掉已被其它任务覆盖的文件。

    返回 added / duplicates / already_in_job，供前端提示「xxx 已经添加过了」。
    force=True 时跨任务重复项也会加进来（用户明确要再下一份）。
    """
    normalized = normalize_selection(selection)
    if not normalized:
        raise ValueError("没有勾选任何文件或文件夹")
    with _index_lock():
        job = get_job(job_id)
        if not job:
            raise ValueError(f"任务不存在: {job_id}")
        usage = share_usage(job_id)
        merged = normalize_selection(job.get("selection"))
        added = {"file_fids": [], "dir_fids": []}
        duplicates: list[dict] = []
        already_in_job = 0

        for key in ("file_fids", "dir_fids"):
            bucket = merged.setdefault(key, [])
            for fid in normalized.get(key) or []:
                owner = usage.get(fid)
                if owner is None:
                    if fid not in bucket:
                        bucket.append(fid)
                        added[key].append(fid)
                    else:
                        already_in_job += 1
                    continue
                if owner.get("job_id") == job_id:
                    if owner.get("state") == "selected":
                        already_in_job += 1
                    else:
                        duplicates.append(dict(owner, fid=fid, reason="in_job_done"))
                    continue
                if force:
                    if fid not in bucket:
                        bucket.append(fid)
                        added[key].append(fid)
                else:
                    duplicates.append(dict(owner, fid=fid, reason="other_job"))

        # 用户这次提交上来的文件，如果之前被「文件进度」删过，先把删除标记撤掉，
        # 否则子进程会把它们当已删除过滤掉（页面上表现为「勾了但没反应」）。
        # 注意要覆盖**全部请求的 fid**，不只是新增的：删掉后重新勾选同一个文件时，
        # 它可能还留在 selection 里（already_in_job），只撤「新增项」会漏掉这条标记。
        requested_files = list(normalized.get("file_fids") or [])
        if requested_files:
            try:
                forget_deleted_paths(job_id, fids=requested_files)
            except OSError as e:
                raise ValueError(f"撤销删除标记失败: {e}") from e

        merged = {k: v for k, v in merged.items() if v}
        job["selection"] = merged
        job["selection_mode"] = "explicit"
        job["updated_at"] = _now()
        saved = upsert_job(job)
        # 运行中的子进程只认这个文件：写完它才会把新勾选的文件排进队列
        try:
            write_selection_file(job_id, merged)
        except OSError as e:
            raise ValueError(f"勾选已记入任务，但写入勾选文件失败: {e}") from e
        return {
            "job": saved,
            "added": {"files": len(added["file_fids"]), "dirs": len(added["dir_fids"])},
            "duplicates": duplicates,
            "already_in_job": already_in_job,
        }


def _drop_state_files(job_id: str, fids: list[str]) -> int:
    """从 pipeline_state 里删掉这些 fid 的单文件记录（含 failed_files）。

    删掉后 share_usage 不再把它们标成「已在任务中」，前端目录树即可重新勾选。
    本地已落盘的文件不动。
    """
    targets = {str(f).strip() for f in fids if str(f).strip()}
    if not targets:
        return 0
    path = job_state_file(job_id)
    state = _read_yaml(path)
    files = state.get("files")
    if not isinstance(files, list) or not files:
        return 0
    keep: list = []
    dropped_paths: set[str] = set()
    for item in files:
        fid = str(item.get("fid") or "").strip() if isinstance(item, dict) else ""
        if fid and fid in targets:
            path_text = str(item.get("path") or "")
            if path_text:
                dropped_paths.add(path_text)
            continue
        keep.append(item)
    removed = len(files) - len(keep)
    if removed <= 0:
        return 0
    state["files"] = keep
    # failed_files 与 files 可能重复记录，按路径一起清掉
    failed = state.get("failed_files")
    if isinstance(failed, list) and dropped_paths:
        state["failed_files"] = [p for p in failed if str(p) not in dropped_paths]
    counts: dict[str, int] = {}
    for item in keep:
        if not isinstance(item, dict):
            continue
        st = str(item.get("state") or "pending").strip() or "pending"
        counts[st] = counts.get(st, 0) + 1
    state["counts"] = counts
    state["updated_at"] = _now()
    _atomic_yaml_write(path, state)
    return removed


def remove_job_selection(job_id: str, selection: dict | None) -> dict:
    """取消勾选：把若干 fid 从任务勾选里摘掉，并清掉对应的单文件状态。

    清掉 pipeline_state.files 后，这些 fid 在 share_usage 里不再属于本任务，
    前端目录树可以重新勾选、重新添加。任务运行中拒绝操作（避免与流水线抢状态文件）。
    """
    normalized = normalize_selection(selection)
    if not normalized:
        raise ValueError("没有指定要取消勾选的文件或文件夹")
    with _index_lock():
        job = get_job(job_id)
        if not job:
            raise ValueError(f"任务不存在: {job_id}")
        runtime = refresh_job_runtime(job, persist=False)
        if runtime.get("running"):
            raise RuntimeError("任务正在运行：先暂停任务，再取消勾选")
        current = normalize_selection(job.get("selection"))
        removed = {"file_fids": [], "dir_fids": []}
        for key in ("file_fids", "dir_fids"):
            drop = set(normalized.get(key) or [])
            keep: list[str] = []
            for fid in current.get(key) or []:
                if fid in drop:
                    removed[key].append(fid)
                else:
                    keep.append(fid)
            if keep:
                current[key] = keep
            else:
                current.pop(key, None)
        job["selection"] = current
        job["selection_mode"] = "explicit"
        job["updated_at"] = _now()
        saved = upsert_job(job)
    # 目录树里被取消的条目可能只在 pipeline_state 里（不在 selection），
    # 所以按「请求里给出的文件 fid」清理状态记录，而不只是清理命中 selection 的那部分。
    state_targets = set(removed["file_fids"]) | set(normalized.get("file_fids") or [])
    dropped = _drop_state_files(job_id, sorted(state_targets))
    return {
        "job": saved,
        "removed": {"files": len(removed["file_fids"]), "dirs": len(removed["dir_fids"])},
        "state_files_removed": dropped,
    }


# ---------- 逐文件控制：暂停 / 开始 / 删除 ----------

# 允许「暂停」的状态（done/cleanup/failed 不在其列：已完成的不需要管）
FILE_PAUSABLE_STATES = ("pending", "transfer_retry", "transferring", "ready", "retry", "downloading")
# 允许「开始」的状态：paused=用户暂停过，failed=失败后重新排队
FILE_RESUMABLE_STATES = ("paused", "failed")
_FILE_OP_ALIASES = {
    "pause": "pause",
    "suspend": "pause",
    "resume": "resume",
    "start": "resume",
    "delete": "delete",
    "remove": "delete",
}


def read_file_override(job_id: str) -> dict:
    """读逐文件控制文件（读不出来按空处理）。

    返回 {"paused","deleted","retry"} 三个路径集合，外加 deleted_map（fid → 路径）：
    删除是按路径记的，但用户之后可能只拿着 fid 重新勾选同一个文件，需要靠这张表把删除标记撤掉。
    """
    result: dict = {
        "paused": set(),
        "deleted": set(),
        "retry": set(),
        "deleted_map": {},
    }
    try:
        data = _read_yaml(job_override_file(job_id))
    except Exception:
        return result
    if not isinstance(data, dict):
        return result
    for key in ("paused", "deleted", "retry"):
        raw = data.get(key)
        if isinstance(raw, list):
            result[key] = {str(v).strip() for v in raw if str(v).strip()}
    raw_map = data.get("deleted_map")
    if isinstance(raw_map, dict):
        result["deleted_map"] = {
            str(k).strip(): str(v).strip()
            for k, v in raw_map.items()
            if str(k).strip() and str(v).strip()
        }
    return result


def write_file_override(job_id: str, override: dict) -> None:
    payload = {
        key: sorted(str(v) for v in (override.get(key) or set()) if str(v).strip())
        for key in ("paused", "deleted", "retry")
    }
    raw_map = override.get("deleted_map") or {}
    if isinstance(raw_map, dict) and raw_map:
        payload["deleted_map"] = {
            str(k): str(v) for k, v in sorted(raw_map.items()) if str(k) and str(v)
        }
    _atomic_yaml_write(job_override_file(job_id), payload)


def _state_fid_paths(job_id: str, fids: list[str]) -> dict[str, str]:
    """从 pipeline_state 里查 fid → 相对路径（删除前调用，记下「哪个 fid 对应哪条路径」）。"""
    wanted = {str(f).strip() for f in (fids or []) if str(f).strip()}
    if not wanted:
        return {}
    state = _read_yaml(job_state_file(job_id))
    files = state.get("files")
    if not isinstance(files, list):
        return {}
    found: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("fid") or "").strip()
        path = str(item.get("path") or "").strip()
        if fid in wanted and path:
            found[fid] = path
    return found


def forget_deleted_paths(job_id: str, *, paths: list[str] | None = None, fids: list[str] | None = None) -> int:
    """把删除标记撤掉（重新勾选 / 点开始时用），返回撤掉的路径数。

    删除是「按路径持久记」的：用户删掉文件后再从目录树勾回来，如果不撤这条标记，
    子进程会把它当成「已删除」直接过滤掉（追加勾选看起来没反应）。
    """
    targets = {str(p).strip() for p in (paths or []) if str(p).strip()}
    wanted_fids = {str(f).strip() for f in (fids or []) if str(f).strip()}
    if not targets and not wanted_fids:
        return 0
    with _index_lock():
        override = read_file_override(job_id)
        removed: set[str] = set()
        mapping = dict(override.get("deleted_map") or {})
        for fid in list(wanted_fids):
            path = mapping.get(fid)
            if path:
                removed.add(path)
                mapping.pop(fid, None)
        # 没记上 fid → 路径（例如删除时任务刚重启、pipeline_state 还没写）时，
        # 用当前 pipeline_state 兜底，否则「删掉后再勾回来」会被永久挡住。
        unresolved = [fid for fid in wanted_fids if fid not in mapping]
        if unresolved:
            for fid, path in _state_fid_paths(job_id, unresolved).items():
                if path in override["deleted"]:
                    removed.add(path)
                mapping.pop(fid, None)
        for path in list(targets):
            if path in override["deleted"]:
                removed.add(path)
            else:
                continue
            for fid, mapped in list(mapping.items()):
                if mapped == path:
                    mapping.pop(fid, None)
        if not removed:
            return 0
        override["deleted"] -= removed
        override["deleted_map"] = mapping
        write_file_override(job_id, override)
    return len(removed)


def _recount_state_files(state: dict) -> None:
    counts: dict[str, int] = {}
    for item in state.get("files") or []:
        if not isinstance(item, dict):
            continue
        st = str(item.get("state") or "pending").strip() or "pending"
        counts[st] = counts.get(st, 0) + 1
    state["counts"] = counts
    state["updated_at"] = _now()


def _drop_state_paths(job_id: str, paths: list[str]) -> int:
    """按路径从 pipeline_state 删记录（fid 缺失时用；fid 已知时优先 _drop_state_files）。"""
    targets = {str(p).strip() for p in paths if str(p).strip()}
    if not targets:
        return 0
    path = job_state_file(job_id)
    state = _read_yaml(path)
    files = state.get("files")
    if not isinstance(files, list) or not files:
        return 0
    keep = [
        item
        for item in files
        if not (isinstance(item, dict) and str(item.get("path") or "").strip() in targets)
    ]
    removed = len(files) - len(keep)
    if removed <= 0:
        return 0
    state["files"] = keep
    failed = state.get("failed_files")
    if isinstance(failed, list):
        state["failed_files"] = [p for p in failed if str(p) not in targets]
    _recount_state_files(state)
    _atomic_yaml_write(path, state)
    return removed


def _reset_state_paths(job_id: str, paths: list[str]) -> int:
    """任务未运行时「开始」这些文件：状态改回 ready/pending，并清掉失败记录。"""
    targets = {str(p).strip() for p in paths if str(p).strip()}
    if not targets:
        return 0
    path = job_state_file(job_id)
    state = _read_yaml(path)
    files = state.get("files")
    if not isinstance(files, list) or not files:
        return 0
    changed = 0
    for item in files:
        if not isinstance(item, dict) or str(item.get("path") or "").strip() not in targets:
            continue
        if str(item.get("state") or "").strip() in ("done", "cleanup"):
            continue
        item["state"] = "ready" if item.get("already_transferred") else "pending"
        item["note"] = "已恢复（用户）"
        item["speed"] = 0
        changed += 1
    if not changed:
        return 0
    failed = state.get("failed_files")
    if isinstance(failed, list):
        state["failed_files"] = [p for p in failed if str(p) not in targets]
    _recount_state_files(state)
    _atomic_yaml_write(path, state)
    return changed


def _remove_fids_from_selection(job_id: str, fids: list[str]) -> int:
    """把 fid 从任务勾选里摘掉并同步 selection.json（不要求任务暂停，供逐文件删除用）。

    selection_mode 保持 explicit：全删空时子进程读到空勾选 = 本次不下载任何文件。
    """
    targets = {str(f).strip() for f in fids if str(f).strip()}
    if not targets:
        return 0
    with _index_lock():
        job = get_job(job_id)
        if not job:
            return 0
        current = normalize_selection(job.get("selection"))
        removed = 0
        for key in ("file_fids", "dir_fids"):
            kept = [fid for fid in (current.get(key) or []) if fid not in targets]
            removed += len(current.get(key) or []) - len(kept)
            if kept:
                current[key] = kept
            else:
                current.pop(key, None)
        if not removed:
            return 0
        job["selection"] = current
        job["selection_mode"] = "explicit"
        job["updated_at"] = _now()
        upsert_job(job)
    try:
        write_selection_file(job_id, current)
    except Exception:
        pass
    return removed


def apply_file_control(
    job_id: str,
    op: str,
    paths: list[str] | None = None,
    fids: list[str] | None = None,
) -> dict:
    """单文件 暂停 / 开始 / 删除。

    真源是 file_override.yaml（paused 声明式、retry 一次性、deleted 持久），
    运行中由下载子进程在 ≤1s 内应用；未运行时这里直接改 pipeline_state。
    """
    action = _FILE_OP_ALIASES.get(str(op or "").strip().lower())
    if not action:
        raise ValueError(f"不支持的操作: {op}（只支持 pause / resume / delete）")
    wanted_paths = [str(x).strip() for x in (paths or []) if str(x).strip()]
    wanted_fids = [str(x).strip() for x in (fids or []) if str(x).strip()]
    if not wanted_paths and not wanted_fids:
        raise ValueError("没有指定要操作的文件")

    job = get_job(job_id)
    if not job:
        raise ValueError(f"任务不存在: {job_id}")
    runtime = refresh_job_runtime(job, persist=False)
    running = bool(runtime.get("running"))

    # 读-改-写必须互斥：两个请求（连点两行、或两个标签页）各自读到同一份快照再
    # 整份写回，后写者会把前一个的暂停/删除悄悄抹掉（删除丢了文件会被重新下载）。
    with _index_lock():
        override = read_file_override(job_id)
        if action == "pause":
            override["paused"] |= set(wanted_paths)
            override["deleted"] -= set(wanted_paths)
            override["retry"] -= set(wanted_paths)
            mapping = dict(override.get("deleted_map") or {})
            override["deleted_map"] = {
                fid: p for fid, p in mapping.items() if p not in set(wanted_paths)
            }
        elif action == "resume":
            override["paused"] -= set(wanted_paths)
            override["deleted"] -= set(wanted_paths)
            override["retry"] |= set(wanted_paths)
            mapping = dict(override.get("deleted_map") or {})
            override["deleted_map"] = {
                fid: p for fid, p in mapping.items() if p not in set(wanted_paths)
            }
        else:
            override["paused"] -= set(wanted_paths)
            override["retry"] -= set(wanted_paths)
            if wanted_paths:
                override["deleted"] |= set(wanted_paths)
            # 记下 fid → 路径：用户以后可能只拿着 fid 重新勾选同一个文件，
            # 那时要靠这张表把「已删除」撤掉（见 forget_deleted_paths）
            mapping = dict(override.get("deleted_map") or {})
            resolved = _state_fid_paths(job_id, wanted_fids)
            # pipeline_state 里查不到（刚重启、还没跑过一拍）时退化为按位置配对：
            # 前端一次操作只发「一个路径 + 一个 fid」，两个列表天然一一对应。
            if len(wanted_fids) == len(wanted_paths):
                for fid, path_text in zip(wanted_fids, wanted_paths):
                    resolved.setdefault(fid, path_text)
            for fid, path_text in resolved.items():
                mapping[fid] = path_text
            override["deleted_map"] = mapping
        write_file_override(job_id, override)

    result = {
        "job_id": job_id,
        "op": action,
        "paths": wanted_paths,
        "fids": wanted_fids,
        "applied": "pipeline" if running else "immediate",
        "running": running,
    }
    if action == "delete":
        # 勾选里摘掉，前端目录树可以重新勾选（运行中也允许：子进程靠 deleted 集合过滤）
        removed = _remove_fids_from_selection(job_id, wanted_fids)
        result["selection_removed"] = removed
        if not running:
            dropped = _drop_state_files(job_id, wanted_fids)
            dropped += _drop_state_paths(job_id, wanted_paths)
            result["state_files_removed"] = dropped
    elif action == "resume" and not running:
        result["state_files_reset"] = _reset_state_paths(job_id, wanted_paths)
    return result


def rename_job(job_id: str, name: str) -> dict:
    """修改任务显示名：trim、非空、与其它任务不重名，并写回 jobs.yaml。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("任务名不能为空")
    with _index_lock():
        jobs = load_jobs_index()
        job = next((j for j in jobs if str(j.get("id")) == job_id), None)
        if not job:
            raise ValueError(f"任务不存在: {job_id}")
        if name == str(job.get("name") or "").strip():
            return job
        for other in jobs:
            if other is job:
                continue
            if str(other.get("name") or "").strip() == name:
                raise ValueError(f"任务名已存在: {name}")
        job["name"] = name
        job["updated_at"] = _now()
        save_jobs_index(jobs)
        return job


def delete_job(job_id: str, *, force_stop: bool = True) -> None:
    job = get_job(job_id)
    if not job:
        raise ValueError(f"任务不存在: {job_id}")
    # 与 start_job 共用启动锁：否则「删目录」可以与「Popen + 写 pid」交错，
    # 锁文件被 rmtree 掉以后新一次 start 会在新 inode 上重新加锁 → 双进程，
    # 而 pid 文件因目录消失写不进去 → 索引里没有、页面上也停不掉的孤儿进程。
    with _job_start_lock(job_id):
        if force_stop:
            try:
                pause_job(job_id, mode="hard")
            except Exception:
                stop_job_process(job_id)
        with _index_lock():
            jobs = [j for j in load_jobs_index() if j.get("id") != job_id]
            save_jobs_index(jobs)
        # 清掉任务目录（pid / control / pipeline_state / 报告），避免同链接重建时继承旧状态
        shutil.rmtree(job_dir(job_id), ignore_errors=True)


def _job_file_stats(state: dict) -> dict:
    """从 pipeline_state.files 汇总体积与完成数。

    - total_files / total_size: 分享内全部文件
    - done_files: 本地已有（state 为 done 或 cleanup；cleanup 表示已落盘待清网盘）
    - existing_or_done_size: 上述「本地已有」文件的 size 之和
    - existing_or_done_files_percent / existing_or_done_percent: 0–100，一位小数；无数据时 None
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
        size = max(_safe_int(item.get("size"), 0), 0)
        total_size += size
        st = str(item.get("state") or "").strip()
        if st in ("done", "cleanup"):
            done_files += 1
            existing_or_done_size += size

    if total_files == 0:
        counts = state.get("counts") if isinstance(state.get("counts"), dict) else {}
        known = (
            "pending",
            "transfer_retry",
            "transferring",
            "ready",
            "retry",
            "downloading",
            "paused",
            "cleanup",
            "done",
            "failed",
        )
        total_files = sum(_safe_int(counts.get(k), 0) for k in known)
        done_files = _safe_int(counts.get("done"), 0) + _safe_int(counts.get("cleanup"), 0)

    existing_or_done_files_percent = (
        round(100.0 * done_files / total_files, 1) if total_files > 0 else None
    )
    existing_or_done_percent = (
        round(100.0 * existing_or_done_size / total_size, 1) if total_size > 0 else None
    )

    return {
        "total_files": total_files,
        "total_size": total_size,
        "done_files": done_files,
        "existing_or_done_size": existing_or_done_size,
        "existing_or_done_files_percent": existing_or_done_files_percent,
        "existing_or_done_percent": existing_or_done_percent,
    }


def job_files(job_id: str, offset: int = 0, limit: int = 2000) -> dict:
    """GET /api/jobs/{id}/files 用：下发单文件级下载进度。

    pipeline_state.files 已经由 scheduler 写入 downloaded/total/speed/progress，
    这里只做归一化（补齐缺失字段、按 state 修正完成态），不读 aria2，避免轮询放大。
    """
    state = _read_yaml(job_state_file(job_id))
    raw_files = state.get("files")
    if not isinstance(raw_files, list):
        raw_files = []

    # 逐文件控制存在 file_override.yaml 里：删除的过滤掉，暂停的在展示层覆盖成 paused
    override = read_file_override(job_id)
    paused_paths = override["paused"]
    deleted_paths = override["deleted"]
    shown = [
        item
        for item in raw_files
        if isinstance(item, dict) and str(item.get("path") or "").strip() not in deleted_paths
    ]

    files = []
    counts: dict[str, int] = {}
    for item in shown:
        path = str(item.get("path") or "")
        size = max(_safe_int(item.get("size"), 0), 0)
        total = max(_safe_int(item.get("total"), 0), size)
        downloaded = max(_safe_int(item.get("downloaded"), 0), 0)
        st = str(item.get("state") or "").strip()
        note = str(item.get("note") or "")
        if path and path in paused_paths and st not in ("done", "cleanup", "failed", "paused"):
            st = "paused"
            note = note or "已暂停（用户）"
        if total > 0:
            downloaded = min(downloaded, total)
        if st in ("done", "cleanup") and total > 0:
            # 已落盘/待清理按 100% 展示，避免前端停在 99%
            downloaded = total
        counts[st] = counts.get(st, 0) + 1
        files.append(
            {
                "path": path,
                "fid": str(item.get("fid") or ""),
                "size": size,
                "state": st,
                "downloaded": downloaded,
                "total": total,
                "speed": max(_safe_int(item.get("speed"), 0), 0),
                "progress": round(100.0 * downloaded / total, 1) if total > 0 else 0.0,
                "note": note,
                # 逐文件操作按钮的可用性（前端不再重复判断状态机）
                "paused": st == "paused",
                "can_pause": st in FILE_PAUSABLE_STATES,
                "can_resume": st in FILE_RESUMABLE_STATES,
                "can_delete": st not in ("done", "cleanup"),
            }
        )

    file_total = len(files)
    offset = max(_safe_int(offset, 0), 0)
    limit = min(max(_safe_int(limit, 2000), 1), 5000)
    stats_source = dict(state)
    stats_source["files"] = shown
    return {
        "job_id": job_id,
        "files": files[offset : offset + limit],
        "offset": offset,
        "limit": limit,
        "file_total": file_total,
        "counts": counts if files else (state.get("counts") or {}),
        "stats": _job_file_stats(stats_source),
        "updated_at": state.get("updated_at"),
    }


def refresh_job_runtime(job: dict, persist: bool = True) -> dict:
    """根据 PID / 控制文件刷新运行态。

    以进程是否存活为准：无 pid、进程已死或已被回收时不得视为运行中，
    并将 jobs.yaml 中过期的 running/soft_pausing 写回 paused。
    """
    job_id = str(job.get("id"))
    pid = _read_pid(job_pid_file(job_id))
    alive = bool(pid and _pid_alive(pid) and _pid_is_job_process(pid))
    if pid and not alive:
        _remove_quietly(job_pid_file(job_id))
        pid = None

    # 无 pid / 进程已死：不得继续显示运行中（含 stop.sh 杀进程后 pid 文件已删）
    if not alive and job.get("status") in ("running", "soft_pausing"):
        prev = job.get("status")
        job["status"] = "paused"
        job["message"] = "软暂停完成" if prev == "soft_pausing" else "进程已退出"
        job["updated_at"] = _now()
        if persist:
            upsert_job(job)

    control = _read_yaml(job_control_file(job_id))
    state = _read_yaml(job_state_file(job_id))
    stats = _job_file_stats(state)
    runtime = {
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
    # 提取码属于半敏感信息，不下发给浏览器；只告知是否已设置
    runtime.pop("share_pwd", None)
    runtime["share_pwd_set"] = bool(str(job.get("share_pwd") or "").strip())
    return runtime


def list_jobs() -> list[dict]:
    """GET /api/jobs 用：刷新运行态，并把"进程已退出"的过期状态合并成一次写盘。"""
    jobs = load_jobs_index()
    runtimes = [refresh_job_runtime(dict(job), persist=False) for job in jobs]
    stale = [
        runtime
        for raw, runtime in zip(jobs, runtimes)
        if raw.get("status") != runtime.get("status")
    ]
    if stale:
        with _index_lock():
            fresh = load_jobs_index()
            by_id = {str(item.get("id")): item for item in fresh}
            for runtime in stale:
                item = by_id.get(str(runtime.get("id")))
                if item is None:
                    continue
                item["status"] = runtime.get("status")
                item["message"] = runtime.get("message")
                item["updated_at"] = runtime.get("updated_at") or _now()
            save_jobs_index(fresh)
    return runtimes


def write_selection_file(job_id: str, selection: dict | None) -> None:
    """把勾选范围写进子进程读取的 selection.json。

    用「临时文件 + os.replace」原子替换：子进程可能同时在读（运行中追加勾选），
    读到半截 JSON 会被当成「未限定范围」→ 误下载整个分享。
    """
    normalized = normalize_selection(selection)
    payload = {
        "file_fids": list(normalized.get("file_fids") or []),
        "dir_fids": list(normalized.get("dir_fids") or []),
    }
    path = job_selection_file(job_id)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        _write_private_text(tmp, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    finally:
        _remove_quietly(tmp)


def write_control(job_id: str, command: str) -> None:
    _atomic_yaml_write(
        job_control_file(job_id),
        {"command": command, "updated_at": _now()},
    )


def start_job(job_id: str) -> dict:
    if not get_job(job_id):
        raise ValueError(f"任务不存在: {job_id}")
    with _job_start_lock(job_id):
        # 拿到锁后重新读一遍，避免"判定未运行"与"另一个请求刚启动"之间的竞态
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

        selection = normalize_selection(job.get("selection"))
        # explicit 模式下即使勾选为空也要写文件：空勾选表示「这次不下载任何文件」，
        # 而不是旧行为里的「下载整个分享目录」。
        explicit = str(job.get("selection_mode") or "") == "explicit"
        selection_file = job_selection_file(job_id)
        if selection or explicit:
            write_selection_file(job_id, selection)
        elif os.path.isfile(selection_file):
            _remove_quietly(selection_file)

        # 这套任务用哪套 Aria2 配置；配置被删了就退回默认，并在日志里写明
        requested_profile = str(job.get("aria2_profile") or "").strip()
        try:
            profile_name = aria2cfg.resolve_name(load_section("aria2"), requested_profile)
        except Exception:
            profile_name = requested_profile
        profile_note = ""
        if requested_profile and profile_name and profile_name != requested_profile:
            profile_note = f"警告：Aria2 配置「{requested_profile}」不存在，改用「{profile_name}」"

        # 启动前预检：地址/令牌配错时立刻 400 说清楚，不让子进程把每个文件重试 10 次
        if profile_name:
            try:
                probe_aria2_profile(profile_name)
            except Exception as e:
                raise ValueError(f"Aria2 配置「{profile_name}」连不上: {e}") from e

        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.join(BASE_DIR, "src", "server") + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        env["QUARK_AUTO_DL_JOB_ID"] = job_id
        env["QUARK_AUTO_DL_SHARE_URL"] = str(job.get("share_url") or "")
        env["QUARK_AUTO_DL_SHARE_PWD"] = str(job.get("share_pwd") or "")
        env["QUARK_AUTO_DL_STATE_FILE"] = job_state_file(job_id)
        env["QUARK_AUTO_DL_CONTROL_FILE"] = job_control_file(job_id)
        env["QUARK_AUTO_DL_VERIFY_REPORT_FILE"] = job_verify_file(job_id)
        env["QUARK_AUTO_DL_JOB_LOG_FILE"] = log_path
        env["QUARK_AUTO_DL_SELECTION_FILE"] = selection_file
        env["QUARK_AUTO_DL_FILE_OVERRIDE_FILE"] = job_override_file(job_id)
        if profile_name:
            env["QUARK_AUTO_DL_ARIA2_PROFILE"] = profile_name

        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        out = open(log_path, "a", encoding="utf-8")
        try:
            out.write(f"\n===== start {_now()} job={job_id} share={job.get('share_url', '')} =====\n")
            out.write(f"===== python={_python_bin()} =====\n")
            out.write(f"===== aria2={profile_name or '(未配置)'} =====\n")
            if profile_note:
                out.write(f"===== {profile_note} =====\n")
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
        try:
            os.makedirs(job_dir(job_id), exist_ok=True)
            _write_private_text(job_pid_file(job_id), str(proc.pid))
            job["status"] = "running"
            job["message"] = "运行中"
            job["updated_at"] = _now()
            upsert_job(job)
        except Exception:
            # 目录被删 / 磁盘满时不能把刚拉起的进程丢在后台（索引里没有它，页面上停不掉）
            try:
                proc.kill()
            except OSError:
                pass
            raise
        return refresh_job_runtime(job)


def stop_job_process(job_id: str, timeout_sec: int = 20) -> None:
    pid = _read_pid(job_pid_file(job_id))
    if not pid:
        return
    if not _pid_alive(pid) or not _pid_is_job_process(pid):
        _remove_quietly(job_pid_file(job_id))
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
    _remove_quietly(job_pid_file(job_id))


def _job_paths_from_state(job_id: str) -> set[str]:
    state = _read_yaml(job_state_file(job_id))
    paths: set[str] = set()
    for item in state.get("files") or []:
        if isinstance(item, dict) and item.get("path"):
            paths.add(str(item["path"]))
    for p in state.get("failed_files") or []:
        paths.add(str(p))
    return paths


def _aria2_client_from_config(job_id: str = ""):
    """按任务选中的 Aria2 配置建客户端（多配置时各任务可以连不同的 aria2）。"""
    from aria2_api import Aria2Client
    import logging

    try:
        config = load_section("aria2")
    except Exception as e:
        raise RuntimeError(f"读取 aria2 配置失败: {e}") from e

    job = get_job(job_id) if job_id else None
    profile_name = aria2cfg.resolve_name(config, (job or {}).get("aria2_profile"))
    aria2 = aria2cfg.options_for(config, profile_name)

    logger = logging.getLogger("jobs_manager")
    remote = bool(aria2.get("aria2_remote"))
    return Aria2Client(
        host=str(aria2.get("aria2_host") or ""),
        secret=str(aria2.get("aria2_secret") or ""),
        # 这里只用于 pause / 清理，按路径对齐任务名；目录留空也无所谓（不再拼 /tmp 兜底）
        download_dir="" if remote else str(aria2.get("aria2_download_dir") or ""),
        request_timeout=int(aria2.get("request_timeout") or 10),
        download_headers=None,  # 只用于 pause/清理，不提交下载，无需直链请求头
        poll_interval=int(aria2.get("poll_interval") or 30),
        download_timeout=int(aria2.get("download_timeout") or 7200),
        logger=logger,
        job_id=job_id,
        remote=remote,
        remote_dir=str(aria2.get("aria2_remote_dir") or ""),
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
    aria2_error = ""
    paths = _job_paths_from_state(job_id)
    try:
        client = _aria2_client_from_config(job_id)
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
        aria2_error = str(e)
        job["message"] = f"暂停已下发，但 Aria2 处理失败: {e}"

    if mode == "hard":
        stop_job_process(job_id, timeout_sec=15)
        job["status"] = "paused"
        message = (
            f"硬暂停完成：已暂停排队 {len(aria2_result.get('paused_waiting', []))}，"
            f"已暂停下载中 {len(aria2_result.get('paused_active', []))}（可下次恢复）"
        )
    else:
        if runtime["running"]:
            job["status"] = "soft_pausing"
            message = (
                f"软暂停中：已清排队 {len(aria2_result.get('removed_waiting', []))}，"
                f"等待活跃任务完成"
            )
        else:
            job["status"] = "paused"
            message = "已暂停（进程未运行）"
    if aria2_error:
        # 不能只显示「硬暂停完成」：aria2 里还在跑的话，下次开始会重复下载同一文件
        job["aria2_error"] = aria2_error
        message = f"{message}；Aria2 处理失败: {aria2_error}"
    job["message"] = message
    job["updated_at"] = _now()
    # 重新读一遍再合并：期间 start_job/refresh 可能写过 log_path、status
    with _index_lock():
        latest = get_job(job_id) or {}
        latest.update(
            {
                "status": job.get("status"),
                "message": job.get("message", ""),
                "updated_at": job["updated_at"],
            }
        )
        merged = upsert_job(latest)
    result = refresh_job_runtime(dict(merged))
    result["aria2_result"] = aria2_result
    return result


# ---------- 全局配置（高级设置） ----------

def config_path(basename: str) -> str:
    return resolve_config_path("", basename)


def load_section(basename: str) -> dict:
    path = config_path(basename)
    if not os.path.exists(path):
        return {}
    return load_config(path)


def list_aria2_profiles(*, mask_secrets: bool = True) -> dict[str, Any]:
    """设置页 / 新增任务下拉用：全部 Aria2 配置 + 默认配置名。"""
    config = load_section("aria2")
    profiles = aria2cfg.profiles_of(config)
    return {
        "profiles": aria2cfg.public_profiles(profiles) if mask_secrets else profiles,
        "default": aria2cfg.default_name(config),
    }


def aria2_target(name: str = "") -> dict[str, str]:
    """按配置名取 RPC 地址与 secret（名字为空 / 已删除时用默认配置）。"""
    config = load_section("aria2")
    profile_name = aria2cfg.resolve_name(config, name)
    options = aria2cfg.options_for(config, profile_name)
    return {
        "name": profile_name,
        "host": str(options.get("aria2_host") or "").strip(),
        "secret": str(options.get("aria2_secret") or ""),
        # 远端标记与远端目录一起带回去：调用方（测试接口）没显式给的时候可以沿用
        "remote": bool(options.get("aria2_remote")),
        "remote_dir": str(options.get("aria2_remote_dir") or ""),
    }


def test_aria2_connection(
    host: str,
    secret: str = "",
    timeout: int = 8,
    *,
    remote: bool = False,
    remote_dir: str = "",
) -> dict[str, Any]:
    """连一次 aria2 RPC（getVersion + getGlobalStat + getGlobalOption），设置页「测试」用。"""
    from aria2_api import Aria2Client
    import logging

    client = Aria2Client(
        host=str(host or "").strip(),
        secret=str(secret or ""),
        download_dir="",
        request_timeout=int(timeout or 8),
        download_headers=None,
        poll_interval=30,
        download_timeout=60,
        logger=logging.getLogger("aria2_test"),
        remote=bool(remote),
        remote_dir=str(remote_dir or ""),
    )
    version = client.aria2_get_version()
    stat = client.aria2_get_global_stat()
    # 对方默认下载目录：远端模式下最常出问题的地方（目录只读 → 每个文件 errorCode=16）
    default_dir = ""
    try:
        option = client.aria2_get_global_option()
        default_dir = str(option.get("dir") or "").strip()
    except Exception:
        default_dir = ""
    return {
        # 回显真正请求的地址：用户少写 /jsonrpc 时能看出被补成了什么
        "host": client.host,
        "version": str(version.get("version") or ""),
        "enabled_features": version.get("enabledFeatures") or [],
        "num_active": int(stat.get("numActive") or 0),
        "num_waiting": int(stat.get("numWaiting") or 0),
        "download_speed": int(stat.get("downloadSpeed") or 0),
        # 远端：对方 aria2 的默认 dir（本机目录对远端没有意义）；本机：实际下载目录
        "dir": str(client.remote_dir or default_dir or ""),
        "server_dir": default_dir,
    }


def probe_aria2_profile(profile_name: str, timeout: int = 5) -> dict[str, Any]:
    """按配置名探测一次 Aria2 RPC（启动任务前的预检）。

    启动后才发现的连接错误要靠子进程逐文件重试（10 次）才暴露，
    日志里只剩 501/400，用户看不出是「地址填成了 AriaNg 网页」。
    这里花一次 getVersion 的时间把话说清楚，连不上就不启动任务。
    """
    from aria2_api import Aria2Client
    import logging

    config = load_section("aria2")
    options = aria2cfg.options_for(config, profile_name)
    client = Aria2Client(
        host=str(options.get("aria2_host") or ""),
        secret=str(options.get("aria2_secret") or ""),
        download_dir="",
        request_timeout=max(int(timeout or 5), 1),
        download_headers=None,
        poll_interval=30,
        download_timeout=60,
        logger=logging.getLogger("aria2_probe"),
        remote=bool(options.get("aria2_remote")),
        remote_dir=str(options.get("aria2_remote_dir") or ""),
    )
    # 目录不再参与预检：两个目录都可选，都留空时提交任务不带 dir，
    # 由 aria2 自己的默认目录决定（本机不校验、不移动），不需要提前读 getGlobalOption。
    version = client.aria2_get_version()
    return version if isinstance(version, dict) else {}


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "*" * (len(text) - 8) + text[-4:]


def get_all_configs(*, mask_secrets: bool = True) -> dict[str, Any]:
    sections: dict[str, dict] = {}
    raw_aria2: dict = {}
    for basename in ("quark", "aria2"):
        data = dict(load_section(basename))
        if basename == "aria2":
            # 先留一份未脱敏的，供「旧版单配置 → 多配置」迁移使用
            raw_aria2 = dict(data)
        if mask_secrets:
            for key in SECRET_KEYS.get(basename, set()):
                if key in data and data[key]:
                    data[key] = _mask_secret(data[key])
                    data[f"{key}__masked"] = True
        sections[basename] = data

    aria2_section = sections["aria2"]
    profiles = aria2cfg.profiles_of(raw_aria2)
    aria2_section["aria2_profiles"] = (
        aria2cfg.public_profiles(profiles) if mask_secrets else profiles
    )
    aria2_section["aria2_default"] = aria2cfg.default_name(raw_aria2)
    if "aria2_profiles" in raw_aria2:
        # 已经是多配置形态：旧版单配置字段不再回显，免得页面上填两遍
        for key in aria2cfg.PROFILE_KEYS:
            aria2_section.pop(key, None)
    return {
        "quark": sections["quark"],
        "aria2": sections["aria2"],
        "paths": {k: config_path(k) for k in ("quark", "aria2")},
    }


def _is_masked_placeholder(value: Any) -> bool:
    text = str(value or "")
    return "*" in text and not str(text).startswith("http")


# 这些配置项决定"往本机哪个目录写文件"，保存前必须过白名单校验。
# 注意：quark.save_to_dir 是网盘内的目录名（不是本机路径），不能纳入校验。
LOCAL_WRITE_PATH_KEYS = {
    "aria2_download_dir",
    "download_destination_dir",
    "state_file",
    "verify_report_file",
}


def _real_path(path: str) -> str:
    """逐级 realpath：末级目录可能还不存在，把已存在的祖先解成真实路径再拼回去。"""
    current = os.path.abspath(path)
    suffix: list[str] = []
    while current and not os.path.exists(current):
        parent, name = os.path.split(current)
        if not name or parent == current:
            break
        suffix.append(name)
        current = parent
    base = os.path.realpath(current) if current else os.path.realpath(path)
    for name in reversed(suffix):
        base = os.path.join(base, name)
    return os.path.abspath(base)


def _check_write_path(label: str, value: Any, roots: list[str]) -> None:
    text = str(value or "").strip()
    if not text:
        return
    if not os.path.isabs(text):
        raise ValueError(f"配置项 `{label}` 必须是绝对路径: {text}")
    # 必须用 realpath：abspath 只做字符串归一，allowed_roots 里放一个软链就能写到任意位置
    normalized = _real_path(text)
    real_roots = [_real_path(root) for root in roots if root]
    if any(
        normalized == root or normalized.startswith(root.rstrip(os.sep) + os.sep)
        for root in real_roots
    ):
        return
    raise ValueError(
        f"配置项 `{label}` 不在允许的目录内（允许: {', '.join(roots)}）: {text}"
    )


def _validate_write_paths(merged: dict) -> None:
    """本机写入路径只能指向白名单根目录，避免把下载目录改到 ~/.ssh、/etc 等位置。"""
    roots = allowed_roots(load_web_settings())
    for key, value in merged.items():
        if key not in LOCAL_WRITE_PATH_KEYS:
            continue
        _check_write_path(key, value, roots)
    # 多 Aria2 时下载目录写在每条配置里，同样要过白名单
    for profile in merged.get("aria2_profiles") or []:
        if not isinstance(profile, dict):
            continue
        name = str(profile.get("name") or "").strip() or "未命名"
        for key in aria2cfg.PROFILE_PATH_KEYS:
            _check_write_path(f"aria2_profiles[{name}].{key}", profile.get(key), roots)


def save_section(basename: str, updates: dict) -> str:
    # 读-改-写要互斥：设置页保存与扫码登录回调可能同时写 conf/quark.yaml，
    # 各自读到旧文件再整份写回会丢掉对方刚写的项。
    with _index_lock():
        return _save_section_locked(basename, updates)


def _save_section_locked(basename: str, updates: dict) -> str:
    path = config_path(basename)
    current = load_section(basename) if os.path.exists(path) else {}
    merged = dict(current)
    updates = dict(updates or {})

    if basename == "aria2" and isinstance(updates.get("aria2_profiles"), (list, tuple)):
        # 多配置一次性整体替换：先合并掩码 secret，再校验，最后把旧版单配置字段清掉
        incoming = updates.pop("aria2_profiles")
        profiles = aria2cfg.merge_incoming(current, list(incoming))
        aria2cfg.validate_profiles(profiles)
        for key in aria2cfg.PROFILE_KEYS:
            merged.pop(key, None)
        merged["aria2_profiles"] = profiles
        names = [profile["name"] for profile in profiles]
        wanted = str(
            updates.get("aria2_default") or current.get("aria2_default") or ""
        ).strip()
        merged["aria2_default"] = wanted if wanted in names else (names[0] if names else "")
        updates.pop("aria2_default", None)  # 已按配置列表校正过，别再被通用循环覆盖

    secret_keys = SECRET_KEYS.get(basename, set())
    for key, value in updates.items():
        if key.endswith("__masked"):
            continue
        # 只有「掩码占位」才算未修改；空串是显式清空（例如把带令牌的 aria2 改成无令牌）
        if key in secret_keys and (value is None or _is_masked_placeholder(value)):
            continue
        # 分享链接改由 jobs 管理，高级配置不再覆盖
        if basename == "quark" and key in ("share_url", "share_pwd"):
            continue
        merged[key] = value

    _validate_write_paths(merged)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    _write_private_text(
        tmp,
        f"# {basename} config (managed by web UI)\n"
        + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
    )
    # 覆盖前留一份备份：Web 保存会把原文件里的注释一并冲掉
    if os.path.exists(path):
        try:
            shutil.copy2(path, f"{path}.bak")
            os.chmod(f"{path}.bak", 0o600)
        except OSError:
            pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def save_all_configs(payload: dict) -> dict[str, str]:
    saved = {}
    for basename in ("quark", "aria2"):
        section = payload.get(basename)
        if isinstance(section, dict):
            saved[basename] = save_section(basename, section)
    return saved


def tail_file(path: str, lines: int = 80, max_bytes: int = 512 * 1024) -> str:
    """读取文件末尾若干行；最多读 max_bytes，避免超大日志把内存撑爆。"""
    if not path or not os.path.isfile(path):
        return ""
    lines = max(1, min(_safe_int(lines, 80), 2000))
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = min(size, max_bytes)
        f.seek(size - chunk)
        data = f.read(chunk)
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])
