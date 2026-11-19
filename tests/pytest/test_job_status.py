"""任务结束态文案：一个文件都没处理时不能报「全部文件下载成功」。"""

from job_status import EMPTY_SELECTION_MESSAGE, decide_final_status


def test_no_files_is_not_success_message():
    final = decide_final_status([], 0, 0, "report.yaml")
    assert final["status"] == "done"
    assert final["message"] == EMPTY_SELECTION_MESSAGE
    assert "全部文件下载成功" not in final["message"]
    assert "重新勾选" in final["message"]


def test_failures_reported_with_counts():
    final = decide_final_status(["a"], 2, 3, "log/report.yaml")
    assert final["status"] == "failed"
    assert "校验失败 2 个" in final["message"]
    assert "流水线失败 3 个" in final["message"]
    assert "log/report.yaml" in final["message"]


def test_all_good_is_done():
    final = decide_final_status(["a", "b"], 0, 0)
    assert final == {"status": "done", "message": "全部文件下载成功"}
