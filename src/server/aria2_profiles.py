# -*- coding: utf-8 -*-
"""多 Aria2 配置：解析、迁移与选择。

`conf/aria2.yaml` 支持两种形态：

1. 旧版单配置（2.5.x 及以前，仍可直接用）::

       aria2_host: "http://127.0.0.1:6800/jsonrpc"
       aria2_secret: "xxx"
       aria2_download_dir: "/vol2/.../downloads"
       download_destination_dir: "/vol2/.../done"

2. 多配置（2.6.0 起，设置页「＋」添加）::

       aria2_profiles:
         - name: 本地
           aria2_host: "http://127.0.0.1:6800/jsonrpc"
           aria2_secret: "xxx"
           aria2_download_dir: "/vol2/.../downloads"
           download_destination_dir: "/vol2/.../done"
         - name: 另一台
           ...
       aria2_default: 本地

读旧版时只在内存里迁移成一条名为「默认」的配置（下次在设置页保存才写盘）。

除 PROFILE_KEYS 之外的键（poll_interval / 超时 / 排队阈值等）是**全局项**，
所有配置共用，避免每加一台 aria2 都要重复填一遍。
"""

from typing import Any

# 每条配置独有的字段（其余键都是全局项）
PROFILE_KEYS = (
    "aria2_host",
    "aria2_secret",
    "aria2_download_dir",
    "download_destination_dir",
)
# 可选字段：远端 aria2 的下载目录（对方机器上的绝对路径）。
# 对方默认目录不一定可写（实测某台机器 /downloads 只读 → 每个文件 errorCode=16），
# 填了就在这里提交 addUri 时带上 dir；留空则完全用对方的全局配置。
PROFILE_OPTIONAL_KEYS = ("aria2_remote_dir",)
# 所有按配置独有处理的字段（必填 + 可选）
PROFILE_ALL_KEYS = PROFILE_KEYS + PROFILE_OPTIONAL_KEYS
# 本机写入路径，保存前要过白名单校验（远端目录在对方机器上，不能过本机白名单）
PROFILE_PATH_KEYS = ("aria2_download_dir", "download_destination_dir")
# 每条配置的开关（布尔值）：远端 aria2 = 只提交下载请求 + 查进度，不管对方的落盘目录
PROFILE_FLAGS = ("aria2_remote",)
DEFAULT_PROFILE_NAME = "默认"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "y")


