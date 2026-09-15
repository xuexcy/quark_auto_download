代码及文档均由 AI 生成(本行除外)
# Quark Auto Download

夸克网盘 + OpenList + Aria2 自动批量下载脚本。

在有限网盘空间内，通过「转存 → 下载 → 删除转存」循环，批量拉取分享链接中的文件。

## 功能特性

- **调度器 + 多 Worker**：网盘（转存/清理）、下载主队列、下载重试旁路并行
- **按容量批量转存**：每轮尽量填满可用空间，而非一次只转一个文件
- **路径偏好有序**：按完整相对路径优先；失败跳过，不堵塞后续任务
- **错误分类**：永久错误立即出局；临时错误进重试旁路
- **Aria2 排队水位**：`waiting` 过高时暂停转存与提交下载，清理不受影响
- **完成后校验**：对照任务列表检查 completed 目录，生成失败报告（含具体原因）
- **空目录清理**：删除转存文件后清理空父目录，结束时再全量扫一遍
- **0 字节跳过**：分享元数据 `size=0` 的文件直接跳过（避免 Aria2 HTTP 416）

## 目录结构

```
code/
├── bin/                 # 启停脚本（run / stop / restart）
├── src/                 # 业务代码
│   ├── quark_main.py    # 入口
│   ├── pipeline.py      # 流水线
│   ├── scheduler.py     # 调度状态机
│   ├── file_api.py      # 转存/下载/清理编排
│   ├── verify_download.py
│   └── *_api.py         # Quark / OpenList / Aria2
├── conf/                # 运行配置（勿提交密钥）
│   └── example/         # 配置样例
├── state/               # PID、流水线状态、校验报告
├── log/                 # 按日期的运行日志与报告副本
└── test/                # API 测试
```

## 依赖

- Python 3.7+
- 夸克网盘账号（Cookie）
- OpenList / Alist（挂载夸克）
- Aria2（JSON-RPC）

```bash
pip install -r requirements.txt
# 或: pip install requests pyyaml
```

## 配置

复制样例到 `conf/` 后按需修改：

```bash
cp conf/example/quark.example.yaml conf/quark.yaml
cp conf/example/openlist.example.yaml conf/openlist.yaml
cp conf/example/aria2.example.yaml conf/aria2.yaml
```

### quark.yaml

```yaml
quark_cookie: "kps=xxx; sign=xxx; vcode=xxx"
share_url: "https://pan.quark.cn/s/xxxxxxxxxxxx#/list/share"
share_pwd: ""
file_name_regex: ""          # 按完整路径过滤，留空不过滤
save_to_dir: "alist共享"
request_timeout: 10
task_poll_interval: 3
task_timeout: 180
```

`share_url` 填浏览器完整链接；带 `#/list/share/<fid>` 时只下该子目录。

### openlist.yaml

```yaml
openlist_host: "http://127.0.0.1:5255"
openlist_token: ""
openlist_quark_path: "/quark"   # 应对准夸克转存根目录
request_timeout: 10
```

### aria2.yaml（常用项）

```yaml
aria2_host: "http://127.0.0.1:6800/jsonrpc"
aria2_secret: ""
aria2_download_dir: "/downloads"
download_destination_dir: "/downloads_done"
poll_interval: 30
aria2_max_retries: 3
download_submit_max_retries: 10
strict_download_order: true

# Aria2 waiting 水位：>= 上限暂停转存/提交；<= 下限恢复；清理不停
aria2_queue_pause_threshold: 700
aria2_queue_resume_threshold: 500

# state_file: ""              # 默认 state/pipeline_state.yaml
# verify_report_file: ""      # 默认 state/download_verify_report.yaml
```

## 使用方法

### 启停（推荐）

在项目根目录执行（`sh` / `bash` / `./` 均可）：

```bash
./bin/run.sh       # 后台启动，写入 state/quark_main.pid，stdout→log/run.out
./bin/stop.sh      # 按 PID 停止
./bin/restart.sh   # 先 stop 再 run
```

### 前台运行

```bash
python src/quark_main.py
```

## 运行流程简述

1. 拉取分享文件列表（日志中小于 1MB 显示为 `K`）
2. 跳过 completed 中已存在的文件；接管 Aria2 已有任务
3. Worker 循环：清理优先 → 按容量转存 → 提交/轮询下载 → 删转存释放空间
4. Aria2 `waiting` 过高时暂停转存与新提交（清理继续）
5. 全部结束后：全量清理空文件夹 → 校验 completed → 写失败报告

## 日志与报告

| 路径 | 说明 |
|------|------|
| `log/<日期>/quark_main.<时间>.log` | 本次运行主日志 |
| `log/run.out` | `bin/run.sh` 后台标准输出 |
| `state/pipeline_state.yaml` | 流水线任务状态（明文 YAML） |
| `state/download_verify_report.yaml` | 最新校验报告 |
| `log/<日期>/download_verify_report.<时间>.yaml` | 本次报告副本 |

失败报告中的 `reason` / `detail` / `pipeline_note` 会带上具体原因，例如：

- `下载失败达最大重试(3/3): No URI available.`（直链对 Aria2 不可用）
- `跳过下载: 分享文件大小为 0 字节；…HTTP 416…`
- `status=416`（空文件 Range 请求失败）

说明：分享列表里的 `size` 偶发不准；本地已成功落盘时，不以元数据 size 偏差单独判失败。

## 测试

```bash
python test/test_aria2.py
python test/test_openlist.py
python test/test_quark.py
```

## 注意事项

- 保持夸克 Cookie、OpenList、Aria2 可用
- `openlist_quark_path` 需与 `save_to_dir` 指向同一转存根
- 分享中真正的空文件（0 B）无法可靠下载，脚本会跳过并记入报告
- `No URI available.` 表示 Aria2 侧直链失效/被拒，与「网页能转存」不矛盾（鉴权链路不同）

## 许可证

本项目仅供学习和个人使用，请遵守相关服务的使用条款。
