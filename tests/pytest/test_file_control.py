"""逐文件 暂停 / 开始 / 删除：file_override.yaml 与 job_files 的合并展示。"""

import json
import os
import stat
import types

import pytest
import yaml

import jobs_manager


@pytest.fixture()
def env(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    index = tmp_path / "jobs.yaml"
    monkeypatch.setattr(jobs_manager, "JOBS_DIR", str(jobs_dir))
    monkeypatch.setattr(jobs_manager, "JOBS_INDEX", str(index))
    return types.SimpleNamespace(dir=jobs_dir, index=index, root=tmp_path)


def add_job(job_id="j1", selection=None, selection_mode="explicit", status="paused"):
    job = {
        "id": job_id,
        "name": "测试任务",
        "share_url": "https://pan.quark.cn/s/abc",
        "selection": selection if selection is not None else {},
        "selection_mode": selection_mode,
        "status": status,
        "log_path": "",
    }
    jobs_manager.save_jobs_index([job])
    return job


def write_state(job_id, files, failed=None):
    path = jobs_manager.job_state_file(job_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {
                "files": files,
                "counts": {},
                "failed_files": failed or [],
                "updated_at": "2026-11-16 20:00:00",
            },
            f,
            allow_unicode=True,
        )
    return path


def test_pause_ready_file_shows_paused(env):
    add_job(selection={"file_fids": ["f1"]})
    write_state("j1", [{"path": "a/b.mp4", "fid": "f1", "size": 1000, "state": "ready"}])

    result = jobs_manager.apply_file_control("j1", "pause", ["a/b.mp4"], ["f1"])
    assert result["op"] == "pause"
    assert result["applied"] == "immediate"
    assert result["running"] is False

    assert jobs_manager.read_file_override("j1")["paused"] == {"a/b.mp4"}

    data = jobs_manager.job_files("j1")
    item = data["files"][0]
    assert item["state"] == "paused"
    assert item["paused"] is True
    assert item["can_pause"] is False
    assert item["can_resume"] is True
    assert item["can_delete"] is True
    assert data["counts"] == {"paused": 1}


def test_pause_keeps_progress_of_downloading_file(env):
    add_job(selection={"file_fids": ["f1"]})
    write_state(
        "j1",
        [{"path": "a.mp4", "fid": "f1", "size": 1000, "state": "downloading",
          "downloaded": 250, "total": 1000, "speed": 2048}],
    )

    jobs_manager.apply_file_control("j1", "pause", ["a.mp4"], ["f1"])

    item = jobs_manager.job_files("j1")["files"][0]
    # 暂停只改展示状态，进度不清零（aria2 那边也保留）
    assert item["state"] == "paused"
    assert item["progress"] == 25.0
    assert item["downloaded"] == 250
    assert item["can_resume"] is True


def test_resume_resets_state_when_job_idle(env):
    add_job(selection={"file_fids": ["f1"]})
    write_state("j1", [{"path": "a.mp4", "fid": "f1", "size": 10, "state": "paused"}])
    jobs_manager.write_file_override(
        "j1", {"paused": {"a.mp4"}, "deleted": set(), "retry": set()}
    )

    result = jobs_manager.apply_file_control("j1", "resume", ["a.mp4"], ["f1"])
    assert result["state_files_reset"] == 1

    override = jobs_manager.read_file_override("j1")
    assert override["paused"] == set()
    assert override["retry"] == {"a.mp4"}

    item = jobs_manager.job_files("j1")["files"][0]
    assert item["state"] == "pending"
    assert item["note"] == "已恢复（用户）"
    assert item["can_pause"] is True


def test_resume_failed_file_clears_failed_list(env):
    add_job(selection={"file_fids": ["f1"]})
    write_state("j1", [{"path": "x.mp4", "fid": "f1", "size": 10, "state": "failed"}], failed=["x.mp4"])

    jobs_manager.apply_file_control("j1", "resume", ["x.mp4"], ["f1"])

    state = yaml.safe_load(open(jobs_manager.job_state_file("j1"), encoding="utf-8"))
    assert state["failed_files"] == []
    assert state["files"][0]["state"] == "pending"
    assert jobs_manager.job_files("j1")["files"][0]["can_resume"] is False


def test_delete_drops_state_and_selection(env):
    add_job(selection={"file_fids": ["f1", "f2"]})
    write_state(
        "j1",
        [
            {"path": "a/b.mp4", "fid": "f1", "size": 100, "state": "pending"},
            {"path": "a/c.mp4", "fid": "f2", "size": 200, "state": "done", "downloaded": 200, "total": 200},
        ],
    )

    result = jobs_manager.apply_file_control("j1", "delete", ["a/b.mp4"], ["f1"])
    assert result["selection_removed"] == 1
    assert result["state_files_removed"] == 1

    assert jobs_manager.read_file_override("j1")["deleted"] == {"a/b.mp4"}
    data = jobs_manager.job_files("j1")
    assert [f["path"] for f in data["files"]] == ["a/c.mp4"]
    assert data["file_total"] == 1

    selection = json.loads(open(jobs_manager.job_selection_file("j1"), encoding="utf-8").read())
    assert selection == {"file_fids": ["f2"], "dir_fids": []}
    assert jobs_manager.get_job("j1")["selection"] == {"file_fids": ["f2"]}


def test_can_flags_per_state(env):
    add_job()
    write_state(
        "j1",
        [
            {"path": "done.mp4", "size": 1, "state": "done"},
            {"path": "cleanup.mp4", "size": 1, "state": "cleanup"},
            {"path": "failed.mp4", "size": 1, "state": "failed"},
            {"path": "transferred.mp4", "size": 1, "state": "transferring"},
        ],
    )
    flags = {f["path"]: (f["can_pause"], f["can_resume"], f["can_delete"]) for f in jobs_manager.job_files("j1")["files"]}
    # 已完成 / 清理中也能删：删的是「任务里的这条记录」，删完可以在目录树里重新勾选下载
    assert flags["done.mp4"] == (False, False, True)
    assert flags["cleanup.mp4"] == (False, False, True)
    assert flags["failed.mp4"] == (False, True, True)
    assert flags["transferred.mp4"] == (True, False, True)


def test_read_override_tolerates_broken_yaml(env):
    add_job()
    path = jobs_manager.job_override_file("j1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("paused: [\n  - oops\n")
    override = jobs_manager.read_file_override("j1")
    assert override == {"paused": set(), "deleted": set(), "retry": set(), "deleted_map": {}}
    # 坏文件不能让整个文件列表接口 500
    assert jobs_manager.job_files("j1")["files"] == []


def test_override_file_is_private_and_sorted(env):
    add_job()
    jobs_manager.write_file_override(
        "j1", {"paused": {"b.mp4", "a.mp4"}, "deleted": set(), "retry": {"c.mp4"}}
    )
    path = jobs_manager.job_override_file("j1")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    data = yaml.safe_load(open(path, encoding="utf-8"))
    assert data == {"paused": ["a.mp4", "b.mp4"], "deleted": [], "retry": ["c.mp4"]}


def test_running_job_defers_state_changes_to_pipeline(env, monkeypatch):
    add_job(status="running")
    write_state("j1", [{"path": "a.mp4", "fid": "f1", "size": 10, "state": "downloading"}])
    monkeypatch.setattr(
        jobs_manager, "refresh_job_runtime", lambda job, persist=True: {"running": True}
    )

    result = jobs_manager.apply_file_control("j1", "delete", ["a.mp4"], ["f1"])
    assert result["applied"] == "pipeline"
    # 运行中由子进程自己摘队列，这里不能动 pipeline_state
    assert "state_files_removed" not in result
    state = yaml.safe_load(open(jobs_manager.job_state_file("j1"), encoding="utf-8"))
    assert [f["path"] for f in state["files"]] == ["a.mp4"]


def test_op_aliases_and_errors(env):
    add_job()
    assert jobs_manager.apply_file_control("j1", "suspend", ["a.mp4"])["op"] == "pause"
    assert jobs_manager.apply_file_control("j1", "start", ["a.mp4"])["op"] == "resume"
    assert jobs_manager.apply_file_control("j1", "remove", ["a.mp4"])["op"] == "delete"

    with pytest.raises(ValueError, match="不支持的操作"):
        jobs_manager.apply_file_control("j1", "explode", ["a.mp4"])
    with pytest.raises(ValueError, match="没有指定要操作的文件"):
        jobs_manager.apply_file_control("j1", "pause", [], [])
    with pytest.raises(ValueError, match="任务不存在"):
        jobs_manager.apply_file_control("nope", "pause", ["a.mp4"])


def test_start_job_passes_override_file_env(env, monkeypatch):
    add_job(selection={"file_fids": ["f1"]})
    captured = {}
    # 启动前的 Aria2 预检在这里必须打桩：真去连本机 conf/aria2.yaml 里的配置会让用例依赖环境
    monkeypatch.setattr(jobs_manager, "probe_aria2_profile", lambda name, timeout=5: {"version": "test"})

    class FakeProc:
        pid = 424242

    monkeypatch.setattr(
        jobs_manager, "refresh_job_runtime", lambda job, persist=True: {"running": False, "pid": None}
    )
    monkeypatch.setattr(jobs_manager, "dated_job_log_path", lambda job_id: str(env.root / "job.log"))

    def fake_popen(args, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr(jobs_manager.subprocess, "Popen", fake_popen)
    jobs_manager.start_job("j1")

    override = captured["env"]["QUARK_AUTO_DL_FILE_OVERRIDE_FILE"]
    assert override == jobs_manager.job_override_file("j1")
    assert override.endswith("file_override.yaml")


def test_readd_after_delete_clears_marker(env):
    """删掉的文件从目录树重新勾选时，必须撤掉「已删除」标记，否则子进程会一直过滤它。"""
    add_job(selection={"file_fids": ["f1"]})
    write_state("j1", [{"path": "a/b.mp4", "fid": "f1", "size": 1000, "state": "ready"}])

    jobs_manager.apply_file_control("j1", "delete", ["a/b.mp4"], ["f1"])
    override = jobs_manager.read_file_override("j1")
    assert override["deleted"] == {"a/b.mp4"}
    # 删除只记了路径，额外记下 fid → 路径，供「只拿 fid 重新勾选」时撤标记
    assert override["deleted_map"] == {"f1": "a/b.mp4"}

    jobs_manager.add_job_selection("j1", {"file_fids": ["f1"]})

    override = jobs_manager.read_file_override("j1")
    assert override["deleted"] == set()
    assert override["deleted_map"] == {}
    payload = json.loads(
        open(jobs_manager.job_selection_file("j1"), encoding="utf-8").read()
    )
    assert payload["file_fids"] == ["f1"]


def test_forget_deleted_paths_by_fid_only(env):
    add_job()
    jobs_manager.write_file_override(
        "j1", {"paused": set(), "deleted": {"a/b.mp4", "c.mp4"}, "retry": set(),
               "deleted_map": {"f1": "a/b.mp4", "f2": "c.mp4"}}
    )

    assert jobs_manager.forget_deleted_paths("j1", fids=["f1"]) == 1

    override = jobs_manager.read_file_override("j1")
    assert override["deleted"] == {"c.mp4"}   # 只撤 f1 对应那条，c.mp4 的标记仍在
    assert override["deleted_map"] == {"f2": "c.mp4"}


def test_pause_after_delete_drops_mapping(env):
    add_job()
    jobs_manager.write_file_override(
        "j1", {"paused": set(), "deleted": {"a/b.mp4"}, "retry": set(),
               "deleted_map": {"f1": "a/b.mp4"}}
    )

    jobs_manager.apply_file_control("j1", "pause", ["a/b.mp4"], ["f1"])

    override = jobs_manager.read_file_override("j1")
    assert override["deleted"] == set()
    assert override["deleted_map"] == {}
    assert override["paused"] == {"a/b.mp4"}


def test_delete_without_state_records_fid_mapping(env):
    """任务刚重启、pipeline_state 还没写时，删除也要记下 fid → 路径（靠一次操作的一一对应）。"""
    add_job(selection={"file_fids": ["f1"]})

    jobs_manager.apply_file_control("j1", "delete", ["a/b.mp4"], ["f1"])
    override = jobs_manager.read_file_override("j1")
    assert override["deleted"] == {"a/b.mp4"}
    assert override["deleted_map"] == {"f1": "a/b.mp4"}

    assert jobs_manager.forget_deleted_paths("j1", fids=["f1"]) == 1
    assert jobs_manager.read_file_override("j1")["deleted"] == set()


def test_forget_deleted_paths_falls_back_to_state(env):
    """deleted_map 丢了（老版本写的标记）也能靠 pipeline_state 把标记撤掉。"""
    add_job(selection={"file_fids": ["f1"]})
    write_state("j1", [{"path": "a/b.mp4", "fid": "f1", "state": "downloading"}])
    override = {"paused": set(), "deleted": {"a/b.mp4"}, "retry": set(), "deleted_map": {}}
    jobs_manager.write_file_override("j1", override)

    assert jobs_manager.forget_deleted_paths("j1", fids=["f1"]) == 1
    assert jobs_manager.read_file_override("j1")["deleted"] == set()


def test_readd_existing_selection_also_clears_marker(env):
    """文件还在 selection 里（already_in_job）时重新勾选，也要撤掉删除标记。"""
    add_job(selection={"file_fids": ["f1"]})
    write_state("j1", [{"path": "a/b.mp4", "fid": "f1", "state": "downloading"}])
    jobs_manager.write_file_override(
        "j1", {"paused": set(), "deleted": {"a/b.mp4"}, "retry": set(), "deleted_map": {"f1": "a/b.mp4"}}
    )

    result = jobs_manager.add_job_selection("j1", {"file_fids": ["f1"]})
    assert result["already_in_job"] == 1
    override = jobs_manager.read_file_override("j1")
    assert override["deleted"] == set()
    assert override["deleted_map"] == {}


def test_delete_done_file_frees_it_for_reselect(env):
    """已完成的文件也能删：删掉记录后目录树可以重新勾选，重新勾选会撤掉删除标记。"""
    add_job(selection={"file_fids": ["f1"]})
    write_state("j1", [{"path": "a/done.mp4", "fid": "f1", "size": 1000, "state": "done"}])

    result = jobs_manager.apply_file_control("j1", "delete", ["a/done.mp4"], ["f1"])
    assert result["applied"] == "immediate"
    assert result["state_files_removed"] == 1
    assert result["selection_removed"] == 1

    # 进度里消失、勾选里摘掉、删除标记落下
    assert jobs_manager.job_files("j1")["files"] == []
    assert jobs_manager.get_job("j1")["selection"] == {}
    assert jobs_manager.read_file_override("j1")["deleted"] == {"a/done.mp4"}
    # 目录树不再把它标成「已在任务中」
    assert jobs_manager.share_usage("j1") == {}

    # 重新勾选同一个 fid：added 计数 +1，删除标记被撤掉（否则子进程会一直过滤它）
    added = jobs_manager.add_job_selection("j1", {"file_fids": ["f1"]})
    assert added["added"] == {"files": 1, "dirs": 0}
    assert jobs_manager.get_job("j1")["selection"] == {"file_fids": ["f1"]}
    assert jobs_manager.read_file_override("j1")["deleted"] == set()
