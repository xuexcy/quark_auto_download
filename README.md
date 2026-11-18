代码及文档均由 AI 生成(本行除外)
# Quark Auto Download

夸克网盘 + Aria2 自动批量下载。支持 **Web 多分享链接** 管理：每个链接可单独开始 / 软暂停 / 硬暂停。

当前版本 **2.8.10**（相对 2.3.0 的变化见文末「变更记录」：新增扫码登录，不再需要手工扒 Cookie；2.4.1 补上换 Cookie 的真实链路与「单文件超限」的明确报错；2.4.2 把「浏览目录并勾选」搬到右侧详情区，可反复追加并跨任务去重；2.4.3 目录结构整理为 `src/server` + `src/web` + `tests/`；2.4.4 分享目录里给超单文件上限的文件打标（2.4.5 起默认关闭）；2.4.5 查明 23018 是 UA 触发的限制，改用官方客户端 UA 后大文件也能取直链；2.4.7 页面显示版本号、页面与 API 都不缓存，`/api/health` 还能自证 PID/代码目录，部署后刷新即生效；2.4.8 修详情区底部日志被目录树/进度面板挤没的布局问题；2.4.9 已勾选的条目可以在目录树取消，也能在「文件进度」里逐个删除，删掉之后可以重新勾选；2.5.0 设置页新增只读的「运行环境」面板，重启按「谁在托管进程」自动选路，systemd 下靠进程自退被 `Restart=always` 拉起；2.5.1 修掉转存轮询里的 `NameError`（会把已转存成功的文件当失败、留下云端孤儿副本）并把 32003「容量不足」讲清楚；2.5.2 任务**运行中**也能在目录树继续勾选并「下载所选」，新文件排进队列、本批下完自动接着下；2.5.3 「文件进度」每行右侧可逐个 **暂停 / 开始 / 删除**，下载中点暂停会同时暂停 Aria2 任务并保留进度，删掉的文件能回目录树重新勾选；2.5.4 把两个按钮并到一行、整行高度占满左侧两行；2.6.0 支持配置**多套 Aria2**，新增任务时选择用哪一套；2.6.1 修掉「完成后目录留空会把文件移到进程当前目录」的坑；2.7.0 支持**远端 Aria2**（填对方的 RPC 地址就能共用本应用下载，本机不参与落盘）；2.8.0 把「Aria2 地址填错」从「重试 10 次 501」变成一句能看懂的中文诊断（并在开始任务前先预检），同时修掉一轮代码 review 挖出来的并发、清理卡死与校验口径问题；2.8.1 让远端 Aria2 能指定「远端下载目录」，并把 aria2 的 errorCode（16/18 等）翻成人话——「任务提交成功但速度一直 0」的真因就是对方默认下载目录不可写，设置页改完配置还会自动测一次连通性。；2.8.2 让下载目录在**本机配置**里也可以留空——留空就用 Aria2 自己的默认目录（启动时读 `aria2.getGlobalOption` 的 `dir`），不用再纠结远端还是本机；2.8.3 把「临时目录 / 完成目录」都改成可选并定清三种组合——只填完成目录就直接下到完成目录，两个都空就用 Aria2 默认目录；2.8.4 把这些语义收干净：`<任务ID>` 只当下载中的暂存目录（最终落点看不到它），两个目录都空时提交任务干脆不带 `dir`、本机不校验不移动，远端与「本机不知道落点」统一成一个判断；2.8.5 在「文件进度」里给「连续 5 分钟 0 字节」的行打标记（多为直链被 CDN 412/403 拒绝）。；2.8.6 把设置页的下载目录收成两套（远端一个「下载目录」、本机「临时下载目录 + 下载目录」），不再三个输入框并排；2.8.7 修掉「非远端时仍显示『下载目录（对方机器上的绝对路径）』这句标签」的样式问题；2.8.8 右侧面板改名「目录」，标题栏新增「整个目录」勾选框（一勾就是下载整个目录），文件行点名字也能勾选；2.8.9 「文件进度」里**每一行**都能删（含已完成的，删的是任务里的记录、磁盘上的文件不动），删完就能回「目录」重新勾选；2.8.10 把「目录」树的缩进统一成固定台阶（每层 18.4px，「目录」那行到第 1 层也是一格）。

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
   - 详情区顶部是 **目录** 面板：默认折叠（有下载记录的任务），点头部展开；子目录点开才加载
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
5. **统计**：详情页展示文件总数、总计大小、已下载文件数、当前已有大小
6. **文件进度**：统计下方逐个文件列出状态、进度条、已下载 / 总大小与当前速度；顶部有过滤框（大任务只渲染前 400 行，用过滤框缩小范围）。任务不在运行时不再轮询该接口
   - 每行右侧是按钮（**并排一行**，整行高度与左边的「文件名 + 进度条」两行同高）：**⏸ 暂停** / **▶ 开始** / **✕ 删除**
     同一时刻最多两个：可暂停的行是 `⏸ ✕`，暂停/失败的行是 `▶ ✕`，已完成的行没有按钮
   - 暂停：下载中的文件会同时把 Aria2 任务 `forcePause`（进度保留、不会被轮询再 unpause 回来）；还没开始下载的只改状态，不碰 Aria2
   - 开始：`已暂停` 回到待下载/待转存；`失败` 也会重新入队并清掉失败记录
   - 删除：移除 Aria2 任务、尽力删掉云端转存副本、从任务队列与勾选里摘掉，之后可在「目录」里重新勾选；正在转存的文件会等这次转存结束再删（避免留云端孤儿副本）
   - 已完成（`已完成` / 清理中）的行没有 ⏸/▶，只剩 **✕ 删除**：删掉的是「任务里的这条记录」，本地已下载的文件留在磁盘上；
     删完这个文件在「目录」里就能重新勾选（若磁盘上的文件还在，会立刻按「本地已存在」判定完成；想真正重下就先删掉本地文件）
   - 运行中的按钮点了约 1 秒内生效（子进程每个 tick 读一次控制文件）
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
- **「⚠ 0 字节 N 分钟」的红标 = 这个文件提交成功了但一个字节都没下来**：pipeline 每 tick 记录
  aria2 的 completedLength，连续 0 字节满 5 分钟（ZERO_STALL_SEC = 300）就在该行标出来，
  任务日志同时打一条「⚠ … 已连续 N 秒 0 字节」（每个文件只打一次）。常见原因：直链被 CDN 412/403 拒、
  远端 aria2 的落盘目录不可写（errorCode 16/18）、任务刚被暂停。任务停止后标记自动消失，
  因为状态文件是死的，不该一直挂红标。阈值只影响显示，不会自动暂停或重试。
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

