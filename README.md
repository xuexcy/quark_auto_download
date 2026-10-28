代码及文档均由 AI 生成(本行除外)
# Quark Auto Download

夸克网盘 + OpenList + Aria2 自动批量下载。支持 **Web 多分享链接** 管理：每个链接可单独开始 / 软暂停 / 硬暂停。

当前版本 **2.1.0**（相对 2.0.0 的修复见文末「变更记录」）。

## 功能特性

- **Web 多任务**：一次添加多个分享链接；每个链接默认暂停，可独立开始/暂停
- **软暂停**：清除 Aria2 **排队**任务；**下载中**继续跑并跟踪到完成，然后停止该任务进程
- **硬暂停**：Aria2 **暂停**下载中 + 排队（保留进度），尽快停止该任务进程；再次开始时 unpause 恢复
- **断点续跑**：再次「开始」会识别 completed 已有文件，并接管 Aria2 中仍在进行 / 已暂停的任务
- **调度器 + Worker**：转存有序入待下载队列；下载从队列取任务可并发；失败跳过不堵队列
- **Aria2 排队水位**：全局 waiting 过高时暂停转存与新提交（清理不停）
- **任务隔离**：每个任务独占下载子目录（`<aria2_download_dir>/<job_id>/`），只接管/暂停自己的 Aria2 任务，互不串味
- **失败不丢数据**：落盘失败、下载超时、Aria2 任务丢失时一律**保留网盘副本**，可再次开始续传
- **完成后校验 + 空目录清理 + 0 字节跳过**
- **可选访问控制**：HTTP Basic 登录、关闭 `/docs`、非 GET 请求同源校验、配置路径白名单
- **按日分目录日志**：Web / 任务日志分离；stdout 与 stderr 均写入对应日志文件

## 目录结构

```
quark_auto_download/
├── bin/           # Web 服务脚本：start.sh / stop.sh / restart.sh
├── src/           # 业务代码（quark_main / pipeline / web_app / jobs_manager / log_util …）
├── web/static/    # 前端：index.html（任务）/ settings.html（配置）
├── conf/          # Cookie / OpenList / Aria2 / Web 认证等全局配置
├── py_venv/       # 虚拟环境（不入库，见 .gitignore）
├── state/
│   ├── web.pid
│   ├── jobs.yaml          # 分享任务列表（含各任务当前 log_path）
│   └── jobs/<id>/         # 每任务状态、控制文件、PID、报告（删除任务时一并清理）
└── log/
    └── <YYYY_MM_DD>/
        ├── web/
        │   └── <HH_MM_SS>.log              # Web 服务日志（每次 start/restart 新建）
        └── jobs/
            └── <job_id>_<HH_MM_SS>.log     # 分享任务下载日志（每次开始新建）
```

下载落盘位置：

```
<aria2_download_dir>/<job_id>/<分享内相对路径>    # 下载中（每个任务一个子目录）
<download_destination_dir>/<分享内相对路径>        # 下载完成（多任务共用，重名自动加时间戳）
```

## 依赖与首次安装

```bash
cd /path/to/quark_auto_download
/usr/bin/python3 -m venv py_venv
py_venv/bin/python -m pip install -r requirements.txt   # requests pyyaml fastapi uvicorn
```

- Web 由 `bin/start.sh` 用 `py_venv/bin/python3` 启动。
- 下载任务子进程（`src/quark_main.py`）默认也使用**项目内 `py_venv`**：`jobs_manager._python_bin()`
  依次尝试 `QUARK_AUTO_DL_PYTHON` → `py_venv/bin/python3` → `venv/bin/python3` → 上级目录的
  `bin/python3`（兼容旧布局）→ `python3`。`start.sh` 会显式 `export QUARK_AUTO_DL_PYTHON`，
  保证 Web 与任务用同一套依赖。

## 配置

```bash
cp conf/example/quark.example.yaml    conf/quark.yaml
cp conf/example/openlist.example.yaml conf/openlist.yaml
cp conf/example/aria2.example.yaml    conf/aria2.yaml
cp conf/example/web.example.yaml      conf/web.yaml      # 可选：Web 登录与路径白名单
```

配置文件含 Cookie / Token / 提取码，程序写入时会强制 **0600** 权限。

### quark.yaml

`quark_cookie`、`save_to_dir` 等为全局配置；分享链接由 Web 任务列表管理，配置里的 `share_url` 不再生效。

新增 `total_space_gb`：网盘总容量（GB），仅用于估算剩余空间，请按账号实际容量填写（默认 10）。
此前该值硬编码 10GB，容量不符时会导致"可用空间"判断错误。

### web.yaml（可选）

```yaml
web_username: "admin"
web_password: ""        # 留空 = 不启用登录；也可用环境变量覆盖
allowed_roots:          # 设置页允许写入的目录根
  - "/vol1"
  - "/vol2"
  - "/vol3"
  - "/vol4"
```

- 设置了 `web_password` 后，页面与所有接口都要求 HTTP Basic 认证（浏览器会弹登录框）。
- `allowed_roots` 限制 `aria2_download_dir` / `download_destination_dir` / `state_file` /
  `verify_report_file` 只能落在这些目录下，防止把下载目录改到 `~/.ssh`、`/etc` 一类位置。

