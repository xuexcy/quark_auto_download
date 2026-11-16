#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克网盘 + Aria2 自动批量下载脚本
功能：在有限的网盘空间内，自动循环"转存 → 下载 → 删除"
"""

import json
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
from pipeline import DownloadPipeline
from quark_api import QuarkClient

QUARK_CONFIG_PATH = resolve_config_path("QUARK_AUTO_DL_QUARK_CONFIG", "quark")
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


def load_all_configs() -> tuple[dict, dict]:
    """读取 quark / aria2 配置；旧版单文件 conf/config.yaml 仍兼容。"""
    if os.path.exists(QUARK_CONFIG_PATH) and os.path.exists(ARIA2_CONFIG_PATH):
        return load_config(QUARK_CONFIG_PATH), load_config(ARIA2_CONFIG_PATH)

    if os.path.exists(LEGACY_CONFIG_PATH):
        merged = load_config(LEGACY_CONFIG_PATH)
        return merged, merged

    raise FileNotFoundError(
        "配置文件不存在，请创建以下文件：\n"
        f"- {os.path.join(CONF_DIR, 'quark.yaml')}\n"
        f"- {os.path.join(CONF_DIR, 'aria2.yaml')}\n"
        "（也可使用 .yml，或继续使用旧版 conf/config.yaml）"
    )


QUARK_CONFIG, ARIA2_CONFIG = load_all_configs()

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
# 前端勾选范围（由 jobs_manager.start_job 写到 state/jobs/<id>/selection.json）
SELECTION_FILE = str(os.getenv("QUARK_AUTO_DL_SELECTION_FILE") or "").strip()
# 逐文件暂停/删除控制文件（由 jobs_manager.apply_file_control 写到 state/jobs/<id>/file_override.yaml）
OVERRIDE_FILE = str(os.getenv("QUARK_AUTO_DL_FILE_OVERRIDE_FILE") or "").strip()
VERIFY_REPORT_FILE = str(
    os.getenv("QUARK_AUTO_DL_VERIFY_REPORT_FILE")
    or ARIA2_CONFIG.get("verify_report_file")
    or os.path.join(BASE_DIR, "state", "download_verify_report.yaml")
).strip()
QUARK_REQUEST_TIMEOUT = int(QUARK_CONFIG.get("request_timeout", 10))
QUARK_TASK_POLL_INTERVAL = int(QUARK_CONFIG.get("task_poll_interval", 3))
QUARK_TASK_TIMEOUT = int(QUARK_CONFIG.get("task_timeout", 180))
# 网盘总容量（GB）：用于估算剩余空间。按实际账号容量配置，之前是硬编码 10GB。
QUARK_TOTAL_SPACE_GB = float(QUARK_CONFIG.get("total_space_gb", 10))
# 请求用的 User-Agent：留空 = 默认的官方桌面客户端 UA（避免浏览器 UA 下的 23018 单文件限制）
QUARK_CLIENT_UA = str(QUARK_CONFIG.get("quark_client_ua") or "").strip()
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
    """初始化 Quark 与 Aria2 客户端。"""
    quark_client = QuarkClient(
        cookie=QUARK_COOKIE,
        request_timeout=QUARK_REQUEST_TIMEOUT,
        save_to_dir=SAVE_TO_DIR,
        share_url=SHARE_URL,
        share_pwd=SHARE_PWD,
        logger=log,
        task_poll_interval=QUARK_TASK_POLL_INTERVAL,
        task_timeout=QUARK_TASK_TIMEOUT,
        total_space_bytes=int(QUARK_TOTAL_SPACE_GB * 1024 * 1024 * 1024),
        user_agent=QUARK_CLIENT_UA,
    )
    quark_client.resolve_save_to_dir()
    aria2_client = Aria2Client(
        host=ARIA2_HOST,
        secret=ARIA2_SECRET,
        download_dir=ARIA2_DOWNLOAD_DIR,
        request_timeout=ARIA2_REQUEST_TIMEOUT,
        # 直链下载要带 Cookie/Referer/UA，否则夸克 CDN 会 403
        download_headers=quark_client.download_headers(),
        poll_interval=POLL_INTERVAL,
        download_timeout=DOWNLOAD_TIMEOUT,
        logger=log,
        job_id=JOB_ID,
    )
    return quark_client, aria2_client


def load_selection(strict: bool = False) -> tuple[set[str], set[str], bool]:
    """读取前端勾选范围，返回 (文件 fid 集合, 目录 fid 集合, 是否限定了范围)。

    文件不存在/内容损坏都按"全量下载"处理（third=False），避免因为前端数据问题把任务卡死；
    文件存在但两个集合都空 = 用户把勾选全取消了，此时 third=True 表示本次不下载任何文件。

    strict=True（运行中追加勾选时用）：读不出来就抛错，绝不退化成"下载整个分享"。
    """
    if not SELECTION_FILE or not os.path.isfile(SELECTION_FILE):
        return set(), set(), False
    try:
        with open(SELECTION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        if strict:
            raise ValueError(f"读取勾选范围失败: {e}") from e
        log.warning(f"读取勾选范围失败（按全量下载处理）: {e}")
        return set(), set(), False
    if not isinstance(data, dict):
        if strict:
            raise ValueError("勾选范围格式不正确")
        log.warning("勾选范围格式不正确（按全量下载处理）")
        return set(), set(), False

    def _fid_set(key: str) -> set[str]:
        raw = data.get(key) or []
        if not isinstance(raw, list):
            return set()
        return {str(v).strip() for v in raw if str(v).strip()}

    return _fid_set("file_fids"), _fid_set("dir_fids"), True


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
    all_files = list_share_selection(quark_client, share_id, stoken, target_fid)

    if not all_files:
        log.warning("未找到任何文件，退出。")

    return stoken, share_id, target_fid, all_files


def list_share_selection(
    quark_client,
    share_id: str,
    stoken: str,
    target_fid: str,
    verbose: bool = True,
    strict: bool = False,
) -> list[dict]:
    """按当前 selection.json 列出分享里要下载的文件（排序 + 正则过滤）。

    启动时由 load_share_files 调用；运行中追加勾选时由流水线回调重新调用（verbose=False 少刷日志）。
    """
    selected_file_fids, selected_dir_fids, selection_constrained = load_selection(strict=strict)
    if selection_constrained and not selected_file_fids and not selected_dir_fids:
        log.warning("勾选为空：本次任务不下载任何文件（如需其它文件，请在页面上重新勾选后再开始）")
        return []
    if selected_file_fids or selected_dir_fids:
        log.info(
            f"  仅下载勾选项：选中文件 {len(selected_file_fids)} 个 / "
            f"选中目录 {len(selected_dir_fids)} 个"
        )
    all_files = quark_client.list_share_files(
        share_id,
        stoken,
        target_fid,
        selected_file_fids=selected_file_fids,
        selected_dir_fids=selected_dir_fids,
    )
    log.info(f"  共找到 {len(all_files)} 个文件")
    all_files = sorted(all_files, key=FileApi.file_sort_key)
    if verbose:
        for f in all_files:
            log.info(f"    {FileApi.file_path(f)}  ({FileApi.format_size(f.get('size', 0))})")

    if FILE_NAME_PATTERN is not None:
        before_count = len(all_files)
        all_files = [f for f in all_files if FILE_NAME_PATTERN.search(FileApi.file_path(f))]
        log.info(f"▶ 按正则过滤: {FILE_NAME_REGEX}")
        log.info(f"  过滤后剩余 {len(all_files)}/{before_count} 个文件")

    return all_files


def main():
    quark_client, aria2_client = init_clients()
    file_api = FileApi(
        quark_client=quark_client,
        aria2_client=aria2_client,
        save_to_fid=quark_client.save_to_fid,
        aria2_download_dir=ARIA2_DOWNLOAD_DIR,
        download_destination_dir=DOWNLOAD_DESTINATION_DIR,
        aria2_max_retries=ARIA2_MAX_RETRIES,
        download_log_interval=DOWNLOAD_LOG_INTERVAL,
        logger=log,
        download_submit_max_retries=DOWNLOAD_SUBMIT_MAX_RETRIES,
        download_submit_retry_interval=DOWNLOAD_SUBMIT_RETRY_INTERVAL,
        job_id=JOB_ID,
    )

    log.info("═" * 60)
    log.info("夸克网盘自动批量下载脚本启动")
    if JOB_ID:
        log.info(f"任务 ID: {JOB_ID}")
    log.info("═" * 60)
    log.info(f"本次日志文件: {LOG_PATH}")
    log.info(f"分享链接: {SHARE_URL}")
    log.info(f"下载临时目录: {file_api.download_root}")
    log.info(f"下载完成目录: {DOWNLOAD_DESTINATION_DIR}")
    log.info(f"网盘配额(用于估算剩余空间): {QUARK_TOTAL_SPACE_GB:g} GB")

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
    if OVERRIDE_FILE:
        log.info(f"逐文件控制文件: {OVERRIDE_FILE}")
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
        selection_file=SELECTION_FILE,
        override_file=OVERRIDE_FILE,
        # 运行中追加勾选：前端改 selection.json 后，流水线回调这里重新列举分享
        reload_selection=(
            (
                lambda: list_share_selection(
                    quark_client, share_id, stoken, target_fid, verbose=False, strict=True
                )
            )
            if SELECTION_FILE
            else None
        ),
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
                # 结束态要如实反映结果：全部成功 → done，有失败 → failed（不要一律 paused 冒充正常）
                if failed_count or failed_files:
                    job["status"] = "failed"
                    job["message"] = (
                        f"处理结束但有失败：校验失败 {failed_count} 个 / "
                        f"流水线失败 {len(failed_files)} 个；详见 {VERIFY_REPORT_FILE}"
                    )
                else:
                    job["status"] = "done"
                    job["message"] = "全部文件下载成功"
                job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                upsert_job(job)
        except Exception as e:
            log.warning(f"更新任务状态失败: {e}")


if __name__ == "__main__":
    main()