## 变更记录

### 2.8.10

「目录」树的缩进统一成固定台阶（2026-11-18）

**起因**：用户反馈「目录一行和下面的行，缩进不够，下面的行应该再往右边一点点，整体树形结构缩进应该一致」。

**问题**：看着像对齐，实际每一层都差一点 ——

| 位置 | 改前 | 原因 |
| --- | --- | --- |
| 「目录」标题栏勾选框 | 53.8px | 标题栏 padding .7rem + 光标 .9rem + gap .45rem |
| 第 1 层勾选框 | 57px（只比标题栏右 3.2px） | 行 padding .3rem + 光标 1rem + gap .35rem，几乎没有层级感 |
| 光标列宽 | 标题栏 .9rem / 行 1rem | 两个类各自写死 |
| 勾选框 margin | 行有 UA 默认 ~4px，标题栏被 `margin: 0` 单独覆盖 | 少一处覆盖就错开 |

**改动**（只改 `src/web/static/index.html` 的 CSS）：

- `.job-tree` 上定义四个变量：`--tree-caret: 1rem`、`--tree-gap: .35rem`、`--tree-step: 1.15rem`、`--tree-base: 1.5rem`。
- `.tree-row` 的 `padding-left` 改成 `calc(var(--tree-base) + var(--depth) * var(--tree-step))`。
- `.tree-caret` 宽度、`.tree-row` / `.job-tree-head` 的 `gap`、`.tree-check` 的 `margin` 全部改用这组变量（`.tree-check` 统一 `margin: 0`，消掉 UA 默认外边距）。

**结果**（本机实测，窗口 756px）：

| 层级 | 勾选框左边缘 | 与上一层差 |
| --- | --- | --- |
| 标题栏「目录」 | 53.8px | — |
| 第 1 层 | 72.2px | +18.4px |
| 第 2 层 | 90.6px | +18.4px |
| 第 3 层 | 109.0px | +18.4px |

同一层的目录行与文件行光标列完全重合（偏差 0）；「目录」标题栏到第 1 层也是一个完整台阶，不再出现「第 1 层看起来和目录同级」。

**验证**：`node --check`（抽出 `<script>` 块）通过；在本机页面用 `treeRowEl()` 造 4 层假行量矩形得到上表；`pytest` 仍 **251 passed**（无后端与 JS 逻辑改动）。

### 2.8.9

「文件进度」里每一行都能删，包括已经下载完成的（2026-11-18）

**起因**：用户提出「所有进度都可以点击删除，比如已经完成的任务，只要不在进度中的，都可以选择并下载」——
2.5.3 起的规则是已完成 / 清理中的行不给按钮，于是某个文件下完之后，这一行就再也无法从任务里摘掉，
也就没法回「目录」把它重新勾一次（比如想换个下载目录重下，或者当初只是试下一集）。

**改动**：

- 后端（`src/server/jobs_manager.py` `job_files()`）：`can_delete` 恒为 `true`，不再按状态过滤。
  删除动作本身与状态无关——写 `file_override.yaml` 的 `deleted` + `deleted_map`、把 fid 从
  `jobs.yaml` / `selection.json` 摘掉，运行中再交给子进程 `_drop_file()` 处理后置（该函数本来就在
  `state == "done"` 时跳过删云端副本，避免删掉已经落盘对应的网盘文件）。
- 前端（`src/web/static/index.html`）：done / cleanup 行只留 **✕**；按钮 title 与操作提示区分语义——
  已完成的提示「已删除记录：xxx（本地文件保留在磁盘上，可在「目录」里重新勾选）」，
  其余状态仍是「已删除：xxx（可在「目录」里重新勾选）」。
- 结果：删掉记录后 fid 不在 selection 里、`share_usage()` 也不再把它算成占用，于是「目录」里这个文件
  重新可选；本地文件还在的话，重新勾选后会立刻被「本地已存在」判定为完成（想真重下要先删本地文件）。

**验证**：`pytest` **251 passed**（`test_can_flags_per_state` 期望改为 done/cleanup 也可删，新增
`test_delete_done_file_frees_it_for_reselect` 覆盖「删 done → job_files 空 / selection 空 / deleted 记录 /
share_usage 空 → 可再 add」）。本机真浏览器用假任务（4 行 done/cleanup/downloading/failed）核对：done/cleanup 行确实只剩 ✕，
点 ✕ 后行消失、提示文案正确、`counts` 同步减少，刷新页面后删掉的记录不再出现，
`file_override.yaml` 写入 `deleted` 与 `deleted_map`。

