# -*- coding: utf-8 -*-
"""多 Aria2 配置：迁移、掩码合并、默认选择、保存校验与 Web 接口。"""

import contextlib

import pytest
import yaml
from fastapi.testclient import TestClient

import aria2_profiles as ap
import jobs_manager
import web_app


LEGACY = {
    "aria2_host": "http://127.0.0.1:6800/jsonrpc",
    "aria2_secret": "secret-legacy-1234",
    "aria2_download_dir": "/data/downloads",
    "download_destination_dir": "/data/completed",
    "poll_interval": 60,
    "aria2_queue_pause_threshold": 700,
}


# ---------- 纯函数：迁移与解析 ----------

def test_legacy_single_config_migrates_to_one_profile():
    profiles = ap.profiles_of(LEGACY)
    assert [p["name"] for p in profiles] == [ap.DEFAULT_PROFILE_NAME]
    assert profiles[0]["aria2_host"] == "http://127.0.0.1:6800/jsonrpc"
    assert profiles[0]["aria2_download_dir"] == "/data/downloads"
    # 全局项不混进单条配置
    assert "poll_interval" not in profiles[0]


def test_empty_config_has_no_profiles():
    assert ap.profiles_of({}) == []
    assert ap.profiles_of(None) == []
    assert ap.default_name({}) == ""
    assert ap.resolve_name({}, "任意") == ""


def test_profiles_dedupe_names_and_drop_blank_entries():
    config = {
        "aria2_profiles": [
            {"name": "本地", "aria2_host": "http://a", "aria2_download_dir": "/d1"},
            {"name": "本地", "aria2_host": "http://b", "aria2_download_dir": "/d2"},
            {"name": "空行"},  # 整条空白，丢弃
        ]
    }
    names = ap.profile_names(config)
    assert names == ["本地", "本地-2"]


def test_default_and_unknown_name_fall_back():
    config = {
        "aria2_profiles": [
            {"name": "A", "aria2_host": "http://a", "aria2_download_dir": "/d1"},
            {"name": "B", "aria2_host": "http://b", "aria2_download_dir": "/d2"},
        ],
        "aria2_default": "B",
    }
    assert ap.default_name(config) == "B"
    assert ap.resolve_name(config, "A") == "A"
    # 任务上记的名字被删了 → 退回默认
    assert ap.resolve_name(config, "已删除") == "B"
    assert ap.resolve_name(config, "") == "B"

    config["aria2_default"] = "不存在"
    assert ap.default_name(config) == "A"


def test_options_for_merges_global_and_profile_keys():
    config = dict(LEGACY)
    config.update(
        {
            "aria2_profiles": [
                {"name": "A", "aria2_host": "http://a", "aria2_secret": "s1",
                 "aria2_download_dir": "/d1", "download_destination_dir": "/c1"},
                {"name": "B", "aria2_host": "http://b", "aria2_secret": "s2",
                 "aria2_download_dir": "/d2", "download_destination_dir": "/c2"},
            ],
            "aria2_default": "A",
        }
    )
    options = ap.options_for(config, "B")
    assert options["aria2_host"] == "http://b"
    assert options["aria2_download_dir"] == "/d2"
    assert options["poll_interval"] == 60  # 全局项照旧可读
    assert "aria2_profiles" not in options and "aria2_default" not in options


def test_public_profiles_mask_secret():
    profiles = [{"name": "A", "aria2_host": "http://a", "aria2_secret": "abcdefgh12345678"}]
    masked = ap.public_profiles(profiles)
    assert masked[0]["aria2_secret"] == "abcd********5678"
    assert masked[0]["aria2_secret__masked"] is True
    # 原对象不被改写
    assert profiles[0]["aria2_secret"] == "abcdefgh12345678"


