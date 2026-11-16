代码及文档均由 AI 生成(本行除外)
# Quark Auto Download

夸克网盘 + Aria2 自动批量下载。支持 **Web 多分享链接** 管理：每个链接可单独开始 / 软暂停 / 硬暂停。

当前版本 **2.5.4**（相对 2.3.0 的变化见文末「变更记录」：新增扫码登录，不再需要手工扒 Cookie；2.4.1 补上换 Cookie 的真实链路与「单文件超限」的明确报错；2.4.2 把「浏览目录并勾选」搬到右侧详情区，可反复追加并跨任务去重；2.4.3 目录结构整理为 `src/server` + `src/web` + `tests/`；2.4.4 分享目录里给超单文件上限的文件打标（2.4.5 起默认关闭）；2.4.5 查明 23018 是 UA 触发的限制，改用官方客户端 UA 后大文件也能取直链；2.4.7 页面显示版本号、页面与 API 都不缓存，`/api/health` 还能自证 PID/代码目录，部署后刷新即生效；2.4.8 修详情区底部日志被目录树/进度面板挤没的布局问题；2.4.9 已勾选的条目可以在目录树取消，也能在「文件进度」里逐个删除，删掉之后可以重新勾选；2.5.0 设置页新增只读的「运行环境」面板，重启按「谁在托管进程」自动选路，systemd 下靠进程自退被 `Restart=always` 拉起；2.5.1 修掉转存轮询里的 `NameError`（会把已转存成功的文件当失败、留下云端孤儿副本）并把 32003「容量不足」讲清楚；2.5.2 任务**运行中**也能在目录树继续勾选并「下载所选」，新文件排进队列、本批下完自动接着下；2.5.3 「文件进度」每行右侧可逐个 **暂停 / 开始 / 删除**，下载中点暂停会同时暂停 Aria2 任务并保留进度，删掉的文件能回目录树重新勾选；2.5.4 把两个按钮并到一行、整行高度占满左侧两行）。

## 功能特性

- **Web 多任务**：一次添加多个分享链接；每个链接默认暂停，可独立开始/暂停
- **勾选下载（可反复追加）**：新增任务后右侧「分享目录」按需展开，勾选文件 / 文件夹再点「下载所选」；勾选是**追加**语义，下完几个还能用同一个链接任务继续勾剩下的，已下过 / 已被其他任务勾过的文件会被拦下并提示来源任务名；任务**正在下载**时也能继续追加，新文件排进队列、本批下完接着下（不勾选 = 整棵分享目录）
- **软暂停**：清除 Aria2 **排队**任务；**下载中**继续跑并跟踪到完成，然后停止该任务进程
- **硬暂停**：Aria2 **暂停**下载中 + 排队（保留进度），尽快停止该任务进程；再次开始时 unpause 恢复
- **断点续跑**：再次「开始」会识别 completed 已有文件，并接管 Aria2 中仍在进行 / 已暂停的任务
- **调度器 + Worker**：转存有序入待下载队列；下载从队列取任务可并发；失败跳过不堵队列
- **Aria2 排队水位**：全局 waiting 过高时暂停转存与新提交（清理不停）
- **任务隔离**：每个任务独占下载子目录（`<aria2_download_dir>/<job_id>/`），只接管/暂停自己的 Aria2 任务，互不串味
- **失败不丢数据**：落盘失败、下载超时、Aria2 任务丢失时一律**保留网盘副本**，可再次开始续传
- **完成后校验 + 空目录清理 + 0 字节跳过**
- **可选访问控制**：HTTP Basic 登录、关闭 `/docs`、非 GET 请求同源校验、配置路径白名单
- **扫码登录**：设置页点「生成二维码」，用手机夸克 App 扫码并确认，自动换取 Cookie 并落盘；也保留手工粘贴（保存前先校验，无效不写入）
- **单文件进度**：详情页逐个文件展示「等待转存 / 转存中 / 待下载 / 下载中 / 已暂停 / 已完成 / 失败」、进度条、速度与大小，5s 刷新
- **逐文件控制**：每行右侧一行按钮 —— ⏸ 暂停（下载中会同时暂停 Aria2 任务并保留进度）、▶ 开始（含失败重试）、✕ 删除（连同 Aria2 任务与云端副本一起清掉，之后可重新勾选）；这一行高度占满左侧「文件名 + 进度条」两行；已完成的行不出按钮
- **无 OpenList 依赖**：直链、目录判存、删云端副本全部走夸克原生接口
- **按日分目录日志**：Web / 任务日志分离；stdout 与 stderr 均写入对应日志文件

## 目录结构

```
quark_auto_download/
├── bin/           # Web 服务脚本：start.sh / stop.sh / restart.sh
├── src/
│   ├── server/    # 业务代码（quark_main / pipeline / web_app / jobs_manager / log_util …）
│   └── web/       # 前端静态资源
│       └── static/  # index.html（任务）/ settings.html（配置）
├── tests/
│   ├── pytest/    # 单元测试（.venv/bin/python -m pytest -q）
│   └── munual/    # 手工联调脚本（需真实 Aria2 / Quark 环境，不参与 pytest / CI）
├── conf/          # Cookie / Aria2 / Web 认证等全局配置（conf/*.yaml 不入库）
├── docs/          # 调研与方案：quark-qr-login.md（扫码登录协议、换 Cookie 五步链路、真机记录）
├── REVIEW.md      # 架构与历史问题审查记录（各版本为什么这么改）
├── .github/       # CI：compileall + pytest（Python 3.11 / 3.12）
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
py_venv/bin/python -m pip install -r requirements.txt        # 运行时依赖（已固定版本）
py_venv/bin/python -m pip install -r requirements-dev.txt    # 跑测试才需要：pytest / httpx
py_venv/bin/python -m pytest -q                              # 单元测试
```

二维码渲染用 `segno`（纯 Python，不需要 Pillow / 系统图形库）；缺这个包时扫码接口仍能生成
session，只是返回的 `qr_svg` 为空，页面会提示改用手工 Cookie。