## 使用方法

日常使用 **仅通过 Web + `bin/` 脚本**：`./bin/start.sh` 启动 Web，在浏览器里管理多分享任务；下载由 Web 拉起的 `quark_main.py` 子进程执行。

### 启动 / 停止 Web（NAS 局域网）

```bash
./bin/start.sh      # 仅后台启动 Web；终端只打印 PID/LOG，运行日志不刷屏
./bin/stop.sh       # 停止 Web **以及所有分享任务** 的 quark_main 子进程，并清理 pid 文件
./bin/restart.sh    # 等价于 stop.sh + start.sh
```

- **`stop.sh`**：按 `state/web.pid` 与 `state/jobs/*/job.pid` 精确杀进程；兜底清理只杀
  "命令行里同时含本项目绝对路径 + `uvicorn web_app:app` / `src/quark_main.py`" 的进程，
  不再用宽泛的 `pkill -f`，不会误杀编辑器或其它部署。
- **`start.sh` / `restart.sh` 后**：只拉起 Web，**不会**自动恢复各分享任务；需在页面上重新点「开始」。

浏览器：`http://<NAS局域网IP>:8787/`

```bash
QUARK_AUTO_DL_WEB_HOST=0.0.0.0 QUARK_AUTO_DL_WEB_PORT=8787 ./bin/start.sh
```

### 页面操作

主页 `/` 为 **左侧任务栏 + 右侧详情**；全局配置在 **设置** 页 `/settings`（侧栏底部入口）。

1. **新增**：侧栏右上角 **+** 弹出简表单，填写分享链接（可填提取码）→ 新增（默认**暂停**）
2. **选择任务**：点击侧栏中的任务，右侧显示详情（开始 / 暂停 / 删除 / 日志与统计）
3. **开始**：启动该链接的下载进程；会接续本地已完成 / Aria2 进行中或已暂停任务
4. **暂停**
   - **软暂停**：删排队；下载中继续，跟踪完成后再停进程
   - **硬暂停**：暂停下载中 + 排队，立即停进程（轮询会立刻停下，不会再把任务 unpause 回来）
5. **统计**：详情页展示文件总数、总计大小、已下载文件数、当前已有大小
6. **分享链接**：详情中的链接可点击在新标签打开；列表定时刷新为原地更新，不整页闪烁
7. **日志**：展开详情中的日志区可查看该次运行下载日志
8. **设置**：配置 Quark Cookie / OpenList / Aria2（保存后即时生效；服务启停请用 `bin/` 脚本）

### 安全说明（重要）

- 默认监听 `0.0.0.0:8787` 且**不启用登录**；请在内网使用，并建议在 `conf/web.yaml` 设置
  `web_password`。未设置密码时启动日志会打印告警。
- 所有页面与接口都可加登录；`/docs`、`/redoc`、`/openapi.json` 已关闭。
- 非 GET 请求会校验 `Origin` / `Referer` 与 `Host` 同源（浏览器跨站请求必带这两个头），
  防止恶意网页触发「启动任务 / 重启服务」。
- `/api/jobs` **不再下发提取码**（`share_pwd`），只返回 `share_pwd_set` 布尔值。
- 设置页只有 `allowed_roots` 下的路径可被写入；配置项 `*_dir` / `*_file` 必须是绝对路径。
- **请勿做公网端口映射 / 内网穿透**；若必须，请先设置强密码并确认 `allowed_roots` 范围。

### 调试任务子进程（可选）

`src/quark_main.py` 是 Web 启动的下载任务入口；需要单独前台调试时可：

```bash
QUARK_AUTO_DL_SHARE_URL='https://pan.quark.cn/s/xxx' \
QUARK_AUTO_DL_JOB_ID='debug' \
QUARK_AUTO_DL_JOB_LOG_FILE=/tmp/quark-debug.log \
PYTHONPATH=src py_venv/bin/python src/quark_main.py
```

未指定 `QUARK_AUTO_DL_JOB_LOG_FILE` 时，日志落在 `log/<日期>/jobs/<JOB_ID>_<时间>.log`。

## 日志

Web 与任务分目录存放；**每次启动新建一个带时间戳的文件**；**stdout / stderr 一并写入**。

| 类型 | 路径 |
|------|------|
| Web 服务 | `log/<YYYY_MM_DD>/web/<HH_MM_SS>.log` |
| 分享任务下载 | `log/<YYYY_MM_DD>/jobs/<job_id>_<HH_MM_SS>.log` |

格式（`src/log_util.py`）：

```
[INFO] [2026-09-17 18:17:10,956] [MainThread] [quark_main] [quark_main.py:run:245]: 本次日志文件: ...
```

环境变量：

