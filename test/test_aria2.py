#!/usr/bin/env python3
"""
测试 Aria2 API
"""

import sys
import os
import json
import logging

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria2_api import Aria2Client


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


def test_aria2_api():
    """测试 Aria2 API"""
    logger = setup_logger()

    # 从配置文件读取参数
    config = load_config()
    aria2_host = config.get('aria2_host', 'http://localhost:6800/jsonrpc')
    aria2_secret = config.get('aria2_secret', '')
    download_dir = config.get('aria2_download_dir', '/tmp')
    request_timeout = config.get('request_timeout', 30)
    openlist_token = config.get('openlist_token', '')
    poll_interval = config.get('poll_interval', 5)
    download_timeout = config.get('download_timeout', 3600)

    try:
        # 创建 Aria2 客户端
        aria2_client = Aria2Client(
            host=aria2_host,
            secret=aria2_secret,
            download_dir=download_dir,
            request_timeout=request_timeout,
            openlist_token=openlist_token,
            poll_interval=poll_interval,
            download_timeout=download_timeout,
            logger=logger
        )

        logger.info("=== 测试 Aria2 API ===")

        # 获取现有任务状态
        logger.info("获取现有任务状态...")
        tasks_status = aria2_client.aria2_get_existing_tasks_by_name()

        if not tasks_status:
            logger.info("当前没有活跃任务")
        else:
            logger.info(f"找到 {len(tasks_status)} 个任务:")

            # 分类显示任务
            active_tasks = {name: status for name, status in tasks_status.items() if status == 'active'}
            waiting_tasks = {name: status for name, status in tasks_status.items() if status == 'waiting'}
            paused_tasks = {name: status for name, status in tasks_status.items() if status == 'paused'}
            complete_tasks = {name: status for name, status in tasks_status.items() if status == 'complete'}
            error_tasks = {name: status for name, status in tasks_status.items() if status == 'error'}
            removed_tasks = {name: status for name, status in tasks_status.items() if status == 'removed'}

            if active_tasks:
                logger.info(f"活跃任务 ({len(active_tasks)} 个):")
                for name, status in active_tasks.items():
                    logger.info(f"  - {name}: {status}")

            if waiting_tasks:
                logger.info(f"等待任务 ({len(waiting_tasks)} 个):")
                for name, status in waiting_tasks.items():
                    logger.info(f"  - {name}: {status}")

            if paused_tasks:
                logger.info(f"暂停任务 ({len(paused_tasks)} 个):")
                for name, status in paused_tasks.items():
                    logger.info(f"  - {name}: {status}")

            if complete_tasks:
                logger.info(f"完成任务 ({len(complete_tasks)} 个):")
                for name, status in list(complete_tasks.items())[:10]:  # 只显示前10个
                    logger.info(f"  - {name}: {status}")
                if len(complete_tasks) > 10:
                    logger.info(f"  ... 还有 {len(complete_tasks) - 10} 个完成任务")

            if error_tasks:
                logger.info(f"错误任务 ({len(error_tasks)} 个):")
                for name, status in error_tasks.items():
                    logger.info(f"  - {name}: {status}")

            if removed_tasks:
                logger.info(f"已移除任务 ({len(removed_tasks)} 个):")
                for name, status in list(removed_tasks.items())[:5]:  # 只显示前5个
                    logger.info(f"  - {name}: {status}")
                if len(removed_tasks) > 5:
                    logger.info(f"  ... 还有 {len(removed_tasks) - 5} 个已移除任务")

        logger.info("=== Aria2 API 测试完成 ===")

    except Exception as e:
        logger.error(f"Aria2 API 测试失败: {e}")
        logger.error("请确保 Aria2 服务正在运行，并且配置正确")


if __name__ == "__main__":
    test_aria2_api()