- Web 由 `bin/start.sh` 用 `py_venv/bin/python3` 启动。
- 下载任务子进程（`src/server/quark_main.py`）默认也使用**项目内 `py_venv`**：`jobs_manager._python_bin()`
  依次尝试 `QUARK_AUTO_DL_PYTHON` → `py_venv/bin/python3` → `venv/bin/python3` → 上级目录的
  `bin/python3`（兼容旧布局）→ `python3`。`start.sh` 会显式 `export QUARK_AUTO_DL_PYTHON`，
  保证 Web 与任务用同一套依赖。

## 配置

```bash
cp conf/example/quark.example.yaml    conf/quark.yaml
cp conf/example/aria2.example.yaml    conf/aria2.yaml
cp conf/example/web.example.yaml      conf/web.yaml      # 可选：Web 登录与路径白名单
```

配置文件含 Cookie / Token / 提取码，程序写入时会强制 **0600** 权限。

### quark.yaml

`quark_cookie`、`save_to_dir` 等为全局配置；分享链接由 Web 任务列表管理，配置里的 `share_url` 不再生效。

`quark_cookie` 可以完全不用手抄：设置页 →「夸克登录」→ 扫码登录会自动写入（0600 权限）。
手工粘贴时也会先请求 `/1/clouddrive/member` 校验，通过后才落盘。

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
  "命令行里同时含本项目绝对路径 + `uvicorn web_app:app` / `src/server/quark_main.py`" 的进程，
  不再用宽泛的 `pkill -f`，不会误杀编辑器或其它部署。
- **`start.sh` / `restart.sh` 后**：只拉起 Web，**不会**自动恢复各分享任务；需在页面上重新点「开始」。

浏览器（本机）：`http://127.0.0.1:8787/`

要从局域网其它机器访问，需显式监听 `0.0.0.0`，并且**必须先设置 `web_password`**
（否则服务会拒绝启动，见下方「安全说明」）：

```bash
QUARK_AUTO_DL_WEB_HOST=0.0.0.0 QUARK_AUTO_DL_WEB_PORT=8787 ./bin/start.sh
```

用域名或反向代理访问时，把实际 `Host` 加进白名单：
`QUARK_AUTO_DL_WEB_ALLOWED_HOSTS=nas.local,192.168.1.10`。

### 页面操作

主页 `/` 为 **左侧任务栏 + 右侧详情**；全局配置在 **设置** 页 `/settings`（侧栏底部入口）。

1. **新增**：侧栏右上角 **+** 弹出简表单，填写分享链接（可填提取码）→ 新增（默认**暂停**）
2. **选择任务**：点击侧栏中的任务，右侧显示详情（开始 / 暂停 / 删除 / 日志与统计）
   - 详情区顶部是 **分享目录** 面板：默认折叠（有下载记录的任务），点头部展开；子目录点开才加载
   - 勾选文件 / 文件夹后点 **下载所选** 才开始下载；勾中文件夹即下载整棵子树（不用展开子目录）
   - 勾选是**追加**的：先下两个、下完再回来勾几个都行，已下过的自动跳过；
     同一分享里已被别的任务勾过 / 下过的文件会打标并禁用勾选，强行提交会提示「跳过：xxx.mp4（已在「任务B」中）」
   - 任务**正在下载**时也能继续勾选点 **下载所选**：勾选项追加到任务，新文件排到队列末尾，本批下完自动接着下
     （提示「已加入队列，当前批次下完接着下」；运行中只是排队，不会立刻新开一路下载）
   - 运行中**取消勾选**仍需先暂停任务（避免删掉正在进行的下载）
   - 一个都不勾就点开始 = 下载整个分享目录；列表里显示「仅下载勾选：N 文件 / M 文件夹」
3. **开始**：启动该链接的下载进程；会接续本地已完成 / Aria2 进行中或已暂停任务
4. **暂停**
   - **软暂停**：删排队；下载中继续，跟踪完成后再停进程
   - **硬暂停**：暂停下载中 + 排队，立即停进程（轮询会立刻停下，不会再把任务 unpause 回来）
5. **统计**：详情页展示文件总数、总计大小、已下载文件数、当前已有大小
6. **文件进度**：统计下方逐个文件列出状态、进度条、已下载 / 总大小与当前速度；顶部有过滤框（大任务只渲染前 400 行，用过滤框缩小范围）。任务不在运行时不再轮询该接口
   - 每行右侧是按钮（**并排一行**，整行高度与左边的「文件名 + 进度条」两行同高）：**⏸ 暂停** / **▶ 开始** / **✕ 删除**
     同一时刻最多两个：可暂停的行是 `⏸ ✕`，暂停/失败的行是 `▶ ✕`，已完成的行没有按钮
   - 暂停：下载中的文件会同时把 Aria2 任务 `forcePause`（进度保留、不会被轮询再 unpause 回来）；还没开始下载的只改状态，不碰 Aria2
   - 开始：`已暂停` 回到待下载/待转存；`失败` 也会重新入队并清掉失败记录
   - 删除：移除 Aria2 任务、尽力删掉云端转存副本、从任务队列与勾选里摘掉，之后可在「分享目录」重新勾选；正在转存的文件会等这次转存结束再删（避免留云端孤儿副本）
   - 已完成（`已完成` / 清理中）的行不给按钮；运行中的按钮点了约 1 秒内生效（子进程每个 tick 读一次控制文件）
7. **分享链接**：详情中的链接可点击在新标签打开；列表定时刷新为原地更新，不整页闪烁
8. **日志**：展开详情中的日志区可查看该次运行下载日志
9. **设置**：配置 Quark Cookie / Aria2（保存后即时生效；服务启停请用 `bin/` 脚本）
   - **夸克登录**：两个 tab ——「扫码登录」（生成二维码 → 手机夸克 App 扫码并确认 → 自动换 Cookie 落盘）
     与「手工 Cookie」（粘贴后校验再保存，另有「测试当前 Cookie」按钮）
   - **运行环境**：只读展示本服务由谁托管（systemd / `bin/start.sh` 的 pid 文件 / 未识别）、开机自启是否开启、unit 名，
     并给一行可复制的 `sudo systemctl enable|disable <unit>`；页面**不提供**自启开关（见「注意事项」）

### 安全说明（重要）

