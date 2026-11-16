# quark_auto_download 代码审查报告

- 仓库：https://github.com/xuexcy/quark_auto_download
- 审查版本：HEAD `a4a07eb`（"修复落盘判定与并发竞态，新增访问控制与任务隔离（2.1.0）"）
- 审查范围：全部 Python 源码（当时在 src/ 下、约 4.3k 行；2.4.3 起移到 src/server/）、前端（2 个 HTML）、shell 脚本、配置示例、测试
- 方式：静态通读 + 关键路径交叉验证（未连接真实夸克/OpenList/Aria2，无法做端到端验证）

---

## 一、总体评价

| 维度 | 评价 |
|------|------|
| 定位与文档 | ★★★★★ README 写清了状态机、失败语义、暂停语义，甚至给出"原则"（没落盘不删副本），在个人自用项目里非常少见 |
| 架构 | ★★★★☆ 三层（Quark 分享 → 转存自己网盘 → OpenList 直链 → Aria2 → 落盘 → 清理）职责清楚；调度器/文件层/API 层分离干净 |
| 数据安全 | ★★★☆☆ 主路径（运行时轮询）已经按 README 的原则做了 `keep_cloud` 分支，但**启动路径**和**"本地已存在"判定**两个缺口会绕过这条原则 |
| 并发/一致性 | ★★★☆☆ 有线程锁 + 文件锁 + 原子写，思路正确；但 `jobs_manager` 有两处无锁的读改写，会丢更新 |
| 性能 | ★★☆☆☆ 全盘递归算容量、每文件多次 RPC、启动时逐文件探测，规模一上来就是分钟级阻塞 |
| 可测试性 | ★☆☆☆☆ 三个"测试"脚本是连真实服务的调试脚本，无断言、无 pytest、无 CI；核心状态机 0 覆盖 |
| 安全 | ★★★☆☆ Basic 认证 + 同源校验 + 掩码下发 + 文档关闭，意识不错；但没有 Host 白名单、默认无密码且监听 0.0.0.0 |

**结论**：功能完整、可自用，代码质量明显高于一般"AI 一把梭"产物；但**先修 P1 的 3~4 处再放到 NAS 上跑真实数据**，尤其第 1、2 条——它们会让"云端删了、本地也没有"这个作者自己最担心的场景真的发生。

---

## 二、做得好的地方（值得保留）

1. **每个任务独占 `<aria2_download_dir>/<job_id>/` 目录**，Aria2 任务只认同目录下的文件（`aria2_api._extract_task_filename`），多任务同名文件天然隔离——这是多任务并行最容易踩的坑，作者提前想到了。
2. **失败语义分层明确**：`mark_failed(keep_cloud=...)` 把"可重试"和"该释放容量"区分开；超时、gid 失联、被反复暂停、任务被移除都走到 `keep_cloud=True`（pipeline.py:580-612、636-648、701-709）。
3. **软/硬暂停语义真的不同**：软暂停删排队、跟踪活跃到底；硬暂停 pause 保留进度、下次 unpause 续传（jobs_manager.pause_job + aria2_api）。
4. **Aria2 排队水位滞回**（700/500）避免抖动，且"清理不受水位影响"——很务实的取舍。
5. **写配置/索引有工程习惯**：`.bak` 备份、`os.replace` 原子写、`0600` 权限、`flock` 文件锁、退出清理超时。
6. **Web 安全基线**：docs/redoc/openapi 全关、密钥掩码下发、`share_pwd` 不下发只给 `share_pwd_set`、`hmac.compare_digest` 常量时间比较、非 GET 校验同源、写路径白名单（`allowed_roots`）。
7. **前端没有 XSS**：所有插值走 `escapeHtml` 或 `textContent`，日志用 `pre.textContent`；`syncJobs` 用增量 diff 而不是整表重建（也不闪）。
8. **README 诚实标注"代码及文档均由 AI 生成"**，并给出失败场景表——文档比代码还用心。

---

## 三、问题清单

### P1-1 🔴 启动路径违反"没落盘就不删副本"原则（README 自己写的第 1 条铁律）

`src/server/scheduler.py:105-111`

```python
if aria2_status == "complete":
    skipped.append(f"{path} (Aria2已完成)")
    self.file_api.finish_downloaded_file(path)   # ← 返回值被丢弃
    job.state = "cleanup"                        # ← 直接进清理
    job.already_transferred = True
```

`finish_downloaded_file()` 的契约（file_api.py:252-263）就是"**返回 False = 文件没有真正落到目标目录**"。
这里不看返回值，于是：下载目录里文件已消失 / 目标盘满 / 权限不足 / 跨挂载点复制失败时，文件没落到
`download_destination_dir`，状态却是 `cleanup`，紧接着清理 Worker 就把**网盘转存副本删掉**
（`cleanup_files_batch` 只查 OpenList 里在不在，从不查本地）。

对照 `src/server/pipeline.py:614-631` 同一个场景的正确写法：

```python
moved = self.file_api.finish_downloaded_file(job.path)
if not moved:
    # 关键：没真正落盘就绝不能进 cleanup，否则云端副本会被删掉
    self.scheduler.apply_download_to_cleanup(job, failed=True, keep_cloud=True, ...)
    return
```

**同一套语义在 pipeline 里写对了，在 scheduler 里写错了**——典型的重复实现不同步。

**修法**：`scheduler.initialize` 复用 pipeline 的判断；或更彻底——把"移动 + 判定 + 转 cleanup"收敛到
`file_api` 的一个方法里，两处都调用它。

```python
if aria2_status == "complete":
    if not self.file_api.finish_downloaded_file(path):
        job.state = "failed"
        job.note = "Aria2 显示完成但未能移动到目标目录（已保留网盘副本）"
        self.failed_files.append(path)
    else:
        job.state = "cleanup"
        job.already_transferred = True
    continue
```

---

### P1-2 🔴 "本地已存在"只比文件名，不比大小；校验器又明确不看大小 → 截断文件被永久当成功

三处串起来构成一个真实的丢数据路径：

1. `src/server/file_api.py:168-176` `get_local_downloaded_file_names()` 只收集相对路径，**不看 size**；
2. `src/server/scheduler.py:94-103` 命中就 `state="cleanup"`（本地有）或 `"done"`，然后删网盘副本；
3. `src/server/verify_download.py:45-55`

```python
elif expected_size > 0 and actual_size != expected_size:
    size_note = "...分享侧 size 可能不准，以本地文件为准"
    if pipeline_failed:
        reasons.append(size_note)     # ← 流水线成功时，大小不一致也不判失败
...
ok = exists and job.state == "done" and not pipeline_failed
```

**触发场景**（都不罕见）：
- 上一次运行 `shutil.move` 跨挂载点 = copy+unlink，中途断电 / `kill -9` / 磁盘满 → 目标目录留下**半个文件**；
  下次启动只看名字 → 当完成 → 删云端副本 → 分享过期后彻底找不回。
- 用户手工把同名旧版/占位文件放进 `download_destination_dir`（README 明确鼓励"本地已有就跳过"）。
- 0 字节文件：pipeline 会跳过不下载，但如果本地恰好有一个同名 0 字节文件，也会被认成"已完成"。

**修法**（三处一起改，缺一不可）：

1. 建立`{路径: size}` 字典而不是`set`，命中时比对 `expected_size`（`expected_size == 0` 时跳过比对）。
2. 不一致就别跳过：删除本地残缺文件或改名 `.partial`，重新下载。
3. `inspect_job` 里 `ok` 加上 `(expected_size == 0 or actual_size == expected_size)`——"分享侧 size 偶发不准"
   这个理由只能当解释，不能当判据；真不准时它最多多提示一次，而不是放过一个坏文件。

顺带一条更根本的建议：**把"删云端副本"的决策点前移到删之前那一瞬**——`_execute_cleanup` 里对每个
`job` 先 os.path.exists + size 校验，不过就跳过并标记失败。这样即便状态机以后又出 bug，也不会真丢数据。
这与 README 的原则一致，代价是每文件一次 `stat`，可以忽略。

---

### P1-3 🟠 性能：全盘递归算容量 + 每文件多次 RPC，规模一大就分钟级阻塞

| 位置 | 行为 | 代价 |
|------|------|------|
| `quark_api.py:149-183` | `get_available_space()` = `list_my_files_recursive("0")` **遍历整个网盘**累加 size | 10 万文件 ≈ 数千次分页请求；作者注释也承认"可能耗时数分钟"；TTL 只有 180s，等于每 3 分钟重来一次 |
| `file_api.py:183-219` `scheduler.initialize` | 每个"未转存"文件调一次 `openlist_file_exists`（内部 2 次 RPC） | 1 万文件 = 2 万次串行 RPC，启动几分钟 |
| `file_api.py:428-465` `submit_download` | 每个文件 `aria2_find_task_by_path` → `tellActive`+`tellWaiting(0,1000)`+`tellStopped(0,1000)` | 每文件 3 次 RPC，且一次性拉 2000 条记录 |
| `jobs_manager.py:447-496` | 每次 `GET /api/jobs`（前端 5s 轮询）对每个 job 读 2 个 YAML | n 个任务 × 每 5s |
| `quark_api.py:179-183` | 算容量失败 → **回落 9GB** | 失败被静默吞掉，表现为"莫名卡在等待容量" |

**修法**：
- 容量优先用接口给的配额（夸克 `/1/clouddrive/config` 或 member 信息里的 `total_capacity/use_capacity`），
  退一步用"上次已知值 + 增量修正"（代码里已经有 `adjust_available_space_cache`，思路是对的），全盘扫描只做兜底且 TTL 拉到 30min 以上。
- `initialize` 的 OpenList 探测改成"按目录批量 list 一次"而不是逐文件 exists。
- `aria2_find_task_by_path` 复用一次 `tellActive/tellWaiting` 的结果，按批提交。
- `total_space_gb` 默认 10GB 是个坑：盘里已用超 10GB 时 `available_size` 直接算成 0，程序会一直"等容量"，
  日志却只说"空间不足"。建议首次启动时校验配置容量 vs 实际已用，明显不合理就告警。

---

### P1-4 🟠 `jobs_manager` 两处无锁读改写，会丢更新

**a) `pause_job`（jobs_manager.py:621-671）**

```python
job = get_job(job_id)                 # 627 读快照
...
aria2_...                              # 可能几百 ms~秒
stop_job_process(job_id, timeout_sec=15)   # 最长 15s
job["status"] = "paused"
upsert_job(job)                        # 668 整条覆盖
```

这中间如果 `start_job` 抢到 `_job_start_lock` 把任务启动了（写入新的 `status=running`、`log_path`、`started_at`），
`pause_job` 结尾的 `upsert_job` 会把**旧快照整体写回**，于是：`log_path` 退回上一次的路径（页面上"任务日志"指向旧的日 志文件）、
`status` 退回 paused。更糟的是 `stop_job_process` 已经先一步把刚起来的进程 SIGTERM 掉——用户点了"开始"实际被静默杀掉。

