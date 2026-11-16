"""单文件进度：/api/jobs/{id}/files 的数据归一化。"""

import yaml

import jobs_manager


def make_state(tmp_path, monkeypatch, files, counts=None):
    state_file = tmp_path / "pipeline_state.yaml"
    state_file.write_text(
        yaml.safe_dump(
            {"files": files, "counts": counts or {}, "updated_at": "2026-11-16 12:00:00"},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jobs_manager, "job_state_file", lambda job_id: str(state_file))
    return state_file


def test_progress_of_downloading_file(tmp_path, monkeypatch):
    make_state(
        tmp_path,
        monkeypatch,
        [
            {"path": "a/b.mp4", "size": 1000, "state": "downloading",
             "downloaded": 250, "total": 1000, "speed": 1024},
        ],
    )
    data = jobs_manager.job_files("j1")
    assert data["file_total"] == 1
    item = data["files"][0]
    assert item["path"] == "a/b.mp4"
    assert item["progress"] == 25.0
    assert item["downloaded"] == 250
    assert item["speed"] == 1024


def test_done_and_cleanup_render_as_100_percent(tmp_path, monkeypatch):
    make_state(
        tmp_path,
        monkeypatch,
        [
            {"path": "a.mp4", "size": 100, "state": "done", "downloaded": 10, "total": 100},
            {"path": "b.mp4", "size": 100, "state": "cleanup", "downloaded": 0, "total": 100},
        ],
    )
    files = jobs_manager.job_files("j1")["files"]
    assert [f["progress"] for f in files] == [100.0, 100.0]
    assert [f["downloaded"] for f in files] == [100, 100]


def test_missing_progress_fields_fall_back_to_size(tmp_path, monkeypatch):
    make_state(tmp_path, monkeypatch, [{"path": "c.mp4", "size": 2048, "state": "pending"}])
    item = jobs_manager.job_files("j1")["files"][0]
    assert item["total"] == 2048
    assert item["downloaded"] == 0
    assert item["progress"] == 0.0


def test_offset_and_limit(tmp_path, monkeypatch):
    files = [{"path": f"f{i}.mp4", "size": 10, "state": "done"} for i in range(5)]
    make_state(tmp_path, monkeypatch, files)
    data = jobs_manager.job_files("j1", offset=1, limit=2)
    assert data["file_total"] == 5
    assert [f["path"] for f in data["files"]] == ["f1.mp4", "f2.mp4"]
    assert jobs_manager.job_files("j1", offset=-5, limit=0)["limit"] == 1


def test_empty_or_broken_state(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_manager, "job_state_file", lambda job_id: str(tmp_path / "nope.yaml"))
    data = jobs_manager.job_files("j1")
    assert data["files"] == []
    assert data["file_total"] == 0
