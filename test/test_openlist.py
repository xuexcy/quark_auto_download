#!/usr/bin/env python3
"""
测试 OpenList API
"""

import sys
import os
import json
import logging

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openlist_api import OpenListClient


def setup_logger():
    """设置日志"""
    logging.basicConfig(
        level=logging.DEBUG,  # 改为DEBUG级别以显示API耗时
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    return logging.getLogger(__name__)


def load_config():
    """加载配置文件"""
    conf_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'conf')

    config = {}

    # 加载 aria2 配置
    aria2_config_path = os.path.join(conf_dir, 'aria2.json')
    if os.path.exists(aria2_config_path):
        with open(aria2_config_path, 'r', encoding='utf-8') as f:
            config.update(json.load(f))

    # 加载 openlist 配置
    openlist_config_path = os.path.join(conf_dir, 'openlist.json')
    if os.path.exists(openlist_config_path):
        with open(openlist_config_path, 'r', encoding='utf-8') as f:
            config.update(json.load(f))

    # 加载 quark 配置
    quark_config_path = os.path.join(conf_dir, 'quark.json')
    if os.path.exists(quark_config_path):
        with open(quark_config_path, 'r', encoding='utf-8') as f:
            config.update(json.load(f))

    return config


def format_file_size(size_bytes):
    """格式化文件大小"""
    if size_bytes == 0:
        return "0 B"

    size_names = ["B", "KB", "MB", "GB", "TB"]
    size_index = 0
    size = float(size_bytes)

    while size >= 1024 and size_index < len(size_names) - 1:
        size /= 1024
        size_index += 1

    return f"{size:.1f} {size_names[size_index]}"


def test_openlist_api():
    """测试 OpenList API"""
    logger = setup_logger()

    # 从配置文件读取参数
    config = load_config()
    openlist_host = config.get('openlist_host', 'http://localhost:5244')
    openlist_token = config.get('openlist_token', '')
    request_timeout = config.get('request_timeout', 30)

    # 测试目录路径
    test_paths = [
        "/",           # 根目录
        "/downloads",  # 下载目录（如果存在）
    ]

    try:
        # 创建 OpenList 客户端
        openlist_client = OpenListClient(
            host=openlist_host,
            token=openlist_token,
            request_timeout=request_timeout
        )

        logger.info("=== 测试 OpenList API ===")

        for path in test_paths:
            try:
                logger.info(f"列出目录: {path}")
                files = openlist_client.openlist_list(path)

                if not files:
                    logger.info(f"  目录为空或不存在: {path}")
                    continue

                logger.info(f"  找到 {len(files)} 个项目:")

                # 统计信息
                total_files = 0
                total_dirs = 0
                total_size = 0

                for file_info in files:
                    name = file_info.get('name', '未知')
                    is_dir = file_info.get('is_dir', False)
                    size = file_info.get('size', 0)

                    if is_dir:
                        total_dirs += 1
                        logger.info(f"  📁 {name}/")
                    else:
                        total_files += 1
                        total_size += size
                        size_str = format_file_size(size)
                        logger.info(f"  📄 {name} ({size_str})")

                # 显示统计信息
                logger.info(f"  统计: {total_dirs} 个目录, {total_files} 个文件")
                if total_files > 0:
                    logger.info(f"  总大小: {format_file_size(total_size)}")

                logger.info("")  # 空行分隔

            except Exception as e:
                logger.error(f"列出目录 {path} 失败: {e}")

        logger.info("=== OpenList API 测试完成 ===")

    except Exception as e:
        logger.error(f"OpenList API 测试失败: {e}")
        logger.error("请确保 OpenList 服务正在运行，并且配置正确")


if __name__ == "__main__":
    test_openlist_api()