**修法**：`pause_job` 也进 `_job_start_lock(job_id)`；`upsert_job` 改成"按字段合并"而不是整条替换
（或者每次写前重新 `get_job` 再改）。

**b) `refresh_job_runtime` 在"读"里写盘（jobs_manager.py:461-466）**：状态对账时 `upsert_job` 合理，
但它是被 `list_jobs()`（GET 接口，5s 一次）间接触发的。n 个任务同时"进程已退出"时，一次 GET 会做 n 次
"加锁 → 读整个 jobs.yaml → 改一条 → 写回"，O(n²) 写放大。建议：收集完一次性批量写。

---

### P2-5 🟠 安全：默认无密码 + 监听 0.0.0.0 + 同源校验可被 DNS rebinding 绕过

- `bin/start.sh:7` 默认 `QUARK_AUTO_DL_WEB_HOST=0.0.0.0`；
- `web_app.py:49` `AUTH_ENABLED = bool(WEB_PASSWORD)`，`conf/example/web.example.yaml` 默认 `web_password: ""`
  → **开箱即无认证**，同一局域网任何人都能：增删任务、改 `aria2_download_dir`/`allowed_roots`、触发重启、
  读 `/api/config`（密钥被掩码，但保留首 4 位 + 尾 4 位，Cookie 的尾 4 位也是有信息量的）；
- `web_app.py:84-97` 只比较 `Origin/Referer` 的 netloc 与 `Host` 头，**没有 Host 白名单**：
  恶意页面把域名解析到内网 IP（DNS rebinding）时，`Host` 和 `Origin` 都是攻击者域名，校验照样通过；
  另外 `if not host: return True`（87 行）——缺 Host 头直接放行（HTTP/1.0 可构造）。

**修法**（按性价比）：
1. `start.sh` 默认只监听 `127.0.0.1`，需要局域网访问时显式设 `QUARK_AUTO_DL_WEB_HOST=0.0.0.0`，且此时**强制要求** `web_password`（启动时直接拒绝启动并提示）；
2. 加 `allowed_hosts` 配置（默认 `127.0.0.1,localhost,<本机 IP>`），校验 `Host` 头；
3. 无 Host 头时改为 `return False`；
4. 若确实要跨公网，前置 HTTPS（或用 Tailscale 之类），Basic 明文口令在内网也别当安全边界。

---

### P2-6 🟡 转存成功判定：不校验 fid 覆盖，且两处判定式不一致

`src/server/quark_api.py:470-511`

```python
if (not data or (data.get("status") != 200 and data.get("code") != 0)) and any(fid_token_list):
    ...  # 去掉 fid_token 重试
if data.get("status") != 200 and data.get("code") != 0:
    raise RuntimeError(...)
```

- `and` 意味着 `status != 200` 但 `code == 0` 时**不报错**，接着 `data["data"]` 可能 KeyError；
  而 `create_my_dir` 那边用的是 `(not in (0, None) and status != 200)`——两套口径。
- `data` 若是 list/None，`data.get` 直接 AttributeError（不是走异常分支，而是崩在上面的条件里）。
- `file_api.transfer_file_batch:396-409` 只要 `saved_fids` 非空就把**整组**标记为已转存；
  夸克对同名/已存在/超额条目可能少返 fid，此时文件其实没进 `save_to_dir`，后面下载必然失败
  （`submit_download` 拿不到直链），最后按"下载失败"删掉——日志里看到的是"下载失败"，根因在转存。

**修法**：统一 `_is_ok(data)` 工具函数；转存后按 `len(saved_fids) == len(group_files)` 校验，不足则把差额
标成失败并记录（或转存后立即用一次 `list_my_dir_entries` 核对）。

---

### P2-7 🟡 `_normalize_my_fid` 用 `'-'` 截断，配置里合法 fid 会被切坏

`quark_api.py` 中 `_normalize_my_fid` 见到 `-` 就取前半段（注释：兼容 `fid-名字` 的写法）。
顺手写配置文件时把 `save_to_dir` 写成 fid 很常见，但 fid 本身是十六进制串、一般不含 `-`；
一旦夸克换成含 `-` 的 id 形式，这里会静默指向错误目录（甚至"新建目录"到别人看不见的地方）。
**修法**：只在该字段明显不是 fid 形态（如含中文/空格）时才做拆分，或干脆要求配置里只能填目录名/纯 fid 并显式校验。

---

### P2-8 🟡 跨设备 `shutil.move` + 秒级时间戳：极小概率覆盖真实文件

`file_api.py:243-249`

```python
if os.path.exists(dst_path):
    suffix = time.strftime("%Y%m%d_%H%M%S")     # 秒级
    dst_path = os.path.join(..., f"{base}_{suffix}{ext}")
shutil.move(src_path, dst_path)                  # dst 已存在时 POSIX rename 会直接覆盖
```

两个同名文件在同一秒内完成（并发下载 + 同名不同目录被拍平到同一相对路径）时，第二个会覆盖第一个。
**修法**：加 `uuid4().hex[:6]` 或毫秒 + 递增序号，并用 `os.open(..., O_CREAT|O_EXCL)` 抢目标名。

---

### P3-9 其他值得一改的小问题

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| 1 | `bin/stop.sh:75-82` | 兜底 kill 用 `ps` 全表匹配"项目路径 + `src/server/quark_main.py`"，注释说"避免误杀 vim/tail"，但 `vim /path/src/server/quark_main.py` 两个条件都命中，照样被杀 | 再加一条排除条件（`argv[0]` 必须是 venv python / 或 cmdline 以 python 开头） |
| 2 | `quark_api.py:149-183` | 算容量失败静默回落 9GB 并缓存 180s | 回落值应标`低置信`，日志 warning，且不要在"等待容量"分支里用它阻塞 |
| 3 | `quark_api.py:513-544` | `_wait_task_and_get_fids` 收到非 ok `message` 只 `continue` | 连续 N 次异常应快速失败，而不是耗满 `task_timeout=180s` |
| 4 | `error_classify.py:11-43` | 子串匹配 `"401"/"500"/"503"` 等裸数字，`"size=1401"`、`"5000000 bytes"` 会误判 per-mand/temporary | 改成 `re.search(r"\b(401|403|500|...)\b")`，或匹配 `http 500`/`status.*500` |
| 5 | `jobs_manager.py:66-72` | `_write_private_text` 的 0600 只在**创建**时生效，已存在的文件重写不改权限 | `os.chmod(path, 0o600)` |
| 6 | `jobs_manager.py:73-85` | `_atomic_yaml_write` 无 `fsync`（掉电可能 0 字节） | `f.flush(); os.fsync(f.fileno())` 后再 `os.replace` |
| 7 | `quark_main.py` 结尾 | 无论全成功还是全失败都写 `status=paused`（message="全部处理完毕（已回到暂停）"） | 增加 `failed` 终态，前端 `statusView` 里区分（现已统计 failed_files，只差状态位） |
| 8 | `pipeline.py:171-222` | `_persist_state` 由 3 个 worker 每 tick 各调一次，持锁全量序列化 | 合并成"主循环每 tick 一次 + 关键状态变更时立刻写" |
| 9 | `src/web/static/index.html:942` | 5s 轮询 `/api/jobs`，同时每次都拉 250 行日志 | 日志面板折叠时不拉；或用 `ETag`/`If-Modified-Since` |
| 10 | `src/web/static/index.html:396-402` | `escapeHtml` 未转 `'` | 目前所有属性都用双引号，暂时安全，但建议补上 |
| 11 | `verify_download.py` | 校验只在**整个任务结束后**跑一次，不参与"删不删副本"的决策 | 见 P1-2：把校验前移到清理之前 |
| 12 | 仓库根 | 无 `LICENSE`、无 CI、`requirements.txt` 未固定版本 | 加 `LICENSE`、`requirements.txt` 用 `==`，GitHub Actions 跑 lint |

---

## 四、测试：目前等于没有

`tests/munual/` 下 3 个脚本（`test_quark.py` 158 行、`test_openlist.py` 122 行、`test_aria2.py` 120 行）都是
"直接连真实服务跑一遍、`logging` 打印结果"的调试脚本：

- 没有断言、没有 pytest/unittest、没有 fixture；
- 依赖真实 Cookie / OpenList / Aria2 与线上数据；
- `tests/munual/README.md` 自己说"配置参数都是示例值，需要根据实际情况修改"——即 CI 里跑不了。

于是**最该被测试覆盖的部分恰好是零覆盖**：`PipelineScheduler` 的状态迁移（pending→transferring→ready→downloading→cleanup→done）、
`mark_failed(keep_cloud=...)` 的分支、`move_file_to_destination` 的三种冲突分支、`classify_error` 的误判。

这些全都可以**不连网**测：`FileApi` 已经接受注入的 client，写几个 `Fake` 就能覆盖。
建议先加 `tests/pytest/test_scheduler.py`，第一条用例就写：

> `Aria2 报 complete 但 finish_downloaded_file 返回 False 时，job 不得进入 cleanup`

——这条用例如果先存在，P1-1 这个 bug 就进不了主干。同理第二条写"本地文件 size 不符不得跳过"。

---

## 五、建议的修复顺序

1. **P1-1**：`scheduler.initialize` 检查 `finish_downloaded_file()` 返回值（10 行改动，直接堵住最危险的一条）。
2. **P1-2**：本地已存在判定加 size 比对；`inspect_job` 的 `ok` 纳入 size；`_execute_cleanup` 删前再 stat 一次。
3. **P2-5**：默认监听 `127.0.0.1` + 无密码时启动告警改为拒绝（或强制 `--host` 显式确认）。
4. **P1-4**：`pause_job` 加锁、`upsert_job` 改字段合并。
5. 补最小单测（状态机 4~6 条 + 移动/冲突 2 条），再谈性能优化（P1-3）。

修完前 4 条，这个项目在 NAS 上跑几十万文件的批量下载，我作为审阅者是敢用的。
---

## 六、修复记录（2.2.0，2026-11-16）

以下条目均已在工作区修复，`pytest`（51 用例）与 Web 冒烟验证通过；改动尚未提交。

