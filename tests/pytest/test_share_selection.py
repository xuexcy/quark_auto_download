"""分享目录树打标 + 「可反复勾选」的追加与去重逻辑。"""

import contextlib

import yaml

import jobs_manager


def patch_jobs(monkeypatch, tmp_path, jobs, states=None):
    """把 jobs.yaml / pipeline_state.yaml 换成内存里的小世界。"""
    states = states or {}
    monkeypatch.setattr(jobs_manager, "load_jobs_index", lambda: [dict(j) for j in jobs])
    monkeypatch.setattr(jobs_manager, "_index_lock", lambda: contextlib.nullcontext())

    def _upsert(job):
        for i, item in enumerate(jobs):
            if item["id"] == job["id"]:
                jobs[i] = dict(job)
                return dict(job)
        jobs.append(dict(job))
        return dict(job)

    monkeypatch.setattr(jobs_manager, "upsert_job", _upsert)

    def _state_file(job_id):
        return str(states.get(job_id, tmp_path / f"{job_id}-missing.yaml"))

    monkeypatch.setattr(jobs_manager, "job_state_file", _state_file)


def write_state(tmp_path, name, files):
    path = tmp_path / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump({"files": files}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_share_key_normalizes_url():
    base = "https://pan.quark.cn/s/abc123"
    assert jobs_manager.share_key(base) == "s/abc123"
    assert jobs_manager.share_key(base + "?pwd=1234") == "s/abc123"
    assert jobs_manager.share_key("  " + base + "/  ") == "s/abc123"
    assert jobs_manager.share_key("") == ""


def test_share_usage_marks_selection_and_downloaded(tmp_path, monkeypatch):
    state = write_state(
        tmp_path,
        "j1",
        [
            {"path": "a.mp4", "fid": "f1", "size": 10, "state": "done"},
            {"path": "b.mp4", "fid": "f2", "size": 10, "state": "downloading"},
        ],
    )
    jobs = [
        {"id": "j1", "name": "任务一", "share_url": "https://pan.quark.cn/s/abc"},
        {"id": "j2", "name": "其它分享", "share_url": "https://pan.quark.cn/s/other"},
    ]
    patch_jobs(monkeypatch, tmp_path, jobs, states={"j1": state})
    usage = jobs_manager.share_usage("j1")
    assert usage["f1"] == {"job_id": "j1", "job_name": "任务一", "state": "done", "kind": "file"}
    assert usage["f2"]["state"] == "downloading"


def test_share_usage_only_same_share(tmp_path, monkeypatch):
    state = write_state(tmp_path, "j2", [{"path": "x.mp4", "fid": "fx", "state": "done"}])
    jobs = [
        {"id": "j1", "name": "A", "share_url": "https://pan.quark.cn/s/abc"},
        {"id": "j2", "name": "B", "share_url": "https://pan.quark.cn/s/other"},
    ]
    patch_jobs(monkeypatch, tmp_path, jobs, states={"j2": state})
    assert jobs_manager.share_usage("j1") == {}


def test_add_job_selection_appends_and_keeps_old(tmp_path, monkeypatch):
    jobs = [{"id": "j1", "name": "A", "share_url": "https://pan.quark.cn/s/abc",
             "selection": {"file_fids": ["old1"]}}]
    patch_jobs(monkeypatch, tmp_path, jobs)
    result = jobs_manager.add_job_selection("j1", {"file_fids": ["new1", "new2"]})
    assert result["added"] == {"files": 2, "dirs": 0}
    assert jobs[0]["selection"]["file_fids"] == ["old1", "new1", "new2"]


def test_add_job_selection_runs_twice_without_growing(tmp_path, monkeypatch):
    jobs = [{"id": "j1", "name": "A", "share_url": "https://pan.quark.cn/s/abc"}]
    patch_jobs(monkeypatch, tmp_path, jobs)
    jobs_manager.add_job_selection("j1", {"file_fids": ["f1"]})
    again = jobs_manager.add_job_selection("j1", {"file_fids": ["f1"]})
    assert again["added"] == {"files": 0, "dirs": 0}
    assert again["already_in_job"] == 1
    assert jobs[0]["selection"]["file_fids"] == ["f1"]


def test_add_job_selection_blocks_other_job_duplicate(tmp_path, monkeypatch):
    state = write_state(tmp_path, "j2", [{"path": "a.mp4", "fid": "f9", "state": "done"}])
    jobs = [
        {"id": "j1", "name": "A", "share_url": "https://pan.quark.cn/s/abc"},
        {"id": "j2", "name": "B", "share_url": "https://pan.quark.cn/s/abc"},
    ]
    patch_jobs(monkeypatch, tmp_path, jobs, states={"j2": state})
    result = jobs_manager.add_job_selection("j1", {"file_fids": ["f9"]})
    assert result["added"] == {"files": 0, "dirs": 0}
    assert result["duplicates"][0]["job_name"] == "B"
    assert result["duplicates"][0]["reason"] == "other_job"
    assert not jobs[0].get("selection")


def test_add_job_selection_force_allows_other_job(tmp_path, monkeypatch):
    state = write_state(tmp_path, "j2", [{"path": "a.mp4", "fid": "f9", "state": "done"}])
    jobs = [
        {"id": "j1", "name": "A", "share_url": "https://pan.quark.cn/s/abc"},
        {"id": "j2", "name": "B", "share_url": "https://pan.quark.cn/s/abc"},
    ]
    patch_jobs(monkeypatch, tmp_path, jobs, states={"j2": state})
    result = jobs_manager.add_job_selection("j1", {"file_fids": ["f9"]}, force=True)
    assert result["added"] == {"files": 1, "dirs": 0}
    assert jobs[0]["selection"]["file_fids"] == ["f9"]


def test_add_job_selection_requires_marks(tmp_path, monkeypatch):
    jobs = [{"id": "j1", "name": "A", "share_url": "https://pan.quark.cn/s/abc"}]
    patch_jobs(monkeypatch, tmp_path, jobs)
    try:
        jobs_manager.add_job_selection("j1", {})
    except ValueError as e:
        assert "没有勾选" in str(e)
    else:
        raise AssertionError("空勾选应当报错")


def _patch_runtime(monkeypatch, running=False):
    monkeypatch.setattr(
        jobs_manager, "refresh_job_runtime", lambda job, persist=True: {"running": running}
    )


def test_remove_job_selection_drops_fid_and_state(tmp_path, monkeypatch):
    """取消勾选：从 selection 摘掉，同时删掉 pipeline_state 里的单文件记录。"""
    state = write_state(
        tmp_path,
        "j1",
        [
            {"path": "a.mp4", "fid": "f1", "size": 10, "state": "done"},
            {"path": "b.mp4", "fid": "f2", "size": 20, "state": "failed"},
            {"path": "c.mp4", "fid": "f3", "size": 30, "state": "done"},
        ],
    )
    jobs = [
        {
            "id": "j1",
            "name": "A",
            "share_url": "https://pan.quark.cn/s/abc",
            "selection": {"file_fids": ["f1", "f2"]},
            "selection_mode": "explicit",
        }
    ]
    patch_jobs(monkeypatch, tmp_path, jobs, states={"j1": state})
    _patch_runtime(monkeypatch)

    result = jobs_manager.remove_job_selection("j1", {"file_fids": ["f1"]})

    assert result["removed"] == {"files": 1, "dirs": 0}
    assert result["state_files_removed"] == 1
    assert jobs[0]["selection"] == {"file_fids": ["f2"]}
    left = yaml.safe_load(state.read_text(encoding="utf-8"))
    assert [f["fid"] for f in left["files"]] == ["f2", "f3"]
    assert left["counts"] == {"failed": 1, "done": 1}
    # 取消后该 fid 不再被任务占用 → 目录树可以重新勾选（f2/f3 仍留着）
    usage = jobs_manager.share_usage("j1")
    assert "f1" not in usage
    assert usage["f2"]["state"] == "failed"


def test_remove_job_selection_all_keeps_explicit_empty(tmp_path, monkeypatch):
    """把勾选全取消后 selection 为空，但仍是 explicit 模式（= 不下载任何文件）。"""
    jobs = [
        {
            "id": "j1",
            "name": "A",
            "share_url": "https://pan.quark.cn/s/abc",
            "selection": {"file_fids": ["f1"], "dir_fids": ["d1"]},
            "selection_mode": "explicit",
        }
    ]
    patch_jobs(monkeypatch, tmp_path, jobs)
    _patch_runtime(monkeypatch)

    result = jobs_manager.remove_job_selection("j1", {"file_fids": ["f1"], "dir_fids": ["d1"]})

    assert jobs[0]["selection"] == {}
    assert jobs[0]["selection_mode"] == "explicit"
    assert result["removed"] == {"files": 1, "dirs": 1}


def test_remove_job_selection_requires_targets(tmp_path, monkeypatch):
    jobs = [{"id": "j1", "name": "A", "share_url": "https://pan.quark.cn/s/abc"}]
    patch_jobs(monkeypatch, tmp_path, jobs)
    try:
        jobs_manager.remove_job_selection("j1", {})
    except ValueError as e:
        assert "没有指定" in str(e)
    else:
        raise AssertionError("空的选择应当报错")


def test_remove_job_selection_rejects_running_job(tmp_path, monkeypatch):
    jobs = [
        {
            "id": "j1",
            "name": "A",
            "share_url": "https://pan.quark.cn/s/abc",
            "selection": {"file_fids": ["f1"]},
        }
    ]
    patch_jobs(monkeypatch, tmp_path, jobs)
    _patch_runtime(monkeypatch, running=True)
    try:
        jobs_manager.remove_job_selection("j1", {"file_fids": ["f1"]})
    except RuntimeError as e:
        assert "正在运行" in str(e)
    else:
        raise AssertionError("运行中的任务应当拒绝取消勾选")
    assert jobs[0]["selection"] == {"file_fids": ["f1"]}