### 2.8.8

右侧面板改叫「目录」，标题栏可以一勾下载整棵目录（2026-11-18）

**起因**：用户删掉某个文件（199 4K.mp4）后想重新勾上再下，觉得「下载所选」点不动——一是只能点那个很小的复选框，
二是想把整个分享目录重新下一个遍时没有「全选」入口。

**改动**（只动前端 `src/web/static/index.html`）：

- 面板标题「分享目录」→「目录」，默认提示「勾选「目录」= 下载整个目录」。
- 标题栏新增 **整个目录** 勾选框（`data-role="root-check"`）：勾上 = 把分享根目录作为 `dir_fid` 提交，
  即下载这个分享里的全部文件；此时下面每一行都会变成「已勾选且置灰」（继承自根目录），再点一次取消。
  根目录已被本任务勾选时，取消 = 调 `selection/remove` 把根目录从任务勾选里摘掉；被别的任务占用时勾选框同样置灰并给出归属。
- 文件行点名字 / 尺寸也能勾选或取消（复选框本身照旧），小方框不再是非点不可的入口。
- 点标题栏的勾选框不会误触发折叠；`treeSyncHeader()` 会按「本任务勾选 / 已被占用」同步这个勾选框的状态与禁用。

**验证**：本机语法检查 `node --check` 通过；NAS 上 2.8.8 真机（任务「凡人修仙」）——标题显示「目录」、
标题栏勾选框存在；勾上后所有行 `checked=true / disabled=true`、按钮可用、摘要「本次已勾选 0 文件 / 1 文件夹」；
取消勾选后全部恢复可点；点文件名字可选中（按钮由禁用变可用）。`pytest` 仍 **250 passed**（无后端改动）。
### 2.8.7

设置页：非远端时不再显示「下载目录（对方机器上的绝对路径）」这句标签（2026-11-18）

**起因**：勾掉「远端 aria2」后，页面上仍留着一行「下载目录（对方机器上的绝对路径；留空 = 用对方的默认目录）」，
只是下面的输入框不见了——像是要你填、又没地方填。

**根因**：`src/web/static/settings.html:59` 的 `label { display: block; }` 是作者样式，
优先级高于浏览器对 `hidden` 属性的默认 `display: none`。所以 `label.hidden = true` 对 `<label>` 完全无效
（`<input>` 没有声明 `display`，隐藏正常）。之前验收只数了「可见的 input 数量」，漏掉了 label 本身。

- 加 `.aria2-card label[hidden] { display: none; }`，注释写明为什么要压过 `label { display: block }`。
- 同类问题首页早有先例：`.file-stall[hidden] { display: none; }`（`index.html:354`）；首页没有 `label {}` 规则，不必改。
- 验证（本机 8787）：本机配置的卡片里那句远端标签 `hidden=true / display=none / 高度 0`，两个本机标签正常显示；
  勾上「远端」后反向成立（远端标签显示、两个本机标签 `display:none`）。无后端改动，`pytest` 仍 250 passed。
### 2.8.6

设置页的下载目录输入框收成两套（2026-11-18）

**起因**：勾上「远端 aria2」后页面上仍看得到三个目录输入框（远端下载目录 + 临时下载目录 + 下载目录），
而实际语义只有两套——远端只填一个「下载目录」，本机填「临时下载目录」+「下载目录」。之前只是把两个本机输入框
置灰（`disabled`）而没有隐藏，很容易被当成「三个都要填」。

- `src/web/static/settings.html`：目录字段统一由 `addDirField()` 创建（label 与 input 一起记进 `dirFields`），
  切换远端时整组 `hidden`：远端只显示「下载目录（对方机器上的绝对路径；留空 = 用对方的默认目录）」，
  本机显示「临时下载目录（可选）」+「下载目录（完成后移入；留空 = 留在临时目录根下）」。
  同时删掉只服务于置灰的两条 `.aria2-card.remote input[...]:disabled { opacity:.45 }` 样式。
- 藏起来的那组值仍随保存提交（切回原模式还在，不会因为被隐藏而被清空）；后端与配置文件格式都没变。
- 验证：本机 dev server 实测——本机模式 2 个可见目录框，勾「远端」后只剩 1 个（`visibleDirInputs = 1`），
  取消勾选回到 2 个；标签与取值均正确。无后端改动，`pytest` 仍 **250 passed**。
### 2.8.5

文件进度给「下载 5 分钟仍 0 字节」的行打标记（2026-11-18）

**起因**：NAS 上两个 4K 文件提交下载后一直 0%，日志只有 `速度: 0 KB/s`，页面上看不出是「在排队」
还是「直链被 CDN 拒了」——412 Precondition Failed 是 Aria2 拿到的响应，本应用只在日志里间接看得到。
现在这类「提交成功但一个字节都没下来」的文件会在自己那一行显示 `⚠ 0 字节 N 分钟`。

- 子进程侧（`pipeline.py`）：`track_zero_bytes(task, completed, total, now)` 维护 `zero_since`——
  aria2 `completedLength` 连续为 0 时记下起点，一旦有字节就清掉并重新计时；起点写进任务快照。
  超过 `ZERO_BYTES_STALL_SEC = 300` 且尚未告警过时，日志打一条 `⚠ <文件> 已连续 N 秒 0 字节：
  直链可能被 CDN 拒绝（412/403）或落盘目录不可写`（每文件一次，恢复后再次停摆会重新告警）。
