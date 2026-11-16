# -*- coding: utf-8 -*-
"""运行环境自检：本进程由谁托管、开机自启是否开启（**只读**）。

设计前提（见 README「为什么没有『开机自启』开关」）：
- 只读：不调用需要 sudo 的命令，不修改任何系统配置；
- unit 名从 /proc/self/cgroup 解析，不读配置，因此代码层面不可能去操作别的 unit；
- 读不到 systemd（macOS / 容器 / 手动 uvicorn）时如实返回 pidfile 或 unknown，不猜。
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

from config_loader import BASE_DIR

CGROUP_PATH = "/proc/self/cgroup"
PID_FILE = os.path.join(BASE_DIR, "state", "web.pid")
UNIT_SUFFIX = ".service"
SHOW_KEYS = ("ActiveState", "UnitFileState", "Restart", "FragmentPath", "MainPID")
CACHE_TTL = 10.0

_unit_cache: list[str | None] = []
_info_cache: dict[str, Any] = {"at": 0.0, "value": None}


def reset_cache() -> None:
    """测试用：清掉 unit 与 runtime_info 的缓存。"""
    _unit_cache.clear()
    _info_cache["at"] = 0.0
    _info_cache["value"] = None


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def unit_from_cgroup(text: str | None) -> str | None:
    """从 /proc/self/cgroup 取出本进程所属的 .service 名（不含后缀）。

    systemd 下形如 `0::/system.slice/quark_auto_download.service`；
    docker/容器通常是 `0::/`，桌面会话是 `.../session-1.scope`，都返回 None。
    """
    if not text:
        return None
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        segments = [s for s in parts[2].split("/") if s]
        if not segments:
            continue
        last = segments[-1]
        if last.endswith(UNIT_SUFFIX) and len(last) > len(UNIT_SUFFIX):
            return last[: -len(UNIT_SUFFIX)]
    return None


def own_unit() -> str | None:
    """本进程所属的 systemd unit 名（不带 .service）；不在 systemd 下则为 None。"""
    if not _unit_cache:
        _unit_cache.append(unit_from_cgroup(_read_text(CGROUP_PATH)))
    return _unit_cache[0]


def _systemctl_show(unit: str) -> dict[str, str]:
    """`systemctl show -p …`（普通用户可读，不需要 sudo）。失败/不可用返回 {}。"""
    cmd = ["systemctl", "show"]
    for key in SHOW_KEYS:
        cmd += ["-p", key]
    cmd.append(unit)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    data: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    return data


def _pidfile_is_mine() -> bool:
    """bin/start.sh 会把 pid 写进 state/web.pid；命中说明我们是脚本托管的。"""
    text = _read_text(PID_FILE)
    if not text:
        return False
    try:
        return int(text.strip().split()[0]) == os.getpid()
    except (ValueError, IndexError):
        return False


def _autostart_from_state(state: str | None) -> bool | None:
    if not state:
        return None
    if state.startswith("enabled"):
        return True
    if state.startswith("disabled") or state.startswith("masked"):
        return False
    return None


def runtime_info(refresh: bool = False) -> dict[str, Any]:
    """当前运行环境快照（默认缓存 10 秒，避免每次 /api/health 都 fork systemctl）。"""
    now = time.time()
    cached = _info_cache.get("value")
    if not refresh and cached and now - float(_info_cache.get("at") or 0.0) < CACHE_TTL:
        return dict(cached)

    unit = own_unit()
    show = _systemctl_show(unit) if unit else {}
    supervised = bool(show) and (
        show.get("MainPID") == str(os.getpid())
        or os.environ.get("SYSTEMD_EXEC_PID") == str(os.getpid())
    )
    if supervised:
        supervisor = "systemd"
    elif _pidfile_is_mine():
        supervisor = "pidfile"
    else:
        supervisor = "unknown"

    unit_file_state = show.get("UnitFileState") or None
    info: dict[str, Any] = {
        "supervisor": supervisor,
        "unit": unit,
        "unit_file_state": unit_file_state,
        "autostart": _autostart_from_state(unit_file_state),
        "restart_policy": (show.get("Restart") or None) if supervisor == "systemd" else None,
        "restart_hint": f"sudo systemctl restart {unit}" if unit else None,
    }
    _info_cache["at"] = now
    _info_cache["value"] = dict(info)
    return info


def can_self_restart(info: dict[str, Any] | None = None) -> bool:
    """systemd 托管且 Restart=always 时，退出进程就能被拉起（不用 sudo）。"""
    info = info if info is not None else runtime_info()
    return info.get("supervisor") == "systemd" and info.get("restart_policy") == "always"