- 默认只监听 **`127.0.0.1:8787`**（仅本机可访问）。要对外监听必须设置 `web_password`：
  监听非回环地址却没有密码时服务**直接拒绝启动**（调试用逃生开关 `QUARK_AUTO_DL_WEB_ALLOW_INSECURE=1`，风险自负）。
- 每个请求都校验 `Host` 头是否在白名单内（`localhost` / `127.0.0.1` / `::1` / 监听地址），
  防止 DNS rebinding；用域名访问需设置 `QUARK_AUTO_DL_WEB_ALLOWED_HOSTS`。
- 缺失 `Origin` / `Referer` 的非 GET 请求**不再**被当成同源，直接拒绝。
- 所有页面与接口都可加登录；`/docs`、`/redoc`、`/openapi.json` 已关闭。
- 非 GET 请求会校验 `Origin` / `Referer` 与 `Host` 同源（浏览器跨站请求必带这两个头），
  防止恶意网页触发「启动任务 / 重启服务」。
- `/api/jobs` **不再下发提取码**（`share_pwd`），只返回 `share_pwd_set` 布尔值。
- 设置页只有 `allowed_roots` 下的路径可被写入；配置项 `*_dir` / `*_file` 必须是绝对路径。
- **请勿做公网端口映射 / 内网穿透**；若必须，请先设置强密码并确认 `allowed_roots` 范围。

### 调试任务子进程（可选）

`src/server/quark_main.py` 是 Web 启动的下载任务入口；需要单独前台调试时可：

```bash
QUARK_AUTO_DL_SHARE_URL='https://pan.quark.cn/s/xxx' \
QUARK_AUTO_DL_JOB_ID='debug' \
QUARK_AUTO_DL_JOB_LOG_FILE=/tmp/quark-debug.log \
PYTHONPATH=src/server py_venv/bin/python src/server/quark_main.py
```

未指定 `QUARK_AUTO_DL_JOB_LOG_FILE` 时，日志落在 `log/<日期>/jobs/<JOB_ID>_<时间>.log`。

## 日志

Web 与任务分目录存放；**每次启动新建一个带时间戳的文件**；**stdout / stderr 一并写入**。

| 类型 | 路径 |
|------|------|
| Web 服务 | `log/<YYYY_MM_DD>/web/<HH_MM_SS>.log` |
| 分享任务下载 | `log/<YYYY_MM_DD>/jobs/<job_id>_<HH_MM_SS>.log` |

格式（`src/server/log_util.py`）：

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
| `QUARK_AUTO_DL_WEB_HOST` / `QUARK_AUTO_DL_WEB_PORT` | 监听地址 / 端口（默认 `127.0.0.1:8787`） |
| `QUARK_AUTO_DL_WEB_ALLOWED_HOSTS` | `Host` 白名单（逗号分隔），域名 / 反向代理访问时必填 |
| `QUARK_AUTO_DL_WEB_ALLOW_INSECURE` | `1` = 允许「非回环监听 + 无密码」（默认拒绝启动） |

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

- 保持 Cookie、Aria2 可用；直链由夸克原生 `file/download` 接口获取（需 Cookie 未过期）
- 多任务并行时注意网盘容量与 Aria2 排队水位
- 分享中 0 字节文件会跳过；`No URI available` 表示直链对 Aria2 不可用
- 正常结束 / 软暂停结束会尽量做网盘清理；硬暂停可能留下未完成转存，再次开始后继续处理
- 删除任务会一并删除 `state/jobs/<job_id>/`，同一链接重新添加时是全新状态
- **23018「单文件大小超限」其实跟 UA 有关**（2.4.5 实测纠正）：同一个账号、同一个 fid（6.62 GiB），
  `file/download` 的结果取决于请求的 `User-Agent` ——
  浏览器 UA → `400 code=23018 "download file size limit"`；
  官方桌面客户端 UA（`quark-cloud-drive/2.5.20 …`）→ `200` + 直链，两个域名（`drive.quark.cn`、
  `drive-pc.quark.cn`）都一样。所以本项目默认用客户端 UA 请求（`quark_api.DESKTOP_USER_AGENT`），
  也可以用 `quark_client_ua` 覆盖回浏览器 UA。
  这跟账号容量是两件事：容量（`/1/clouddrive/member` 的 `total_capacity`）决定「一共能存多少」，
  23018 拦的是「单个文件能不能下载」，接口里也没有暴露这个阈值。
  2.4.4 加的「⚠ 超 5 GiB」提示因此默认关闭（`quark_max_file_size_gb: 0`），只在你自己改回浏览器 UA
  并想提前预警时才需要打开。
- **Cookie 会轮换，旧 Cookie 的直链会被 CDN 拒绝**：同一账号重新扫码登录后，旧 Cookie 调
  `/1/clouddrive/*` 接口仍然返回 200，但用它换来的下载直链会被 CDN 以
  `412 Precondition Failed`（Tengine）拒掉（2026-11-16 在 NAS 上实测：9 月的旧 Cookie 全部 412，
  重新扫码后同一文件正常 206）。所以换机器/重新登录之后看到 412，先重新扫码，不要怀疑网络。
- **开机自启要自己开，页面只显示状态**：设置页的「运行环境」面板是只读的（`systemd` 托管时顺带给一行可复制命令）。
  要开机自启就 `sudo systemctl enable quark_auto_download`，取消就 `disable` —— 这件事由运维层面决定，
  Web 接口不改系统配置，也不需要 sudo 权限。
- **网盘只有 10 GiB 左右时，41 GiB 的分享不是「同时下」，而是「转存一个 → 下完删一个」**：转存前会按
  `/1/clouddrive/member` 的剩余容量做闸门，塞不下就每 30 秒打一行 `[调度] 容量不足，等待腾出空间: <文件> (8647 MB > 127 MB)`。
  这时 `[下载主]` 那行会一直是 `ready(待下载)=0`（队列里没东西可下，Worker 空转，不是卡死）。
  手工清空网盘（含回收站）后再继续即可。
- **`[下载主]`、`[网盘]`、`[下载重试]` 是三个 Worker 的计数行，不是错误**：每 30 秒各打一行
  `pending=… transferring=… ready(待下载)=… downloading=… done=… failed=… | 主下载=… | Aria2排队=…(正常)`，
  出问题要看的是这几行的数字变化和同一份日志里的 `ERROR`/`失败` 行。
