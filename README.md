代码及文档均由 AI 生成(本行除外)
# Quark Auto Download

夸克网盘 + Aria2 自动批量下载。支持 **Web 多分享链接** 管理：每个链接可单独开始 / 软暂停 / 硬暂停。

当前版本 **2.8.20**。

## 功能特性

- **Web 多任务**：一次添加多个分享链接；每个链接默认暂停，可独立开始/暂停
- **勾选下载（可反复追加）**：新增任务后右侧「目录」按需展开，勾选文件 / 文件夹再点「下载所选」；勾选是**追加**语义，下完几个还能用同一个链接任务继续勾剩下的，已下过 / 已被其他任务勾过的文件会被拦下并提示来源任务名；任务**正在下载**时也能继续追加，新文件排进队列、本批下完接着下（不勾选 = 整棵分享目录）
- **软暂停**：清除 Aria2 **排队**任务；**下载中**继续跑并跟踪到完成，然后停止该任务进程
- **硬暂停**：Aria2 **暂停**下载中 + 排队（保留进度），尽快停止该任务进程；再次开始时 unpause 恢复
- **断点续跑**：再次「开始」会识别 completed 已有文件，并接管 Aria2 中仍在进行 / 已暂停的任务
- **调度器 + Worker**：转存有序入待下载队列；下载从队列取任务可并发；失败跳过不堵队列
- **Aria2 排队水位**：全局 waiting 过高时暂停转存与新提交（清理不停）
- **多套 Aria2 配置**：设置页「Aria2」面板可维护多条 RPC（＋ 添加 / 逐条「测试」连通性 / 勾「默认」决定新任务预选项），新增任务时在下拉里选一套；任务详情显示 `aria2=<配置名>`，没选就跟随默认配置
- **远端 Aria2**：配置里勾「远端 aria2」后只需要填 RPC 地址和令牌（两个目录留空即可），下载任务提交到对方的 Aria2、文件落在对方机器上；本机不做移动/校验，进度与完成判定都按 Aria2 状态算（见「注意事项」）
- **任务隔离**：每个任务独占下载子目录（`<aria2_download_dir>/<job_id>/`），只接管/暂停自己的 Aria2 任务，互不串味
- **失败不丢数据**：落盘失败、下载超时、Aria2 任务丢失时一律**保留网盘副本**，可再次开始续传
- **完成后校验 + 空目录清理 + 0 字节跳过**
- **可选访问控制**：HTTP Basic 登录、关闭 `/docs`、非 GET 请求同源校验、配置路径白名单
- **扫码登录**：设置页点「生成二维码」，用手机夸克 App 扫码并确认，自动换取 Cookie 并落盘；也保留手工粘贴（保存前先校验，无效不写入）
- **单文件进度**：详情页逐个文件展示「等待转存 / 转存中 / 待下载 / 下载中 / 已暂停 / 已完成 / 失败」、进度条、速度与大小，5s 刷新；连续 5 分钟 0 字节的行会标 `⚠ 0 字节 N 分钟`（多半是直链被 CDN 412/403 拒）
- **逐文件控制**：每行右侧一行按钮 —— ⏸ 暂停（下载中会同时暂停 Aria2 任务并保留进度）、▶ 开始（含失败重试）、✕ 删除（连同 Aria2 任务与云端副本一起清掉，之后可重新勾选）；这一行高度占满左侧「文件名 + 进度条」两行；**每一行都能删**（含已完成的，删的是任务里的记录，磁盘上的文件不动）
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

### aria2.yaml（多套 Aria2）

2.6.0 起支持多条 Aria2 配置；也可以在设置页「Aria2」面板里增删、逐条点「测试」验证连通性：

```yaml
aria2_profiles:
  - name: "默认"
    aria2_host: "http://127.0.0.1:6800/jsonrpc"
    aria2_secret: ""
    aria2_download_dir: "/downloads"
    download_destination_dir: "/downloads_done"
  - name: "备机"
    aria2_host: "http://192.168.1.101:6800/jsonrpc"
    aria2_secret: "token"
    aria2_download_dir: "/downloads"
    download_destination_dir: "/downloads_done"
  - name: "朋友的"            # 远端：只有地址和令牌
    aria2_remote: true
    aria2_host: "http://friend.example.com:6800/jsonrpc"
    aria2_secret: "friend-token"
    aria2_remote_dir: "/data/downloads"   # 远端下载目录，留空 = 用对方默认
    aria2_download_dir: ""            # 远端不用填
    download_destination_dir: ""      # 远端不用填

aria2_default: "默认"        # 新增任务时预选的配置
poll_interval: 60            # 其余键是全局项，所有配置共用
```