| 编号 | 状态 | 修复要点 |
|------|------|----------|
| P1-1 | ✅ | `scheduler.initialize` 检查 `finish_downloaded_file()` 返回值，False 时 `mark_failed(..., keep_cloud=True)`，不再进 cleanup；回归用例 `tests/pytest/test_scheduler_state.py` |
| P1-2 | ✅ | `FileApi.get_local_downloaded_file_sizes()` 返回 `{relpath: size}`；`scheduler._local_size_matches()` 统一判定（期望 0 时不比）；`verify_download.inspect_job` 的 `ok` 纳入 size 与 0 字节（`QUARK_AUTO_DL_STRICT_SIZE=0` 可放宽）；清理前 `local_copy_is_intact()` 复核 |
| P1-3 | ✅ | 容量优先读配额接口，失败才递归统计，再失败才回落并标低置信；`space_cache_ttl` 可配（默认 1800s）；Aria2 任务快照按批复用（每文件 3 RPC → 每批 3 RPC）；OpenList 存在性按目录批量；`_persist_state` 节流；`list_jobs` 批量落盘 |
| P1-4 | ✅ | `pause_job` 与 `start_job` 共用任务级锁；`upsert_job` 按字段合并，不再整条覆盖 `log_path` / `status`；`refresh_job_runtime(persist=False)` + `list_jobs` 一次回写 |
| P2-5 | ✅ | 默认只监听 `127.0.0.1`；`Host` 白名单（`QUARK_AUTO_DL_WEB_ALLOWED_HOSTS`）；缺失 `Origin` / `Referer` 判非同源；非回环且无密码拒绝启动（`QUARK_AUTO_DL_WEB_ALLOW_INSECURE=1` 逃生） |
| P2-6 | ✅ | 统一 `_is_ok(data)`；`save_files_to_my_disk` 校验 `data` 结构；转存后回查目标目录，只把真正出现的文件标成功，未出现的逐条失败 |
| P2-7 | ✅ | 仅当 `text.partition('-')[0]` 命中 `^[0-9a-fA-F]{16,}$` 才截断，普通目录名原样保留 |
| P2-8 | ✅ | `_reserve_destination_path()` 用 `os.O_CREAT \| os.O_EXCL` 原子抢名，占用则加时间戳 / 序号，不再覆盖同名文件；用例 `tests/pytest/test_file_api_move.py` |
| P3-1 | ✅ | `stop.sh` 兜底要求命令行第二列含 `python`，`vim src/server/quark_main.py` 不再被误杀 |
| P3-2 | ✅ | 回落值标低置信 + warning；低置信时 `wait_capacity` 不再无限等，判失败并提示检查 `total_space_gb` / Cookie |
| P3-3 | ✅ | `TASK_ABNORMAL_LIMIT = 5`，连续异常快速失败，不再耗满 `task_timeout` |
| P3-4 | ✅ | 改正则匹配，数字必须带 HTTP / status 上下文；用例覆盖 `size=1401`、`5000000 bytes`、`status=403` |
| P3-5 | ✅ | `os.fchmod(fd, 0o600)`，已存在文件重写也会收紧权限 |
| P3-6 | ✅ | `flush + fsync` 后再 `os.replace`，并对目录 fd fsync |
| P3-7 | ✅ | `quark_main` 按结果写 `done` / `failed`；前端 `statusView` 区分「已完成 / 有失败」 |
| P3-8 | ✅ | `_persist_state(force=)` 节流 5s，退出与中断时强制写 |
| P3-9 | ✅ | 日志按需拉取（`refreshLogIfNeeded`：同任务同状态且非运行中则跳过），不再每 5s 拉 250 行 |
| P3-10 | ✅ | `escapeHtml` 补单引号 → `&#39;` |
| P3-11 | ✅ | 见 P1-2：校验前移到「删不删副本」的判定里 |
| P3-12 | ⚠️ 部分 | 已加 `requirements.txt` 固定版本、`requirements-dev.txt`、`.github/workflows/ci.yml`、`pytest.ini`；**`LICENSE` 未加** —— 许可证是作者的法律决定，README 已有「仅供学习和个人使用」表述，应由作者自行选择 |

## 七、新增功能：分享目录树 + 勾选下载

原方案「一个任务 = 一个链接，链接可以是分享目录或分享目录里的某个子目录」是成立的，
现在扩展为「一个任务 = 一个链接 + 可选的勾选范围」。

链路：

1. 前端 `POST /api/share/tree {share_url, share_pwd, pdir_fid}` → `QuarkClient.list_share_dir_entries()`：
   只列一层，按 `pdir_fid` 懒加载，返回 `{fid, name, is_dir, size, updated_at}`（目录在前）。
   stoken 在进程内缓存 900s（键 = share_url + 提取码的 sha1），不落库。
2. 勾选结果存进任务：`selection = {file_fids: [...], dir_fids: [...]}`，空 = 整个分享目录；
   也可用 `PUT /api/jobs/{id}/selection` 事后修改。
3. `start_job` 把 selection 写到 `state/jobs/<id>/selection.json`（0600）并注入
   `QUARK_AUTO_DL_SELECTION_FILE`；`quark_main.load_selection()` 读取后传给
   `list_share_files(..., selected_file_fids=..., selected_dir_fids=...)` 做递归过滤：
   勾中的目录整棵子树沿用 `_inherited=True`；未勾的目录只在里面还有单独勾选的文件时才继续下钻。
4. 下载、转存、清理的后续流程完全不变。

写测试时又发现一个真 bug 并修掉：`list_share_files` 原有一个「只勾目录时跳过未选目录递归」的优化，
会让「只勾深层子目录、父目录未勾」的场景一个文件都收不到 —— 已删除该跳过分支，改为只标记继承状态继续递归。

## 八、改造记录（2.3.0，2026-11-16）：去掉 OpenList + 单文件进度

### 8.1 为什么要去掉 OpenList

OpenList 在本项目里只承担四件事，全部都能由夸克原生接口替代：

| 用途 | 原实现 | 现实现 |
| --- | --- | --- |
| 取下载直链 | `openlist_get_download_url` | `POST /1/clouddrive/file/download`（`QuarkClient.get_download_urls`） |
| 判网盘文件是否存在 | `openlist_list` + 逐级解析目录 | `/1/clouddrive/file/sort`（`list_my_dir_entries`）按名字匹配 |
| 建云端目录 | `openlist_ensure_dir` | `ensure_my_dir_path`（本来就有调用，OpenList 那次是重复的） |
| 删云端副本 | `openlist_delete_files_in_dir` | `delete_my_fids`（`/1/clouddrive/file/delete`） |

顺带解决两个隐患：OpenList 的 `Authorization` 头被直接塞进 aria2 的 `header` 选项，意味着令牌会出现在 aria2 的 RPC 参数和日志里；以及多一份要用户手工起服务、填 token 的部署负担。

一个绕不开的前提：`file/download` 只接受自己网盘里的 fid，分享目录里的文件必须先 `save_files_to_my_disk` 转存、拿到自己的 fid 才能换直链。所以「转存 → 取直链 → 下载 → 删云端副本」这条链路没有变化，去掉的只是挂在链路上的外挂服务。

### 8.2 代码改动

- `src/server/openlist_api.py` 删除；`conf/example/openlist.example.yaml`、`tests/munual/test_openlist.py` 一并删除。
- `QuarkClient` 新增 `get_download_urls(fids) -> dict[fid, url]`（两个 base 轮流试：`drive.quark.cn`、`drive-pc.quark.cn`）与 `download_headers()`（返回 aria2 用的 `Cookie`/`Referer`/`User-Agent` 三行）。
- `Aria2Client.__init__` 的 `openlist_token` 参数换成 `download_headers`；`aria2_add_url` 里 `options["header"]` 由令牌改为直链所需请求头。
- `FileApi`：删 `openlist_client`/`openlist_quark_path`/`openlist_path()`/`openlist_dir_path()`，新增 `_parent_fid_for()`、`_entries_by_name()`、`resolve_my_fid()`、`download_url_for()`。转存校验时顺手把命中的 `fid` 回填到 `file_item["my_fid"]`，后续取直链/删副本不用再查目录。
- `Scheduler.initialize` 的「网盘里是否已有」判定从 OpenList 列表改成 `_my_drive_fid(path)`（按 relative_dir 缓存一次目录列表），命中即视为已转存。
- `jobs_manager`/`config_loader`/`web_app`/`quark_main` 里所有 `openlist` 配置项、密钥白名单、配置读写分支删除；`settings.html` 的 OpenList 面板整块去掉。

### 8.3 新增：单文件下载进度

- 后端：pipeline 轮询 aria2 时把 `completed/total/speed/progress` 写进 `job.download_task`，随 `persist_payload` 落盘到 `state/jobs/<id>/pipeline_state.yaml` 的每个 file 上；`done`/`cleanup` 直接记 100%，避免前端停在 99%。
- 接口：`GET /api/jobs/{id}/files?offset=&limit=` → `{files:[{path,size,state,downloaded,total,speed,progress,note}], file_total, counts, stats}`。
- 前端：任务详情新增「文件进度」面板，含过滤输入框与汇总行（完成 x/y、进行中 n、失败 m）；单文件最多渲染 400 行，行节点复用、只改 `textContent` 与 `width`，全部走 `textContent` 所以文件名不需要转义；5s 轮询复用现有刷新节奏，非运行态不再重复拉取。

### 8.4 验证

- `pytest` 56 用例全绿（原 51 + 新增 `tests/pytest/test_job_files.py` 5 个归一化用例）。
- `compileall src` 通过；起服务冒烟：`/api/jobs`、`/api/jobs/<不存在>/files`（404）、`/`、`/settings` 均符合预期，`/settings` 里已无 OpenList 字样。
- 用一个假任务（5 个文件、各状态齐全）在真实浏览器里验证前端：进度条宽度分别为 37.5%/100%/0%/0%/0%，
  汇总行显示「完成 1/5 · 进行中 2 · 失败 1」，过滤「剧集」后只剩 2 行且 5s 轮询后过滤词不丢；
  切换任务时过滤词自动清空、无文件的任务面板隐藏。

## 九、2.4.x：扫码登录与真机验证（2026-11-16）

### 9.1 扫码登录（2.4.0 新增）

`src/server/quark_login.py`（CAS 取 token / 轮询 service_ticket / 换 Cookie / segno 渲染 SVG）+ 四个接口
（`POST /api/quark/login/qr`、`GET /api/quark/login/qr/{sid}`、`POST /api/quark/cookie`、
`POST /api/quark/cookie/test`）+ 设置页「夸克登录」面板（扫码 / 手工 Cookie 两个 tab）。
协议与证据见 `docs/quark-qr-login.md`。

### 9.2 真机踩出来的两个问题（2.4.1 修）

1. **换 Cookie 端点猜错了**：`/cas/login?client_id=532&code=<ticket>` 实测只返回登录页 HTML 和
   `_UP_*` 埋点 Cookie，没有 `__pus`。真实链路是「同一个 `requests.Session` 顺序 GET 5 个 pan 接口」：
   `uop.quark.cn/` → `pan.quark.cn/` → `pan.quark.cn/account/info?st=<ticket>&lw=scan` →
   `pan.quark.cn/account/user_login_method_info?fr=pc&platform=pc` →
   `drive-pc.quark.cn/1/clouddrive/auth/pc/flush?pr=ucpro&fr=pc&uc_param_str=`。
   `_exchange_ticket()` 已按此重写，失败时返回每一步的 HTTP 状态与 Cookie 名做诊断。
2. **二维码显示不全**：`segno.svg_inline()` 只输出绝对 `width/height`、没有 `viewBox`，被
   `.qr-canvas{overflow:hidden}` 裁掉右下角 → `_with_viewbox()` 换成 `viewBox`，容器 190px→220px。

### 9.3 真机验证结果