- 状态落盘（`scheduler.persist_payload`）：每个文件多一个 `zero_since`，不引入额外 RPC。
- Web 侧（`jobs_manager.job_files`）：只有「任务在跑 + 该文件 downloading + downloaded==0 + total>0」
  才算停摆，按 `time.time() - zero_since` 得到 `stalled_sec`，`stalled = stalled_sec >= ZERO_STALL_SEC`。
  任务停止后状态文件是死的，不再显示红标；用户暂停的行按 `paused` 展示，也不标。
- 前端（`src/web/static/index.html`）：`.file-row-head` 里加了 `.file-stall` 徽标（默认 `hidden`），
  只渲染后端算好的 `stalled/stalled_sec`，不参与状态机；hover 提示指向 412/403/23018 与落盘权限。
- 测试：新增 `tests/pytest/test_stall_marker.py`（11 条：起点累加/有字节清零/未知大小不计/时钟回拨、
  payload 透传 `zero_since`、满 5 分钟才 marked、不到阈值、已有字节、任务未运行、无 total、
  暂停行不标）→ 全仓 **250 passed**。
- 阈值只影响显示，不会自动暂停/重试；要改只改 `ZERO_STALL_SEC`（Web）与 `ZERO_BYTES_STALL_SEC`（子进程）。
### 2.8.4

目录语义收口：`<job_id>` 只当暂存、两个都不填就不提交 dir（2026-11-18）

**起因**：2.8.3 用法上还有三处含糊，用户直接点了出来 ——
「`<临时>/<job_id>` 应用不用 job_id 吧」「临时目录和完成目录都没有填，是不是就不提交，不用管是否是本机」
「是否是远端只涉及到是否去最终落点校验文件，对吗」。三条都对，改成下面这样：

- `job_id` 只做「下载中的暂存区」：`<临时>/<job_id>/<相对路径>`，下完就挪走，最终落点（完成目录，或临时目录根下）
  里看不到 job_id。保留这层隔离的理由：同一分享加成两个任务时同名文件会互相覆盖、aria2 自动改名成 `.1`，
  而按 `download_root` 递归清理空目录也可能误删共享临时目录里用户自己的空文件夹。
- 两个目录都不填：`plan_download_dirs()` 返回 `mode="none"`，提交任务时**不带 `dir`**（用 Aria2 自己的默认目录），
  也不再读 `getGlobalOption`；本机不扫描、不移动、不校验，完成与否只看 Aria2 状态。
- 远端与「本机不知道落点」合成一个判断 `FileApi.local_verifiable = bool(aria2_download_dir) and not remote_download`：
  搬文件、扫本地副本、按本地副本决定「能不能删云端副本」、按本地副本出校验报告，都用它短路。
  它取代了原来分散在 `file_api` / `pipeline` / `verify_download` 里的 `remote_download` 判断。
- 代码：`aria2_api.plan_download_dirs(download_dir, destination_dir, *, remote=False)` 去掉 client 参数（不再可能连 RPC），
  `resolve_download_dir()` 删除；`Aria2Client.download_dir` 在根目录为空时返回空串（不再拼出裸 `job_id` 相对路径）；
  `aria2_add_url` 无目录时只给 `out=<相对路径>`（保留子目录防撞名）；`quark_main.init_clients()` 不再建探针客户端；
  `probe_aria2_profile()` 只探 `getVersion`（目录不再参与预检）。
- 设置页：说明改成「两个目录都可不填」，两个标签标注「可选」，完成后目录写明「留空 = 留在临时目录根下」。
- 测试：新增 `tests/pytest/test_local_verifiable.py`（无目录时各本地判定短路、完成判定返回 True、清理不再被本地副本拦住、
  只填临时目录时文件从 `<临时>/<job_id>/` 挪到 `<临时>/`）、`test_aria2_remote.py` 加「无目录时 addUri 不带 dir 且保留子目录」、
  重写 plan 用例为无 client 签名（含「plan 不该连 Aria2」「预检不该读默认目录」）→ 全仓 **239 passed**。

### 2.8.3

两个目录都可选：临时目录 / 完成目录按组合决定下到哪（2026-11-18）

**起因**：2.8.2 只做了「临时目录留空 → 用 aria2 默认目录」，但用户真正要的语义是三条：
① 有临时目录 → 下到临时目录，完成目录非空则下完移过去；② 没有临时目录、有完成目录 → 直接下到完成目录；
③ 两个都没有 → 提交任务时不用管目录。远端保持原样（不提交目录、不校验文件、只看 aria2 状态）。

- `src/server/aria2_api.py` 新增 `plan_download_dirs(client, download_dir, destination_dir, *, remote=False)`，
  返回 `{download_dir, destination_dir, mode}`，mode ∈ `temp` / `dest` / `default` / `remote`
- `src/server/quark_main.py`：`DOWNLOAD_DESTINATION_DIR` 不再默认等于临时目录（原来留空会变成「移到临时目录本身」）；
  `init_clients()` 先用不带 `job_id` 的探针客户端把目录定下来，再建真正带 `job_id` 的客户端 ——
  2.8.2 是先建客户端再解析目录，`Aria2Client` 在构造时就固化了 `download_dir`，留空时会把 `dir` 提交成相对路径