- 每套配置有四个键：`aria2_host` / `aria2_secret` / `aria2_download_dir` / `download_destination_dir`，
  外加可选的 `aria2_remote`（默认 `false`）与 `aria2_remote_dir`（远端下载目录，默认空 = 用对方的默认目录）；
  远端目录不受本机 `allowed_roots` 约束（那是对方机器上的路径）；
  轮询间隔、超时、重试、队列水位等都是全局项，所有配置共用。
- 老的单配置写法（顶层直接写这四个键）仍能启动：内存里当成一条名为「默认」的配置，
  在设置页保存一次后自动改写成 `aria2_profiles`。
- 保存时按**配置名**匹配来沿用旧 secret：页面上看到的是 `abcd****wxyz` 掩码，原样保存不会覆盖；
  但**改名等于新建**，secret 需要重填。
- 每条配置的下载目录同样受 `allowed_roots` 白名单约束（逐条校验）。
- 设置页的目录输入框只有两套、**互斥**（2.8.6 起）：勾了「远端 aria2」只显示一个「下载目录」
  （对方机器上的绝对路径，对应 `aria2_remote_dir`）；不勾才显示「临时下载目录」+「下载目录（完成后移入）」两个，
  分别对应 `aria2_download_dir` / `download_destination_dir`。被藏起来的那组值仍保存在配置里，切回来还在。
- 本机模式下两个目录都**可以留空**（2.8.2/2.8.3）：两个都空就不提交 `dir`、由 Aria2 用自己的默认目录，
  本机不校验、不移动；只填「下载目录」就直接下到那里；只填「临时下载目录」则先下到 `<临时>/<job_id>/`，
  下完挪到 `<临时>/` 根下——最终路径里看不到任务 ID。
- 「本地已有副本」判定与清理前的安全校验都按临时目录算；两个目录都没填时退回「只信 Aria2 的完成状态」。

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
   - **页面整体可以往下滚**：目录 / 文件进度 / 日志三块各自有最大高度，超出在自己框里滚动，不再互相挤（窄窗口也能一直往下看到底）
   - 详情区顶部是 **目录** 面板：**一律默认折叠**（不区分任务有没有下载记录），点头部展开；子目录点开才加载
   - 展开高度**按内容算**：两行就占两行的高度（不再固定撑成一大块）；最多**约一屏**（`calc(100vh - 3.5rem)`，实测 469px 窗口下 461px / 98%），超出在树体内滚动，页面整体还能继续往下滚
   - 勾选文件 / 文件夹后点 **下载所选** 才开始下载；勾中文件夹即下载整棵子树（不用展开子目录）
   - 标题栏左侧那个勾选框 = **整个目录**：勾上就是「这个分享里的全部文件」，不用逐个勾
   - 文件行点名字（或尺寸）也能勾选，不必非得点那个小方框
   - 勾选是**追加**的：先下两个、下完再回来勾几个都行，已下过的自动跳过；
     同一分享里已被别的任务勾过 / 下过的文件会打标并禁用勾选，强行提交会提示「跳过：xxx.mp4（已在「任务B」中）」
   - 任务**正在下载**时也能继续勾选点 **下载所选**：勾选项追加到任务，新文件排到队列末尾，本批下完自动接着下
     （提示「已加入队列，当前批次下完接着下」；运行中只是排队，不会立刻新开一路下载）
   - 运行中**取消勾选**仍需先暂停任务（避免删掉正在进行的下载）
   - 一个都不勾就点开始 = 下载整个目录；列表里显示「仅下载勾选：N 文件 / M 文件夹」
3. **开始**：启动该链接的下载进程；会接续本地已完成 / Aria2 进行中或已暂停任务
4. **暂停**
   - **软暂停**：删排队；下载中继续，跟踪完成后再停进程
   - **硬暂停**：暂停下载中 + 排队，立即停进程（轮询会立刻停下，不会再把任务 unpause 回来）