- **运行中追加勾选是「排队」而不是「另开一路下载」**：Web 端把新 fid 追加到任务后，会**原子重写**
  `state/jobs/<job_id>/selection.json`（写 tmp 再 `os.replace`：子进程可能正好在读，读到半截 JSON 会被当成
  「没有勾选文件」而误下整个分享）；下载进程每秒比对文件指纹（`mtime_ns` + 大小），变化后重新列举分享、
  把新文件按 `pending` 排到队尾，日志打 `[勾选] 检测到勾选更新：新增 N 个文件进入队列`，
  已在队列 / 已下载的跳过（`[勾选] 跳过 N 个已在队列中的文件`）。
  重新列举失败不推进指纹，下个 tick 继续重试，队列不受影响。受网盘容量限制，实际仍然是「转存一个 → 下完删一个」。
- **逐文件按钮只改本地队列 + Aria2，不动你下载目录里的半成品**：`delete` 会 `forceRemove` Aria2 任务、
  尽力删掉云端转存副本、并把 fid 从 `jobs.yaml` / `selection.json` 摘掉（所以能重新勾选），
  但已经下到一半的本地文件留在下载目录里。重新勾选同一个文件时，Aria2 默认 `allow-overwrite=false` +
  `auto-file-renaming=true`，可能把新的存成 `xxx.mkv.1`（这一点未在真机验证）；介意就先手动删掉半成品。
- **逐文件暂停/删除在运行中约 1 秒生效**：Web 只写 `state/jobs/<job_id>/file_override.yaml`，
  子进程每个 tick（1 秒）读一次；正在 `transferring`（转存中）的文件会等这次转存结束才暂停/删除，
  因为此刻打断会在网盘里留下已转存但没登记的孤儿副本（占配额）。
- **「重启 Web 服务」按钮按托管方式走不同路径**：systemd 托管且 `Restart=always` 时是「进程退出 → 约 5 秒后 systemd 拉起」，
  所以点完页面会短暂连不上，刷新即可（PID 会变）；`Restart` 不是 `always` 时接口直接返回 409 让手动 `systemctl restart`
  （避免点一下变成关机）；用 `bin/start.sh` 起的服务仍走 `bin/restart.sh`；手动 `uvicorn` 起的服务识别不出来，也返回 409。

## 变更记录

### 2.5.4

「文件进度」按钮排成一行（2026-11-16）

- **改动**：每行右侧原来的竖排按钮列（⏸ 在上、✕ 在下）改成**并排一行**，`.file-actions` 用
  `flex-direction: row` + `align-items: stretch`，整行高度跟着左侧「文件名 + 进度条」两行走
  （实测 `.file-actions` 与 `.file-main` 同高 25px，两个按钮各 32×25px 在同一 y 上）；按钮改成
  `inline-flex` 居中，宽度交给内容。
- **验证**：先在 NAS 页面上注入候选 CSS 量矩形（确认并排、同高），再落盘部署；页面徽标 2.5.4。

### 2.5.3

「文件进度」逐个文件 暂停 / 开始 / 删除（2026-11-16）

- **需求**：进度列表每行右侧加按钮（占两行，与左侧文件名 + 进度条同高）；已完成的不给按钮；
  已开始下载的点暂停要**同时**在 Aria2 里暂停；还没开始下载的暂停不必碰 Aria2。
- **控制通道**：新增 `state/jobs/<id>/file_override.yaml`（Web 进程写、任务子进程读，0600 原子写），三个键：
  - `paused: [...]` 声明式：集合里的文件必须保持暂停。子进程每 tick 重放一次，
    所以转存完成 / 提交把状态抢回 `ready` 也会被压回 `paused`（自愈）。
  - `deleted: [...]` 持久：重启后不再下载，追加勾选时同样被过滤。
  - `retry: [...]` 一次性意图：点「开始」或失败重开时写入，子进程幂等消费。
- **子进程（pipeline）**：`_apply_file_override()` 按「先删、再恢复、最后压暂停」落地；
  `_enforce_paused()` 只在 `downloading` 且有 gid 时 `forcePause`（进度保留、`_poll_one_download` 加了
  `state != "downloading"` 守卫，不会再把它 unpause 回来），`transferring` 跳过（此刻打断会留下云端孤儿副本），
  其余状态直接改 `paused`；`_drop_file()` 依次 `aria2_remove` → 删云端副本 → 从队列摘除，
  `transferring` 的删除延后到这次转存结束；`_purge_deleted_cloud_copies()` 在启动时补删
  「任务没在跑时删掉的文件」的云端副本（否则那几 GB 白占 10 GiB 配额）。
- **调度器**：`JobState` 新增 `paused`；`mark_user_paused()` / `resume_file()`（`paused` 回 `ready`|`pending`，
  `failed` 重新入队并清 `failed_files`）/ `drop_files()`；落盘 `queues.paused`。
- **接口**：`POST /api/jobs/{job_id}/files/action`，body `{"op": "pause|resume|delete", "paths": [...], "fids": [...]}`
  （别名 `suspend` / `start` / `remove`）。任务在跑 → 只写 override，交给子进程（`applied: "pipeline"`）；
  任务没在跑 → 直接改 `pipeline_state.yaml`（`applied: "immediate"`）。删除一律把 fid 从
  `jobs.yaml` 与 `selection.json` 摘掉，所以「删了就能重新勾选」；`GET /api/jobs/{id}/files` 合并 override
  （跳过已删、`paused` 覆盖状态）并给出 `paused / can_pause / can_resume / can_delete`。
- **前端**：每行改成「左：文件名 + meta + 进度条（两行），右：按钮列（占两行）」；⏸ / ▶ / ✕ 按 `can_*` 显示；
  暂停的行 meta 显示「已暂停 · 37.5% · 375 B/1000 B」，汇总多一项「已暂停 N」。
- **测试**：`tests/pytest/test_file_control.py`(11) + `test_file_action_api.py`(3) +
  `test_pipeline_file_override.py`(8)，全仓 **149 passed**。