- 扫码 → Cookie 落盘（555 字节）→ `POST /api/quark/cookie/test` 返回 `{"ok": true}`；
  `/1/clouddrive/member` 返回 `total_capacity` = 10 GiB、`use_capacity` = 7121510664、`member_type` = `NORMAL`（无昵称字段）。
- `get_download_urls()` 对 0 字节文件返回 451 字符的 `https://dl-pc-sz.drive.quark.cn/...` 直链 —— 2.3.0 的直链链路首次真实闭环。
- **23018 单文件大小限制**：7.11 GiB 文件被 `file/download` 拒（两个 base 都是
  `400 code=23018 "download file size limit[<fid>]"`）。已新增 `QuarkDownloadRejected`（不换域名重试）、
  `download_url_for()` 报错带路径、`error_classify` 把 23018 归为 permanent。阈值未测出。
- 测试 77 个全绿（`tests/pytest/`）。

## 十、2.4.2：目录树搬到右侧 + 可反复勾选（2026-11-16）

### 10.1 交互改造

原来的勾选在「新增」表单里，一次定生死：选完只能新建任务，想补勾就得改 `selection` 接口或者重建任务。
2.4.2 改成：

- 目录树从侧边栏挪到右侧详情区顶部（`[data-role="tree-panel"]`），新增任务后自动出现，有下载记录的任务默认折叠。
- 勾选是**追加**语义：点「下载所选」把当前勾选 union 进任务的 `selection`，不清空旧值；
  于是可以「下两个 → 下完 → 回来再勾几个 → 再下」，已下过的文件靠本地存在性判定自动跳过。
- 目录上直接标状态并禁用勾选：`已勾选` / `已勾选 · 任务B` / `已下载` / `下载中` / `失败`，避免重复下同一份。
- 重复项默认拦截，`force: true` 才放行（真需要两份时）。

### 10.2 后端

- `src/server/jobs_manager.py`：`share_key(share_url)` 把链接归一成 `s/<id>`（丢 `?pwd=`、尾斜杠），
  只比较同一分享；`share_usage(job_id="") -> {fid: {job_id, job_name, state, kind}}` 先收各任务
  `selection` 记 `selected`，再用 `state/jobs/<id>/pipeline_state.yaml` 的 `files[].fid` 覆盖成真实下载态；
  `add_job_selection(job_id, selection, force=False)` 做 union 并回 `{added, duplicates, already_in_job}`，
  `duplicates[].reason` ∈ `other_job` / `in_job_done`；`job_files()` 增补 `fid` 字段。
- `src/server/web_app.py`：`ShareTreeRequest.job_id`（传了就只认任务上的链接/提取码，找不到 404），
  返回的每个 entry 带 `taken`；新增 `POST /api/jobs/{job_id}/selection/add`（`JobSelectionAddRequest.force`），
  旧的 `PUT /api/jobs/{job_id}/selection` 保留。
- `src/server/scheduler.py` `persist_payload()` 的每个 file 补 `fid`，供前端判断「已下载」。

### 10.3 顺手修掉的两个真 bug

1. **同分享链接会复用任务**（`jobs_manager.add_job`）：原逻辑「同一 `share_url` 已存在就复用」，会把旧任务改名、
   并把 `selection` 整体覆盖 —— 与追加语义直接冲突（实测：任务「预演任务B」建第二个同名链接任务后变「预演任务C」
   且勾选清空）。已改成只保留重名检查，同链接可建多个任务，重复文件交给勾选时的 `share_usage` 拦。
2. **前端把被拦下的重复项标成「已勾选」**（`treeDownloadSelected`）：原实现无脑把提交的 fid 全标成本任务已选，
   导致提示里写着「已在任务B中」、行上却显示「已勾选」。改为先用 `duplicates` 建 `dupMap`，命中时沿用返回的
   `job_id/job_name/state`；并补了 `already_in_job` 的提示文案。

### 10.4 验证

- `tests/pytest/` 91 用例全绿（新增 `test_share_selection.py` 8 例、`test_share_tree_api.py` 4 例、
  `test_job_create.py` 2 例：`share_key` 归一化、`share_usage` 分状态、追加不增长、跨任务拦截带 reason、
  `force` 放行、空勾选报错、`/api/share/tree` 的 `taken` 与 404、`selection/add` 透传、建任务不复用）。
- 真实浏览器（只读预演服务器，假目录 + 假 aria2 启动）走通 7 个场景：新增后右侧自动出树、点目录懒加载子层、
  勾 2 文件 → 摘要「本次已勾选 2 文件」、点「下载所选」→「已加入 2 个文件…已开始下载」+ 行禁用 + badge「已勾选」、
  勾目录 →「已加入 0 个文件 / 1 个文件夹」、跨任务重复 →「跳过：封神2.mp4（已在「任务B」中）」且 badge 显示来源任务、
  `pipeline_state.yaml` 写入 done 后显示「已下载」。


## 十一、2.4.3：目录结构调整（2026-11-16）

按使用者要求把仓库从「src 平铺 + 仓库根 web/ + 两个平级测试目录」收敛成：

```
src/
├── server/   # 原 src/*.py
└── web/static/   # 原 web/static/
tests/
├── pytest/   # 原 tests_pytest/
└── munual/   # 原 tests_manual/（沿用使用者给的拼写）
```

跟着改的锚点（都是路径常量，逻辑没动）：

- `src/server/config_loader.py`：`BASE_DIR` 从「`__file__` 的上一级」改成上溯两级（`src/server` → 根），
  `conf/`、`log/`、`state/` 的解析因此不变
- `src/server/jobs_manager.py`：`MAIN_SCRIPT` → `BASE_DIR/src/server/quark_main.py`；
  拉子进程时的 `PYTHONPATH` → `BASE_DIR/src/server`
- `src/server/web_app.py`：`WEB_DIR` → `BASE_DIR/src/web`（`STATIC_DIR` 随之指向 `src/web/static`）
- `bin/start.sh` 的 `PYTHONPATH`、`bin/stop.sh` 兜底匹配串（`src/server/quark_main.py`）
- `tests/pytest/conftest.py` 与 `tests/munual/test_*.py` 的 `sys.path` 注入改成 `src/server`；`pytest.ini` 的 `testpaths = tests/pytest`

验证：`pytest` 91 全绿；`compileall src` 通过；服务以 `PYTHONPATH=src/server` 起在 8787，
`/`、`/settings`、`/static/index.html` 均 200；浏览器里页面正常渲染、目录树面板与夸克登录面板都在。
## 十二、2.4.4：超单文件上限提前打标（2026-11-16）

23018 只在 `file/download` 时才暴露，用户勾完一整季再点下载、等任务跑起来才看到失败，体验很差。
这一版把判断提前到浏览阶段，但**只提示、不拦截**（用户可能先下着、或改用官方客户端）。

- 后端：`src/server/web_app.py` 新增 `DEFAULT_MAX_FILE_SIZE_GB = 5.0` 与 `_max_download_file_size()`
  （读 `quark_max_file_size_gb`，配置缺失/异常/非法值一律回落默认；<=0 关闭打标）。
  `POST /api/share/tree` 给每个 entry 补 `oversize`，响应补 `max_file_size`（字节，0=未开启）。
- 前端：目录树每行多一个 `[data-role="limit"]` 虚线黄标（`tree-badge warn`），文案「超出 5 GiB」，
  hover 说明「会被夸克 23018 拒绝」；`treeFetch()` 把响应里的 `max_file_size` 换算成 `treeState.maxFileSizeGb`
  供文案使用。勾选后「下载所选」成功时追加「N 个文件超出单文件上限（5 GiB），可能下载失败」。
- 目录不打标：目录 size 是聚合值，与单文件限制无关。
- 阈值仍是经验值 5 GiB（真实阈值没测出来），所以做成配置项而不是硬编码。文案只用「⚠ 超 5 GiB」+「可能」，
  避免让人误以为是接口给的硬阈值。
- 用户问过「为什么是 5 GB，账号容量不是 10 GB 吗」——这两个数没有关系：10 GiB 是
  `/1/clouddrive/member` 的 `total_capacity`（总空间），23018 限制的是单文件下载，跟剩余空间无关。
  顺手把 `member`/`config` 的返回全字段扫了一遍，没有下载上限字段（只有 `md5_size_limit`=1 TiB 等），
  所以无法从接口推导真实值。

验证：`tests/pytest/test_share_tree_api.py` 新增 3 例（正常打标 / 填 0 关闭 / 配置异常回落默认），
全仓 94 个用例全绿；预演服务器（`/tmp/oversize_preview_app.py`，FakeClient 造 7 GiB 文件）里实测：
7 GiB 那行出现「超出 5 GiB」、900 MB 与目录都没有，勾选后提示「已加入 1 个文件 / 0 个文件夹；
1 个文件超出单文件上限（5 GiB），可能下载失败；已开始下载」。截图 `~/Downloads/quark-oversize-badge.png`。

## 十三、2.4.5：23018 的真因是 User-Agent（2026-11-16）

起因：用户问「那我什么openlist+aria2可以成功下载，他们不用取直链吗」——既然 OpenList 走的是同一套接口，
为什么它能下 6.6 GiB、我们不能。

### 13.1 源码核对（OpenListTeam/OpenList，drivers/quark_uc/）

- `util.go:113` `func (d *QuarkOrUC) getDownloadLink(file model.Obj)`：body `{"fids": []string{file.GetID()}}`，
  `d.request("/file/download", http.MethodPost, ...)`，取 `resp.Data[0].DownloadUrl`。
- `util.go:27` `request()`：`pr`=`ucpro`、`fr`=`pc`，响应里用 `__puus` 回写 cookie。
- `meta.go` 默认值：`ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) quark-cloud-drive/2.5.20 …"`、
  `referer=https://pan.quark.cn`、`api=https://drive.quark.cn/1/clouddrive`。

**结论**：端点、参数、body 与我们完全一致；唯一差别是 **User-Agent**。所以问题不在接口用法，而在请求身份。

### 13.2 实测对照（同一账号、同一 fid：6.62 GiB / 7110174386 字节）

| 请求 UA | drive.quark.cn | drive-pc.quark.cn |
| --- | --- | --- |
| 浏览器 `Chrome/131` | `400 code=23018` | `400 code=23018` |
| `quark-cloud-drive/2.5.20 …` | `200`，url 2198 字符 | `200`，url 2210 字符 |

- CDN 侧不看 UA：带 Cookie+Referer 就能 `200`，不带 Cookie 一律 `403 RequestDeniedByCallback … require login`。
- 用本项目代码取到的 597 字符直链实测：`Range: bytes=0-1023` → `206`、
  `content-range: bytes 0-1023/7110174386`、`content-type: video/x-matroska`、首字节 `1a45dfa3`。

### 13.3 改动

- `quark_api.DESKTOP_USER_AGENT`（客户端 UA）成为默认；`QuarkClient.__init__(user_agent="")` 空值即用默认，
  `headers["User-Agent"]` 与 `download_headers()` 自动跟随。