- `src/server/jobs_manager.py`：`probe_aria2_profile()` 改用 `plan_download_dirs`，只填完成目录时不再去读默认目录
- 设置页：面板说明与两个字段标签改成「可选」，「下载临时目录」不再声称留空等于默认目录
- 7 个新用例（四种组合 + 远端 + 「只填完成目录时不该读默认目录」）→ 239 passed

### 2.8.2

下载目录不再区分远端/本机：本机配置也可以留空，改用 Aria2 自己的默认目录（2026-11-18）

- `aria2_profiles.validate_profiles()` 去掉「非远端必须填 `aria2_download_dir`」的校验，只剩 RPC Host 必填
- `src/server/aria2_api.py` 新增 `resolve_download_dir(client, download_dir, *, remote=False)`：填了就用填的，
  留空则问 aria2 的 `dir`；读 RPC 失败或默认目录为空都直接抛错 —— 绝不退化成相对路径，否则文件会落到进程当前目录
- 子进程 `quark_main.init_clients()` 用该函数解析真正的下载根目录，日志打「未配置下载临时目录，改用 Aria2 默认目录: …」
- `probe_aria2_profile()` 在启动任务前把同一件事验一遍，失败信息带配置名（页面点「开始」直接能看到原因）
- 设置页标签改为「下载临时目录（留空 = 用 Aria2 默认目录）」，面板说明与远端勾选文案同步更新
- 5 个新用例（已填 / 远端 / 留空取默认 / 默认也没有 / 读 RPC 失败）+ 2 个预检用例 → 233 passed
- NAS 真机：`飞牛本机`（本机 aria2）留空时解析到 `/vol2/@team/家庭共享_薛成韵/下载/aria2`；`灰机nas`（远端）返回空。
  顺带发现该配置的 `aria2_secret` 是空的（预检一直「未授权」），已按 `aria2.conf` 的 `rpc-secret` 写回

### 2.8.1

远端 Aria2 可指定下载目录 + aria2 错误码翻成人话 + 设置页自动测试（2026-11-18）

**起因**：NAS 上任务 j67bc357015（「凡人修仙」，3 个 4K.mp4）转存成功、下载也提交成功，
但页面上每个文件一直 `0.0% 速度: 0 KB/s`，30 秒后 `✗ 下载失败，准备重试 … Download aborted. | 重试 1/3`，
三轮后判失败。查远端 aria2（`https://dl.660888.xyz:6800/jsonrpc`）：
提交的 3 个任务状态 `active` 但 `completedLength=0/totalLength=0`，`tellStopped` 里全是
`error | code 16 | Download aborted. | len 0/2324135706 | dir /downloads/198 4K.mp4`。
逐目录实测 addUri：`/downloads`、`/mnt`、`/media`、`/home`、`/root`、`/` 全部 error 16，
`/data`、`/download` 是 error 18，只有 `/var/tmp`、`/tmp/quark`、`/config` 能 `complete`。
**结论：对方的默认下载目录 `/downloads` 不可写**（只读挂载或无权限），与本机直链/Cookie/UA 无关。

远端下载目录
- `aria2.yaml` 每套配置新增可选键 `aria2_remote_dir`（默认空 = 用对方默认目录），远端模式下提交时
  `dir = <远端目录>/<分享内相对目录>`；留空则和以前一样只带 `out`。远端路径不过本机 `allowed_roots` 校验
- 设置页远端卡片多一个输入框「远端下载目录（留空 = 用对方的默认目录）」，勾上「远端」才显示
- `POST /api/aria2/test` 返回值新增 `dir`（实际会用的目录）与 `server_dir`（对方 aria2 的 `getGlobalOption.dir`），
  成功提示会显示「…，下载目录 /data/downloads」

把 aria2 的错误码讲清楚
- `aria2_api.describe_task_error()`：errorCode 9/13/16/17/18/19/22/24 映射成人话，附 `errorCode` 原文与
  `files[0].path`；未知码原样回 `errorMessage`
- 16/18（写不了目录）与「磁盘空间不足」这类**不可重试**错误：`pipeline._poll_one_download` 里直接判失败
  （`keep_cloud=True` 保留云端副本），不再白跑 3 轮重试；`error_classify` 同步补规则
- 页面上失败原因因此从 `Download aborted.` 变成
  「aria2 无法在下载目录创建/写入文件（目录只读、不存在，或文件被占用）（aria2 errorCode=16: Download aborted.）；目标路径: /downloads/198 4K.mp4」

设置页自动测试
- 改完 host / 令牌 / 远端目录、勾选远端，800ms 防抖后自动测一次连通性（`testAria2Card`），
  结果写在卡片下方；保存成功后自动把所有卡片测一遍，`#cfg-msg` 汇总「N 套全部连接正常」或「X / N 套连不上」
- 定时器用 `WeakMap` 挂卡片，卡片被删/重绘不会往孤儿节点写状态

验证
- `tests/pytest/test_aria2_task_error.py` 10 个新用例（16/18 文案与不可重试、6 仍可重试、远端 `dir` 拼接、
  空远端目录不带 `dir`、尾部斜杠不产生 `//`、`aria2_remote_dir` 往返与掩码 secret 保留）
- 全仓 **214 passed**；`compileall -q src` 通过；两个页面 `node --check` 通过

真机验证（NAS 任务 j67bc357015「凡人修仙」）
- 部署后子进程日志出现 `远端下载目录: /var/tmp/quark`，三个文件重新提交成功；
  远端 RPC `tellActive` 显示 `45471d6ff3db6558 len 2099249152 / 2324135706 speed 23299891 path /var/tmp/quark/198 4K.mp4`，
  页面 `198 4K.mp4` 显示 90.3%、约 28 MB/s（此前固定 0%）
