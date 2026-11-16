"""新增任务：同一分享链接可以建多个任务（互不覆盖），任务名不能重复。"""

import contextlib

import pytest

import jobs_manager


def patch_world(monkeypatch, tmp_path, jobs):
    monkeypatch.setattr(jobs_manager, "load_jobs_index", lambda: [dict(j) for j in jobs])
    monkeypatch.setattr(
        jobs_manager, "save_jobs_index", lambda items: jobs.__setitem__(slice(None), [dict(i) for i in items])
    )
    monkeypatch.setattr(jobs_manager, "_index_lock", lambda: contextlib.nullcontext())
    monkeypatch.setattr(jobs_manager, "job_dir", lambda job_id: str(tmp_path / job_id))
    monkeypatch.setattr(
        jobs_manager, "job_control_file", lambda job_id: str(tmp_path / (job_id + ".control.yaml"))
    )
    monkeypatch.setattr(
        jobs_manager, "get_job", lambda job_id: next((j for j in jobs if j["id"] == job_id), None)
    )


def test_same_share_creates_two_independent_jobs(tmp_path, monkeypatch):
    jobs = []
    patch_world(monkeypatch, tmp_path, jobs)

    first = jobs_manager.add_job(
        "https://pan.quark.cn/s/abc", name="任务一", selection={"file_fids": ["f1"]}
    )
    second = jobs_manager.add_job(
        "https://pan.quark.cn/s/abc", name="任务二", selection={"file_fids": ["f2"]}
    )

    assert first["id"] != second["id"]
    assert [j["name"] for j in jobs] == ["任务一", "任务二"]
    # 老任务的勾选不能被后来的任务覆盖
    assert jobs[0]["selection"] == {"file_fids": ["f1"]}
    assert jobs[1]["selection"] == {"file_fids": ["f2"]}


def test_duplicate_name_is_rejected(tmp_path, monkeypatch):
    jobs = []
    patch_world(monkeypatch, tmp_path, jobs)
    jobs_manager.add_job("https://pan.quark.cn/s/abc", name="任务一")

    with pytest.raises(ValueError, match="任务名已存在"):
        jobs_manager.add_job("https://pan.quark.cn/s/abc", name="任务一")
