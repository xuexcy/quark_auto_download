#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克网盘 + OpenList + Aria2 自动批量下载脚本
功能：在有限的网盘空间内，自动循环"转存 → 下载 → 删除"
"""

import logging
import os
import re
import time

from aria2_api import Aria2Client
from config_loader import (
    BASE_DIR,
    CONF_DIR,
    load_config,
    resolve_config_path,
    resolve_legacy_config_path,
)
from file_api import FileApi
from log_util import configure_logging, dated_job_log_path
from openlist_api import OpenListClient
from pipeline import DownloadPipeline
from quark_api import QuarkClient

QUARK_CONFIG_PATH = resolve_config_path("QUARK_AUTO_DL_QUARK_CONFIG", "quark")
OPENLIST_CONFIG_PATH = resolve_config_path("QUARK_AUTO_DL_OPENLIST_CONFIG", "openlist")
ARIA2_CONFIG_PATH = resolve_config_path("QUARK_AUTO_DL_ARIA2_CONFIG", "aria2")
LEGACY_CONFIG_PATH = resolve_legacy_config_path()


def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return default


def _require(config: dict, key: str):
    value = config.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"配置项 `{key}` 不能为空")
    return value


def load_all_configs() -> tuple[dict, dict, dict]:
    has_new_split = all(
        os.path.exists(path) for path in (QUARK_CONFIG_PATH, OPENLIST_CONFIG_PATH, ARIA2_CONFIG_PATH)
    )
    if has_new_split:
        return (
            load_config(QUARK_CONFIG_PATH),
            load_config(OPENLIST_CONFIG_PATH),
            load_config(ARIA2_CONFIG_PATH),
        )

    if os.path.exists(LEGACY_CONFIG_PATH):
        merged = load_config(LEGACY_CONFIG_PATH)
        return merged, merged, merged

    raise FileNotFoundError(
        "配置文件不存在，请创建以下文件：\n"
        f"- {os.path.join(CONF_DIR, 'quark.yaml')}\n"
        f"- {os.path.join(CONF_DIR, 'openlist.yaml')}\n"
        f"- {os.path.join(CONF_DIR, 'aria2.yaml')}\n"
        "（也可使用 .yml，或继续使用旧版 conf/config.yaml）"
    )


QUARK_CONFIG, OPENLIST_CONFIG, ARIA2_CONFIG = load_all_configs()

# 任务级覆盖（Web 多分享链接）
JOB_ID = os.getenv("QUARK_AUTO_DL_JOB_ID", "").strip()
if os.getenv("QUARK_AUTO_DL_SHARE_URL", "").strip():
    QUARK_CONFIG = dict(QUARK_CONFIG)
    QUARK_CONFIG["share_url"] = os.environ["QUARK_AUTO_DL_SHARE_URL"].strip()
    if "QUARK_AUTO_DL_SHARE_PWD" in os.environ:
        QUARK_CONFIG["share_pwd"] = os.environ.get("QUARK_AUTO_DL_SHARE_PWD", "")

QUARK_COOKIE = _require(QUARK_CONFIG, "quark_cookie")
SHARE_URL = _require(QUARK_CONFIG, "share_url")
SHARE_PWD = QUARK_CONFIG.get("share_pwd", "")
SAVE_TO_DIR = _require(QUARK_CONFIG, "save_to_dir")
FILE_NAME_REGEX = QUARK_CONFIG.get("file_name_regex", "").strip()

OPENLIST_HOST = _require(OPENLIST_CONFIG, "openlist_host").rstrip("/")
OPENLIST_TOKEN = OPENLIST_CONFIG.get("openlist_token", "")
OPENLIST_QUARK_PATH = _require(OPENLIST_CONFIG, "openlist_quark_path").rstrip("/")
OPENLIST_REQUEST_TIMEOUT = int(OPENLIST_CONFIG.get("request_timeout", 10))

ARIA2_HOST = _require(ARIA2_CONFIG, "aria2_host")
ARIA2_SECRET = ARIA2_CONFIG.get("aria2_secret", "")
ARIA2_DOWNLOAD_DIR = _require(ARIA2_CONFIG, "aria2_download_dir")
DOWNLOAD_DESTINATION_DIR = ARIA2_CONFIG.get("download_destination_dir", ARIA2_DOWNLOAD_DIR)
POLL_INTERVAL = int(ARIA2_CONFIG.get("poll_interval", 60))
DOWNLOAD_TIMEOUT = int(ARIA2_CONFIG.get("download_timeout", 7200))
ARIA2_REQUEST_TIMEOUT = int(ARIA2_CONFIG.get("request_timeout", 10))
ARIA2_MAX_RETRIES = int(ARIA2_CONFIG.get("aria2_max_retries", 3))
DOWNLOAD_LOG_INTERVAL = int(ARIA2_CONFIG.get("download_log_interval", 300))
DOWNLOAD_SUBMIT_MAX_RETRIES = int(ARIA2_CONFIG.get("download_submit_max_retries", 10))
DOWNLOAD_SUBMIT_RETRY_INTERVAL = int(ARIA2_CONFIG.get("download_submit_retry_interval", 30))
STRICT_DOWNLOAD_ORDER = _as_bool(ARIA2_CONFIG.get("strict_download_order"), True)
ARIA2_QUEUE_PAUSE_THRESHOLD = int(ARIA2_CONFIG.get("aria2_queue_pause_threshold", 700))
ARIA2_QUEUE_RESUME_THRESHOLD = int(ARIA2_CONFIG.get("aria2_queue_resume_threshold", 500))
STATE_FILE = str(
    os.getenv("QUARK_AUTO_DL_STATE_FILE")
    or ARIA2_CONFIG.get("state_file")
    or os.path.join(BASE_DIR, "state", "pipeline_state.yaml")
).strip()
CONTROL_FILE = str(os.getenv("QUARK_AUTO_DL_CONTROL_FILE") or "").strip()
VERIFY_REPORT_FILE = str(
    os.getenv("QUARK_AUTO_DL_VERIFY_REPORT_FILE")
    or ARIA2_CONFIG.get("verify_report_file")
    or os.path.join(BASE_DIR, "state", "download_verify_report.yaml")
).strip()
QUARK_REQUEST_TIMEOUT = int(QUARK_CONFIG.get("request_timeout", 10))
QUARK_TASK_POLL_INTERVAL = int(QUARK_CONFIG.get("task_poll_interval", 3))
QUARK_TASK_TIMEOUT = int(QUARK_CONFIG.get("task_timeout", 180))
FILE_NAME_PATTERN = None
if FILE_NAME_REGEX:
    try:
        FILE_NAME_PATTERN = re.compile(FILE_NAME_REGEX)
    except re.error as e:
        raise ValueError(f"配置项 `file_name_regex` 不是有效正则: {e}") from e

RUN_DATE = time.strftime("%Y_%m_%d")
RUN_TIME = time.strftime("%H_%M_%S")
JOB_LOG_FILE = os.getenv("QUARK_AUTO_DL_JOB_LOG_FILE", "").strip()
if JOB_ID:
    LOG_PATH = JOB_LOG_FILE or dated_job_log_path(JOB_ID)
else:
    LOG_PATH = dated_job_log_path("quark_main")
LOG_DIR = os.path.dirname(LOG_PATH) or os.path.join(BASE_DIR, "log", RUN_DATE, "jobs")

# 统一将 print / traceback / warnings / logging 写入同一日志文件（行缓冲追加）
configure_logging(LOG_PATH)
log = logging.getLogger(__name__)


def init_clients():
    """初始化 Quark、OpenList 和 Aria2 客户端。"""
    quark_client = QuarkClient(
        cookie=QUARK_COOKIE,
        request_timeout=QUARK_REQUEST_TIMEOUT,
        save_to_dir=SAVE_TO_DIR,
        share_url=SHARE_URL,
        share_pwd=SHARE_PWD,
        logger=log,
        task_poll_interval=QUARK_TASK_POLL_INTERVAL,
        task_timeout=QUARK_TASK_TIMEOUT,
    )
    quark_client.resolve_save_to_dir()
    openlist_client = OpenListClient(
        host=OPENLIST_HOST,
        token=OPENLIST_TOKEN,
        request_timeout=OPENLIST_REQUEST_TIMEOUT,
    )
    aria2_client = Aria2Client(
        host=ARIA2_HOST,
        secret=ARIA2_SECRET,
        download_dir=ARIA2_DOWNLOAD_DIR,
        request_timeout=ARIA2_REQUEST_TIMEOUT,
        openlist_token=OPENLIST_TOKEN,
        poll_interval=POLL_INTERVAL,
        download_timeout=DOWNLOAD_TIMEOUT,
        logger=log,
    )
    return quark_client, openlist_client, aria2_client


def load_share_files(quark_client):
    """获取分享链接信息并列出待转存的分享文件列表。"""
    log.info("▶ 正在获取分享链接信息...")
    stoken, share_id, root_fid = quark_client.get_share_token()
    _, url_folder_fid = quark_client.parse_share_url()
    if url_folder_fid:
        target_fid = url_folder_fid
        log.info(f"  使用分享链接中的子目录 fid: {url_folder_fid}")
    else:
        target_fid = root_fid
        log.info("  未在分享链接中解析到子目录 fid，使用分享根目录")

    log.info(f"  share_id={share_id}  root_fid={root_fid}  target_fid={target_fid}")

    log.info("▶ 正在获取文件列表...")
    all_files = quark_client.list_share_files(share_id, stoken, target_fid)
    log.info(f"  共找到 {len(all_files)} 个文件")
    all_files = sorted(all_files, key=FileApi.file_sort_key)
    for f in all_files:
        log.info(f"    {FileApi.file_path(f)}  ({FileApi.format_size(f.get('size', 0))})")

    if FILE_NAME_PATTERN is not None:
        before_count = len(all_files)
        all_files = [f for f in all_files if FILE_NAME_PATTERN.search(FileApi.file_path(f))]
        log.info(f"▶ 按正则过滤: {FILE_NAME_REGEX}")
        log.info(f"  过滤后剩余 {len(all_files)}/{before_count} 个文件")

    if not all_files:
        log.warning("未找到任何文件，退出。")

    return stoken, share_id, target_fid, all_files


def main():
    quark_client, openlist_client, aria2_client = init_clients()
    file_api = FileApi(
        quark_client=quark_client,
        openlist_client=openlist_client,
        aria2_client=aria2_client,
        save_to_fid=quark_client.save_to_fid,
        openlist_quark_path=OPENLIST_QUARK_PATH,
        aria2_download_dir=ARIA2_DOWNLOAD_DIR,
        download_destination_dir=DOWNLOAD_DESTINATION_DIR,
        aria2_max_retries=ARIA2_MAX_RETRIES,
        download_log_interval=DOWNLOAD_LOG_INTERVAL,
        logger=log,
        download_submit_max_retries=DOWNLOAD_SUBMIT_MAX_RETRIES,
        download_submit_retry_interval=DOWNLOAD_SUBMIT_RETRY_INTERVAL,
    )

    log.info("═" * 60)
    log.info("夸克网盘自动批量下载脚本启动")
    if JOB_ID:
        log.info(f"任务 ID: {JOB_ID}")
    log.info("═" * 60)
    log.info(f"本次日志文件: {LOG_PATH}")
    log.info(f"分享链接: {SHARE_URL}")
    log.info(f"下载临时目录: {ARIA2_DOWNLOAD_DIR}")
    log.info(f"下载完成目录: {DOWNLOAD_DESTINATION_DIR}")

    stoken, share_id, target_fid, all_files = load_share_files(quark_client)
    if not all_files:
        return

    order_desc = (
        "转存+入待下载队列有序（失败跳过；下载可并发）"
        if STRICT_DOWNLOAD_ORDER
        else "转存可插队填容量；下载可并发"
    )
    log.info(f"▶ 调度器+Worker 启动，共 {len(all_files)} 个文件（{order_desc}）")
    log.info(f"状态文件: {STATE_FILE}")
    if CONTROL_FILE:
        log.info(f"控制文件: {CONTROL_FILE}")
    pipeline = DownloadPipeline(
        file_api=file_api,
        share_id=share_id,
        stoken=stoken,
        poll_interval=POLL_INTERVAL,
        logger=log,
        strict_download_order=STRICT_DOWNLOAD_ORDER,
        state_file=STATE_FILE,
        aria2_queue_pause_threshold=ARIA2_QUEUE_PAUSE_THRESHOLD,
        aria2_queue_resume_threshold=ARIA2_QUEUE_RESUME_THRESHOLD,
        control_file=CONTROL_FILE,
        job_id=JOB_ID,
    )
    pipeline.initialize(all_files)
    failed_files = pipeline.run()

    # 若由用户暂停退出，更新 jobs 索引状态
    if JOB_ID and pipeline._user_pause_mode:
        try:
            from jobs_manager import get_job, upsert_job

            job = get_job(JOB_ID)
            if job:
                job["status"] = "paused"
                job["message"] = (
                    "软暂停完成" if pipeline._user_pause_mode == "soft" else "硬暂停完成"
                )
                job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                upsert_job(job)
        except Exception as e:
            log.warning(f"更新任务状态失败: {e}")

    if pipeline._user_pause_mode:
        log.info(f"流水线因用户暂停退出（mode={pipeline._user_pause_mode}）")
        return

    log.info("\n" + "═" * 60)
    log.info("全部批次处理完毕！")
    run_verify_report = os.path.join(LOG_DIR, f"download_verify_report.{RUN_TIME}.yaml")
    verify_report = pipeline.verify_completed(VERIFY_REPORT_FILE)
    # 同步一份到本次运行日志目录，便于归档
    if os.path.abspath(run_verify_report) != os.path.abspath(VERIFY_REPORT_FILE):
        from verify_download import write_verify_report

        write_verify_report(verify_report, run_verify_report)
        log.info(f"  本次运行报告副本: {run_verify_report}")
    failed_count = int(verify_report.get("summary", {}).get("failed", 0) or 0)
    if failed_count or failed_files:
        log.warning(
            f"存在未成功下载的文件：校验失败 {failed_count} 个，"
            f"流水线失败列表 {len(failed_files)} 个；详见报告 {VERIFY_REPORT_FILE}"
        )
    else:
        log.info("所有文件下载成功，completed 目录校验通过 🎉")

    if JOB_ID:
        try:
            from jobs_manager import get_job, upsert_job

            job = get_job(JOB_ID)
            if job:
                job["status"] = "paused"
                job["message"] = "全部处理完毕（已回到暂停）"
                job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                upsert_job(job)
        except Exception as e:
            log.warning(f"更新任务状态失败: {e}")


if __name__ == "__main__":
    main()