- 页面进度来自 5 秒一次的 state 落盘，轮询太密会看到 0.0（不代表没在下）

验证时顺带修掉的两个链路缺陷
- **子进程没把远端目录传下去**：`quark_main.py` 构造 `Aria2Client` 时只传了 `remote`，`remote_dir` 丢在配置里，
  所以界面/接口看着都对、实际提交的仍是对方默认 `/downloads`。现在传 `remote_dir=ARIA2_REMOTE_DIR`，
  并在启动日志里打一行 `远端下载目录: …`（空则「(用对方默认目录)」），这类「配置没接线到子进程」只能靠日志自证
- **`POST /api/aria2/test` 只带 name 时丢掉已保存的远端设置**：现在 `remote/remote_dir` 省略（null）就沿用同名配置，
  换了 host 则不继承（避免用旧令牌/旧目录测陌生地址）
- **删掉的文件重新勾选不生效**：`file_override.yaml` 的 `deleted` 按路径持久记，而 `add_job_selection` 只把 fid 加回勾选，
  子进程又把 `deleted` 只增不减地累积，于是「删掉 → 从目录树勾回来」永远被过滤（页面表现为勾了没反应、任务秒完成）。
  现在删除时额外记 `deleted_map`（fid → 路径），重新勾选/点开始时按 fid 撤标记；子进程改为「以文件里的 deleted 为准」而不是累积
- 新增 4 条用例（只给 name 沿用远端目录、换 host 不继承、`aria2_target()` 暴露 remote/remote_dir、删除后重勾撤标记）
- **真机复验又挖出两处「删了勾不回来」**：① `share_usage()` 把还在 `pipeline_state` 里的已删文件当成「已在任务中」，
  重新勾选被 `in_job_done` 挡下（现按该任务的 `deleted` 集合跳过）；② 只撤「新增项」的标记，文件还留在 selection 里
  走的是 `already_in_job` 分支，且任务刚重启时 `pipeline_state` 为空、`deleted_map` 建不出来（现对**本次请求的全部 fid**
  撤标记，delete 时按「一路径一 fid」配对兜底，`forget_deleted_paths()` 还会回退查 `pipeline_state`）
- 合计新增 8 条用例 → 全仓 **226 passed**；NAS 真机复验：delete 写入 `deleted_map`，`selection/add` 后 `deleted` 与 `deleted_map` 都清空

### 2.8.0

Aria2 地址填错的诊断与预检 + 一轮代码 review 的修复（2026-11-18）

**起因**：NAS 上新增任务（分享里 3 个 4K.mp4，共 6796 MB）转存全部成功，但每个文件提交下载都报
`501 Server Error: Not Implemented for url: https://dl.660888.xyz/`，进重试旁路 1/10，任务卡住。
在 NAS 上实测：该地址 443 端口是 **AriaNg 网页控制台**（GET 200、POST 一律 `501`，`darkhttpd`），
真正的 RPC 在 `https://dl.660888.xyz:6800/jsonrpc`（无令牌回 JSON-RPC `Unauthorized`，带配置里的
secret 返回 `version=1.36.0`）。也就是说配置里填的是网页地址，不是 RPC 地址。

Aria2 诊断
- `aria2_api.normalize_rpc_url()`：host 末尾斜杠/空白归一，路径为空或 `/` 时自动补 `/jsonrpc`
- `_aria2_call()` 重写：先看是不是 JSON-RPC 再判状态码；网页/网关返回 HTML 时抛
  `Aria2ConfigError「Aria2 RPC 地址不对…RPC 地址形如 http://主机:6800/jsonrpc；AriaNg 网页地址不能当 RPC 用」`；
  `error.code==1`/`Unauthorized` 抛「令牌（rpc-secret）不对」；连接失败、HTTP 5xx 带 host 与响应片段
- `error_classify` 把「RPC 地址不对 / 不是 JSON-RPC / RPC 未授权 / RPC 令牌」列为永久错误（重试无意义）
- `jobs_manager.probe_aria2_profile()` + `start_job` **启动前预检** `aria2.getVersion`（超时 5s）：
  连不上直接 `ValueError("Aria2 配置「x」连不上: …")` → 页面 400 显示原因，不再起子进程
- `pipeline` 对同一 Aria2 错误 60 秒只打一条（附「过去 60 秒同错误 N 次，已抑制」），
  不再三个 Worker 每 tick 刷屏

数据安全 / 一致性（review 挖出来的）
- **清理卡死**：清理一直失败（本地无可信副本、删后回查仍可见）会让 `cleanup` 闸门永久挡住转存、任务永不结束。
  现在按路径记首次失败时间，超过 `CLEANUP_GIVE_UP_SEC`（600s）标失败并**保留云端副本**，让后续文件继续转存
- **转存 fid 认领**：目标目录已有同名文件时，原先只按 basename 认领，会把**用户原有文件**当成自己的转存结果
  在清理阶段删掉；改为与 `save_files_to_my_disk()` 返回的 `saved_fids` 取交集，认不上就按「未生效」处理
- **远端下载**：`complete` 时比对 aria2 的 `totalLength` 与任务登记大小，不一致保留云端副本并判失败
  （远端模式本来就没有本地副本，这是唯一防线）
- **提交竞态**：`_try_submit_job` 提交（含取直链 + addUri 两次 HTTP）回来后重新取当前 job，
  状态已不是 `ready/retry`（用户刚暂停/删除）就撤销刚提交的 Aria2 任务，不覆盖用户操作