def test_merge_incoming_keeps_secret_by_name():
    current = {
        "aria2_profiles": [
            {"name": "A", "aria2_host": "http://a", "aria2_secret": "real-secret",
             "aria2_download_dir": "/d1"},
        ]
    }
    incoming = [
        {"name": "A", "aria2_host": "http://a2", "aria2_secret": "re************",
         "aria2_download_dir": "/d1"},
    ]
    merged = ap.merge_incoming(current, incoming)
    assert merged[0]["aria2_secret"] == "real-secret"
    assert merged[0]["aria2_host"] == "http://a2"


def test_validate_profiles_reports_missing_fields():
    with pytest.raises(ValueError, match="缺少 RPC Host"):
        ap.validate_profiles([{"name": "A", "aria2_download_dir": "/d"}])
    # 下载目录留空 = 用 aria2 自己的默认目录，不再报错
    ap.validate_profiles([{"name": "A", "aria2_host": "http://a"}])


# ---------- save_section / get_all_configs ----------

def patch_conf(monkeypatch, tmp_path, config):
    path = tmp_path / "aria2.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(jobs_manager, "config_path", lambda basename: str(tmp_path / f"{basename}.yaml"))
    monkeypatch.setattr(jobs_manager, "allowed_roots", lambda settings: [str(tmp_path)])
    monkeypatch.setattr(jobs_manager, "load_web_settings", lambda: {})
    return path


def test_save_section_writes_profiles_and_drops_legacy_keys(monkeypatch, tmp_path):
    path = patch_conf(monkeypatch, tmp_path, dict(LEGACY))
    jobs_manager.save_section(
        "aria2",
        {
            "aria2_profiles": [
                {"name": "本地", "aria2_host": "http://a", "aria2_secret": "sec-a-123456",
                 "aria2_download_dir": str(tmp_path / "d1"),
                 "download_destination_dir": str(tmp_path / "c1")},
                {"name": "NAS", "aria2_host": "http://b", "aria2_secret": "sec-b-123456",
                 "aria2_download_dir": str(tmp_path / "d2"),
                 "download_destination_dir": str(tmp_path / "c2")},
            ],
            "aria2_default": "NAS",
            "aria2_queue_pause_threshold": 600,
        },
    )
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert [p["name"] for p in saved["aria2_profiles"]] == ["本地", "NAS"]
    assert saved["aria2_default"] == "NAS"
    assert saved["aria2_queue_pause_threshold"] == 600
    # 旧版顶层四键被清掉，避免两套配置并存
    for key in ap.PROFILE_KEYS:
        assert key not in saved


def test_save_section_keeps_masked_secret_and_fixes_default(monkeypatch, tmp_path):
    current = {
        "aria2_profiles": [
            {"name": "A", "aria2_host": "http://a", "aria2_secret": "real-secret",
             "aria2_download_dir": str(tmp_path / "d")},
        ],
        "aria2_default": "A",
    }
    path = patch_conf(monkeypatch, tmp_path, current)
    jobs_manager.save_section(
        "aria2",
        {
            "aria2_profiles": [
                {"name": "A", "aria2_host": "http://a", "aria2_secret": "re********et",
                 "aria2_download_dir": str(tmp_path / "d")},
            ],
            # 指向不存在的配置：保存时纠正成列表第一条
            "aria2_default": "不存在的名字",
        },
    )
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["aria2_profiles"][0]["aria2_secret"] == "real-secret"
    assert saved["aria2_default"] == "A"


def test_save_section_rejects_profile_dir_outside_allowed_roots(monkeypatch, tmp_path):
    patch_conf(monkeypatch, tmp_path, {})
    with pytest.raises(ValueError, match="不在允许的目录内"):
        jobs_manager.save_section(
            "aria2",
            {
                "aria2_profiles": [
                    {"name": "坏", "aria2_host": "http://a", "aria2_download_dir": "/etc/passwd"},
                ]
            },
        )


