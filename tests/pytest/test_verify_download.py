# -*- coding: utf-8 -*-
"""完成后校验：改名副本、cleanup 收尾态、空「完成后目录」都不该误报失败。"""

import os

from scheduler import Job
from verify_download import _local_file_info, inspect_job


def make_job(tmp_path, *, state="done", size=5, name="Show/S01E01.mkv", content=b"12345"):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return Job(path=name, size=size, file_item={}, state=state), target


def test_resolver_finds_renamed_copy(tmp_path):
    job, _ = make_job(tmp_path)
    renamed = tmp_path / "Show" / "S01E01_20261118_101010.mkv"
    job_path = tmp_path / job.path
    job_path.rename(renamed)

    def resolver(relative_path):
        return str(renamed) if relative_path == job.path else ""

    result = inspect_job(job, str(tmp_path), set(), resolver=resolver)
    assert result["ok"] is True
    assert result["actual_size"] == 5
    assert result["local_path"].endswith("S01E01_20261118_101010.mkv")


def test_cleanup_state_counts_as_success(tmp_path):
    job, _ = make_job(tmp_path, state="cleanup")
    result = inspect_job(job, str(tmp_path), set())
    # 文件已落盘，只是还在等删云端副本
    assert result["ok"] is True


def test_unfinished_state_is_failure(tmp_path):
    job, _ = make_job(tmp_path, state="downloading")
    result = inspect_job(job, str(tmp_path), set())
    assert result["ok"] is False
    assert "流水线未正常结束" in result["reason"]


def test_missing_file_is_failure(tmp_path):
    job, target = make_job(tmp_path)
    target.unlink()
    result = inspect_job(job, str(tmp_path), set())
    assert result["ok"] is False
    assert "未找到文件" in result["reason"]


def test_truncated_file_is_failure(tmp_path):
    job, target = make_job(tmp_path, size=10, content=b"12345")
    result = inspect_job(job, str(tmp_path), set())
    assert result["ok"] is False
    assert "大小与本地不一致" in result["reason"]


def test_empty_destination_dir_uses_resolver_not_cwd(tmp_path, monkeypatch):
    job, target = make_job(tmp_path)
    monkeypatch.chdir(tmp_path)

    def resolver(relative_path):
        return str(target) if relative_path == job.path else ""

    exists, size, found = _local_file_info("", job.path, resolver)
    assert exists is True
    assert size == 5
    assert found == str(target)
