"""任务结束态判定：把「有没有文件 / 有没有失败」翻成终态与一句中文说明。

单独成模块是为了可测试——quark_main 在 import 时就要读配置，没法在单测里直接导入。
"""

EMPTY_SELECTION_MESSAGE = (
    "没有可下载的文件：勾选里没有文件（可能都在「文件进度」里被删过，"
    "或选中的目录是空的），可从「目录」重新勾选"
)


def decide_final_status(all_files, failed_count, failed_files_count, report_path="") -> dict:
    """返回 {"status": "done"|"failed", "message": str}。

    一个文件都没处理时绝不能报「全部文件下载成功」——那是「勾选被逐文件删除
    标记吃光」的典型现场，用户看到「选了却没下载」。
    """
    if not all_files:
        return {"status": "done", "message": EMPTY_SELECTION_MESSAGE}
    if failed_count or failed_files_count:
        return {
            "status": "failed",
            "message": (
                f"处理结束但有失败：校验失败 {failed_count} 个 / "
                f"流水线失败 {failed_files_count} 个；详见 {report_path}"
            ),
        }
    return {"status": "done", "message": "全部文件下载成功"}