def mask_secret(value: Any) -> str:
    """与设置页其它敏感字段一致：长度 <= 8 全星号，否则前 4 + 星号 + 后 4。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "*" * (len(text) - 8) + text[-4:]


def _legacy_profile(config: dict) -> dict | None:
    """旧版单配置 → 一条「默认」配置；一个字段都没有时返回 None。"""
    if not any(_text(config.get(key)) for key in PROFILE_KEYS):
        return None
    profile = {"name": DEFAULT_PROFILE_NAME}
    for key in PROFILE_ALL_KEYS:
        profile[key] = _text(config.get(key))
    profile["aria2_remote"] = _truthy(config.get("aria2_remote"))
    return profile


def _dedupe(name: str, seen: set[str]) -> str:
    name = name or "aria2"
    base = name
    suffix = 2
    while name in seen:
        name = f"{base}-{suffix}"
        suffix += 1
    seen.add(name)
    return name


def profiles_of(config: dict | None) -> list[dict]:
    """规范化配置列表（旧版自动迁移）。空字段补成空串，名字去重。"""
    config = config if isinstance(config, dict) else {}
    raw = config.get("aria2_profiles")
    items: list[dict] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                items.append(dict(item))
    elif isinstance(raw, dict):
        # 容忍 {名字: {...}} 的写法
        for name, item in raw.items():
            if isinstance(item, dict):
                entry = dict(item)
                entry.setdefault("name", str(name))
                items.append(entry)
    if not items:
        legacy = _legacy_profile(config)
        return [legacy] if legacy else []

    result: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        profile: dict[str, Any] = {"name": _dedupe(_text(item.get("name")) or f"aria2-{index + 1}", seen)}
        for key in PROFILE_ALL_KEYS:
            profile[key] = _text(item.get(key))
        profile["aria2_remote"] = _truthy(item.get("aria2_remote"))
        if not any(profile[key] for key in PROFILE_KEYS):
            continue  # 整条空白（设置页加了行没填），直接忽略
        result.append(profile)
    return result


def profile_names(config: dict | None) -> list[str]:
    return [profile["name"] for profile in profiles_of(config)]


def default_name(config: dict | None) -> str:
    """aria2_default 指向的配置名；没配置或指向不存在的名字时退回第一条。"""
    names = profile_names(config)
    if not names:
        return ""
    wanted = _text((config or {}).get("aria2_default"))
    return wanted if wanted in names else names[0]


def resolve_name(config: dict | None, name: str | None) -> str:
    """把任务上记的名字解析成真实配置名：空 / 已被删除都退回默认配置。"""
    names = profile_names(config)
    if not names:
        return ""
    wanted = _text(name)
    return wanted if wanted in names else default_name(config)


def options_for(config: dict | None, name: str | None = None) -> dict:
    """全局项 + 选中配置的 PROFILE_KEYS，形如旧版单配置，供运行时代码原样读取。"""
    config = config if isinstance(config, dict) else {}
    profiles = profiles_of(config)
    chosen: dict | None = None
    if profiles:
        wanted = _text(name) or default_name(config)
        chosen = next((p for p in profiles if p["name"] == wanted), None)
        if chosen is None:
            fallback = default_name(config) or profiles[0]["name"]
            chosen = next((p for p in profiles if p["name"] == fallback), profiles[0])
    options = {
        key: value
        for key, value in config.items()
        if key not in ("aria2_profiles", "aria2_default") and key not in PROFILE_KEYS
    }
    if chosen:
        options.update({key: chosen.get(key, "") for key in PROFILE_ALL_KEYS})
        options.update({flag: bool(chosen.get(flag)) for flag in PROFILE_FLAGS})
    else:
        options.setdefault("aria2_remote", False)
    return options


def public_profiles(profiles: list[dict]) -> list[dict]:
    """给前端看：secret 掩码并标 __masked，保存时原样回传即可保留原值。"""
    result: list[dict] = []
    for profile in profiles or []:
        item = dict(profile)
        secret = item.get("aria2_secret") or ""
        if secret:
            item["aria2_secret"] = mask_secret(secret)
            item["aria2_secret__masked"] = True
        result.append(item)
    return result


def merge_incoming(current: dict | None, incoming: list | None) -> list[dict]:
    """保存前合并：掩码/空 secret 沿用同名旧配置的 secret；整条空白丢弃。

    注意按**名字**匹配——重命名配置后需要重新填一次 Secret（设置页有提示）。
    """
    old = {profile["name"]: profile for profile in profiles_of(current)}
    result: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(incoming or []):
        if not isinstance(item, dict):
            continue
        name = _dedupe(_text(item.get("name")) or f"aria2-{index + 1}", seen)
        profile: dict[str, str] = {"name": name}
        for key in PROFILE_ALL_KEYS:
            value = _text(item.get(key))
            if key == "aria2_secret" and (not value or "*" in value):
                value = _text((old.get(name) or {}).get("aria2_secret"))
            profile[key] = value
        profile["aria2_remote"] = _truthy(item.get("aria2_remote"))
        if not any(profile[key] for key in PROFILE_KEYS):
            continue
        result.append(profile)
    return result


def validate_profiles(profiles: list[dict]) -> None:
    """校验每条配置。

    下载目录不再是必填项：留空表示用 aria2 自己的默认目录（aria2.conf 里的 dir，
    启动时通过 aria2.getGlobalOption 读取）。远端配置本来就不需要本机目录。
    """
    for profile in profiles or []:
        name = profile.get("name") or "未命名"
        if not _text(profile.get("aria2_host")):
            raise ValueError(f"Aria2 配置「{name}」缺少 RPC Host")

