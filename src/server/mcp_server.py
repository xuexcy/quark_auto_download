# -*- coding: utf-8 -*-
"""MCP server（面向 agent 的下载入口），挂载在 Web 服务的 /mcp 路径下。

给 NAS 上的 DSH agent（或其他支持 MCP 的客户端）用：对话里发夸克链接 →
agent 调 submit_download 工具提交下载；用 job_status / list_downloads 查进度。

安全约定：
- /mcp 不走 HTTP Basic 登录（MCP 客户端不是浏览器），改用独立 Bearer token：
  conf/web.yaml 配 `mcp_token: <随机串>`（或环境变量 QUARK_AUTO_DL_MCP_TOKEN）。
  未配置 token 时拒绝启动 MCP 挂载，避免把下载入口暴露给局域网任何人。
- 复用 Web 服务的 Host 白名单中间件；同源校验对无 Origin 的 MCP 客户端天然放行。

工具内部全部复用 jobs_manager 现有逻辑，不做新的下载实现。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import jobs_manager as jm

logger = logging.getLogger(__name__)

# submit_download 不带 name 时自动生成的任务名前缀（保证唯一，不与手建任务重名）
_AUTO_NAME_PREFIX = "MCP-"


def resolve_share_job(share_url: str, name: str = "") -> dict:
    """按分享链接找已有任务；找不到就创建。

    - 带 name：优先精确匹配同名任务；不存在则用该名字创建（重名会抛 ValueError）
    - 不带 name：先按 share_key 匹配已有任务（同链接复用，勾选是追加语义），
      都没有就自动起名 `MCP-<分享id>`（冲突时加时间戳后缀）
    返回 (job, created)。
    """
    share_url = str(share_url or "").strip()
    name = str(name or "").strip()
    if name:
        for job in jm.list_jobs():
            if str(job.get("name") or "").strip() == name:
                return job, False
        job = jm.add_job(share_url, name=name)
        return job, True
    key = jm.share_key(share_url)
    for job in jm.list_jobs():
        if jm.share_key(str(job.get("share_url") or "")) == key:
            return job, False
    auto = _auto_name(share_url)
    for job in jm.list_jobs():
        if str(job.get("name") or "").strip() == auto:
            auto = f"{auto}-{time.strftime('%H%M%S')}"
            break
    job = jm.add_job(share_url, name=auto)
    return job, True


def _auto_name(share_url: str) -> str:
    match = re.search(r"/s/([0-9A-Za-z_-]+)", share_url)
    return f"{_AUTO_NAME_PREFIX}{match.group(1) if match else time.strftime('%Y%m%d%H%M%S')}"


def add_selection(job_id: str, paths: list[str]) -> dict:
    """把「路径」（相对分享根的文件/目录路径）转成勾选并追加到任务。

    MCP 工具面向对话场景，调用方通常只有路径而不是 fid；这里先浏览分享目录树
    找到对应条目拿 fid。支持只给文件名（在整棵树里模糊匹配唯一条目时可用）。
    空列表 = 不限定范围（下载整棵分享目录）。
    返回 {added, duplicates, unmatched}。
    """
    from quark_api import QuarkClient
    from config_loader import load_merged_configs

    job = jm.get_job(job_id)
    if not job:
        raise ValueError(f"任务不存在: {job_id}")
    wanted = [p.strip().lstrip("/") for p in (paths or []) if str(p).strip()]
    if not wanted:
        return {"added": [], "duplicates": [], "unmatched": []}

    # 递归拉整棵分享树（一次性；条目数受单分享规模限制，对话场景可接受）
    entries = _walk_share_tree(job)
    file_fids: list[str] = []
    dir_fids: list[str] = []
    unmatched: list[str] = []
    for path in wanted:
        hit = _match_entry(entries, path)
        if hit is None:
            unmatched.append(path)
        elif hit.get("is_dir"):
            dir_fids.append(str(hit.get("fid")))
        else:
            file_fids.append(str(hit.get("fid")))
    result = {"added": [], "duplicates": [], "unmatched": unmatched}
    if file_fids or dir_fids:
        sel = jm.add_job_selection(job_id, {"file_fids": file_fids, "dir_fids": dir_fids})
        result["added"] = sel.get("added") or []
        result["duplicates"] = sel.get("duplicates") or []
    return result


def _walk_share_tree(job: dict) -> list[dict]:
    """递归枚举分享目录，返回带 path 的条目列表（顶层 path = 文件名）。"""
    from quark_api import QuarkClient
    from config_loader import load_merged_configs

    config = load_merged_configs()
    client = QuarkClient(
        cookie=str(config.get("quark_cookie") or ""),
        request_timeout=15,
        save_to_dir="",
        share_url=str(job.get("share_url") or ""),
        share_pwd=str(job.get("share_pwd") or ""),
        logger=logger,
    )
    stoken, share_id, root_fid = client.get_share_token()
    _, url_folder_fid = client.parse_share_url()
    base_fid = url_folder_fid or root_fid
    out: list[dict] = []

    def walk(fid: str, prefix: str) -> None:
        for entry in client.list_share_dir_entries(share_id, stoken, fid):
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            path = f"{prefix}/{name}" if prefix else name
            item = {**entry, "path": path}
            out.append(item)
            if entry.get("is_dir") and len(out) < 5000:
                walk(str(entry.get("fid")), path)

    walk(base_fid, "")
    return out


def _match_entry(entries: list[dict], path: str) -> dict | None:
    """先精确匹配 path，再退化为「文件名唯一命中」。"""
    for entry in entries:
        if str(entry.get("path")) == path:
            return entry
    hits = [e for e in entries if str(e.get("path")).rsplit("/", 1)[-1] == path]
    return hits[0] if len(hits) == 1 else None


def summarize_job(job: dict) -> dict:
    """把任务归一成 agent 友好的状态摘要（job_status / list 工具共用）。"""
    runtime = jm.refresh_job_runtime(dict(job), persist=False)
    stats = runtime.get("stats") or {}
    counts = runtime.get("counts") or {}
    total = int(stats.get("total_files") or 0)
    done = int(stats.get("done_files") or 0)
    failed = runtime.get("failed_files") or []
    running = bool(runtime.get("running"))
    if running:
        phase = "downloading"
    elif total > 0 and done >= total and not failed:
        phase = "completed"
    elif total > 0 and done >= total and failed:
        phase = "completed_with_failures"
    else:
        phase = "paused"
    return {
        "job_id": str(runtime.get("id")),
        "name": str(runtime.get("name")),
        "status": phase,
        "running": running,
        "total_files": total,
        "done_files": done,
        "failed_files": failed,
        "counts": counts,
        "percent": runtime.get("existing_or_done_files_percent"),
        "total_size": runtime.get("total_size"),
        "message": str(runtime.get("message") or ""),
        "log_path": str(runtime.get("log_path") or ""),
    }


def failed_files_detail(job_id: str, limit: int = 50) -> list[dict]:
    """失败文件明细（供 agent 提示用户哪些没下成）。"""
    files = jm.job_files(job_id, limit=5000).get("files") or []
    out = []
    for item in files:
        if str(item.get("state")) != "failed":
            continue
        out.append(
            {
                "path": str(item.get("path") or ""),
                "note": str(item.get("note") or ""),
                "size": item.get("size"),
            }
        )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# fastmcp 工具定义（薄封装，全部转调上面的纯函数 + jobs_manager）
# ---------------------------------------------------------------------------


def build_mcp_server():
    """创建 FastMCP 实例并注册工具；由 web_app 挂载到 /mcp。延迟 import，
    避免 fastmcp 缺失时整个 Web 服务起不来。"""
    from fastmcp import FastMCP

    mcp = FastMCP(
        "quark-download",
        instructions=(
            "夸克网盘自动下载服务。用户发来夸克分享链接时调用 submit_download 提交下载，"
            "用 job_status 跟踪进度，全部完成时向用户汇报结果（含失败文件）。"
            "同一链接重复提交不会重复建任务（按链接复用已有任务、勾选追加）。"
        ),
    )

    @mcp.tool
    def submit_download(
        share_url: str,
        share_pwd: str = "",
        paths: Optional[list[str]] = None,
        name: str = "",
        aria2_profile: str = "",
    ) -> dict:
        """提交夸克网盘分享链接开始下载。

        Args:
            share_url: 夸克分享链接（必须包含 /s/，如 https://pan.quark.cn/s/xxxx）
            share_pwd: 提取码，没有则留空
            paths: 只下部分文件/目录时传相对路径列表（如 ["第01集.mp4"] 或 ["Season 1"]）；
                留空 = 下载整棵分享目录
            name: 任务显示名，留空则自动命名；同链接重复提交会复用已有任务（追加语义）
            aria2_profile: Aria2 配置名，留空用默认
        Returns:
            {job_id, name, status, total_files, ...}：任务摘要，status 为 downloading/paused 等
        """
        job, created = resolve_share_job(share_url, name)
        job_id = str(job.get("id"))
        # 复用已有任务时带上新提取码（分享链接可能换码）；新建时 add_job 已带
        if not created and share_pwd:
            jm.update_job_pwd(job_id, share_pwd)
        sel = add_selection(job_id, paths or [])
        if aria2_profile:
            jm.set_job_aria2_profile(job_id, aria2_profile)
        started = jm.start_job(job_id)
        summary = summarize_job(started)
        summary["created"] = created
        summary["selection_added"] = len(sel.get("added") or [])
        summary["duplicates"] = sel.get("duplicates") or []
        summary["unmatched_paths"] = sel.get("unmatched") or []
        return summary

    @mcp.tool
    def job_status(job_id: str) -> dict:
        """查询单个下载任务的进度。

        Args:
            job_id: submit_download 返回的任务 ID
        Returns:
            {status: downloading|completed|completed_with_failures|paused, done_files,
             total_files, failed_files, percent, ...}
        """
        job = jm.get_job(job_id)
        if not job:
            raise ValueError(f"任务不存在: {job_id}")
        return summarize_job(job)

    @mcp.tool
    def list_downloads() -> dict:
        """列出全部下载任务及状态摘要。"""
        jobs = jm.list_jobs()
        return {"jobs": [summarize_job(job) for job in jobs]}

    @mcp.tool
    def job_failures(job_id: str) -> dict:
        """列出任务里下载失败的文件明细（路径 + 原因）。"""
        if not jm.get_job(job_id):
            raise ValueError(f"任务不存在: {job_id}")
        return {"failures": failed_files_detail(job_id)}

    return mcp
