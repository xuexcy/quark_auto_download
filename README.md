代码及文档均由 AI 生成(本行除外)
# Quark Auto Download

夸克网盘 + OpenList + Aria2 自动批量下载脚本

## 项目描述

这是一个自动化脚本，用于批量下载夸克网盘中的文件。通过结合夸克网盘API、OpenList文件管理服务和Aria2下载器，实现高效的批量下载功能。脚本在有限的网盘空间内，通过"转存 → 下载 → 删除"的循环机制，自动管理下载流程。

## 功能特性

- **自动批量下载**: 支持从夸克网盘分享链接批量下载文件
- **智能空间管理**: 在有限网盘空间内循环利用，转存-下载-删除
- **多组件集成**: 集成夸克网盘、OpenList和Aria2服务
- **灵活配置**: 支持多种配置选项，包括超时、重试等
- **日志记录**: 详细的下载日志和状态跟踪
- **测试支持**: 提供完整的API测试套件

## 依赖要求

- Python 3.7+
- 夸克网盘账号
- OpenList 服务 (推荐使用 Alist)
- Aria2 下载器

## 安装步骤

1. 克隆或下载项目代码到本地目录

2. 安装Python依赖：
   ```bash
   pip install requests
   ```

3. 配置相关服务：
   - 启动 Aria2 服务
   - 启动 OpenList/Alist 服务
   - 获取夸克网盘Cookie

## 配置说明

项目使用三个配置文件，位于 `conf/` 目录下：

### 1. quark.json - 夸克网盘配置
```json
{
  "quark_cookie": "kps=xxx; sign=xxx; vcode=xxx",
  "share_url": "https://pan.quark.cn/s/xxxxxxxxxxxx",
  "share_pwd": "",
  "share_sub_dir": "",
  "file_name_regex": "",
  "save_to_dir": "alist共享",
  "request_timeout": 10,
  "task_poll_interval": 3,
  "task_timeout": 180
}
```

### 2. openlist.json - OpenList配置
```json
{
  "openlist_host": "http://127.0.0.1:5255",
  "openlist_token": "",
  "openlist_quark_path": "/quark",
  "request_timeout": 10,
  "refresh_delay_after_transfer": 30
}
```

### 3. aria2.json - Aria2配置
```json
{
  "aria2_host": "http://127.0.0.1:6800/jsonrpc",
  "aria2_secret": "",
  "aria2_download_dir": "/downloads",
  "download_destination_dir": "/downloads_done",
  "poll_interval": 60,
  "download_timeout": 7200,
  "request_timeout": 10,
  "aria2_max_retries": 3,
  "download_log_interval": 300,
  "download_submit_max_retries": 10,
  "download_submit_retry_interval": 30
}
```

**配置步骤：**
1. 复制 `conf/example/` 下的示例配置文件到 `conf/` 目录
2. 根据实际情况修改配置文件中的参数
3. 确保所有服务正常运行并可访问

## 使用方法

### 启动脚本
```bash
python quark_main.py
```

### 运行脚本
```bash
./run.sh
```

### 重启脚本
```bash
./restart.sh
```

## 测试

项目提供了完整的测试套件，位于 `test/` 目录下：

- `test_aria2.py`: 测试Aria2 API功能
- `test_openlist.py`: 测试OpenList API功能
- `test_quark.py`: 测试夸克网盘API功能

运行测试：
```bash
python test/test_aria2.py
python test/test_openlist.py
python test/test_quark.py
```

## 日志

下载日志保存在 `log/` 目录下，按日期组织。日志包含详细的下载状态、错误信息和统计数据。

## 注意事项

- 确保夸克网盘Cookie有效且未过期
- 确认Aria2和OpenList服务正常运行
- 根据网盘空间大小合理设置下载参数
- 定期检查日志文件，监控下载状态
- 建议在网络稳定的环境下运行

## 许可证

本项目仅供学习和个人使用，请遵守相关服务的使用条款。
# quark_auto_download
# quark_auto_download
