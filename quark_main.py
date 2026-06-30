#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克网盘 + OpenList + Aria2 自动批量下载脚本
功能：在有限的网盘空间内，自动循环"转存 → 下载 → 删除"
"""

import json
import logging
import os
import re
import time

from aria2_api import Aria2Client
from file_api import ACTIVE_ARIA2_STATUSES, FileApi
from openlist_api import OpenListClient
from quark_api import QuarkClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.join(BASE_DIR, "conf")
LEGACY_CONFIG_PATH = os.path.join(CONF_DIR, "config.json")
QUARK_CONFIG_PATH = os.getenv("QUARK_AUTO_DL_QUARK_CONFIG", os.path.join(CONF_DIR, "quark.json"))
OPENLIST_CONFIG_PATH = os.getenv("QUARK_AUTO_DL_OPENLIST_CONFIG", os.path.join(CONF_DIR, "openlist.json"))
ARIA2_CONFIG_PATH = os.getenv("QUARK_AUTO_DL_ARIA2_CONFIG", os.path.join(CONF_DIR, "aria2.json"))


def load_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}"
        )
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


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

    missing_files = [
        path
        for path in (QUARK_CONFIG_PATH, OPENLIST_CONFIG_PATH, ARIA2_CONFIG_PATH)
        if not os.path.exists(path)
    ]
    if missing_files:
        raise FileNotFoundError(
            "配置文件不存在，请创建以下文件：\n"
            f"- {QUARK_CONFIG_PATH}\n"
            f"- {OPENLIST_CONFIG_PATH}\n"
            f"- {ARIA2_CONFIG_PATH}\n"
            "（或继续使用旧版 conf/config.json）"
        )


QUARK_CONFIG, OPENLIST_CONFIG, ARIA2_CONFIG = load_all_configs()

QUARK_COOKIE = _require(QUARK_CONFIG, "quark_cookie")
SHARE_URL = _require(QUARK_CONFIG, "share_url")
SHARE_PWD = QUARK_CONFIG.get("share_pwd", "")
SHARE_SUB_DIR = QUARK_CONFIG.get("share_sub_dir", "")
SAVE_TO_DIR = _require(QUARK_CONFIG, "save_to_dir")
FILE_NAME_REGEX = QUARK_CONFIG.get("file_name_regex", "").strip()

OPENLIST_HOST = _require(OPENLIST_CONFIG, "openlist_host").rstrip("/")
OPENLIST_TOKEN = OPENLIST_CONFIG.get("openlist_token", "")
OPENLIST_QUARK_PATH = _require(OPENLIST_CONFIG, "openlist_quark_path").rstrip("/")
OPENLIST_REQUEST_TIMEOUT = int(OPENLIST_CONFIG.get("request_timeout", 10))
OPENLIST_REFRESH_DELAY = int(OPENLIST_CONFIG.get("refresh_delay_after_transfer", 30))

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
LOG_DIR = os.path.join(BASE_DIR, "log", RUN_DATE)
LOG_PATH = os.path.join(LOG_DIR, f"quark_main.{RUN_TIME}.log")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
    force=True,
)
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
    target_fid = quark_client.resolve_sub_dir_fid(share_id, stoken, root_fid, SHARE_SUB_DIR)
    log.info(f"  share_id={share_id}  root_fid={root_fid}  target_fid={target_fid}")

    log.info("▶ 正在获取文件列表...")
    all_files = quark_client.list_share_files(share_id, stoken, target_fid)
    log.info(f"  共找到 {len(all_files)} 个文件")
    for f in all_files:
        log.info(f"    {FileApi.file_path(f)}  ({f['size']//1024//1024} MB)")

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
        openlist_refresh_delay=OPENLIST_REFRESH_DELAY,
        download_submit_max_retries=DOWNLOAD_SUBMIT_MAX_RETRIES,
        download_submit_retry_interval=DOWNLOAD_SUBMIT_RETRY_INTERVAL,
    )

    log.info("═" * 60)
    log.info("夸克网盘自动批量下载脚本启动")
    log.info("═" * 60)
    log.info(f"本次日志文件: {LOG_PATH}")
    log.info(f"下载临时目录: {ARIA2_DOWNLOAD_DIR}")
    log.info(f"下载完成目录: {DOWNLOAD_DESTINATION_DIR}")

    stoken, share_id, target_fid, all_files = load_share_files(quark_client)
    if not all_files:
        return

    pending_files = list(all_files)
    active_downloads = {}
    current_inflight_size = 0
    failed_files = []
    total_files = len(all_files)
    log.info(f"▶ 开始流式处理，共 {total_files} 个文件")

    while pending_files or active_downloads:
        quark_files_by_name, local_downloaded_names, aria2_tasks_by_name = file_api.refresh_runtime_state()

        current_inflight_size += file_api.attach_existing_downloads(
            all_files,
            aria2_tasks_by_name,
            active_downloads,
        )

        deletion_blocked = False
        if active_downloads:
            released_size, active_delete_blocked = file_api.process_active_downloads(active_downloads, failed_files)
            deletion_blocked = deletion_blocked or active_delete_blocked
            current_inflight_size = max(current_inflight_size - released_size, 0)

        if not deletion_blocked:
            quark_files_by_name, cleanup_delete_blocked = file_api.cleanup_completed_files(
                all_files,
                quark_files_by_name,
                local_downloaded_names,
                aria2_tasks_by_name,
            )
            deletion_blocked = deletion_blocked or cleanup_delete_blocked

        if deletion_blocked:
            log.warning("  存在转存文件删除未确认成功，暂停提交下一个转存任务...")

        if pending_files and not deletion_blocked:
            active_names = {task["file_name"] for task in active_downloads.values()}
            filtered_files = []
            skipped_files = []
            for file_item in pending_files:
                file_name = FileApi.file_path(file_item)
                aria2_status = str(aria2_tasks_by_name.get(file_name, {}).get("status", "")).lower()
                if file_name in local_downloaded_names:
                    skipped_files.append(f"{file_name} (本地已存在)")
                    continue
                if aria2_status == "complete":
                    skipped_files.append(f"{file_name} (Aria2已完成)")
                    continue
                if file_name in active_names or aria2_status in ACTIVE_ARIA2_STATUSES:
                    skipped_files.append(f"{file_name} (Aria2下载中)")
                    continue
                filtered_files.append(file_item)
            pending_files = filtered_files
            if skipped_files:
                log.info(f"  跳过 {len(skipped_files)} 个文件，避免重复处理:")
                for skipped in skipped_files:
                    log.info(f"    - {skipped}")

        pending_before_submit = len(pending_files)
        consumed = False
        if not deletion_blocked:
            pending_files, submitted_size, consumed = file_api.submit_next_pending_file(
                pending_files=pending_files,
                quark_files_by_name=quark_files_by_name,
                local_downloaded_names=local_downloaded_names,
                aria2_tasks_by_name=aria2_tasks_by_name,
                active_downloads=active_downloads,
                current_inflight_size=current_inflight_size,
                share_id=share_id,
                stoken=stoken,
            )
            current_inflight_size += submitted_size

        if pending_files and pending_before_submit == len(pending_files) and not consumed and not deletion_blocked:
            log.info("  暂无可提交文件，等待下载完成或容量释放...")

        if pending_files or active_downloads:
            time.sleep(POLL_INTERVAL)

    log.info("\n" + "═" * 60)
    log.info("全部批次处理完毕！")
    if failed_files:
        log.warning("以下文件未成功下载，请手动检查：")
        for fn in failed_files:
            log.warning(f"  - {fn}")
    else:
        log.info("所有文件下载成功 🎉")


if __name__ == "__main__":
    main()