def test_get_all_configs_masks_profile_secrets(monkeypatch, tmp_path):
    config = {
        "aria2_profiles": [
            {"name": "A", "aria2_host": "http://a", "aria2_secret": "abcdefgh12345678",
             "aria2_download_dir": "/d1"},
        ],
        "aria2_default": "A",
    }
    patch_conf(monkeypatch, tmp_path, config)
    masked = jobs_manager.get_all_configs()["aria2"]
    assert masked["aria2_profiles"][0]["aria2_secret"].startswith("abcd")
    assert masked["aria2_profiles"][0]["aria2_secret"] != "abcdefgh12345678"
    assert masked["aria2_default"] == "A"
    # 页面不需要再看到旧版单配置字段
    assert "aria2_host" not in masked

    raw = jobs_manager.get_all_configs(mask_secrets=False)["aria2"]
    assert raw["aria2_profiles"][0]["aria2_secret"] == "abcdefgh12345678"


# ---------- add_job ----------

def patch_jobs(monkeypatch, tmp_path, jobs, profiles=None):
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
    aria2_config = {"aria2_profiles": profiles} if profiles is not None else dict(LEGACY)
    monkeypatch.setattr(
        jobs_manager,
        "load_section",
        lambda basename: aria2_config if basename == "aria2" else {},
    )


def test_add_job_records_selected_profile(monkeypatch, tmp_path):
    jobs = []
    patch_jobs(
        monkeypatch,
        tmp_path,
        jobs,
        profiles=[
            {"name": "本地", "aria2_host": "http://a", "aria2_download_dir": "/d1"},
            {"name": "NAS", "aria2_host": "http://b", "aria2_download_dir": "/d2"},
        ],
    )
    job = jobs_manager.add_job("https://pan.quark.cn/s/abc", name="任务", aria2_profile="NAS")
    assert job["aria2_profile"] == "NAS"
    assert jobs[0]["aria2_profile"] == "NAS"


def test_add_job_rejects_unknown_profile(monkeypatch, tmp_path):
    jobs = []
    patch_jobs(
        monkeypatch,
        tmp_path,
        jobs,
        profiles=[{"name": "本地", "aria2_host": "http://a", "aria2_download_dir": "/d1"}],
    )
    with pytest.raises(ValueError, match="Aria2 配置不存在"):
        jobs_manager.add_job("https://pan.quark.cn/s/abc", name="任务", aria2_profile="没有这个")


def test_add_job_without_profile_leaves_it_empty(monkeypatch, tmp_path):
    jobs = []
    patch_jobs(monkeypatch, tmp_path, jobs)
    job = jobs_manager.add_job("https://pan.quark.cn/s/abc", name="任务")
    assert job["aria2_profile"] == ""


# ---------- Web 接口 ----------

def make_client():
    # Host 白名单只放行回环地址，测试客户端要伪装成 127.0.0.1
    return TestClient(web_app.app, base_url="http://127.0.0.1:8787")


def test_api_profiles_returns_masked_list(monkeypatch, tmp_path):
    config = {
        "aria2_profiles": [
            {"name": "A", "aria2_host": "http://a", "aria2_secret": "abcdefgh12345678",
             "aria2_download_dir": "/d1"},
        ],
        "aria2_default": "A",
    }
    patch_conf(monkeypatch, tmp_path, config)
    payload = make_client().get("/api/aria2/profiles").json()
    assert payload["default"] == "A"
    assert payload["profiles"][0]["aria2_host"] == "http://a"
    assert payload["profiles"][0]["aria2_secret"].startswith("abcd")


