# -*- coding: utf-8 -*-
"""配置文件加载（YAML，支持 # 注释）。"""

import os

import yaml

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（src 的上一级），conf/log/state 均相对根目录
BASE_DIR = os.path.dirname(SRC_DIR)
CONF_DIR = os.path.join(BASE_DIR, "conf")
CONFIG_EXTENSIONS = (".yaml", ".yml")


def resolve_config_path(env_key: str, basename: str, conf_dir: str = CONF_DIR) -> str:
    """按环境变量或 conf/<basename>.{yaml,yml} 解析配置路径。"""
    if env_key:
        env_path = os.getenv(env_key)
        if env_path:
            return env_path
    for ext in CONFIG_EXTENSIONS:
        path = os.path.join(conf_dir, f"{basename}{ext}")
        if os.path.exists(path):
            return path
    return os.path.join(conf_dir, f"{basename}.yaml")


def resolve_legacy_config_path(conf_dir: str = CONF_DIR) -> str:
    for name in ("config.yaml", "config.yml"):
        path = os.path.join(conf_dir, name)
        if os.path.exists(path):
            return path
    return os.path.join(conf_dir, "config.yaml")


def load_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误，应为键值映射: {config_path}")
    return data


def load_merged_configs(conf_dir: str = CONF_DIR) -> dict:
    """合并 quark / openlist / aria2 配置（测试用）。"""
    config = {}
    for basename in ("aria2", "openlist", "quark"):
        path = resolve_config_path("", basename, conf_dir=conf_dir)
        if os.path.exists(path):
            config.update(load_config(path))
    return config