- `_drop_file` 对 `cleanup` 态也删云端副本（原先两处都跳过，只能等下次启动补删）
- `resume_file`：失败前云端副本已被清掉的，重新开始时退回 `pending` 重新转存（原先带着过期 fid 回 `ready`，取直链必失败）
- `_entries_by_name(strict=True)`：列举/回查目录失败不再当「空目录」，会记失败，避免虚增可用容量
- `scheduler.cloud_fid_for()` 失败不写缓存（一次网络抖动不该判成「没转存」→ 重复转存占满配额）
- `list_share_files` 补 `_is_ok` 校验（原先分页失败会静默返回半截文件列表）；`save_as` 为 null 不再 AttributeError
- 下载超时只算 `active` 时间（队列深或暂停超 2 小时不再把没开始的判超时）

安全
- `delete_job` 与 `start_job` 共用同一把启动锁（原先删任务目录与启动新进程可交错 → 双进程/孤儿进程）
- `POST /api/aria2/test` 不允许「用已保存的令牌测试请求体里给的陌生地址」（等于绕过配置接口的掩码 + SSRF）：
  换 host 必须重新填令牌，且 host 必须以 `http://`/`https://` 开头
- Basic 认证的 `hmac.compare_digest` 改 bytes 比较（非 ASCII 账号/口令不再全站 500）
- 写入路径白名单改用 realpath（软链不能绕出 `allowed_roots`）
- `save_section` 加锁；secret 字段**空串现在是显式清空**（原先把空串当「未修改」，永远清不掉）

前端
- 日志面板：请求回来校验任务未切换；失败时回退「未缓存」标记，能自动重试（原先失败一次就永不重试）
- 「文件进度」分母改用后端 `file_total`（只加载/渲染前 2000/400 条时不再显示「完成 900/2000」假象）
- 任务被删除后清理 `jobCache`，不再占着详情面板
- 目录树：「下载所选」提交中禁用按钮；切任务后丢弃晚到的分页结果；展开失败退回折叠态（可以重试）
- 逐文件暂停/开始/删除串行发送（后端 `file_override.yaml` 是读-改-写，并发会丢删除指令）
- 任务级 开始/暂停/删除 连点保护
- 设置页扫码轮询用世代令牌 + 连续失败退避（后端重启不再直接放弃），连点只认最后一次会话
- 排队阈值输入框做 `Number.isFinite` + 范围夹紧，后端 `quark_main._as_int()` 兜底
  （`1e999`→JSON `null` 会让子进程启动即 `TypeError`，任务卡在 running 却没有进程）

验证
- 全仓 **204 passed**（新增 `test_verify_download.py` 6、`test_aria2_test_api.py` 4、`test_pipeline_cleanup.py` 2，
  `test_aria2_rpc.py` 9 条覆盖 URL 归一化/HTML 501/令牌错/预检拦截，远端 `out` 断言按新语义改写）
- `compileall -q src` 通过；两个页面的内联脚本 `node --check` 通过

### 2.7.0

远端 Aria2：共用本应用，文件落在对方机器上（2026-11-17）

- **起因**：朋友有公网 IP 和自己的 Aria2，想共用这个应用下载，问「能不能设置远端 aria2」。
- **配置**：每套 Aria2 配置新增可选 `aria2_remote`（设置页卡片上有「远端 aria2（只提交下载请求，不管理落盘目录）」勾选框）。
  勾上之后两个目录置灰、占位文案变「远端，不用填」，保存只校验 `aria2_host`；`options_for()` 把 `aria2_remote` 一起带进子进程。
- **提交**：`Aria2Client(remote=True)` 时 `aria2.addUri` 的 `options` 只有 `{"out": 文件名}`（不再带 `dir`），
  `header` 里的 Cookie/UA 不变；任务匹配键从「归一化相对路径」换成 `basename`（远端返回的是对方路径），
  `aria2_find_task_by_path` / `aria2_remove_by_paths` / `aria2_pause_by_paths` 都走 `_match_key()`。
- **本机职责收缩**：`FileApi(remote_download=True)` 下 `move_file_to_destination` 直接返回 True、
  `finish_downloaded_file` 只打一行「远端 Aria2 下载完成（文件在远端，未做本地移动）」、
  `local_copy_is_intact` 恒 False、`get_local_downloaded_sizes_from_dirs` 返回空字典、`cleanup_empty_download_dirs` 返回空表；
  清理云端副本前的「本地副本完好」守卫在远端模式下跳过（本来就不是本机下的）。
- **完成判定**：新增 `verify_download.verify_remote_downloads()`，按 `job.state in ("done","cleanup")` 算成功，
  报告字段与本地版一致但 `destination_dir` / `local_path` 写「(远端 aria2)」、`actual_size` / `local_exists` 为 `None`；
  `pipeline.verify_completed()` 按 `file_api.remote_download` 二选一。
- **启动**：`quark_main.py` 远端模式跳过 `_require("aria2_download_dir")`、`DOWNLOAD_DESTINATION_DIR` 置空，
  日志写「下载目录: 远端 aria2 自己决定（本机不管理落盘、不移动、不校验）」，
  收尾日志写「所有文件下载成功（远端 Aria2 口径，未做本地校验）🎉」。