- **预演验证**（本机打桩，非真机）：5 行假数据；⏸ → 行变 `paused`、meta 仍是 37.5%、按钮换 ▶；
  ▶ → 回 `pending`；✕ → 行消失、汇总「完成 1/4 · 已暂停 1 · 失败 1」；已完成的行三个按钮全隐藏。

### 2.5.2

任务运行中也能继续追加勾选（2026-11-16）

- **起因**：任务正在下载时，右侧「分享目录」里勾了新文件却点不动「下载所选」——按钮被 `jobIsActive(job)` 硬禁用，
  点了也只会提示「先暂停，再勾选并重新开始才会生效」。
- **光改前端没用**：`add_job_selection` 只把 fid 并进 `jobs.yaml` 的 `selection`，
  而正在跑的子进程只认启动时写下的 `state/jobs/<id>/selection.json`（`load_selection` 读一次就定了），
  所以「解禁按钮」会变成「提示成功但永远不下」。
- **后端**：
  - `jobs_manager.write_selection_file()`：tmp + `os.replace` 原子写 0600 勾选文件；`start_job` 与
    `add_job_selection` 都走它。
  - `scheduler.append_files()`：只追加未知路径（已在 `jobs` 里的任何状态都不覆盖）；本地已有同大小副本 → `done`；
    云端已有副本 → 复用 fid（`already_transferred`）；否则 `pending`。返回 `{added, skipped}`。
  - `pipeline._maybe_reload_selection()`：文件指纹不变就不打接口（每 tick 都跑）；变了才 `reload_selection()` →
    `append_files()` → `_persist_state(force=True)`；失败不推进指纹、同一次改动只报一次错。空档期（`_has_pending_work()`
    为假）也会再检查一次，避免「刚好在收尾瞬间追加」被漏掉。
  - `quark_main.list_share_selection()` + `load_selection(strict=True)`：重新列举时**绝不**退化成「下整个分享」。
- **前端**：`treeSyncHeader` 不再因 running 禁用「下载所选」，title 按运行态换文案；`treeDownloadSelected`
  记录 `wasRunning`，运行中不再调 `/start`（否则会报「任务已在运行中」），提示改为
  「已加入 1 个文件 / 0 个文件夹；已加入队列，当前批次下完接着下」。运行中**取消勾选**仍要求先暂停。
- **测试**：`tests/pytest/test_runtime_append.py` 8 个用例（追加只加未知路径 / 本地已有 / 云端复用 / 指纹不变不重列 /
  改动后进队列并落 `state.yaml` / 失败重试不破坏队列 / 追加后 `selection.json` 内容），全仓 `127 passed`。
- **预演验证**（本机打桩，非真机）：运行中任务勾 1 个文件 → 按钮可点 → `POST /api/jobs/jtest/selection/add`，
  **没有** `/start`，提示「已加入队列，当前批次下完接着下」；已暂停任务同样操作 → 追加后调 `/start`，提示「已开始下载」。

### 2.5.1

修转存轮询的 `NameError`，并把 32003「容量不足」说人话（2026-11-16）
- **现象**：`转存失败: <相对路径> | name 'status' is not defined`，而且重试几次之后变成
  `查询转存任务状态返回异常: {'status': 400, 'code': 32003, 'message': 'capacity limit[0]', …}`，
  网盘明明清空了却一直「容量不足」。
- **根因**：`src/server/quark_api.py` 的 `_wait_task_and_get_fids` 在轮询转存任务时，把
  `status = data_node.get("status")` 和 `if status is None: continue` 合并成了一句只检查、不赋值的判断，
  后面的 `if status == 2:` 直接抛 `NameError`。**转存在夸克侧其实成功了**，是我们读结果时崩了：
  fid 没记下、状态停在失败 → 云端留下的那份副本变成孤儿 → 再点开始又转存一份 → 10 GiB 配额被吃满 → 之后全是 32003。
- **修复**：恢复 `status = data_node.get("status") if isinstance(data_node, dict) else None` 并保留「结构异常 5 次放弃」的计数；
  新增 `_explain_task_error()`，32003 时把 `remain_capacity` 换算成 MB 并给出处置建议（删云端副本 / 清回收站 / 等本次下载清理完），
  两个 wait 函数（`_wait_task_and_get_fids` / `_wait_task_done`）都改用它；`error_classify` 把 `32003`、`capacity limit`、
  `容量不足` 归为临时错误（腾出空间后可重试，不会当永久错误跳过）。
- **验证**：新增 6 个用例（`tests/pytest/test_quark_task_wait.py`，含 status=2 取 fid 的回归点、32003 文案、异常结构放弃、分类为临时），
  全仓 121 个用例全绿。NAS 真机：修好后同一任务重新开始 → `发现已转存文件，跳过重复转存` → `ready(待下载) +1` →
  `[下载主] … downloading=1`，孤儿副本被按「相对路径 + 文件名」识别复用，Aria2 正常拿到直链开始下载。

### 2.5.0

不做「开机自启」开关，改只读探测 + 自退重启（2026-11-16）
- **为什么不给开关**：页面是局域网无密码访问，NAS 上又配了 `sudo -n`；加一个「开机自启」勾选框等于把「改系统级配置」
  塞进 HTTP 接口。unit 文件、`enable` 状态、sudo 规则属于运维 / 打包层（Docker、`compose`、macOS 都没有 systemd），
  应用不该管理自己的部署形态；开关值存 `conf/web.yaml` 还会与 `systemctl is-enabled` 漂移。
  顺带一个具体陷阱：`systemctl disable` **不会**让进程脱离 systemd，unit 里是 `Restart=always` ——
  若按「关掉自启就走 `restart.sh`」，`restart.sh` kill 掉进程后 systemd 立刻拉起一份、脚本又起一份，8787 端口直接打架。
- **新增 `src/server/supervisor.py`**（纯标准库、只读、10 秒缓存）：从 `/proc/self/cgroup` 找自己的 unit（`0::/system.slice/<unit>.service`），
  再用 `systemctl show -p ActiveState -p UnitFileState -p Restart -p FragmentPath -p MainPID` 补状态，
  并用 `MainPID == os.getpid()` 或环境变量 `SYSTEMD_EXEC_PID` 双重确认「这个 unit 就是我」；没有 systemd 时回落看 `state/web.pid` 是否指向本进程。