- 三个入口全部接线：`quark_main.init_clients()`（`QUARK_CLIENT_UA`）、`web_app._share_context()`（`_quark_user_agent()`）、
  任务子进程（走 `quark_main`，天然继承）。
- `conf/quark.yaml` 新增 `quark_client_ua`：留空=客户端 UA，可填浏览器 UA 复现 23018。
- 23018 文案补「也可能是 quark_client_ua 被改成了浏览器 UA」，并回显 `ua=<前 40 字符>…`。
- `DEFAULT_MAX_FILE_SIZE_GB` 5.0 → 0.0：UA 修好后这条限制不再触发，默认不打「⚠ 超 5 GiB」标（可配数字重新开启）。

### 13.4 验证

- `tests/pytest/test_quark_download_url.py` 新增 3 例（默认 UA / 覆盖并 strip / download_headers 跟随），
  `tests/pytest/test_share_tree_api.py` 的回落用例改为断言默认关闭；全仓 **97 passed**，`compileall src` 通过。
- 真机：`QuarkClient.get_download_urls(["053d011b3f0f4d31901a7505cd4c1038"])` → 直链可用（见 13.2 的 206 记录）。

### 13.5 仍未确定

- 23018 的边界值已无实际意义（它由 UA 决定，而不是文件大小）；若哪天客户端 UA 也被拦，说明夸克又加了新的判定维度。
- 转存/删除云端副本的行为不变：仍保留 23018 友好报错，失败不删副本、不盲目重试。

## 十四、2.4.6/2.4.7：版本可见性与缓存（2026-11-16，部署 NAS 后）

### 14.1 现象与定位

- 部署到 fnOS NAS 后，人工核对时判断「好像还是旧版本」。实测服务端返回的 `index.html` 与开发机同名文件 **sha256 完全一致**（`560143bd…b8bc`），`src/server/web_app.py` 的 `version` 已是 2.4.5，进程也是新起的 → 服务端确实是新版本。
- 真正原因是**浏览器缓存**：`/settings` 等 HTML 响应只有 `etag/last-modified`、没有 `Cache-Control`，浏览器沿用旧副本，DOM 里连新加的 `#app-ver` 元素都不存在；强制刷新后立刻出现。

### 14.2 改动

- `src/server/web_app.py`：抽出 `APP_VERSION = "2.4.6"` 供 `FastAPI(version=…)` 复用；`GET /api/health` 返回 `{"ok": true, "version": APP_VERSION}`。
- `src/server/web_app.py`：`_security_guard` 中间件对 `*.html`、`/`、`/settings` 的响应加 `Cache-Control: no-store, must-revalidate`（API 响应不受影响）。
- `src/web/static/index.html`、`src/web/static/settings.html`：标题旁新增版本徽标 `#app-ver`（拉 `/api/health` 填充，形如 `v2.4.6`）。

### 14.3 验证

- `curl -s http://192.168.1.100:8787/api/health` → `{"ok":true,"version":"2.4.6"}`；`GET /` 与 `/settings` 响应头含 `cache-control: no-store, must-revalidate`。
- 真实浏览器（NAS 地址）：强制刷新后首页侧边栏、设置页标题均显示 `v2.4.6`；此前未刷新时 `document.getElementById('app-ver')` 为 `null`，正是缓存证据。
- 本地 `pytest` → 97 passed；`node --check` 对两页内联脚本通过。

### 14.4 2.4.7：把「后端是哪个版本」也变成可核对的事实

- 起因：修好页面缓存后仍收到「后端还是旧版本」的反馈。实测 NAS 上 2.4.x 才有的路由确实生效（`GET /api/jobs/nope/files` → `404 {"detail":"任务不存在"}`，`PUT /api/jobs/nope/selection` → `400 {"detail":"任务不存在: nope"}`），但 `/api/health` 当时只有 `{"ok":true,"version":"2.4.6"}` 且**没有** `Cache-Control`——浏览器把这条 JSON 也缓存了，于是出现「页面已刷新、版本接口仍是旧内容」的假象。
- `GET /api/health` 现在返回 `version / pid / started_at / uptime_seconds / code_root / python`；`code_root` 就是加载 `web_app.py` 的目录，可直接证明后端跑的是哪份代码。
- 中间件改为**除 `/static/*` 外全部 `no-store`**；静态资源保留 `etag`，避免每次都重传 75 KB 页面。
- 设置页副标题下方新增 `#backend-info` 行：`后端 2.4.7 · PID … · 启动 … · /vol2/…/src/server`。

## 十五、2.4.8：底部日志被挤没（2026-11-16）

### 15.1 现象

- 任务详情区把「分享目录」与「文件进度」都展开后，底部**日志只剩一条边框**，正文完全不可见；用户反馈原话：「底部看不到日志」。

### 15.2 定位（用合成 DOM 复现，不依赖真实任务）

- 把 `src/web/static/index.html` 复制到本地静态页，注入与真实渲染同构的四块（`.stats` / `.job-tree` / `.job-files` / `.job-log`）后测量：
  视口 469px 时 `.main-body` 高 405px，内容 860px；`.job-log` 实测 **9px**（只剩上下 border），而内部 `pre.log` 有 192px → 溢出被 `.main-body{overflow:hidden}` 裁掉。
- 根因链：`.detail` 是纵向 flex；`.job-log{flex:1 1 0; min-height:0}` 是唯一可压缩项，于是被上方**按内容撑高**的
  `.job-tree`（`.tree-body{max-height:24rem}`）与 `.job-files`（`max-height:38vh`）挤到 0；
  `.main-body` 又是 `overflow:hidden`，连滚动都救不回来。
- 补充：窄屏（≤820px）媒体查询里有 `pre.log{min-height:12rem}`，它只保住了内层，外层 `.job-log{overflow:hidden}` 照样把 192px 裁成 9px。

### 15.3 改动（`src/web/static/index.html`）

- `.job-log`：`flex:1 1 auto; min-height:10rem`（保底可见）；`.main-body`：`overflow:hidden` → `auto`（极端小窗整体可滚，不再裁剪）。
- `.job-tree` 固定 `flex:0 0 auto`；`.job-tree .tree-body` 限高 `min(18rem, 22vh)`，在面板内滚动。
- `.job-files` 限高 `min(30vh, 16rem)`，`.file-list` 内部滚动（原本就是）。
- 窄屏媒体查询：`pre.log{min-height:12rem}` → `.job-log{min-height:14rem}`。

### 15.4 验证

| 场景 | 视口 | 目录树 | 进度面板 | 日志区 | 需滚动 |
| --- | --- | --- | --- | --- | --- |
| 桌面（三块全展开） | 1400×900 | 235px | 256px | 177px | 否 |
| 桌面（目录树折叠） | 1400×900 | 37px | 256px | 375px | 否 |
| 窄屏 | 756×469 | 37px | 141px | 224px | 是（内容 570 > 405，整体可滚） |

- 修复前同场景：日志区 9px、`pre` 溢出 192px 且被裁；修复后 `pre` 始终落在 `.job-log` 内（`preOverflowsLog: false`）。
- `pytest` → 97 passed（本次仅 CSS 与版本号，无新增用例）。

## 十六、2.4.9：取消勾选 +「文件进度」可删除（2026-11-16）

### 16.1 需求

用户原话：「1. 已勾选的可以取消勾选 2. 文件进度那里可以删除，删除后可以重新勾选」。
2.4.2 之后勾选只能「加」，加错了只能删任务重建，因此需要一条「退回去」的路径。

### 16.2 两个必须先定下来的语义

| 问题 | 2.4.8 的行为 | 2.4.9 的取舍 |
| --- | --- | --- |
| 谁可以取消 | 目录树里被任何任务占用的条目一律置灰 | 只有 `taken.job_id === 当前任务` 时可取消；别的任务的占用仍置灰（避免把别人的任务删空） |
| 勾选被删光意味着什么 | `selection` 为空 = 下载整个分享目录 | 新增 `job.selection_mode = "explicit"`，为空也写 `selection.json`，子进程读到空集合就**什么都不下** |

- 第二条是这个需求真正的坑：如果沿用「空 = 全部」，用户把三个勾全取消再点开始，会突然开始下整个分享目录（几十 GB）。
  所以 `explicit` 标记必须跟着 `selection` 走：`add_job` / `set_job_selection` / `add_job_selection` / `remove_job_selection` 都置位，
  `start_job` 判 `if selection or explicit:` 才写 `selection.json`（两个键都写全，避免子进程把缺失的键当「没限制」）。
- 子进程侧 `quark_main.load_selection()` 返回三元组 `(file_fids, dir_fids, constrained)`：文件不存在/损坏 → `False`（老语义），
  解析成功 → `True`；`load_share_files()` 见到「已限定 + 两边都空」直接返回空列表，并写日志
  「勾选为空：本次任务不下载任何文件（如需其它文件，请在页面上重新勾选后再开始）」，`main()` 原有的 `if not all_files: return` 负责安全收尾。

### 16.3 后端改动

- `jobs_manager.remove_job_selection(job_id, selection)`：空选择 → `ValueError`；任务运行中 → `RuntimeError("任务正在运行：先暂停任务，再取消勾选")`；
  从 `file_fids` / `dir_fids` 两桶摘除、空桶删键，置 `selection_mode="explicit"` 后 `upsert_job`。
- `jobs_manager._drop_state_files(job_id, fids)`：过滤 `pipeline_state.yaml` 的 `files[]`、同步清 `failed_files` 里对应 path、用剩余 files 重算 `counts`，返回删除条数。
  调用方传的是**并集**：`removed.file_fids ∪ (normalized 里剩下的 file_fids)` —— 因为有些 fid 只在 `pipeline_state` 里、
  已经不在 `selection` 里（例如之前被逐个删过），也要一起清，否则「文件进度」会留下幽灵行。
- `web_app` 新增 `POST /api/jobs/{job_id}/selection/remove`（`JobSelectionRemoveRequest`），返回
  `{"job": …, "removed": {"files": n, "dirs": m}, "state_files_removed": k}`，异常统一转 400。
- 目录取消**不连坐**：取消目录 fid 只是不再从它递归，已经列进 `pipeline_state.files` 的文件要靠「文件进度」逐个删。

### 16.4 前端改动（`src/web/static/index.html`）

- `treeRefreshRow()`：`ownedByMe = taken.job_id === selectedId` —— 自己的勾选可点、title 变「取消勾选：把这个文件/文件夹从任务里移除」，
  他人占用才 `taken` 置灰；继承父目录的 `inherited` 仍禁用。
- `treeRemoveTaken(fid, isDir)`：运行中先提示并 `treeRefreshAll()` 回滚；成功后删 `treeState.taken`、
  按 `removed.files + state_files_removed` 决定要不要过滤 `fileState.files` 并重绘进度面板，再 `refreshJobs()`。