def test_api_test_connection_uses_saved_secret_when_masked(monkeypatch, tmp_path):
    config = {
        "aria2_profiles": [
            {"name": "A", "aria2_host": "http://a", "aria2_secret": "real-secret-1234",
             "aria2_download_dir": "/d1"},
        ],
        "aria2_default": "A",
    }
    patch_conf(monkeypatch, tmp_path, config)
    seen = {}

    def fake_test(host, secret="", timeout=8, **kwargs):
        seen["host"] = host
        seen["secret"] = secret
        return {"version": "1.37.0", "enabled_features": [], "num_active": 1,
                "num_waiting": 2, "download_speed": 0}

    monkeypatch.setattr(jobs_manager, "test_aria2_connection", fake_test)
    resp = make_client().post(
        "/api/aria2/test", json={"name": "A", "host": "", "secret": "re********34"}
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.37.0"
    # 页面传来的掩码/空 host 都被已保存的值补全
    assert seen == {"host": "http://a", "secret": "real-secret-1234"}


def test_api_test_connection_without_host_returns_400(monkeypatch, tmp_path):
    patch_conf(monkeypatch, tmp_path, {})
    resp = make_client().post("/api/aria2/test", json={"name": "新", "host": "", "secret": ""})
    assert resp.status_code == 400
    assert "请先填写 RPC Host" in resp.json()["detail"]


def test_api_test_connection_reports_failure(monkeypatch, tmp_path):
    patch_conf(
        monkeypatch,
        tmp_path,
        {"aria2_profiles": [{"name": "A", "aria2_host": "http://a", "aria2_download_dir": "/d"}]},
    )

    def fake_test(host, secret="", timeout=8, **kwargs):
        raise RuntimeError("连接被拒绝")

    monkeypatch.setattr(jobs_manager, "test_aria2_connection", fake_test)
    resp = make_client().post("/api/aria2/test", json={"name": "A", "host": "http://a", "secret": ""})
    assert resp.status_code == 400
    assert "连接失败" in resp.json()["detail"]


def test_api_create_job_passes_profile(monkeypatch):
    captured = {}

    def fake_add_job(share_url, share_pwd="", name="", selection=None, aria2_profile=""):
        captured["profile"] = aria2_profile
        return {"id": "j1", "name": name, "aria2_profile": aria2_profile}

    monkeypatch.setattr(web_app.jm, "add_job", fake_add_job)
    monkeypatch.setattr(web_app.jm, "refresh_job_runtime", lambda job: job)
    resp = make_client().post(
        "/api/jobs",
        json={"name": "任务", "share_url": "https://pan.quark.cn/s/abc", "aria2_profile": "NAS"},
    )
    assert resp.status_code == 200
    assert captured["profile"] == "NAS"


# ---------- 远端 aria2（只有 host + token） ----------

REMOTE_CONFIG = {
    "aria2_profiles": [
        {"name": "朋友的", "aria2_remote": True, "aria2_host": "http://friend:6800/jsonrpc",
         "aria2_secret": "token", "aria2_download_dir": "", "download_destination_dir": ""},
    ],
    "aria2_default": "朋友的",
}


def test_remote_profile_does_not_require_local_dirs():
    profiles = ap.profiles_of(REMOTE_CONFIG)
    assert profiles[0]["aria2_remote"] is True
    ap.validate_profiles(profiles)  # 远端不要求本机目录，不应抛
    options = ap.options_for(REMOTE_CONFIG, "朋友的")
    assert options["aria2_remote"] is True
    assert options["aria2_host"] == "http://friend:6800/jsonrpc"
    assert options["aria2_download_dir"] == ""


def test_local_profile_may_omit_download_dir():
    """本机配置也可以不填下载目录：留空就用 aria2 自己的默认目录（启动时读取）。"""
    profiles = ap.profiles_of({"aria2_profiles": [{"name": "本机", "aria2_host": "http://a"}]})
    ap.validate_profiles(profiles)
    assert ap.options_for({"aria2_profiles": profiles}, "本机")["aria2_download_dir"] == ""


def test_remote_flag_kept_by_merge_and_saved(monkeypatch, tmp_path):
    path = patch_conf(monkeypatch, tmp_path, {})
    jobs_manager.save_section(
        "aria2",
        {
            "aria2_profiles": [
                {"name": "朋友的", "aria2_remote": True,
                 "aria2_host": "http://friend:6800/jsonrpc", "aria2_secret": "tok-123456"},
            ],
            "aria2_default": "朋友的",
        },
    )
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["aria2_profiles"][0]["aria2_remote"] is True
    assert saved["aria2_profiles"][0]["aria2_download_dir"] == ""
    merged = jobs_manager.get_all_configs()["aria2"]["aria2_profiles"]
    assert merged[0]["aria2_remote"] is True