5. **统计**：顶部按钮行里、「暂停」与「删除」之间展示「已有文件数 / 总文件数」与「已有文件大小 / 总文件大小」（两块左右并排、标签在上值在下，上下与「暂停」按钮对齐）
6. **文件进度**：统计下方逐个文件列出状态、进度条、已下载 / 总大小与当前速度；顶部有过滤框（大任务只渲染前 400 行，用过滤框缩小范围）。任务不在运行时不再轮询该接口
   - 面板最高 `min(46vh, 26rem)`，文件多时在面板内滚动，不会把下面的日志顶出屏幕
   - 每行右侧是按钮（**并排一行**，整行高度与左边的「文件名 + 进度条」两行同高）：**⏸ 暂停** / **▶ 开始** / **✕ 删除**
     同一时刻最多两个：可暂停的行是 `⏸ ✕`，暂停/失败的行是 `▶ ✕`，已完成的行没有按钮
   - 暂停：下载中的文件会同时把 Aria2 任务 `forcePause`（进度保留、不会被轮询再 unpause 回来）；还没开始下载的只改状态，不碰 Aria2
   - 开始：`已暂停` 回到待下载/待转存；`失败` 也会重新入队并清掉失败记录
   - 删除：移除 Aria2 任务、尽力删掉云端转存副本、从任务队列与勾选里摘掉，之后可在「目录」里重新勾选；正在转存的文件会等这次转存结束再删（避免留云端孤儿副本）
   - 已完成（`已完成` / 清理中）的行没有 ⏸/▶，只剩 **✕ 删除**：删掉的是「任务里的这条记录」，本地已下载的文件留在磁盘上；
     删完这个文件在「目录」里就能重新勾选（若磁盘上的文件还在，会立刻按「本地已存在」判定完成；想真正重下就先删掉本地文件）
   - 运行中的按钮点了约 1 秒内生效（子进程每个 tick 读一次控制文件）
7. **分享链接**：详情中的链接可点击在新标签打开；列表定时刷新为原地更新，不整页闪烁
8. **日志**：详情底部「任务日志」默认**折叠**（只留一行标题），点标题行展开、再点折叠；折叠时不会去拉日志，展开后**整个面板**（标题行 + 日志框）最高约一屏（`calc(100vh - 5.4rem)`，实测 469px 窗口下面板 382.6px / 97.6% 可视区），滚到底时「任务日志」标题仍在视野顶部、不会被顶出去，日志内容在框内滚动；
   标题行右侧的 **刷新日志** 在折叠时点一下 = 展开并加载
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
- **「⚠ 0 字节 N 分钟」的红标 = 这个文件提交成功了但一个字节都没下来**：pipeline 每 tick 记录
  aria2 的 completedLength，连续 0 字节满 5 分钟（ZERO_STALL_SEC = 300）就在该行标出来，
  任务日志同时打一条「⚠ … 已连续 N 秒 0 字节」（每个文件只打一次）。常见原因：直链被 CDN 412/403 拒、
  远端 aria2 的落盘目录不可写（errorCode 16/18）、任务刚被暂停。任务停止后标记自动消失，
  因为状态文件是死的，不该一直挂红标。阈值只影响显示，不会自动暂停或重试。
- **任务日志默认折叠**：折叠状态**不请求** `/api/jobs/<id>/logs`（5 秒轮询也不会反复读日志），点标题行展开才加载一次 250 行；
  展开后区域高度保底 `clamp(14rem, 46vh, 32rem)`（原来 10rem），窗口矮时整页可滚动看清日志。
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
- **已完成的行也能删，删的是「任务里的记录」不是磁盘上的文件**：点 ✕ 之后这条记录从任务里消失、fid 也从
  `selection.json` 摘掉，于是能在「目录」里重新勾选；但本地已下载的文件仍在原处，重新勾选后会被
  「本地已存在」判定为已完成（想真重下就先删本地文件）。`done` 状态不会去删云端转存副本，配额要等清理时再释放。
- **逐文件暂停/删除在运行中约 1 秒生效**：Web 只写 `state/jobs/<job_id>/file_override.yaml`，
  子进程每个 tick（1 秒）读一次；正在 `transferring`（转存中）的文件会等这次转存结束才暂停/删除，
  因为此刻打断会在网盘里留下已转存但没登记的孤儿副本（占配额）。