- `removeJobFile(fid, label)`：「文件进度」每行的 `✕`（`.file-row-del`）→ 同一个 remove 接口，成功后过滤进度列表、
  清目录树勾选态、提示「已删除：剧集/c.mp4（可在目录树里重新勾选）」。
- 事件全部走 `#detail` 的委托（`[data-role="file-del"]` 与 `change` 里的 `takenNow` 分支），避免每次渲染重绑。

### 16.5 验证

- `pytest` → **103 passed**。新增 6 个用例：`test_remove_job_selection_drops_fid_and_state`（selection 剩 f2、state 剩 f2/f3、
  `counts == {"failed": 1, "done": 1}`、`share_usage()` 里 f1 消失）、`test_remove_job_selection_all_keeps_explicit_empty`、
  `test_remove_job_selection_requires_targets`、`test_remove_job_selection_rejects_running_job`、
  `test_remove_selection_endpoint_passes_fids`、`test_remove_selection_endpoint_rejects_running_job`。
- 真机浏览器（预演服务器 + 假客户端，j1「预演任务A」/ j2「预演任务B」同一分享）：

| 操作 | 结果 |
| --- | --- |
| 取消勾选 f1（已在 selection 且 done） | 徽标消失、进度行 f1 消失 |
| 取消勾选 fb（只在 pipeline_state 里的 downloading） | 进度行 fb 消失、汇总变「完成 0/1」、提示「已取消勾选：b.mp4」 |
| 点 fc 行 `✕` | 进度面板清空并隐藏、目录树 fc 恢复可选、提示「已删除：剧集/c.mp4（可在目录树里重新勾选）」 |
| 重新勾 f1 点「下载所选」 | 「已加入 1 个文件 / 0 个文件夹；已开始下载」，`jobs.yaml` 里 `selection.file_fids: [f1]`、`selection_mode: explicit` |
| j2 占用的 f2 | 仍置灰「已勾选 · 预演任务B」，点不动 |

- 踩坑（写进文档避免下次重复）：任务有 `total_files > 0` 时目录树默认折叠（`.job-tree.folded .tree-body{display:none}`），
  此时行元素 `getBoundingClientRect()` 全是 0×0，`dsh_browser_click` 会点到 (0,0) 的 `.main-bar` 上，看起来「点了没反应」——
  自动化验证必须先点 `[data-role="tree-toggle"]` 展开。

## 十七、2.5.0：不做「开机自启」开关，改只读探测 + 自退重启（2026-11-16）

### 17.1 需求是怎么收敛的

用户先提（m03097）：「加一个设置开机自启选项，默认打开。开启自启状态下，重启走系统 systemctl /
关闭状态下，重启走 restart.sh；你看看这个合不合理？」
讨论后自己收紧（m03106）：「所以网页上不应该提供"开机自启"的功能，或者说也无法提供，
因为后端服务不应该改系统设置，如果需要开机自启，应该用户手动改，对不对？」

结论：**对，不做开关**。但「知道状态」这件事可以留，于是落到「只读展示 + 重启按托管方式选路」。

### 17.2 为什么不给开关（四条理由）

1. **权限面**：本项目页面是局域网无密码访问，NAS 上又配了 `sudo -n`。加一个勾选框等于给 HTTP 接口一条
   「改系统级配置」的通道，任何能打开页面的人都能改开机行为。
2. **职责边界**：应用不该管理自己的部署形态。unit 文件、`enable` 状态、sudo 规则属于运维 / 打包层
   （fpk 包、compose、systemd）；一旦依赖 `systemctl` + sudo，就绑死在「systemd + 免密 sudo」这一种部署上。
3. **状态漂移**：开关值存 `conf/web.yaml`，用户 SSH 敲一句 `systemctl disable` 之后页面就开始撒谎。
4. **不可测试**：polkit 拒绝、unit 不存在、容器里没有 systemd —— 这些分支只能真机试，写不进单测。

用户原本设想里最危险的一处是「用开关决定重启走哪条路」，实际语义会打架：
`systemctl disable` **只影响开机是否启动，不改变进程当前的托管关系**，而 unit 里是 `Restart=always`。
于是「自启关闭 → 走 `restart.sh`」的真实效果是：`restart.sh` kill 掉进程 → systemd 立刻拉起一份 →
脚本再 `start.sh` 起一份 → 8787 端口冲突、两个进程抢 `state/`。反向（macOS 上按开关走 systemctl）则是 command not found。

### 17.3 只读探测：`src/server/supervisor.py`

纯标准库、不写任何文件、10 秒缓存。判定链：

| 步骤 | 依据 | 失败时 |
| --- | --- | --- |
| 1. 找自己的 unit | `/proc/self/cgroup` 形如 `0::/system.slice/<unit>.service` | 文件不存在（macOS / 非 Linux）→ 跳过 |
| 2. 读 unit 状态 | `systemctl show -p ActiveState -p UnitFileState -p Restart -p FragmentPath -p MainPID <unit>` | 非零码 / `OSError`（没有 systemctl）→ `{}` |
| 3. 确认「就是我」 | `MainPID == str(os.getpid())` 或环境变量 `SYSTEMD_EXEC_PID` 相同 | 都不符 → 退回第 4 步 |
| 4. 回落 pid 文件 | `state/web.pid` 首 token 等于 `os.getpid()` | 不等于 → `unknown` |

`runtime_info()` 返回 `supervisor`（`systemd` / `pidfile` / `unknown`）、`unit`、`unit_file_state`、
`autostart`（`enabled*` → `True`，`disabled*` / `masked*` → `False`，其它 `None`）、`restart_policy`、`restart_hint`。
第 3 步是必要的：容器里 `/proc/self/cgroup` 是 `0::/`，某些 CI 里也可能拿到别人的 unit。

### 17.4 重启的三条互斥路径（`POST /api/web/restart`）

| 探测结果 | 行为 | 返回 |
| --- | --- | --- |
| systemd 且 `Restart=always` | 先回 200，`_schedule_process_exit()` 起 daemon 线程 sleep 0.8s 后 `os._exit(1)`，由 systemd 拉起 | `{ok, mode: "systemd-self-exit", message}` |
| systemd 但 `Restart` 不是 `always` | 不退出，提示手动执行 | 409，detail 含 `sudo systemctl restart <unit>` |
| `pidfile` | 原逻辑：`Popen(["/bin/sh","-c","sleep 1; exec '<restart.sh>'"], start_new_session=True)` | `{ok, mode: "restart-script"}` |
| `unknown` | 不动 | 409「无法识别本服务的启动方式…不会自动重启」 |

为什么 `Restart` 必须校验：若是 `on-failure` 而进程 `exit(0)`，systemd 不会拉起，「点重启」等于停机；
所以宁可 409 让用户敲命令。本机（手动 `uvicorn`、无 pid 文件）落在 `unknown` 返回 409 —— 这是诚实的失败，不是 bug。

### 17.5 前端（`src/web/static/settings.html`）

- 新增「运行环境」面板（Aria2 面板之后、底部按钮之前）：`#runtime-info` / `#autostart-info` / `#autostart-cmd-row` / `#autostart-cmd` / `#btn-copy-cmd`。
- `renderRuntime(h)` 按 `supervisor` 分三支渲染；systemd 分支给 `sudo systemctl enable|disable <unit>` 与复制按钮（`navigator.clipboard`，失败提示手动复制）。
- 「重启 Web 服务」按钮的 `confirm` 文案随 `runtimeMode` 变化：systemd 下明确写「进程会退出，约 5 秒后由 systemd 自动拉起」。
- 页面上**没有**任何自启勾选框；面板末行写明「页面不提供『开机自启』开关：改 systemd 配置是系统操作，Web 接口不代劳」。

### 17.6 验证

| 场景 | 结果 |
| --- | --- |
| 单测 | `115 passed`（新增 `test_supervisor.py` 9 个 + `test_web_restart.py` 4 个）；`compileall -q src` 通过 |
| 本机 macOS（手动 uvicorn） | health `supervisor=unknown`；`POST /api/web/restart` → 409；设置页「运行方式：未识别」、cmd 行隐藏 |
| NAS health | `supervisor=systemd`、`unit=quark_auto_download`、`unit_file_state=enabled`、`autostart=true`、`restart_policy=always` |
| NAS 接口重启 | 200 `mode=systemd-self-exit`；pid 1524853 → 1525040；`systemctl show` → `NRestarts=1`、`ActiveState=active` |
| NAS 页面点按钮 | `#cfg-msg`「正在重启…请稍后刷新」、按钮 disabled；pid 1525040 → 1525677（uptime 4s） |
| NAS 面板渲染 | 「systemd 托管（unit quark_auto_download，Restart=always）」「开机自启：已开启（unit 已 enable）」，命令 `sudo systemctl disable quark_auto_download`，无 checkbox |

### 17.7 边界（明确不做）

- 不改 `enable` 状态、不写 unit 文件、不碰 sudo。
- 不判断「该不该自启」：那是用户的运维决定，页面只如实显示。
- `systemd` 之外只认 `bin/start.sh` 的 pid 文件；Docker、前台终端一律 `unknown` 并让人手动重启（猜错代价是停机）。

## 十八、2.5.1：转存轮询的 `NameError` 与 32003 容量不足（2026-11-16）

### 18.1 现象

部署 2.5.0 之后，用户在 NAS 上的任务（5 个文件 41.2 GB，网盘总容量 10 GiB）反复出现：

```
[ERROR] 转存失败: Stand-up.Comedy.S01.2024.2160p.WEB-DL.H265.DDP2.0-BestWEB/S01E01 - 第01期 上：…mkv | name 'status' is not defined
```

清空网盘（`已用 10 MB / 总 10240 MB`）后立刻重试，第一批还是 NameError，30 秒后转成：

```
查询转存任务状态返回异常: {'status': 400, 'code': 32003, 'message': 'capacity limit[{0}]', …, 'remain_capacity': 1658928801 …}
```

即「刚清空也报容量不足」。表面上是两个故障，实际上是同一个。

### 18.2 根因

`src/server/quark_api.py` 的 `_wait_task_and_get_fids`（原 738-782 行）负责轮询转存任务直到 `data.status` 变成 2（成功）。重构时把

```python
status = data_node.get("status")
if status is None:
    …
    continue
```

合并成了一句只检查、不赋值的判断：

```python
if not isinstance(data_node, dict) or data_node.get("status") is None:
    …
    continue
```

后面的 `if status == 2:` / `if status == 3:` 于是抛 `NameError: name 'status' is not defined`。
对照 `git show HEAD:src/quark_api.py`（旧路径，约 513-545 行）原实现是对的：赋值与判空是两句。
仓库在这段时间有另一个会话并发改代码，无法断定引入者，但工作区当前确实是坏的。

后果链（NAS 实测复现，时间戳同一份日志）：

