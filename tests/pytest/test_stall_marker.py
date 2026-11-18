"""0 字节停摆：pipeline 记起点 → 状态文件 → /api/jobs/{id}/files 打标记。"""

import time

import pytest
import yaml

import jobs_manager
from pipeline import ZERO_BYTES_STALL_SEC, track_zero_bytes
from scheduler import Job, PipelineScheduler


class _FileApi:
    """persist_payload / counts 只用到这几个属性。"""

    prefer_path_order = True


def make_scheduler(completed=0, total=1000, zero_since=None, state="downloading"):
    sched = PipelineScheduler(_FileApi())
    job = Job(path="Show/a.mkv", size=total, file_item={"fid": "f1"})
    job.state = state
    task = {"gid": "g1", "completed": completed, "total": total}
    if zero_since is not None:
        task["zero_since"] = zero_since
    job.download_task = task
    sched.jobs[job.path] = job
    sched.ordered_paths.append(job.path)
    return sched


def make_state(tmp_path, monkeypatch, files, status="running"):
    state_file = tmp_path / "pipeline_state.yaml"
    state_file.write_text(
        yaml.safe_dump(
            {"files": files, "counts": {}, "updated_at": "2026-11-18 15:00:00"},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jobs_manager, "job_state_file", lambda job_id: str(state_file))
    monkeypatch.setattr(jobs_manager, "get_job", lambda job_id: {"job_id": job_id, "status": status})
    return state_file


def downloading_file(zero_since, downloaded=0, total=1000):
    return {
        "path": "Show/a.mkv",
        "fid": "f1",
        "size": total,
        "state": "downloading",
        "downloaded": downloaded,
        "total": total,
        "speed": 0,
        "zero_since": zero_since,
    }


# ---------- pipeline 侧：只记起点，不判阈值 ----------


def test_track_zero_bytes_sets_start_and_accumulates():
    task = {}
    assert track_zero_bytes(task, 0, 1000, 1000.0) == 0.0
    assert task["zero_since"] == 1000.0
    assert track_zero_bytes(task, 0, 1000, 1300.0) == pytest.approx(300.0)
    # 一旦有字节就清零，再停摆重新计时
    assert track_zero_bytes(task, 5, 1000, 1310.0) == 0.0
    assert "zero_since" not in task
    assert track_zero_bytes(task, 0, 1000, 1400.0) == 0.0
    assert task["zero_since"] == 1400.0


def test_track_zero_bytes_ignores_unknown_total():
    task = {}
    assert track_zero_bytes(task, 0, 0, 1000.0) == 0.0
    assert "zero_since" not in task


def test_track_zero_bytes_survives_clock_rollback():
    task = {"zero_since": 2000.0}
    assert track_zero_bytes(task, 0, 1000, 1000.0) == 0.0
    assert task["zero_since"] == 1000.0


def test_persist_payload_keeps_zero_since():
    now = time.time()
    sched = make_scheduler(zero_since=now - 400)
    item = sched.persist_payload("sid")["files"][0]
    assert item["zero_since"] == pytest.approx(now - 400, abs=2)
    assert item["downloaded"] == 0


def test_persist_payload_zero_since_defaults_to_zero():
    sched = make_scheduler(zero_since=None)
    assert sched.persist_payload("sid")["files"][0]["zero_since"] == 0.0


# ---------- Web 侧：阈值判定 ----------


def test_stalled_after_five_minutes(tmp_path, monkeypatch):
    make_state(tmp_path, monkeypatch, [downloading_file(time.time() - ZERO_BYTES_STALL_SEC - 100)])
    item = jobs_manager.job_files("j1")["files"][0]
    assert item["stalled"] is True
    assert item["stalled_sec"] >= ZERO_BYTES_STALL_SEC


def test_not_stalled_before_threshold(tmp_path, monkeypatch):
    make_state(tmp_path, monkeypatch, [downloading_file(time.time() - 60)])
    item = jobs_manager.job_files("j1")["files"][0]
    assert item["stalled"] is False
    assert 55 <= item["stalled_sec"] <= 65


def test_not_stalled_when_bytes_moved(tmp_path, monkeypatch):
    make_state(tmp_path, monkeypatch, [downloading_file(time.time() - 400, downloaded=500)])
    item = jobs_manager.job_files("j1")["files"][0]
    assert item["stalled"] is False
    assert item["stalled_sec"] == 0


def test_not_stalled_when_job_not_running(tmp_path, monkeypatch):
    make_state(tmp_path, monkeypatch, [downloading_file(time.time() - 400)], status="stopped")
    item = jobs_manager.job_files("j1")["files"][0]
    assert item["stalled"] is False


def test_not_stalled_without_total_or_zero_since(tmp_path, monkeypatch):
    files = [
        downloading_file(time.time() - 400, total=0),
        downloading_file(0.0, total=1000),
    ]
    make_state(tmp_path, monkeypatch, files)
    items = jobs_manager.job_files("j1")["files"]
    assert [i["stalled"] for i in items] == [False, False]


def test_paused_file_is_not_marked_stalled(tmp_path, monkeypatch):
    make_state(tmp_path, monkeypatch, [downloading_file(time.time() - 400)])
    monkeypatch.setattr(
        jobs_manager,
        "read_file_override",
        lambda job_id: {"paused": {"Show/a.mkv"}, "deleted": set(), "retry": []},
    )
    item = jobs_manager.job_files("j1")["files"][0]
    assert item["state"] == "paused"
    assert item["stalled"] is False