- **Aria2 配置是「任务启动时读一次」**：设置页改 host / secret / 目录，或者换「默认」配置，对**正在跑**的任务都不生效
  （子进程在 `start_job` 时就把配置名和值带走了），要重新「开始」才会用新值。任务上记的配置名被删掉或改名时，
  下次开始会退回默认配置，并在任务日志写 `===== 警告：Aria2 配置「旧名」不存在，改用「默认」 =====`，
  日志头也会有一行 `===== aria2=<配置名> =====` 便于核对。
- **远端 Aria2 模式下本机什么都不做**：`aria2_remote: true` 的配置提交任务时只带文件名（不带 `dir`），
  文件落在对方的下载目录里；本机不做「移动到完成后目录」、不做本地副本大小校验，也不清理本机空目录，
  完成判定只看 Aria2 的 `tellStatus`（报告里的 `destination_dir` / `local_path` 会写成「(远端 aria2)」）。
  夸克直链要的 `Cookie` / `Referer` / `User-Agent` 本来就在提交给 Aria2 的 `header` 里，远端 Aria2 会照用，
  所以对方机器不需要再配 Cookie —— 但这意味着**对方能看到你的直链**，共用前先想清楚这一点。
  夸克直链有时效，下载中途长时间暂停后继续可能 403，删掉该文件重新勾选即可。
- **Aria2 的 RPC 地址要填 `…:6800/jsonrpc`，不是 AriaNg 网页地址**：只填 `https://dl.example.com`（网页控制台）时，
  aria2 的 POST 会得到 `501 Not Implemented`（darkhttpd），旧版本会把它当临时错误重试 10 次、任务卡在
  `临时失败，进入重试旁路`。2.8.0 起：地址没写路径会自动补 `/jsonrpc`；返回的不是 JSON-RPC 时直接报
  「Aria2 RPC 地址不对…AriaNg 网页地址不能当 RPC 用」；令牌错报「令牌（rpc-secret）不对」；
  点「开始」之前先探一次 `aria2.getVersion`，连不上就不启动子进程（页面直接显示原因）。
  远端配置里 `out` 现在带子目录（`Show/S01E01.mkv`），避免对方机器上不同目录的同名文件互相覆盖。
- **远端 Aria2 的下载目录必须可写**：对方 aria2 的默认 `dir` 可能挂在只读卷上（实测一台 NAS 的 `/downloads` 就是），
  提交照样成功（拿到 gid），但 aria2 只回 `errorCode=16 Download aborted.`，页面上看起来就是
  「任务已提交、下载速度一直 0」。2.8.1 起远端配置可以填「远端下载目录」（如 `/data/downloads`，留空 = 用对方默认），
  并且 errorCode 16/18 会被翻译成「aria2 无法在下载目录创建/写入文件（目录只读、不存在，或文件被占用）」这类人话，
  直接判为不可重试（不再白跑 3 轮重试）。换目录前可以先在对方机器上确认哪个路径可写。
- **两个目录都可选，按组合决定下到哪**：`aria2_download_dir`（临时）与 `download_destination_dir`（完成）都不必填。
  `<job_id>` 只是「下载中的暂存目录」，最终落点里看不到它：
  - 有临时目录：先下到 `<临时目录>/<job_id>/<相对路径>`；下完挪到完成目录，**没填完成目录就挪到 `<临时目录>/` 根下**
  - 没有临时目录、有完成目录：**直接下到完成目录**（先落在 `<完成目录>/<job_id>/`，下完去掉 job_id）
  - 两个都没有：提交任务时**不带 `dir`**，由 Aria2 自己的默认目录决定。本机不知道落点，所以不扫描、不移动、不校验，
    完成与否只看 Aria2 状态（`totalLength` 与文件大小不符仍判失败并保留网盘副本）
  两个都空时不再需要读 `getGlobalOption`，启动任务前只探一次 `aria2.getVersion`（连通性），不会因为「没有默认目录」拦住。
  远端配置与以前一致：两个目录都不提交，本机不校验、不移动，完成与否只看 aria2 状态。
- **「重启 Web 服务」按钮按托管方式走不同路径**：systemd 托管且 `Restart=always` 时是「进程退出 → 约 5 秒后 systemd 拉起」，
  所以点完页面会短暂连不上，刷新即可（PID 会变）；`Restart` 不是 `always` 时接口直接返回 409 让手动 `systemctl restart`
  （避免点一下变成关机）；用 `bin/start.sh` 起的服务仍走 `bin/restart.sh`；手动 `uvicorn` 起的服务识别不出来，也返回 409。