1. 19:42:57 `网盘可用空间: 10229 MB（已用 10 MB / 总 10240 MB）` —— 用户刚清空；
2. 19:43:01 `转存失败 … | name 'status' is not defined` —— **转存在夸克侧已经成功**，是我们读结果时崩了；
3. fid 没落进状态文件，云端那 8.6 GB 副本变成孤儿；用户再点开始 → 又转存一份；
4. 19:43:36 下一批直接 `code=32003 capacity limit`（`remain_capacity=1658928801` ≈ 1582 MB，装不下 8.6 GB）；
5. 之后 `/1/clouddrive/member` 的 `use_capacity` 一直停在 8657.9 MB —— 就是那份孤儿副本。

所以「容量不足」是 NameError 的次生灾害，不是独立 bug。

### 18.3 改动

| 文件 | 改动 |
| --- | --- |
| `src/server/quark_api.py` | `_wait_task_and_get_fids` 恢复 `status = data_node.get("status") if isinstance(data_node, dict) else None`；保留 `TASK_ABNORMAL_LIMIT = 5` 的异常结构计数；新增模块级 `_explain_task_error(data)` |
| `src/server/quark_api.py` | `_wait_task_and_get_fids` / `_wait_task_done` 的 `code not in (None, 0)` 分支都改用 `_explain_task_error` |
| `src/server/error_classify.py` | `_TEMPORARY_PATTERNS` 加 `r"\b32003\b"`、`r"capacity\s+limit"`、`r"容量不足"` |
| `tests/pytest/test_quark_task_wait.py` | 新增 6 个用例 |

`_explain_task_error(data)`：非 32003 时退回 `_payload_brief(data)`；32003 时输出

```
网盘容量不足（32003 capacity limit，接口报可用约 N MB）：先删掉网盘里已转存的副本、清空回收站，
或等本次下载完成清理后再重试 | <brief>
```

其中 N = `data.data.remain_capacity / 1048576`（实测 1658928801 → 1582 MB，与日志里的 `remaining` 对得上）。

为什么把 32003 归为「临时错误」：它取决于当前空间，腾出空间后同一文件能成功；归为永久错误会被直接跳过，
等于把「暂时放不下」判死刑。而真正的永久错误（404 分享不存在、403 权限、23018 之外的参数错）仍走永久分支。

### 18.4 验证

| 场景 | 结果 |
| --- | --- |
| 单测 | `121 passed`（新增 6 个）；`compileall -q src` 通过 |
| 回归点 | `test_status_2_returns_save_as_fids`：status=2 → 返回 `save_as_top_fids`（改前抛 NameError） |
| 32003 文案 | `test_capacity_limit_is_explained` 断言含「容量不足」「32003」「1582 MB」 |
| 结构异常 | `test_missing_status_gives_up_after_limit`：`data={}` 连续 5 次 → 「结构异常」放弃 |
| 分类 | `test_capacity_limit_is_temporary_error`：`classify_error(...) == "temporary"` |
| NAS 真机 | 重新开始任务 → `发现已转存文件，跳过重复转存…` → `[调度] 本组转存已有序进入待下载队列(ready) +1` → `[下载主] … downloading=1 | 主下载=0`，19:46:02 `下载任务提交成功 … gid=3c2f7a5571ed274a` |

真机这一段同时验证了 2.3.0 的「按名字复用云端副本」：`_my_drive_fid()` 用 `relative_dir` + basename 在
`/alist共享` 下找到了上一个进程留下的 `S01E01 ….mkv`，所以孤儿副本不但没浪费，还直接进了下载队列。

### 18.5 仍未覆盖

- 32003 的 `remain_capacity` 是夸克自己报的可用值，与 `member` 的 `total-use` 会短暂不一致（前者含回收站/临时占用），
  闸门逻辑仍以 `member` 为准，这里只用于报错文案。
- 转存成功后立即崩溃（进程被 kill、断电）仍会留孤儿副本；当前靠「下次按名字复用」而不是「先记 fid 再轮询」，
  后者需要落盘中间状态，暂不做。


## 十九、2.5.2：任务运行中继续追加勾选（2026-11-16）

### 19.1 需求与旧行为

用户报（m03422，逐字）：「nas上正在下载，分享目录那里，无法选择更多后，无法点击"下载所选"」。

旧实现是「先暂停再追加」，两道闸都在前端：

| 位置 | 旧代码 | 后果 |
| --- | --- | --- |
| `treeSyncHeader()` | `btn.disabled = running || (!selFiles && !selDirs)` | 运行中按钮直接禁用，连 hover 文案都写着「暂停后再勾选」 |
| `treeDownloadSelected()` | 开头 `if (jobIsActive(job)) { setTreeMsg("任务正在运行：先暂停…", "err"); return; }` | 即便手动触发也不发请求 |

### 19.2 为什么不能只改前端

`jobs_manager.add_job_selection()` 只把 fid 并进 `jobs.yaml` 的 `job["selection"]`；而下载子进程在启动时
通过 `QUARK_AUTO_DL_SELECTION_FILE` 指向的 `state/jobs/<id>/selection.json` 读一次勾选（`quark_main.load_selection()`），
之后只按进程内的 `ordered_paths` 干活。所以单方面解禁按钮 = 「提示已加入，但永远不下」——
比禁用更糟。真正要动的是子进程的队列。

### 19.3 后端改动

| 文件 | 改动 | 关键点 |
| --- | --- | --- |
| `src/server/jobs_manager.py` | 新增 `write_selection_file(job_id, selection)`；`start_job` / `add_job_selection` 改走它 | tmp（`.<pid>.<time_ns>.tmp`）+ `_write_private_text`(0600) + `os.replace`：子进程随时可能在读，**必须原子**；写失败抛 `ValueError("勾选已记入任务，但写入勾选文件失败…")` |
| `src/server/scheduler.py` | `append_files(files) -> {added, skipped}`；闭包 `_my_drive_fid` 提成方法 `cloud_fid_for` | 已在 `self.jobs` 的路径**任何状态都不覆盖**（done 不会被拉回 pending）；本地同大小 → `done`；云端有副本 → 复用 fid |
| `src/server/pipeline.py` | `selection_file` / `reload_selection` 参数 + `_maybe_reload_selection()` | 指纹（`st_mtime_ns` + `st_size`）不变不重列；失败不推进指纹、同一改动只报一次错；空档期追加也不漏 |
| `src/server/quark_main.py` | 抽出 `list_share_selection()`；`load_selection(strict=True)` | strict 下读/解析失败一律抛错，**绝不**退化成「下载整个分享」 |

「重新列举」这个动作必须放在子进程里：分享列表要带 `stoken`/`share_id`/`target_fid`，这些只有子进程有；
跨进程传函数也只能靠「写一个文件 + 子进程自己重算」。

### 19.4 前端改动

- `treeSyncHeader`：条件改为 `!selFiles && !selDirs`；title 运行中为「把勾选项追加到正在运行的任务（当前批次下完自动接着下）」。
- `treeDownloadSelected`：删掉运行中早退，记 `wasRunning`；成功后仅当**不在运行**才 `POST /start`（运行中再调会报「任务已在运行中，PID=…」），
  运行中提示追加一句「已加入队列，当前批次下完接着下」。
- 运行中**取消勾选**（`treeRemoveTaken`）保持拦截：现在删的是「正在处理的那一份」，语义上应该先暂停。

### 19.5 验证

| 场景 | 结果 |
| --- | --- |
| 单测 | `127 passed`（新增 `tests/pytest/test_runtime_append.py` 8 个） |
| 追加语义 | 已在 `jobs` 的路径进 `skipped` 且状态不变；本地已有同大小 → `done`；云端有副本 → `my_fid` 复用 |
| 指纹侦测 | 文件不变 → `reload_selection` 调用 0 次；改写后 → 队列 {a,b}，`state.yaml` 顺序 `["a.mp4", "b.mp4"]` |
| 失败重试 | `reload_selection` 抛错 → 不抛出、队列不变、下个 tick 再试（attempts == [1, 1]） |
| 预演（本机打桩） | 运行中任务勾 1 文件 → 按钮可点，`POST /api/jobs/jtest/selection/add`，**未**调 `/start`，提示「已加入队列，当前批次下完接着下」 |
| 预演（暂停任务） | 同样操作 → 追加后调 `/start`，提示「已开始下载」 |

预演用的是 `/tmp` 下的临时 app（打桩 `_share_context` 与 `jm`），验证完脚本与进程都已删除；真实 NAS 未部署（m03414「nas上先不动」）。

### 19.6 边界

- 运行中**取消勾选**仍要求先暂停：此刻删的可能正是正在转存 / 下载的那份，先停再改更安全。
- 追加只是「排进队列」：网盘容量不足时依旧「转存一个 → 下完删一个」，不会因为勾得多就并行推进。
- 若追加的文件**必须**下载却被夸克拒绝（23018、被删、权限变更），它会在队列里按普通失败处理，不影响已在下的一批。

## 二十、2.5.3：逐文件 暂停 / 开始 / 删除（2026-11-16）

### 20.1 需求（m03657，逐字要点）

> 在文件进度那里，要求可以对每个文件进行暂停、开始、删除操作。
> 1. 已经下载完成的，不用管
> 2. 已经开始下载的，点击暂停需要将在 aria2 那里也暂停
> 3. 没有开始下载的，点击暂停就不用管 aria2 那里行尾，
> 现在一个文件是一行文字 + 一行进度条，按钮放在右侧，占两行

落地时的语义表：

| 当前状态 | ⏸ 暂停 | ▶ 开始 | ✕ 删除 |
| --- | --- | --- | --- |
| `pending` / `transfer_retry` / `ready` / `retry` | 直接改 `paused`，**不碰 Aria2** | 回 `ready`（已转存）或 `pending` | 移除 Aria2 任务（若有 gid）、删云端副本、摘队列与勾选 |
| `transferring` | **等这次转存结束**再生效 | — | 同上，**延后**到转存结束（此刻打断会留云端孤儿副本） |
| `downloading` | `forcePause(gid)` + 改 `paused`，进度保留 | 回 `ready`（Aria2 任务仍在，提交时走 `unpause`） | `forceRemove(gid)` + 删云端副本 + 摘队列 |
| `failed` | 不处理（已是终态） | 清 `failed_files`、重新入队 | 摘队列 |
| `done` / `cleanup` | 不处理 | 不处理 | **不给按钮**（用户要求「已经下载完成的不用管」） |

### 20.2 为什么必须新增 `file_override.yaml`

三条约束把「只改一个地方」的路都堵死了：

1. **`pipeline_state.yaml` 是子进程的私有输出**，Web 只读（`job_files`）。Web 去改它，会被子进程下一个
   `_persist_state()` 整份覆盖；反过来，子进程改了 Web 也不知道。
2. **子进程重启不读 `pipeline_state.yaml`**：`initialize()` 每次都重新列举分享、按「本地有没有完整副本 / 云端有没有副本」
   重建整个队列。所以「暂停」若只写进 state，重启就丢了。
3. **Web 与子进程是两个进程**，控制指令只能落文件。

于是新增 `state/jobs/<job_id>/file_override.yaml`（0600，tmp + `os.replace` 原子写），Web 写、子进程读：