- **验证**：新增 `tests/pytest/test_aria2_remote.py` 7 个 + `test_aria2_profiles.py` 3 个（远端不要求目录、本机仍要求、标记经保存后回读），
  全仓 **183 passed**；本机浏览器实测勾选后目录置灰/占位变化、只填 host+secret 保存成功、刷新回显勾选态、
  任务下拉显示「朋友的（远端）」，删掉该测试配置后 `conf/aria2.yaml` 回到两条。
### 2.6.1

「完成后目录」留空不再把文件挪到进程当前目录（2026-11-17）

- **起因**：问「下载临时目录不填会怎样」时顺手检查了另一个目录——`download_destination_dir`。
  下载临时目录留空是硬的：保存时 `validate_profiles` 直接 400「Aria2 配置「x」缺少下载临时目录」，
  绕过去改文件也会在子进程启动时 `_require()` 抛 `配置项 aria2_download_dir 不能为空`，任务起不来。
  但「完成后目录」留空能保存，运行时 `os.path.join("", relative_path)` 得到**相对路径**，
  `shutil.move` 会把下完的文件挪到**进程当前目录**（部署后就是项目根目录）下，还会连带生成同名子目录。
- **改成**：留空 = **不移动**。`FileApi.local_copy_root` 统一回答「本地副本在哪」——配了完成后目录就是它，
  否则是 `<aria2_download_dir>/<job_id>`；`move_file_to_destination` / `finish_downloaded_file` 直接判文件在不在原地，
  `local_copy_is_intact`、`get_local_downloaded_sizes_from_dirs`、清理前的「本地没有可信副本就不删云端」判定都跟着用这个根。
  副作用（有意的）：留空时清理网盘副本不再跳过安全校验——以前是 `if self.download_destination_dir and not intact`，
  等于没配目标目录就无条件删云端；现在必须本地真有大小相符的副本才删。
- **验证**：新增 4 个用例（原地不动且进程目录里不出现文件 / `finish_downloaded_file` 算成功 / `local_copy_is_intact` 按下载目录判 / `get_local_downloaded_sizes_from_dirs` 能数到下载目录里的文件），
  全仓 **173 passed**；设置页文案补「下载临时目录必填；完成后目录留空 = 下载完不移动」，示例配置同步。

### 2.6.0

多套 Aria2 配置 + 新增任务时选择（2026-11-17）

- **需求**：原来只有一套 Aria2（`conf/aria2.yaml` 顶层四个键），想同时喂两台 aria2（比如本机 + NAS）就得换配置文件重启。
  现在设置页「Aria2」面板可以点「＋ 添加」维护多套，新增任务时选一套。
- **配置形态**：`aria2_profiles: [{name, aria2_host, aria2_secret, aria2_download_dir, download_destination_dir}]` + `aria2_default`；
  每套只有这四个键，其余（`poll_interval` / `download_timeout` / `request_timeout` / `aria2_max_retries` / `download_log_interval` /
  `download_submit_max_retries` / `strict_download_order` / `aria2_queue_pause_threshold` / `aria2_queue_resume_threshold`）是全局项。
  老的单配置写法自动迁移成一条「默认」；在设置页保存一次后写盘为 `aria2_profiles` 并清掉顶层四键（避免两套配置并存）。
- **任务级**：`jobs.yaml` 的 job 增加 `aria2_profile`；`start_job` 用环境变量 `QUARK_AUTO_DL_ARIA2_PROFILE` 把配置名传给子进程，
  子进程用 `aria2_profiles.resolve_name()` + `options_for()` 解析出一组值；Web 侧的 `_aria2_client_from_config()`（暂停 / 清理）
  也按任务上的配置名取 host / secret，不再固定读顶层键。
- **掩码与校验**：`GET /api/config` 对每套配置的 secret 打码（前 4 + 8 个 `*` + 后 4，标 `aria2_secret__masked`）；
  保存时按**配置名**沿用旧 secret（`merge_incoming`），改名等于新建要重填；保存前 `validate_profiles` 校验 host 与临时目录必填，
  `_validate_write_paths` 逐条校验两个目录是否在 `allowed_roots` 内（报错文案形如 `配置项 aria2_profiles[NAS].aria2_download_dir 不在允许的目录内`）。
- **连通性测试**：`POST /api/aria2/test` 用卡片上的值调 `aria2.getVersion` + `aria2.getGlobalStat`
  （host 为空、secret 还是掩码时用已保存的同名配置补全），页面显示「连接成功：aria2 1.36.0，活动 0 / 等待 0」。
- **前端**：设置页 Aria2 面板改成「配置卡片列表 + 右上角 ＋」，每张卡片是「配置名 / 默认 radio / 测试 / 删除」一行 + 四个字段，
  删除只改内存并提示「点『保存配置』后生效」；首页「新增任务」表单多一个「Aria2 配置」下拉（打开表单时拉 `/api/aria2/profiles`，
  没有配置时显示「未配置（去设置页添加）」并禁用），任务详情显示 `aria2=<配置名>`。
- **验证**：新增 `tests/pytest/test_aria2_profiles.py` 20 个用例（迁移 / 去重 / 默认回退 / 掩码合并 / 白名单 / 四个接口），全仓 **169 passed**；
  本机浏览器实测「＋ 添加 → 填 NAS → 测试（真连 `192.168.1.100:6800` 成功）→ 保存」后 `conf/aria2.yaml` 只剩 `aria2_profiles`，
  刷新页面 secret 显示 `********`、再点「测试」仍成功（用已保存的 secret 补全）；切默认、删配置（默认自动落到剩下那条）、
  建任务带 `aria2_profile: NAS` 都符合预期。

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