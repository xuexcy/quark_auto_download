#!/usr/bin/env python3
"""
测试 Quark API
"""

import sys
import os
import json
import logging

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quark_api import QuarkClient


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


def test_quark_api():
    """测试 Quark API"""
    logger = setup_logger()

    # 从配置文件读取参数
    config = load_config()
    cookie = config.get('quark_cookie', '')
    request_timeout = config.get('request_timeout', 30)
    save_to_dir = config.get('save_to_dir', '')
    share_url = config.get('share_url', '')
    share_pwd = config.get('share_pwd', '')

    if not cookie:
        logger.error("需要配置有效的 Quark cookie")
        return

    try:
        # 创建 Quark 客户端
        quark_client = QuarkClient(
            cookie=cookie,
            request_timeout=request_timeout,
            save_to_dir=save_to_dir,
            share_url=share_url,
            share_pwd=share_pwd,
            logger=logger
        )
        quark_client.resolve_save_to_dir()

        logger.info("=== 测试 Quark API ===")

        # 测试获取可用空间
        logger.info("获取网盘可用空间...")
        try:
            available_space = quark_client.get_available_space()
            available_gb = available_space / (1024**3)
            logger.info(f"可用空间: {available_gb:.2f} GB")
        except Exception as e:
            logger.error(f"获取可用空间失败: {e}")

        # 测试列出所有文件
        logger.info("获取网盘中所有文件...")
        try:
            all_files = quark_client.list_my_files_recursive("0")

            if not all_files:
                logger.info("网盘中没有文件")
            else:
                logger.info(f"找到 {len(all_files)} 个文件")

                # 统计信息
                total_size = 0
                file_types = {}

                # 显示前20个文件
                logger.info("前20个文件:")
                for i, file_info in enumerate(all_files[:20]):
                    name = file_info.get('file_name', '未知')
                    size = quark_client._to_int(file_info.get('size', 0))
                    total_size += size

                    # 统计文件类型
                    ext = os.path.splitext(name)[1].lower()
                    if ext:
                        file_types[ext] = file_types.get(ext, 0) + 1
                    else:
                        file_types['无扩展名'] = file_types.get('无扩展名', 0) + 1

                    size_str = format_file_size(size)
                    logger.info(f"  {i+1:2d}. {name} ({size_str})")

                if len(all_files) > 20:
                    logger.info(f"  ... 还有 {len(all_files) - 20} 个文件")

                # 显示统计信息
                logger.info("文件统计:")
                logger.info(f"  总文件数: {len(all_files)}")
                logger.info(f"  总大小: {format_file_size(total_size)}")

                # 显示文件类型统计（前10个）
                logger.info("文件类型统计:")
                sorted_types = sorted(file_types.items(), key=lambda x: x[1], reverse=True)
                for ext, count in sorted_types[:10]:
                    logger.info(f"  {ext}: {count} 个")
                if len(sorted_types) > 10:
                    logger.info(f"  ... 还有 {len(sorted_types) - 10} 种类型")

                # 计算并显示容量信息
                total_capacity = 10 * 1024**3  # 10GB
                used_space = total_size
                available_space_calc = total_capacity - used_space
                if available_space_calc < 0:
                    available_space_calc = 0

                logger.info("容量信息:")
                logger.info(f"  总容量: {format_file_size(total_capacity)}")
                logger.info(f"  已使用: {format_file_size(used_space)}")
                logger.info(f"  剩余: {format_file_size(available_space_calc)}")
                logger.info(f"  使用率: {(used_space / total_capacity * 100):.1f}%")

        except Exception as e:
            logger.error(f"获取文件列表失败: {e}")

        logger.info("=== Quark API 测试完成 ===")

    except Exception as e:
        logger.error(f"Quark API 测试失败: {e}")
        logger.error("请确保配置了有效的 Quark cookie")


if __name__ == "__main__":
    test_quark_api()