| 变量 | 含义 |
|------|------|
| `QUARK_AUTO_DL_PYTHON` | 任务子进程使用的 Python（`start.sh` 自动设为 `py_venv/bin/python3`） |
| `QUARK_AUTO_DL_WEB_LOG_FILE` | 指定 Web 日志文件（`start.sh` 会自动设置） |
| `QUARK_AUTO_DL_JOB_LOG_FILE` | 指定任务日志文件（Web 启动任务时自动设置） |
| `QUARK_AUTO_DL_WEB_USER` / `QUARK_AUTO_DL_WEB_PASSWORD` | 覆盖 `conf/web.yaml` 的登录凭据 |
| `QUARK_AUTO_DL_WEB_HOST` / `QUARK_AUTO_DL_WEB_PORT` | 监听地址 / 端口 |

## 暂停说明

| 模式 | Aria2 排队 | Aria2 下载中 | 进程 | 再次开始 |
|------|------------|--------------|------|----------|
| 软暂停 | **删除** | 保留并跟踪到完成 | 活跃结束后退出 | 排队文件重新提交 |
| 硬暂停 | **暂停** | **暂停**（保留进度） | 尽快退出 | **unpause 恢复**，不重复提交 |

文件状态处理：

1. 本地 completed 已有 → 跳过（必要时清理网盘）
2. **有序范围（仅此）**：分享链接获取全部文件 →【有序】→ 有序转存 →【有序进入待下载队列】
   （状态名 `ready` = 待下载队列；`strict_download_order` 只约束这一段）
3. **下载 Worker**（无序/可并发）：从待下载队列取任务；已有 `downloading` 不阻止继续取后续任务
   - Aria2 已有 **paused** → unpause 恢复并记录
   - Aria2 已有 **active/waiting**（含人为恢复）→ 只接管并记录
   - 无已有任务 → addUri 新建并记录
   （启动时发现暂停任务只放入待下载队列，不批量恢复；active/waiting 启动时直接接管）

## 失败与重试语义

| 场景 | 处理 | 网盘副本 |
|------|------|----------|
| 下载完成但**移动到目标目录失败**（跨挂载点、权限、磁盘满） | 标记失败，停止本次处理 | **保留**（下次开始可续传） |
| 单文件下载超过 `download_timeout` | 标记失败 | **保留** |
| Aria2 任务状态连续 5 次查询失败（aria2 重启 / 任务被清空） | 标记失败 | **保留** |
| 同一文件被反复暂停（自动恢复超过 10 次） | 标记失败 | **保留** |
| Aria2 任务被移除（`removed`） | 标记失败 | **保留** |
| 下载 error 且重试达 `aria2_max_retries` | 标记失败并进入清理 | 删除（释放容量） |
| 转存失败 | 永久错误立即出局；临时错误进 `transfer_retry`，达上限出局 | 保留待下次 |

关键原则：**没有真正落盘，就不删网盘副本**——避免"云端删了、本地也没有"。

## 注意事项

- 保持 Cookie、OpenList、Aria2 可用；`openlist_quark_path` 对齐转存根目录
- 多任务并行时注意网盘容量与 Aria2 排队水位
- 分享中 0 字节文件会跳过；`No URI available` 表示直链对 Aria2 不可用
- 正常结束 / 软暂停结束会尽量做网盘清理；硬暂停可能留下未完成转存，再次开始后继续处理
- 删除任务会一并删除 `state/jobs/<job_id>/`，同一链接重新添加时是全新状态

## 变更记录

### 2.1.0

数据安全
- 「下载完成」改为**必须确认文件已移动到目标目录**才进入清理；否则标记失败并保留网盘副本
- 移动文件改用 `shutil.move`，支持跨挂载点（原 `os.replace` 会抛 `EXDEV`）
- `download_timeout` 真正生效（超时标记失败、保留副本）；aria2 任务失联 / 被移除 / 反复暂停都有出口，不再永久卡「运行中」

任务隔离
- 下载目录改为 `<aria2_download_dir>/<job_id>/`，每个任务独占子目录
- Aria2 任务只认本任务下载目录下的文件，取消 basename 兜底匹配；暂停/删除只作用于本任务

安全
- 新增可选 HTTP Basic 认证（`conf/web.yaml` 或环境变量）
- 关闭 `/docs` / `/redoc` / `/openapi.json`
- `/api/jobs` 不再下发 `share_pwd`
- 非 GET 请求校验 `Origin`/`Referer` 同源
- 设置页写入的 `*_dir` / `*_file` 必须在 `allowed_roots` 内，且必须是绝对路径
- 配置文件、状态文件、pid 文件统一 0600

健壮性
- 启动任务加非阻塞文件锁，连点不会起两个进程
- `jobs.yaml` 读-改-写加锁（线程 RLock + 跨进程 flock）
- 判定进程存活时排除僵尸进程，并校验 `/proc/<pid>/cmdline` 是 `quark_main.py`，避免 PID 复用误杀
- 删除任务时清理 `state/jobs/<id>/`
- 同一链接复用任务时也校验任务名唯一性
- `tail_file` 最多读取 512KB，避免超大日志撑爆内存
- `stop.sh` 兜底清理改为按项目绝对路径精确匹配
- 任务子进程优先使用项目内 `py_venv`
- 网盘配额由 `quark.total_space_gb` 配置（原硬编码 10GB）

## 许可证

本项目仅供学习和个人使用，请遵守相关服务的使用条款。
