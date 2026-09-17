代码及文档均由 AI 生成(本行除外)
# Quark Auto Download

夸克网盘 + OpenList + Aria2 自动批量下载。支持 **Web 多分享链接** 管理：每个链接可单独开始 / 软暂停 / 硬暂停。

## 功能特性

- **Web 多任务**：一次添加多个分享链接；每个链接默认暂停，可独立开始/暂停
- **软暂停**：清除 Aria2 **排队**任务；**下载中**继续跑并跟踪到完成，然后停止该任务进程
- **硬暂停**：Aria2 **暂停**下载中 + 排队（保留进度），尽快停止该任务进程；再次开始时 unpause 恢复
- **断点续跑**：再次「开始」会识别 completed 已有文件，并接管 Aria2 中仍在进行 / 已暂停的任务
- **调度器 + Worker**：转存/清理/下载并行；失败跳过不堵队列
- **Aria2 排队水位**：全局 waiting 过高时暂停转存与新提交（清理不停）
- **完成后校验 + 空目录清理 + 0 字节跳过**
- **按日分目录日志**：Web / 任务日志分离；stdout 与 stderr 均写入对应日志文件

## 目录结构

```
code/
├── bin/           # Web 服务脚本：start.sh / stop.sh / restart.sh
├── bin_bak/       # 旧版非 Web CLI 脚本备份（run/stop/restart/web）
├── src/           # 业务代码（quark_main / pipeline / web_app / jobs_manager / log_util …）
├── web/static/    # 前端页面
├── conf/          # Cookie / OpenList / Aria2 等全局配置
├── state/
│   ├── web.pid
│   ├── jobs.yaml          # 分享任务列表（含各任务当前 log_path）
│   └── jobs/<id>/         # 每任务状态、控制文件、PID、报告
└── log/
    └── <YYYY_MM_DD>/
        ├── web/
        │   └── <HH_MM_SS>.log              # Web 服务日志（每次 start/restart 新建）
        └── jobs/
            └── <job_id>_<HH_MM_SS>.log     # 分享任务下载日志（每次开始新建）
```

## 依赖

```bash
pip install -r requirements.txt
# requests pyyaml fastapi uvicorn
```

## 配置

```bash
cp conf/example/quark.example.yaml conf/quark.yaml
cp conf/example/openlist.example.yaml conf/openlist.yaml
cp conf/example/aria2.example.yaml conf/aria2.yaml
```

`quark.yaml` 中的 Cookie、`save_to_dir` 等仍为全局配置；**分享链接改由 Web 任务列表管理**（不再依赖配置文件里的单个 `share_url` 日常切换）。

## 使用方法

### 启动 Web（NAS 局域网）

```bash
./bin/start.sh      # 后台启动；终端只打印 PID/LOG，运行日志不刷屏
./bin/stop.sh
./bin/restart.sh
```

浏览器：`http://<NAS局域网IP>:8787/`

**仅供内网使用，请勿做公网端口映射 / 内网穿透。**

```bash
QUARK_AUTO_DL_WEB_HOST=0.0.0.0 QUARK_AUTO_DL_WEB_PORT=8787 ./bin/start.sh
```

### 页面操作

1. **新增**：填写分享链接（可填提取码）→ 新增（默认**暂停**）
2. **开始**：启动该链接的下载进程；会接续本地已完成 / Aria2 进行中或已暂停任务
3. **暂停**
   - **软暂停**：删排队；下载中继续，跟踪完成后再停进程
   - **硬暂停**：暂停下载中 + 排队，立即停进程
4. **高级配置**：Cookie / OpenList / Aria2
5. **日志**：展开任务可查看该次运行的下载日志（路径保存在任务的 `log_path`）

### 旧版 CLI

仍可在 `bin_bak/` 找到旧的 `run.sh` / `stop.sh` 等；推荐改用 Web。

单任务前台调试：

```bash
QUARK_AUTO_DL_SHARE_URL='https://pan.quark.cn/s/xxx' \
QUARK_AUTO_DL_JOB_ID='debug' \
PYTHONPATH=src python src/quark_main.py
```

未指定 `QUARK_AUTO_DL_JOB_LOG_FILE` 时，日志落在 `log/<日期>/jobs/<JOB_ID>_<时间>.log`。

## 日志

Web 与任务分目录存放；**每次启动新建一个带时间戳的文件**；**stdout / stderr 一并写入**。

| 类型 | 路径 |
|------|------|
| Web 服务 | `log/<YYYY_MM_DD>/web/<HH_MM_SS>.log` |
| 分享任务下载 | `log/<YYYY_MM_DD>/jobs/<job_id>_<HH_MM_SS>.log` |

示例：

```
log/2026_09_17/web/18_17_05.log
log/2026_09_17/jobs/jabc123def0_18_17_05.log
```

格式（`src/log_util.py`）：

```
[INFO] [2026-09-17 18:17:10,956] [MainThread] [quark_main] [quark_main.py:run:245]: 本次日志文件: ...
```

对应 Formatter：

```python
[%(levelname)s] [%(asctime)s] [%(threadName)s] [%(name)s] [%(filename)s:%(funcName)s:%(lineno)d]: %(message)s
```

环境变量（可选）：

| 变量 | 含义 |
|------|------|
| `QUARK_AUTO_DL_WEB_LOG_FILE` | 指定 Web 日志文件（`start.sh` 会自动设置） |
| `QUARK_AUTO_DL_JOB_LOG_FILE` | 指定任务日志文件（Web 启动任务时自动设置） |

## 暂停说明

| 模式 | Aria2 排队 | Aria2 下载中 | 进程 | 再次开始 |
|------|------------|--------------|------|----------|
| 软暂停 | **删除** | 保留并跟踪到完成 | 活跃结束后退出 | 排队文件重新提交 |
| 硬暂停 | **暂停** | **暂停**（保留进度） | 尽快退出 | **unpause 恢复**，不重复提交 |

文件状态处理：

1. 本地 completed 已有 → 跳过（必要时清理网盘）
2. 转存成功 → 进入待下载
3. **按 path 排序有序推进**：主队列每次只轮到 1 个 `ready` 文件；轮到时再判断——有 Aria2 `paused` 则恢复，否则新建  
   （启动时发现暂停任务只放入待下载，不会批量恢复）

## 注意事项

- 保持 Cookie、OpenList、Aria2 可用；`openlist_quark_path` 对齐转存根目录
- 多任务并行时注意网盘容量与 Aria2 排队水位
- 分享中 0 字节文件会跳过；`No URI available` 表示直链对 Aria2 不可用
- 正常结束 / 软暂停结束会尽量做网盘清理；硬暂停可能留下未完成转存，再次开始后继续处理

## 许可证

本项目仅供学习和个人使用，请遵守相关服务的使用条款。