- **`/api/health` 增只读字段**：`supervisor` / `unit` / `unit_file_state` / `autostart` / `restart_policy` / `restart_hint`。
- **设置页新面板「运行环境」**：显示 `systemd 托管（unit xxx，Restart=always）`、`开机自启：已开启`，并给一行
  `sudo systemctl disable xxx` 可复制命令；非 systemd 部署显示「脚本启动」或「未识别」。页面里没有任何自启勾选框。
- **重启按托管方式自动选路**（`POST /api/web/restart`，三条互斥路径，都不调 sudo）：
  ① systemd 且 `Restart=always` → 回 200 后进程 `os._exit(1)`，约 5 秒被 systemd 拉起（返回 `mode: systemd-self-exit`）；
  ② systemd 但 `Restart` 不是 `always` → 409，提示手动执行 `sudo systemctl restart <unit>`（否则点重启等于关机）；
  ③ 有 `state/web.pid` 指向自己 → 仍走 `bin/restart.sh`；三者都不是 → 409 并说明「不会自动重启」。
- NAS 真机验证：`POST /api/web/restart` → pid 1524853 → 1525040（`NRestarts=1`）；设置页点按钮 → pid 1525040 → 1525677，
  页面显示「正在重启…请稍后刷新」。本机（手动 uvicorn）返回 409 并提示手动重启，符合预期。
- 新增 13 个用例（`tests/pytest/test_supervisor.py` 9 个 + `tests/pytest/test_web_restart.py` 4 个），全仓 115 个用例全绿。

### 2.4.9

勾选可以撤销：目录树取消勾选 +「文件进度」删除文件（2026-11-16）
- **取消勾选**：目录树里由**本任务**占用的条目不再置灰，取消勾选会从任务的 `selection` 里摘掉该 fid，
  并清掉 `pipeline_state.files` 里它的记录（`POST /api/jobs/{job_id}/selection/remove`），
  于是这条文件/文件夹重新变成可选；其它任务占用的条目仍置灰，不会被动到。
- **空勾选**：`selection` 为空原来等价于「下载整个分享目录」。为了让「把勾选全删光」表示「什么都不下」，
  新增 `job.selection_mode = explicit`：这种情况下子进程照样写 `selection.json`（空集合），
  `quark_main.load_selection()` 返回「已限定范围 + 空」，`load_share_files()` 直接返回空列表并在日志里提示。
- **文件进度可删除**：「文件进度」每行右侧新增 `✕`，删除会同时把它从进度列表、目录树勾选状态、
  `pipeline_state.files` 里移除，之后可以在目录树里重新勾选。
- **只允许非运行态**：任务正在跑时两个入口都会提示「任务正在运行：先暂停任务，再……」（运行中改状态文件会与流水线抢写）。
- **目录取消不连坐**：取消某个已选目录的勾选时，它子树里**已经列出来**的文件不会被一起删
  （那些要在「文件进度」里逐个删），页面提示文案写明了这一点。
- 新增 6 个接口/状态用例（`tests/pytest/test_share_selection.py`、`tests/pytest/test_share_tree_api.py`），全仓 103 个用例全绿。

### 2.4.8

详情区底部日志被挤没（2026-11-16）
- **现象**：任务详情区把「分享目录」「文件进度」都展开后，底部「日志」只剩一条边框、正文看不见。
- **根因**：`.detail` 是纵向 flex，`.job-log{flex:1 1 0;min-height:0}` 会被上方**内容高度**的两块挤到 0；
  而 `.main-body{overflow:hidden}` 又把溢出部分裁掉，于是日志连滚动都看不到（实测三块 103+421+178 px，日志区只剩 9px）。
- **修复**：日志块改 `flex:1 1 auto; min-height:10rem`（保底一屏可见）；分享目录、进度面板改为各自内部滚动并限高
  （目录 `min(18rem,22vh)`、进度 `min(30vh,16rem)`）；`.main-body` 改 `overflow:auto`，
  极端小的窗口整体可滚而不是被裁；窄屏（≤820px）媒体查询里把 `pre.log{min-height:12rem}` 改为
  `.job-log{min-height:14rem}`，避免内层把外层撑破。
- **验证**：1400×900 下三块全展开不再需要滚动，日志区 177px（折叠目录树时 375px）；756×469 窄屏日志区 224px，内层不再溢出。

### 2.4.7

版本号可见 + 页面与 API 都不缓存（2026-11-16）
- **版本号**：`/api/health` 返回 `{"ok": true, "version": "2.4.7", "pid": …, "started_at": "…", "uptime_seconds": …, "code_root": "…/src/server", "python": "3.11.2"}`，把「进程 PID / 代码目录 / 启动时间」一起暴露，用来区分「页面是旧缓存」和「后端跑的是旧代码」。
- **页面徽标**：首页侧边栏标题、设置页标题旁显示 `v2.4.7`；设置页副标题下方再显示一行 `后端 2.4.7 · PID … · 启动 … · /vol2/…/src/server`。
- **页面与 API 都不缓存**：除 `/static/*`（走 etag）外，所有响应加 `Cache-Control: no-store, must-revalidate`。2.4.6 只给 HTML 加了 no-store，`/api/health` 这类 JSON 仍可能被浏览器缓存，出现「页面是新的、版本接口回旧 JSON」的假象。
- 起因：部署到 fnOS NAS 后人工核对时怀疑「还是旧版本」，实测服务端返回的 `index.html` 与本地文件 sha256 完全一致、旧版本没有的路由（`GET /api/jobs/{id}/files`）也已在 NAS 生效，问题只在浏览器缓存与缺少版本标识。

### 2.4.5

23018「单文件超限」的真因是 User-Agent，改用客户端 UA（2026-11-16）
- **根因**：参考实现（OpenList `drivers/quark_uc/util.go`）与我们调的是同一个 `POST /1/clouddrive/file/download`
  （同样 `pr=ucpro&fr=pc`、同样 `{"fids": [...]}` ），唯一差别是它带官方桌面客户端 UA。
  实测同一个 fid（6.62 GiB）：浏览器 UA → `400 code=23018`；客户端 UA → `200` + 直链（两个域名都一样）
