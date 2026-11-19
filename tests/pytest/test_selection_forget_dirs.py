"""勾选整个目录时撤销「逐文件删除」标记（修「选目录后一个文件都不下」）。"""

import pytest
from fastapi.testclient import TestClient

import jobs_manager as jm
import web_app


class DirFakeClient:
    """只实现 list_share_files 的替身：记录调用、可选抛错。"""

    def __init__(self, files=None, error=None):
        self.files = files if files is not None else []
        self.error = error
        self.calls = []

    def list_share_files(self, share_id, stoken, pdir_fid, relative_dir="", **kwargs):
        self.calls.append(
            {
                "share_id": share_id,
                "stoken": stoken,
                "base_fid": pdir_fid,
                "relative_dir": relative_dir,
                "kwargs": kwargs,
            }
        )
        if self.error:
            raise self.error
        return [dict(f) for f in self.files]


def _patch(monkeypatch, fake, forget_returns=2):
    monkeypatch.setattr(web_app, "_share_context", lambda url, pwd: (fake, "st", "sid", "root"))
    seen = []

    def fake_forget(job_id, **kwargs):
        seen.append((job_id, kwargs))
        return forget_returns

    monkeypatch.setattr(jm, "forget_deleted_paths", fake_forget)
    return seen


JOB = {"id": "j1", "name": "A", "share_url": "https://pan.quark.cn/s/abc", "share_pwd": ""}


def test_forgets_markers_for_every_file_under_dirs(monkeypatch):
    fake = DirFakeClient([{"fid": "f1"}, {"fid": ""}, {"fid": "f2"}])
    seen = _patch(monkeypatch, fake)

    removed = web_app._forget_deleted_for_dirs("j1", JOB, ["d1"])

    assert removed == 2
    assert seen == [("j1", {"fids": ["f1", "f2"]})]
    call = fake.calls[0]
    assert call["kwargs"]["selected_dir_fids"] == {"d1"}
    assert call["base_fid"] == "root" and call["relative_dir"] == ""


def test_list_failure_keeps_markers(monkeypatch):
    fake = DirFakeClient(error=RuntimeError("分享已失效"))
    seen = _patch(monkeypatch, fake)

    assert web_app._forget_deleted_for_dirs("j1", JOB, ["d1"]) == 0
    assert seen == []


def test_no_dirs_does_not_touch_share(monkeypatch):
    fake = DirFakeClient([{"fid": "f1"}])
    seen = _patch(monkeypatch, fake)

    assert web_app._forget_deleted_for_dirs("j1", JOB, ["", "  "]) == 0
    assert fake.calls == [] and seen == []


def test_missing_share_url_is_noop(monkeypatch):
    fake = DirFakeClient([{"fid": "f1"}])
    seen = _patch(monkeypatch, fake)

    assert web_app._forget_deleted_for_dirs("j1", {"id": "j1"}, ["d1"]) == 0
    assert fake.calls == [] and seen == []


@pytest.fixture()
def client():
    return TestClient(web_app.app, base_url="http://127.0.0.1:8787")


def _patch_endpoint(monkeypatch, order):
    monkeypatch.setattr(
        web_app,
        "_forget_deleted_for_dirs",
        lambda job_id, job, dirs: order.append(("forget", tuple(dirs))) or 1,
    )

    def fake_add(job_id, selection, force=False):
        order.append(("add", tuple(selection.get("dir_fids") or [])))
        return {
            "job": {"id": job_id, "name": "A", "selection": selection},
            "added": {"files": 0, "dirs": 1},
            "duplicates": [],
            "already_in_job": 0,
        }

    monkeypatch.setattr(jm, "get_job", lambda job_id: dict(JOB))
    monkeypatch.setattr(jm, "add_job_selection", fake_add)
    monkeypatch.setattr(jm, "refresh_job_runtime", lambda job: job)


def test_endpoint_clears_markers_before_adding_dirs(client, monkeypatch):
    order = []
    _patch_endpoint(monkeypatch, order)

    r = client.post("/api/jobs/j1/selection/add", json={"dir_fids": ["d1"]})

    assert r.status_code == 200
    assert order == [("forget", ("d1",)), ("add", ("d1",))]


def test_endpoint_file_only_selection_does_not_clear_by_dir(client, monkeypatch):
    order = []
    _patch_endpoint(monkeypatch, order)

    r = client.post("/api/jobs/j1/selection/add", json={"file_fids": ["f1"]})

    assert r.status_code == 200
    assert order == [("add", ())]