```yaml
paused:  ["脱口秀/S01E03.mkv"]   # 声明式：集合内的文件必须保持暂停
deleted: ["脱口秀/S01E01.mkv"]   # 持久：重启后也不再下载
retry:   []                      # 一次性意图：点「开始」/失败重开时写，子进程幂等消费
```

`paused` 之所以是**声明式**而不是「一次命令」：子进程可能在同一个 tick 里把这个文件从 `transferring` 推进到
`ready`，甚至提交给 Aria2。只有「每个 tick 重放一次」才压得住——这条同时也是自愈机制（见 20.3）。

「运行中 vs 没运行」两条路：

| 任务状态 | `apply_file_control` 返回 | 谁落地 |
| --- | --- | --- |
| 子进程在跑 | `applied: "pipeline"` | 只写 override，等子进程 ≤1s 后读到 |
| 子进程没跑 | `applied: "immediate"` | Web 直接把 `pipeline_state.yaml` 里这些文件改好（暂停/恢复/摘除），下次启动被 `restart` 语义接住 |

### 20.3 子进程侧（`src/server/pipeline.py`）

| 新方法 | 作用 | 关键细节 |
| --- | --- | --- |
| `_read_file_override()` | 读三个集合 | 文件不存在 → 三个空集；YAML 坏了 → `log.warning` + 返回 `None`（**不推进指纹**，下个 tick 重试，不能因为一个坏文件让整个 tick 崩） |
| `_apply_file_override(force=False)` | 落地意图 | 指纹（`st_mtime_ns`+`st_size`）不变直接返回；顺序固定为 ① 新 `deleted` 逐个删 ② `retry` 逐个恢复 ③ 从 `paused` 里被拿掉的逐个恢复 ④ 把 `paused` 里剩下的逐个压暂停 |
| `_enforce_paused(path)` | 压成 `paused` | 跳过 `paused/done/failed/cleanup`；**跳过 `transferring`**（打断会留孤儿副本，等它变 `ready` 后下一次 tick 自然压住）；只有 `downloading` 且有 gid 才 `aria2_pause`（异常只 `log.warning`，Aria2 挂了不该让暂停失败） |
| `_resume_file(path)` | 恢复一个文件 | 走 `scheduler.resume_file`，成功后 `_persist_state(force=True)` 并打日志 |
| `_drop_file(path)` | 删一个文件 | `transferring` → 进 `_deferred_delete` 直接返回；否则 `aria2_remove(gid)` → 云端副本 `delete_my_fids`（失败只 `log.error` 提示手动清理，不阻塞）→ `scheduler.drop_files` |
| `_flush_deferred_deletes()` | 延后删除 | `_deferred_delete` 里的路径一旦不再是 `transferring/cleanup`，移出集合并真删 |
| `_purge_deleted_cloud_copies()` | 启动补删 | 任务没在跑时用户删的文件只写了 override，云端那几 GB 还占配额；`initialize()` 末尾对 `deleted - _cloud_purged` 各删一次 |

调用点（保证「点了就生效」且不会把刚删的文件又提交出去）：

- `initialize()` 末尾：`_apply_file_override(force=True)` + `_purge_deleted_cloud_copies()` → 重启后暂停态/删除态仍然生效；
- `run()` 主循环：紧跟 `_maybe_reload_selection()` 调 `_tick_file_controls()`（= 读 override + 冲延后删除 + 全量重放 `_applied_paused`）；
- `_download_submit_tick()` / `_retry_submit_tick()` 开头：先 `_tick_file_controls()` 再挑候选，避免把刚被暂停的文件提交给 Aria2；
- `_poll_one_download()` 开头新增守卫 `if job.state != "downloading": return`：本 tick 取到 job 之后它可能刚被暂停/删除，
  不守卫就会走到 `paused → aria2_unpause` 分支，把用户的暂停立刻撤销。

删除云端副本时还需要 `my_fid`：刚重启/刚追加的文件可能还没解析出 fid，`_drop_file` 里退一步用
`scheduler.cloud_fid_for(path)` 现查；两条路都删成功的路径记进 `_cloud_purged`，避免
`_apply_file_override` 与 `_purge_deleted_cloud_copies` 对同一个 fid 删两次（这条是写测试时踩出来的：
断言写成 `[["mine1"], ["mine1"]]` 才发现重复删除）。

### 20.4 调度器（`src/server/scheduler.py`）

- `JobState` 增加 `"paused"`，`counts()` 同步加键，`_log_counts` 输出里多一列 `paused=N`（在 `downloading` 与 `cleanup` 之间）。
- `mark_user_paused(job)` → `paused` + note「已暂停（用户）」；`resume_file(job)` → `paused` 回 `ready|pending`、
  `failed` 回 `ready|pending` 并清 `failed_files`；`drop_files(paths)` → 同时从 `jobs` / `ordered_paths` / `failed_files` 摘除。
- `persist_payload()` 的 `queues` 增加 `paused`，落盘后重启也能看出哪些是用户暂停的（真正的真源仍是 override）。

### 20.5 Web 侧（`src/server/jobs_manager.py` + `web_app.py`）

- `POST /api/jobs/{job_id}/files/action`，body `{"op": "pause|resume|delete", "paths": [...], "fids": [...]}`
  （别名 `suspend` / `start` / `remove`）；任务不存在 404，参数/执行错误 400。
- `apply_file_control()`：op 归一 → 改 override 三集合（互斥：pause 清 `retry`，resume 清 `paused`，delete 清两者）→
  `delete` **总是**把 fid 从 `jobs.yaml` 的 `selection` 与 `selection.json` 摘掉（所以「删了就能重新勾选」），
  没跑时再 `_drop_state_files` + `_drop_state_paths`；`resume` 没跑时 `_reset_state_paths`（`ready|pending`、清 note 与 `failed_files`）。
- `GET /api/jobs/{id}/files` 合并 override：跳过 `deleted`；`paused` 里的文件若 state 不是终态就显示成 `paused`；
  每条补 `paused / can_pause / can_resume / can_delete` 四个布尔（前端据此显示按钮，避免前端自己再推一遍状态机）；
  `counts` / `stats` 都按合并后的列表重算。
- `start_job` 的 env 多一个 `QUARK_AUTO_DL_FILE_OVERRIDE_FILE`，`quark_main` 读它并传给 `DownloadPipeline(override_file=…)`。

### 20.6 前端（`src/web/static/index.html`）

- 行结构：`.file-row`（flex）→ `.file-main`（名字 + meta + 进度条，两行）+ `.file-actions`（⏸ / ▶ / ✕ 竖排，两行高）。
  按钮按 `can_*` 设 `hidden`；`.file-row[data-state="paused"]` 用黄色进度条，`FILE_STATE_LABELS` 加 `paused: "已暂停"`。
- 暂停的行 meta 走专门分支：「已暂停 · 37.5% · 375 B/1000 B」——停下来时也有百分比和已下量，否则看起来像「没下载」。
- 汇总行多一项「已暂停 N」；删除后本地立即把那行摘掉、`treeState.taken.delete(fid)`、重绘目录树，
  提示「已删除：xxx（可在目录树里重新勾选）」；暂停/恢复提示带「（运行中，约 1 秒内生效）」。
- `#detail` 的事件委托统一处理三种 `data-role`（`file-pause` / `file-resume` / `file-del`），
  与既有的目录树委托共用一处，不再各写一份。

### 20.7 验证

| 项目 | 结果 |
| --- | --- |
| 单测 | **149 passed**（新增 `test_file_control.py` 11、`test_file_action_api.py` 3、`test_pipeline_file_override.py` 8） |
| override 持久化 | 文件 0600、键排序；坏 YAML 不抛、不推进指纹（`test_broken_override_file_does_not_break_tick`） |
| 暂停语义 | `ready` → `paused`；`downloading` 暂停后 `progress` 仍 25.0（进度不被清零） |
| 自愈 | 外部把 state 改回 `ready` 后，下一次 tick 又压回 `paused` |
| 延后删除 | `transferring` 时不删、不摘队列；转存结束变 `ready` 后 `_flush_deferred_deletes` 才真删 |
| 启动补删 | `deleted` 的文件在 `initialize()` 里被删云端副本（`[["mine1"]]`，不重复） |
| 运行中 vs 空闲 | 子进程在跑 → 只写 override（`applied: "pipeline"`，state 不动）；空闲 → 直接改 state（`applied: "immediate"`） |
| 预演（本机打桩，真 Chromium） | 每行 `.file-main` 39px == `.file-actions` 39px、按钮列在右侧（列宽 29px）；`done` 行三按钮全 hidden；⏸ → 行变 `paused`、meta「已暂停 · 37.5% · 375 B/1000 B」、按钮换 ▶；▶ → 回 `pending`；✕ → 行消失、汇总「完成 1/4 · 已暂停 1 · 失败 1」 |

预演用的是 `/tmp` 下的临时 app（打桩 `jm.*` 与 `_share_context`，5 行假数据），验证完脚本与进程都已删除；
截图留在 `~/Downloads/quark-file-controls.png`。**真实 NAS 未部署**（用户 m03414「nas上先不动」，那台机器上 S01E01 还在下）。

### 20.8 边界与未验证

- **本地半成品不清理**：`delete` 只 `forceRemove` Aria2 任务，不回删下载目录里的半成品文件。重新勾选同一个文件时，
  Aria2 默认 `allow-overwrite=false` + `auto-file-renaming=true`，可能把新的存成 `xxx.mkv.1`（**未在真机验证**）。
  要彻底干净，删除时该顺带删本地文件，但那需要确认真实路径与 `local_copy_is_intact` 判据，这版没做。
- **全部文件都暂停时进程不退出**：`has_work()` 判的是 `state not in ("done","failed")`，`paused` 也算未完成，
  所以子进程会一直活着（每 30 秒一行计数）。这是有意的——Aria2 任务和进程都在，点「开始」秒恢复；
  代价是「暂停一切」不会释放进程。
- **删云端副本是 best-effort**：失败只 `log.error`「删除网盘副本失败（请手动清理）」，不影响本地队列状态；
  残留的那份仍占 10 GiB 配额，会继续卡住后面的转存。
- **`paused` 不进 `failed_files`**：暂停不是失败，日志里的失败统计不会因为它变化；但 `stats` 的「已下载文件数」也不含它。
- **接口不拦「删已完成」**：「已完成的行不给按钮」只是前端按 `can_delete` 隐藏；直接调接口仍能把 `done` 的文件从列表里摘掉
  （`_drop_file` 对 `done/cleanup` 不删云端副本，本地文件也不动，所以只是观感问题）。要严格可以加一道
  `can_delete` 校验，但那会让「列表中残留一条早就下完的记录」没法清理，暂时保持宽松。
- 逐文件控制与「软暂停/硬暂停」正交：硬暂停会 `_purge_aria2_for_job` 把 Aria2 任务清掉，之后再点某个文件的 ▶，
  它会按 `ready` 重新取直链提交（相当于重下），而不是续传——因为 Aria2 那边的任务已经没了。