- **修复**：`quark_api.DESKTOP_USER_AGENT` 改为默认 UA（`quark-cloud-drive/2.5.20 …`），
  `QuarkClient.download_headers()`、分享目录树、任务子进程全部跟随；`conf/quark.yaml` 的 `quark_client_ua`
  可覆盖回浏览器 UA（复现问题时用）
- **验证**：6.62 GiB 的 `…中专生不是坏孩子.mkv` 经本项目代码取到直链后，`Range: bytes=0-1023`
  返回 `206`、`content-range: bytes 0-1023/7110174386`、`content-type: video/x-matroska`（首字节 `1a45dfa3`）
- **连带**：2.4.4 的「⚠ 超 5 GiB」打标默认关闭（`quark_max_file_size_gb: 0`），因为客户端 UA 下不再有该限制；
  23018 的报错文案补上「也可能是 quark_client_ua 被改成了浏览器 UA」并回显当前 UA 前 40 字符
- 新增 3 个 UA 用例，全仓 97 个用例全绿

### 2.4.4

分享目录给「超单文件上限」的文件打标（2026-11-16）
- `POST /api/share/tree` 的每个条目新增 `oversize` 字段，响应新增 `max_file_size`（字节，0 表示未开启打标）
- 阈值读 `conf/quark.yaml` 的 `quark_max_file_size_gb`，缺省 5 GiB；读配置失败或值非法一律回落到默认值
- 前端目录树给这类文件加虚线黄标「超出 5 GiB」，悬停说明会被夸克 23018 拒绝；勾选后点「下载所选」
  会在结果里追加「N 个文件超出单文件上限（5 GiB），可能下载失败」，但仍照常提交
- 目录不打标（目录的 size 是聚合值，与单文件限制无关）；新增 3 个接口用例，全仓 94 个用例全绿

### 2.4.3

目录结构整理（2026-11-16）
- 业务代码 `src/*.py` → **`src/server/`**；前端 `web/static/` → **`src/web/static/`**
- 测试 `tests_pytest/` → **`tests/pytest/`**；手工脚本 `tests_manual/` → **`tests/munual/`**
- 跟着改的路径：`config_loader.BASE_DIR` 上溯两级、`jobs_manager.MAIN_SCRIPT`（`src/server/quark_main.py`）
  与子进程 `PYTHONPATH`（`src/server`）、`web_app.WEB_DIR`（`src/web`）、`bin/start.sh` 的 `PYTHONPATH`、
  `bin/stop.sh` 的兜底匹配串、`pytest.ini` 的 `testpaths`、两个测试目录里的 `sys.path` 注入
- 无接口 / 行为变化；91 个用例全绿，服务起在 `PYTHONPATH=src/server` 下 `/`、`/settings`、`/static/*` 均 200

### 2.4.2

「浏览目录并选择」从侧边栏搬到右侧详情区，改成可反复追加（2026-11-16）
- **交互**：新增任务（默认暂停）后，右侧详情区顶部出现「分享目录」面板，按需展开目录树、勾选文件 / 文件夹，
  点「下载所选」才开始下载；不再要求在新增表单里先选好
- **追加语义**：勾选结果 union 进任务的 `selection`，不清空已有勾选；同一个任务可以「下两个 → 再勾几个 → 再下」，
  已经下过的文件靠本地存在性判定自动跳过
- **跨任务去重**：以分享内 `fid` 为键，`share_usage()` 汇总同一分享下所有任务（`selection` + `pipeline_state.yaml`
  的 `files[].fid`）判定状态，目录树里对已勾选 / 已下载 / 进行中的条目打标并禁用勾选框；
  重复项在 `POST /api/jobs/{id}/selection/add` 返回的 `duplicates` 里带 `reason`（`other_job` / `in_job_done`）与来源任务名，
  前端提示「跳过：xxx.mp4（已在「任务B」中）」；需要再下一份可带 `force: true`
- **新增接口**：`POST /api/jobs/{job_id}/selection/add`（追加勾选，返回 `added / duplicates / already_in_job`）；
  `POST /api/share/tree` 支持只传 `job_id`（链接与提取码从任务上取），每条 entry 带 `taken` 标注
- **行为修正**：`add_job` 不再「同一分享链接复用已有任务」——那会把旧任务改名并覆盖 `selection`，与追加语义冲突；
  现在同链接可建多个任务，重复文件由勾选时的去重拦下
- 测试补到 91 个（新增 `share_key` 归一化、`share_usage`、追加不增长、跨任务拦截与 `force` 放行、
  `/api/share/tree` 的 `taken`、任务创建不复用等用例）

### 2.4.1

真机扫码后按实际回包修正（2026-11-16）
- **换 Cookie 改成真实链路**：不再是 `/cas/login?client_id=532&code=<ticket>`（实测它只返回登录页 HTML
  + `_UP_*` 埋点 Cookie），改成「同一个 `requests.Session` 顺序走 5 步」：
  `uop.quark.cn/` → `pan.quark.cn/` → `pan.quark.cn/account/info?st=<ticket>&lw=scan` →
  `pan.quark.cn/account/user_login_method_info` → `drive-pc.quark.cn/1/clouddrive/auth/pc/flush`，
  拿到 `__pus`/`__puus` 即成功。真机实测 Cookie 555 字节、`/1/clouddrive/member` 校验通过
- 二维码显示不全修复：`segno` 的 SVG 没有 `viewBox`，塞进固定尺寸容器会被裁掉右下角 → 换成 `viewBox`；
  二维码过期自动续期最多 3 次；页面按 **120 秒倒计时**（服务端有效期 180 秒，提前 60 秒续期）
- **23018「单文件大小超限」**：`src/server/quark_api.py` 新增 `QuarkDownloadRejected`，遇到 23018 直接抛中文错误、
  不换域名重试；`src/server/file_api.py download_url_for()` 报错带上文件路径；`src/server/error_classify.py` 把
  `23018`/`download file size limit` 归为永久错误
- 测试补到 77 个（新增 5 步兑换链路、23018 拒绝 + 不重试、错误分级用例）

