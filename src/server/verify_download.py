# -*- coding: utf-8 -*-
"""全部任务结束后，校验 completed 目录并生成失败报告。"""

from __future__ import annotations

import os
import time
from typing import Any

import yaml

from file_api import normalize_relative_path
from scheduler import Job, PipelineScheduler

# 分享元数据 size 偶发不准：默认仍按 size 严格判定（截断文件绝不能算成功），
# 需要放宽时设 QUARK_AUTO_DL_STRICT_SIZE=0。
STRICT_SIZE_CHECK = os.environ.get("QUARK_AUTO_DL_STRICT_SIZE", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


def _local_file_info(destination_dir: str, relative_path: str) -> tuple[bool, int | None, str]:
    relative_path = normalize_relative_path(relative_path)
    abs_path = os.path.join(destination_dir, relative_path)
    if not os.path.isfile(abs_path):
        return False, None, abs_path
    try:
        return True, int(os.path.getsize(abs_path)), abs_path
    except OSError:
        return False, None, abs_path


def inspect_job(job: Job, destination_dir: str, failed_paths: set[str]) -> dict[str, Any]:
    """检查单个任务是否在 completed 目录成功落盘。"""
    exists, actual_size, abs_path = _local_file_info(destination_dir, job.path)
    expected_size = int(job.size or 0)
    reasons: list[str] = []
    size_note = ""

    pipeline_failed = job.state == "failed" or job.path in failed_paths
    pipeline_note = (job.note or "").strip()
    if pipeline_failed:
        reasons.append(pipeline_note or "流水线标记为失败")

    if job.state not in ("done", "failed"):
        suffix = f"（{pipeline_note}）" if pipeline_note else ""
        reasons.append(f"流水线未正常结束，当前状态: {job.state}{suffix}")

    size_mismatch = (
        expected_size > 0 and actual_size is not None and actual_size != expected_size
    )
    empty_file = bool(exists) and int(actual_size or 0) <= 0

    if not exists:
        reasons.append("completed 目录中未找到文件")
    elif size_mismatch:
        size_note = (
            f"分享元数据大小与本地不一致: 期望 {expected_size} 字节，实际 {actual_size} 字节"
            + ("" if STRICT_SIZE_CHECK else "（分享侧 size 可能不准，以本地文件为准）")
        )
        # 截断/半截文件是最常见的"假成功"，默认按失败处理
        if pipeline_failed or STRICT_SIZE_CHECK:
            reasons.append(size_note)

    if empty_file:
        reasons.append("本地文件大小为 0（疑似占位或写入失败）")

    # 成功：本地存在 + 流水线 done + 非 0 字节 + size 校验通过
    ok = (
        exists
        and job.state == "done"
        and not pipeline_failed
        and not empty_file
        and not (size_mismatch and STRICT_SIZE_CHECK)
    )
    if ok:
        reasons = []
    elif not reasons:
        reasons.append("校验未通过")

    detail = pipeline_note
    if size_note and size_note not in detail:
        detail = f"{detail}；{size_note}" if detail else size_note

    return {
        "path": job.path,
        "local_path": abs_path,
        "expected_size": expected_size,
        "actual_size": actual_size,
        "local_exists": exists,
        "pipeline_state": job.state,
        "pipeline_note": pipeline_note,
        "detail": detail,
        "ok": ok,
        "reason": "；".join(reasons) if reasons else "",
        "reasons": reasons,
        "size_note": size_note,
    }


def verify_completed_downloads(
    scheduler: PipelineScheduler,
    destination_dir: str,
) -> dict[str, Any]:
    """对照调度任务列表，校验 completed 目录。"""
    destination_dir = str(destination_dir or "").rstrip("/")
    failed_paths = set(scheduler.failed_files)
    results: list[dict[str, Any]] = []

    for path in scheduler.ordered_paths:
        job = scheduler.jobs.get(path)
        if not job:
            continue
        results.append(inspect_job(job, destination_dir, failed_paths))

    failures = [item for item in results if not item["ok"]]
    ok_count = len(results) - len(failures)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "destination_dir": destination_dir,
        "summary": {
            "total": len(results),
            "ok": ok_count,
            "failed": len(failures),
        },
        "failures": [
            {
                "path": item["path"],
                "local_path": item["local_path"],
                "expected_size": item["expected_size"],
                "actual_size": item["actual_size"],
                "local_exists": item["local_exists"],
                "pipeline_state": item["pipeline_state"],
                "pipeline_note": item["pipeline_note"],
                "detail": item["detail"],
                "reason": item["reason"],
            }
            for item in failures
        ],
        "all_results": results,
    }


def write_verify_report(report: dict[str, Any], report_path: str) -> str:
    """写入失败校验报告（YAML）。成功时也写摘要。"""
    directory = os.path.dirname(report_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    payload = {
        "generated_at": report["generated_at"],
        "destination_dir": report["destination_dir"],
        "summary": report["summary"],
        "failures": report["failures"],
    }
    tmp_path = f"{report_path}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("# download verify report (files missing or failed in completed dir)\n")
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp_path, report_path)
    return report_path


def log_verify_report(logger, report: dict[str, Any], report_path: str) -> None:
    summary = report["summary"]
    logger.info("▶ 下载结果校验（completed 目录）")
    logger.info(
        f"  目标目录: {report['destination_dir']} | "
        f"合计={summary['total']} 成功={summary['ok']} 失败={summary['failed']}"
    )
    logger.info(f"  报告文件: {report_path}")
    if not report["failures"]:
        logger.info("  校验通过：completed 目录中文件齐全")
        return
    logger.warning("  以下文件未成功下载或校验失败：")
    for item in report["failures"]:
        logger.warning(f"  - {item['path']}")
        detail = item.get("detail") or item.get("reason") or ""
        logger.warning(f"      原因: {detail}")