### 2.4.0

扫码登录（新增）
- 新模块 `src/server/quark_login.py`：申请二维码 token → 渲染二维码 → 轮询 service_ticket → 换 Cookie → 校验，
  状态机 `pending / confirmed / expired / failed`，二维码 3 分钟过期，会话只放内存
- 新增接口 `POST /api/quark/login/qr`、`GET /api/quark/login/qr/{session_id}`、
  `POST /api/quark/cookie`（校验后落盘）、`POST /api/quark/cookie/test`
- 设置页新增「夸克登录」面板：扫码 / 手工 Cookie 两个 tab；扫码成功即自动写入 `conf/quark.yaml`
- 二维码用 `segno` 渲染成内联 SVG，前端不需要额外 JS 库或 CDN
- 等价命令行（不改配置，只验证协议）见 `docs/quark-qr-login.md`

已知边界
- 换 Cookie 的端点无公开文档（2.4.1 已实证）：实测唯一可用的是 pan 侧 5 步登录链路，见
  `docs/quark-qr-login.md` 第 4 节；兑换失败时接口返回的诊断串会带上每一步的 HTTP 状态与收到的 Cookie 名
- 登录 ≠ 授权：扫码登录只拿到「你是谁」，网盘权限仍受账号本身限制（建议先用小号试）

### 2.3.0

去掉 OpenList（改为夸克原生接口）
- 取直链：`POST /1/clouddrive/file/download`（`drive.quark.cn` → `drive-pc.quark.cn` 依次尝试），
  直链下载请求交给 Aria2 时注入 `Cookie` / `Referer` / `User-Agent` 头（原先只加 OpenList 的 `Authorization`）
- 判"是否已转存"：`file/sort` 按目录批量列举（仍是一个目录只列一次），命中即回填 `my_fid` 复用
- 删云端副本：`file/delete` 批量删除 + 删除后回查确认；本地副本缺失或大小不符时依旧拒绝删除
- 删除 `src/server/openlist_api.py`、`conf/example/openlist.example.yaml`、`tests/munual/test_openlist.py`；
  设置页去掉 OpenList 面板；`load_all_configs()` 只读 `quark.yaml` / `aria2.yaml`（旧版单文件仍兼容）
- 说明：转存这一步无法省略——分享文件必须先转存拿到自己的 `fid` 才能换直链

单文件进度
- 新增 `GET /api/jobs/{id}/files?offset=&limit=`：读 `pipeline_state.yaml`，返回每个文件的
  `state` / `downloaded` / `total` / `speed` / `progress` / `note`（不查 Aria2，不打散落盘节流）
- 调度器落盘时写入单文件进度；详情页新增「文件进度」区，按事件差分更新 DOM，支持文件名过滤

### 2.2.0

新增：勾选下载
- 新增接口 `POST /api/share/tree`：按 `pdir_fid` 懒加载分享目录（目录在前，含 file/dir、size）
- 新增任务后可浏览目录树并勾选文件 / 文件夹，勾选结果随任务保存（2.4.2 起移到右侧详情区，`POST /api/jobs/{id}/selection/add` 追加）
- 下载阶段按勾选过滤：勾文件 = 只下该文件；勾目录 = 该目录整棵子树；不勾 = 整个分享目录

数据安全（REVIEW P1-1 / P1-2 / P2-8）
- Aria2 报已完成但文件没落到目标目录 → 标记失败并**保留网盘副本**（原先会进清理流程删云端副本）
- 本地已存在判断加**大小比对**（调度初始化 / 运行态刷新 / 结束校验三处），截断或占位文件不再被当成已下载
- 目标目录重名改用 `O_EXCL` 原子抢名 + 时间戳后缀，同一秒也不会互相覆盖
- 转存后回查目标目录，只把真正出现的文件标为已转存；清理前确认本地有可信副本才删云端
- 校验报告 `ok` 纳入大小与 0 字节判断（`QUARK_AUTO_DL_STRICT_SIZE=0` 可放宽）

容量判断（REVIEW P1-3 / P3-2）
- 可用容量优先读网盘配额接口，失败才全盘递归统计，仍失败才回落估算值并告警
- 估算值（低置信）时不再无限等容量：直接判该文件失败并提示检查 `total_space_gb` 与 Cookie
- 默认缓存 TTL 从 180s 提到可配置 1800s（`space_cache_ttl`）

性能
- 任务列表的运行态刷新改为一次批量落盘（原每条任务都写一次 `jobs.yaml`，O(n²)）
- Aria2 任务快照按批复用（原每个文件 3 次 RPC）；转存存在性改为按目录批量查询
- 流水线状态文件落盘节流（默认 5s，退出时强制写）

并发 / 安全（REVIEW P1-4 / P2-5）
- `pause_job` 与 `start_job` 共用任务级锁；`upsert_job` 改为按字段合并，避免整条覆盖掉 `log_path` / `status`
- 默认只监听 `127.0.0.1`；`Host` 白名单防 DNS rebinding；缺失 `Origin` / `Referer` 不再视为同源
- 非回环监听且无密码时拒绝启动（`QUARK_AUTO_DL_WEB_ALLOW_INSECURE=1` 可绕过）

健壮性 / 其他
- 错误分类改正则匹配：`size=1401 bytes` 这类不再被误判为永久错误
- `_normalize_my_fid` 只在末段确为 16+ 位十六进制时才按 `-` 截断，不再切坏普通目录名
- 夸克接口成功判定统一为 `_is_ok`（`code`/`status` 同时校验），转存结果校验返回的 fid 数量
- 任务轮询连续 5 次异常即判失败，不再耗满 `task_timeout`；配置 / 状态文件写入加 fsync
- 任务结束按结果写 `done` / `failed`（原先无论成败都写 `paused`）
- `stop.sh` 兜底清理要求进程命令行为 python，不再误杀 `vim src/server/quark_main.py`
- 前端：`escapeHtml` 补单引号；轮询不再每 5s 拉 250 行日志；列表区分「已完成 / 有失败」
- 工程：`requirements.txt` 固定版本 + `requirements-dev.txt`、GitHub Actions CI、`tests/pytest/` 51 个 pytest 用例

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