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
- 2.5.4 微调：按钮从「竖排两行」改成「并排一行」，`.file-actions` 用 `flex-direction: row` + `align-items: stretch`，
  整行高度仍跟着左侧两行（实测同高 25px，两按钮各 32×25px、同一 y）；只在 NAS 页面注入候选 CSS 量过矩形后落盘。

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



## 二十一、2.6.0：多套 Aria2 配置 + 任务级选择（2026-11-17）

### 21.1 需求与旧行为

用户要求（m04107）：设置页里能配多个 aria2（点加号添加），创建下载任务时能选用哪一套配置。

| 环节 | 2.5.4 的行为 | 后果 |
| --- | --- | --- |
| 配置 | `conf/aria2.yaml` 顶层 `aria2_host` / `aria2_secret` / `aria2_download_dir` / `download_destination_dir` | 只有一套，换 aria2 要改文件 |
| 子进程 | `quark_main.py` 模块级常量 `ARIA2_HOST` 等，`init_clients()` 用它构造 `Aria2Client` | 一个进程只能连一台 |
| Web 侧 | `jobs_manager._aria2_client_from_config()` 直接读顶层键 | 暂停 / 清理也固定那一台 |

### 21.2 设计取舍

| 问题 | 选择 | 为什么 |
| --- | --- | --- |
| 配置存哪 | 单文件 + `aria2_profiles` 列表 + `aria2_default` | 若按 `<name>.yaml` 拆文件，Web 要枚举目录、每条都要重做一遍掩码与白名单；而且「全局项只有一份」在结构上就成立了（轮询间隔 / 超时 / 队列水位不该每台重复填） |
| 哪些键属于「一套配置」 | 只有 `aria2_host` / `aria2_secret` / `aria2_download_dir` / `download_destination_dir` | 这四项是「连哪台、写到哪」；其余是调度策略，属于部署而不是某台机器 |
| 任务怎么引用 | 存**名字**（`jobs.yaml` 的 `aria2_profile`），不存下标 | 删掉中间一条时下标会整体错位、静默指向另一台；名字失配可以明确退回默认并写日志 |
| 老配置怎么办 | 读时迁移（`profiles_of()` 把顶层四键包成一条「默认」），只在**保存时**改写文件 | 启动时不改用户文件；读写路径复用同一套函数 |
| secret 掩码 | 按**名字**沿用旧值（`merge_incoming`） | 设置页是整表回传，掩码原样保存必须不动真实值；改名等于新建（否则「改个名就继承旧 secret」很意外） |
| 删除配置 | 只改内存，点「保存配置」才落盘 | 误删一条不至于立刻让运行中的任务失去 host（配置在 `start_job` 时才解析） |

### 21.3 后端改动

| 文件 | 改动 |
| --- | --- |
| `src/server/aria2_profiles.py`（新，200 行） | `profiles_of` / `profile_names` / `default_name` / `resolve_name` / `options_for` / `public_profiles` / `merge_incoming` / `validate_profiles`；常量 `PROFILE_KEYS`、`PROFILE_PATH_KEYS`、`DEFAULT_PROFILE_NAME` |
| `src/server/jobs_manager.py` | `add_job(..., aria2_profile="")` 校验存在性并写进 job；`start_job` 解析配置名、设 `QUARK_AUTO_DL_ARIA2_PROFILE`、日志头写 `===== aria2=<名> =====`；`_aria2_client_from_config(job_id)` 按任务的配置名取 host/secret；新增 `list_aria2_profiles` / `aria2_target` / `test_aria2_connection`；`get_all_configs` 掩码 profiles 并隐藏旧四键；`_validate_write_paths` 抽 `_check_write_path` 并逐条校验 profiles 目录；`save_section` 在 `basename=="aria2"` 且带 `aria2_profiles` 时合并 → 校验 → 清旧四键 → 校正默认名 |
| `src/server/aria2_api.py` | 新增 `aria2_get_version()`（`aria2.getVersion`，非 dict 返回 `{}`），供「测试」按钮用 |
| `src/server/quark_main.py` | `ARIA2_PROFILE = resolve_name(ARIA2_FILE_CONFIG, getenv("QUARK_AUTO_DL_ARIA2_PROFILE", ""))`、`ARIA2_CONFIG = options_for(ARIA2_FILE_CONFIG, ARIA2_PROFILE)`，后面的常量逐项取 `ARIA2_CONFIG`；启动日志多一行 `Aria2 配置: X → host` |
| `src/server/web_app.py` | `GET /api/aria2/profiles`、`POST /api/aria2/test`（掩码 / 空 host 用已保存值补全，失败 400「连接失败: …」）、`JobCreateRequest.aria2_profile`、`APP_VERSION = "2.6.0"` |

### 21.4 前端

- 设置页 Aria2 面板改成**卡片列表**：卡头一行是「配置名 input / 默认 radio / 测试 / 删除」，下面是 host、secret、临时目录、完成后目录。卡片全部用 DOM API 构建（`textContent` 赋值），避免把名字或 secret 塞进 `innerHTML` 时被引号打断。
- 「测试」把卡片当前值发给 `POST /api/aria2/test`；host 空、secret 含 `*`（掩码）时后端用已保存的同名配置补全，所以「刚改完还没保存」和「改了名字」两种情况都能测出真实结果（后者会提示连接失败）。
- 删除：先 `collectAria2()` 收回内存，再 splice；删掉的如果是默认，默认自动落到第一条；提示「已删除配置「x」，点「保存配置」后生效」。
- 首页「新增任务」表单加了「Aria2 配置」下拉：打开表单时拉 `/api/aria2/profiles`，选项文字 `名字（默认）`，保留上次选择；没有配置时插入「未配置（去设置页添加）」并 `disabled`，提交时值是空串（= 用默认配置）。
- 任务详情 `metaHtml` 在勾选统计之后加 `· aria2=<配置名>`，只在任务显式选过时显示。

### 21.5 验证

| 项 | 结果 |
| --- | --- |
| 新增 `tests/pytest/test_aria2_profiles.py` | 20 个用例首跑全绿（迁移 / 空配置 / 重名去重 / 默认回退 / `options_for` 合并 / 掩码 / `merge_incoming` / 校验报错 / 保存清理旧四键 / 掩码保留 + 非法默认纠正 / profile 目录越界 / `get_all_configs` / `add_job` 三态 / 四个接口） |
| 全仓 `pytest` | **169 passed**（2.5.4 基线 149 + 20） |
| `compileall` / `node --check` | `src/server` 编译通过；两个页面的 `<script>` 各 1 块（index 55873 字符、settings 15798 字符）语法通过 |
| 设置页真机 | 「＋ 添加」→ 卡片出现且 focus 到 host；填 `http://192.168.1.100:6800/jsonrpc` + token 后点「测试」→「连接成功：aria2 1.36.0，活动 0 / 等待 0」（真连 NAS 的 aria2）；保存后 `conf/aria2.yaml` 只剩 `aria2_profiles` + 全局项，`.bak` 留了旧的单配置 |
| 掩码路径 | 刷新后 secret 显示 `********`，再点「测试」仍成功（后端用已保存的 secret 补全），说明掩码没被当成真值写回 |
| 默认切换 / 删除 | 选另一条为默认 → 保存后 `aria2_default: NAS`；删掉「默认」那条 → 默认自动变 NAS，保存后文件里只剩一条 |
| 建任务带配置 | 首页下拉选 `NAS` → `POST /api/jobs` 落盘 `aria2_profile: NAS`，任务详情显示 `· aria2=NAS`；删除任务后 `state/jobs.yaml` 回空 |

### 21.6 边界与未验证

- **配置是「任务启动时读一次」**：改 host/secret 或换默认对正在跑的任务不生效，必须重新「开始」；`start_job` 把配置名写进环境变量，子进程再解析成具体值。
- **改名等于新建**：`merge_incoming` 按名字沿用 secret，改名后匹配不上，需要在页面上重填 token（否则 aria2 侧鉴权失败会以「连接失败」形式暴露在测试按钮里）。
- **配置名被删后不主动迁移**：任务里仍是旧名，`resolve_name` 在下次 `start_job` 时退回默认并在任务日志写警告；Web 侧的暂停 / 清理同样走 `resolve_name`，不会因为旧名报错。
- **全局项的取舍**：队列水位、并发、超时都是全局一份，多台 aria2 共用同一策略；如果以后要按机器限速，得把 `aria2_queue_*` 也搬进 profile，那会和「一台机器水位过了就停转存」的现有语义冲突。
- **未真机验证「两台 aria2 同时跑不同任务」**：本机只有 NAS 那一台可用，验证的是「一台上配置多套 + 选择生效」；两台真机并发下载需要第二个 aria2 实例。
- **没有做配置排序 / 复制**：卡片按保存顺序排列，删除不支持撤销（只能重新添加并重填 secret）。

### 21.7 2.6.1：完成后目录留空的处理（2026-11-17）

回答「下载临时目录不填会怎样」时发现的相邻问题：

| 字段 | 留空时的旧行为 | 现在 |
| --- | --- | --- |
| `aria2_download_dir`（下载临时目录） | 保存被 `validate_profiles` 拦下（400「Aria2 配置「x」缺少下载临时目录」）；手改文件后子进程启动即 `_require()` 抛「配置项 `aria2_download_dir` 不能为空」 | 不变（这是必须的：没有落盘位置整条流水线没意义） |
| `download_destination_dir`（完成后目录） | 能保存；`os.path.join("", rel)` 得到相对路径，`shutil.move` 把文件挪到**进程 cwd**（部署后是项目根），还会建同名目录；`local_copy_is_intact` 恒 False、清理前「本地没有可信副本就不删云端」的守卫被 `and` 短路跳过 | 留空 = **不移动**，文件留在 `<aria2_download_dir>/<job_id>/`；`FileApi.local_copy_root` 统一给「本地副本在哪」，移动/完成判定/本地已有副本统计/清理守卫都用它 |

改动点（`src/server/file_api.py`）：

- 新增 `local_copy_root` 属性：`self.download_destination_dir or self.download_root`。
- `move_file_to_destination`：目标目录为空时直接 `os.path.isfile(src_path)` 返回，不再做 `shutil.move`。
- `finish_downloaded_file`：目标目录为空时走单独分支，日志写「未配置完成后目录，文件保留在下载目录: …」，返回是否真的存在。
- `local_copy_is_intact` / `get_local_downloaded_sizes_from_dirs`：改用 `local_copy_root`（原来空目标目录直接返回 `False` / `{}`）。
- `cleanup_files_batch` 的删除守卫：`if self.download_destination_dir and not intact` → `if not intact`，没配目标目录也要有本地副本才删云端。

验证：`tests/pytest/test_file_api_move.py` 新增 4 个用例（含 `monkeypatch.chdir` 断言进程目录里不出现文件），全仓 173 passed；NAS 部署后 `/api/health` = 2.6.1、`pytest` 同样 173 passed。


## 二十二、2.7.0：远端 Aria2（2026-11-17）

### 22.1 需求

「朋友有公网 IP 和自己的 Aria2，想共用这个应用下载」。要求按原设计走：远端模式不填下载临时目录、完成后目录留空，
不用单独处理落盘；提交下载时的 UA 与 Cookie 由应用负责（本来就在 `aria2.addUri` 的 `header` 里）。

### 22.2 为什么只改这几处

先划清「本机」与「远端」的边界：本机原来在这条链路上做四件事——
① 给 `addUri` 指定 `dir`；② 用 `tellStatus` 的 `files[].path` 反算相对路径来认领任务；③ 下载完 `shutil.move` 到完成后目录；
④ 清理云端副本前用本地副本大小做「删了不亏」的守卫。远端模式下 ①③④ 都不成立（不知道对方目录、文件不在本机），
② 也不成立（对方返回的是它自己的绝对路径），所以判断点集中在「任务认领键」和「文件完成后的动作」两处，
其余（转存、取直链、队列、暂停、容量闸门）与远端无关，一律不动。

### 22.3 改动表

| 文件 | 改动 |
| --- | --- |
| `src/server/aria2_profiles.py` | `PROFILE_FLAGS = ("aria2_remote",)` + `_truthy()`；`profiles_of` / `merge_incoming` / `options_for` 把标记带进带出（旧单配置形态补 `False`）；`validate_profiles` 对远端跳过「缺少下载临时目录」，`aria2_host` 仍必填 |
| `src/server/aria2_api.py` | `Aria2Client(..., remote=False)`；`aria2_add_url` 远端只发 `{"out": 文件名}`；新增 `_match_key()`，远端按 `basename` 匹配，本机仍按归一化相对路径；`_extract_task_filename` 远端直接取 basename；`aria2_find_task_by_path` / `_remove_by_paths` / `_pause_by_paths` 统一走 `_match_key` |
| `src/server/file_api.py` | `FileApi(..., remote_download=False)`；远端下 `move_file_to_destination` → `True`、`finish_downloaded_file` 只记日志、`local_copy_is_intact` → `False`、`get_local_downloaded_sizes_from_dirs` → `{}`、`cleanup_empty_download_dirs` → `[]`；`cleanup_files_batch` 的本地副本守卫在远端跳过 |
| `src/server/verify_download.py` | 新增 `verify_remote_downloads()`：按 `state in ("done","cleanup")` 且不在 `failed_files` 判成功，`destination_dir` / `local_path` 写「(远端 aria2)」，`actual_size` / `local_exists` 为 `None` |
| `src/server/pipeline.py` | `verify_completed()` 按 `file_api.remote_download` 选报告实现 |
| `src/server/quark_main.py` | `ARIA2_REMOTE = _as_bool(_CONFIG.get("aria2_remote"), False)`；远端不 `_require("aria2_download_dir")`、完成后目录置空；`init_clients` 把 `remote` / `remote_download` 传下去；启动与收尾日志换成远端口径 |
| `src/server/jobs_manager.py` | `_aria2_client_from_config()` 读 `aria2_remote`，远端时 `download_dir=""` |
| `src/web/static/settings.html` | 卡片新增「远端 aria2（只提交下载请求，不管理落盘目录）」勾选框；`syncRemote()` 置灰两个目录、占位改「远端，不用填」、卡片加 `.remote` 描边；`collectAria2()` 提交 `aria2_remote` |
| `src/web/static/index.html` | 任务下拉标注「（默认 · 远端）」 |
| `src/server/web_app.py` | `APP_VERSION = "2.7.0"` |
| `conf/example/aria2.example.yaml` | 增加远端配置示例（只剩 host + secret） |

### 22.4 验证

- 新增 `tests/pytest/test_aria2_remote.py` 7 个：远端 `addUri` 只带 `out` 且 `header` 含 Cookie/UA、本机仍带 `dir`、远端取 basename、本机只认自己目录、远端 `pause_by_paths` 按 basename 命中、`FileApi` 远端五个方法、`verify_remote_downloads` 的 `{total,ok,failed}`。
- `tests/pytest/test_aria2_profiles.py` +3：远端不要求目录、本机仍要求、`aria2_remote` 经 `save_section` 落盘后 `get_all_configs` 回读为 `True`。
- 全仓 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -o addopts= -q` → **183 passed**；`compileall -q src/server` 通过；两个页面的 `<script>` 块 `node --check` 通过。
- 本机浏览器（`http://127.0.0.1:8787/settings`）新增一条远端配置：勾选后 `aria2_download_dir` / `download_destination_dir` 均 `disabled`、占位「远端，不用填」、卡片加 `.remote`；只填 name/host/secret 保存成功，`conf/aria2.yaml` 里该条 `aria2_download_dir: ''`、`download_destination_dir: ''`、`aria2_remote: true`；刷新后回显勾选态；首页下拉显示「朋友的（远端）」；删掉该条再保存，文件回到两条配置。

### 22.5 边界与未验证

- **没有真机连远端**：本机与 NAS 都只有自己的 Aria2，验证停在「配置能存、代码分支正确、报告结构一致」这一层；真正的远端下载需要朋友那台机器。
- **远端看到的直链就是你的账号直链**：`header` 里带着 `Cookie` / `Referer` / `UA`，对方可以在自己机器上重放这个请求，共用前要先接受这一点。
- **直链时效**：夸克直链过期后远端 Aria2 会 403；长时间暂停后再继续，应用不会自动刷新直链，需要先「删除」该文件再重新勾选。
- **远端不给进度以外的东西**：没有本地副本，「已下载过」的判断只存在于 Aria2 侧（应用按名字匹配任务）；对方手工删了文件，应用仍认为已完成。
- **远端模式的清理**：`cleanup_files_batch` 跳过本地副本守卫，意味着「云端副本删不删」只看流水线状态，因此不要在远端模式下把 `download_destination_dir` 当保险绳用——它本来就是空的。

## 二十三、2.8.0：Aria2 地址填错的诊断 + 一轮 review 修复（2026-11-18）

### 23.1 现象

NAS 上新建任务 `j67bc357015`（分享 `https://pan.quark.cn/s/5ba874e85746#/list/share/a970af00fec64d5195a867c7733e9f19`，
勾 3 个文件 `198/199/200 4K.mp4`，共 6796 MB）：转存全部成功（`确认落盘 3/3`），
但每个文件提交下载都打：

```
[下载主] 临时失败，进入重试旁路: 198 4K.mp4 | 1/10 | 501 Server Error: Not Implemented for url: https://dl.660888.xyz/
```

同时三个 Worker 每几秒刷一次 `查询 Aria2 排队数量失败…`、`读取 Aria2 任务快照失败…`，任务永远停在「运行中」。

### 23.2 根因：填的是 AriaNg 网页地址，不是 RPC 地址

在 NAS 上直接打配置里的 host（`aria2_profiles` 的「灰机nas」）：

| 请求 | 结果 |
| --- | --- |
| `GET https://dl.660888.xyz/` | `200`，`<title>AriaNg</title>`（nginx 反代的静态控制台） |
| `POST https://dl.660888.xyz/` / `/jsonrpc` / `/rpc` … | `501`，`text/html`，`Generated by darkhttpd/1.15` |
| `POST https://dl.660888.xyz:6800/jsonrpc`（无 token） | `{"error":{"code":1,"message":"Unauthorized"}}` |
| `POST https://dl.660888.xyz:6800/jsonrpc`（带配置里的 secret） | `{"result":{"version":"1.36.0",…}}` |

即：443 是 AriaNg 网页，RPC 在 6800 端口的 `/jsonrpc`。旧代码把 HTML 501 当普通 HTTP 错误，
`error_classify` 的 `5\d{2}` 又把它判成**临时错误**，于是重试 10 次、每轮再刷一堆查询失败日志——
现象看着像「Aria2 挂了」，实际是地址错，重试多少次都不会好。

### 23.3 修复：让配错误一次就说清楚，并且不要启动

| 文件 | 改动 |
| --- | --- |
| `src/server/aria2_api.py` | 新增 `normalize_rpc_url()`（补 `/jsonrpc`）、`Aria2Error` / `Aria2ConfigError`；`_aria2_call()` 先判 JSON-RPC 再判状态码，HTML 返回抛「RPC 地址不对…AriaNg 网页地址不能当 RPC 用」，`error.code==1` 抛「令牌（rpc-secret）不对」 |
| `src/server/error_classify.py` | 永久错误补 `rpc\s*地址不对` / `不是\s*json-rpc` / `rpc\s*未授权` / `rpc\s*令牌` |
| `src/server/jobs_manager.py` | 新增 `probe_aria2_profile()`；`start_job` 在 Popen **之前**探一次 `aria2.getVersion`（5s），失败抛 `ValueError("Aria2 配置「x」连不上: …")` → 接口 400，页面直接显示 |
| `src/server/web_app.py` | `/api/aria2/test` 成功时回传实际使用的 `host`（前端显示，便于核对是不是刚改的地址） |
| `src/server/pipeline.py` | `_log_aria2_problem()`：同一错误 60 秒只打一条，重复时附「过去 60 秒同错误 N 次，已抑制」 |
| `src/web/static/settings.html` | 测试成功文案带 host |

### 23.4 本轮 review 修掉的问题

四个只读子代理分别看 pipeline/scheduler/verify、web_app/jobs_manager、网络层、两个页面，逐条复核后修复：

数据安全 / 一致性

| 问题 | 后果 | 修复 |
| --- | --- | --- |
| 清理一直失败时 `cleanup` 闸门永久挡住转存（`pipeline.py` 清理分支 + `file_api` 拒绝删云端） | 所有 pending 不再转存，任务永不结束 | `_cleanup_failed_since` + `CLEANUP_GIVE_UP_SEC=600` 后标失败并保留云端副本 |
| 转存回查只按 basename 认领 fid | 目标目录已有同名文件时，清理会删掉**用户原有文件** | 与 `saved_fids` 取交集，认不上按「未生效」处理 |
| 远端 `complete` 就删云端副本 | 远端模式无本地副本，大小不对也照删 | 比对 `totalLength` 与任务登记大小 |
| 提交下载期间用户暂停/删除 | 状态被覆盖回 `downloading`，Aria2 留下无人跟踪任务 | 提交回来锁内复核，必要时 `_discard_submitted_task()` 撤销 |
| `_apply_file_override` 被三个线程并发调用 | 同一次删除发两遍、暂停与恢复交错 | 独立 `self._override_lock` |
| `file_override.yaml` 读-改-写无锁（Web 侧） | 连点/两标签页丢指令，删掉的文件被重新下载 | `apply_file_control` 进 `_index_lock`；前端再把逐文件操作串成一条链 |
| `resume_file` 带着过期 `my_fid` 回 `ready` | 取直链必失败 | note 含「已清理网盘文件」时退回 `pending` 重新转存 |
| `_drop_file` 对 `cleanup` 态跳过删云端 | 云端副本只能等下次启动补删 | 只有 `done` 跳过 |
| `verify_download` 三种正常情况判失败（空完成后目录 / 改名副本 / `cleanup` 收尾） | 下完的文件被写进失败报告，任务标 failed | 传 `local_copy_root` + `resolve_local_copy` resolver；`cleanup` 计入成功 |
| 下载超时从提交时刻算，`waiting`/`paused` 也算 | 队列深或暂停 2 小时后，大批没开始的判超时 | 只对 `active` 计时（`active_at`） |

安全

| 问题 | 修复 |
| --- | --- |
| `delete_job` 不取启动锁 | 与 `start_job` 共用 `_job_start_lock(job_id)`，`rmtree` 放最后；Popen 后任何失败 `proc.kill()` |
| `/api/aria2/test` 允许「用已保存的令牌测试请求体里的陌生地址」 | 换 host 必须重新填令牌；host 必须 `http(s)://` |
| `hmac.compare_digest` 传 str | 改 bytes（非 ASCII 口令不再全站 500） |
| 写路径白名单用 `abspath` | 改逐级 `realpath`（软链绕不出去） |
| `save_section` 无锁、secret 空串无法清空 | 加锁；只有掩码占位算「未修改」 |
| `api_share_tree` 的 `int(size)` 无保护 | `try/except` → 0 |

协议正确性

| 问题 | 修复 |
| --- | --- |
| 远端 `out` 只有 basename | 改传相对路径 `Show/S01E01.mkv`；`_match_key` 保留完整相对路径，`_path_matches()` 兼容早期只给文件名的任务 |
| `list_share_files` 是唯一没 `_is_ok` 的分页接口 | 失败即抛，不再静默返回半截列表 |
| `save_as` 为 `null` 时 `AttributeError` | `or {}` + isinstance |
| `_entries_by_name` 读失败当空目录 | `strict=True` 返回 `None`，清理判失败 |
| `cloud_fid_for` 把失败写进缓存 | 失败不缓存 |
| 阈值可能是 `null`/`1e999` | `quark_main._as_int()` + 前端 `intField()` |
| `_atomic_yaml_write` 残留 `.tmp`；`_read_yaml` 不容错 | finally 清理；坏 YAML 记 warning 返回 `{}` |

前端

- 日志：请求回来校验任务未切换；失败回退「未缓存」标记（原先失败一次就永不重试）
- `jobCache` 同步删除（删掉的任务不再占着详情面板）；「文件进度」分母改用 `file_total`
- 目录树：「下载所选」提交中禁用；切任务丢弃晚到分页；展开失败退回折叠态可重试
- 任务级 开始/暂停/删除 连点保护；逐文件操作串行
- 扫码轮询：世代令牌 `qrChain` + 连续失败退避（3 次才停） + 「生成二维码」禁用与只认最后一次会话

### 23.5 验证

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -o addopts= -q` → **204 passed**
  （新增 `test_verify_download.py` 6 条、`test_aria2_test_api.py` 4 条、`test_pipeline_cleanup.py` 2 条，
  `test_aria2_rpc.py` 9 条覆盖 URL 归一化 / HTML 501 / 令牌错 / 预检拦截；远端 `out` 断言按新语义改写）
- `compileall -q src` 通过；`index.html` / `settings.html` 的 `<script>` 块 `node --check` 通过
- 过程中发现并修掉一处自己引入的缩进错误：`_wait_task_and_get_fids` 的 `save_as` 判空掉出 `if status == 2:`，
  触发 `UnboundLocalError`（4 个用例当场变红，已恢复缩进）

### 23.6 边界与未验证

- **/api/aria2/test 的 SSRF 只挡了「改 host」这条路径**：如果已保存配置本身就是内网地址，测试按钮仍会打过去，
  这是产品语义允许的（用户自己配的地址）。
- **`delete_job` 的锁是进程内锁**：多 worker 部署（`uvicorn --workers N`）仍然挡不住跨进程竞态，
  目前只支持单进程。
- **远端 `out` 语义变了**：2.8.0 之前提交的远端任务只有 basename，`_path_matches()` 用 basename 兜底兼容，
  但两个不同目录的同名文件仍然会互相覆盖（这是 2.8.0 才修的历史遗留）。
- **前端 5s 轮询仍没有单飞 / `visibilitychange`**，多标签页时请求会叠加；只做了「旧响应不覆盖新数据」的守卫。
- **设置页 `fillConfig()` 仍会全量重建 Aria2 卡片**：扫码成功、保存配置后，别的卡片上未保存的编辑会被冲掉。
- **Aria2 RPC 地址本身没有自动纠正**：`normalize_rpc_url()` 只补路径，不会把 443 改 6800——
  端口是用户配置，误判比不判更糟。**NAS 上那条「灰机nas」仍需把 host 改成 `https://dl.660888.xyz:6800/jsonrpc`。**

### 23.7 2.8.1：远端 Aria2「速度一直 0」的真因与远端目录（2026-11-18）

23.6 里那条「NAS 上『灰机nas』仍需把 host 改成 `:6800/jsonrpc`」在 2.8.0 部署时已改完并复测通过
（`POST /api/aria2/test` → `{"ok":true,"host":"https://dl.660888.xyz:6800/jsonrpc","version":"1.36.0"}`）。
改完 host 后用户重开任务，出现新症状：

**现象** 任务 `j67bc357015`（「凡人修仙」，3 个 4K.mp4）转存成功、Aria2 提交成功（三个 gid 都有），
但日志里每个文件固定 `↓ 198 4K.mp4  0.0%  速度: 0 KB/s`，30 秒后
`✗ 下载失败，准备重试 … | Download aborted. | 重试 1/3`，三轮后判失败。

**取证**（NAS 上直连远端 RPC，secret 从 `conf/aria2.yaml` 读、不打印）

| 观察点 | 结果 |
| --- | --- |
| `aria2.getGlobalOption` | `dir=/downloads`、split=64、max-connection-per-server=32、continue=true、save-session=/config/aria2.session |
| `tellActive` | 2 条 `active`，但 `completedLength=0 / totalLength=0`（一个字节都没写） |
| `tellStopped` | 4 条全 `error / code 16 / Download aborted. / len 0 / 2324135706 / dir=/downloads/198 4K.mp4` |
| addUri 探针（每个目录一个 11 KB 页面） | `/downloads` `/mnt` `/media` `/home` `/root` `/` → error 16；`/data` `/download` → error 18；`/var/tmp` `/tmp/quark` `/config` → complete |

即：**对方 aria2 容器里的默认下载目录 `/downloads` 不可写**（只读挂载或无权限）。aria2 拿到直链后
立刻失败，只留下 `errorCode=16 Download aborted.`；而 2.8.0 之前 pipeline 把这个 message 当临时错误，
白重试 3 轮，页面上就是「提交成功但速度一直 0」。与本机直链、Cookie、UA 无关。

**改动**

| 文件 | 改动 |
| --- | --- |
| `src/server/aria2_profiles.py` | 新增 `PROFILE_OPTIONAL_KEYS = ("aria2_remote_dir",)` / `PROFILE_ALL_KEYS`，读写、合并、`options_for` 都带上它（远端目录不过本机白名单） |
| `src/server/aria2_api.py` | `Aria2Client(remote_dir=…)`；远端 `dir = posixpath.join(remote_dir, relative_dir)`；新增 `_ARIA2_ERROR_HINTS`（9/13/16/17/18/19/22/24）与 `describe_task_error()`（附 `errorCode` 原文与 `files[0].path`）、`PERMANENT_ARIA2_ERROR_CODES=(16,18)`、`aria2_get_global_option()` |
| `src/server/pipeline.py` | 下载 `error` 分支先翻译错误，再 `classify_error() == "permanent"` → 直接 `apply_download_to_cleanup(failed=True, keep_cloud=True)`，不重试 |
| `src/server/error_classify.py` | 永久错误补 `aria2 errorcode 16/18`、「无法在下载目录创建/写入」「无法创建下载子目录」 |
| `src/server/jobs_manager.py` | 传 `remote_dir`；`test_aria2_connection` 返回 `dir`（远端目录或对方 `getGlobalOption.dir`）与 `server_dir` |
| `src/server/web_app.py` | `Aria2TestRequest.remote / remote_dir`；`APP_VERSION = "2.8.1"` |
| `src/web/static/settings.html` | 远端卡片多一个「远端下载目录」输入；改 host/令牌/远端目录、勾选远端后 800ms 防抖**自动测试**；保存成功后自动测全部卡片并在 `#cfg-msg` 汇总 |

**测试**

- 新增 `tests/pytest/test_aria2_task_error.py` 10 条：16/18 文案含路径、未知码原样、16/18 → permanent、
  6 与裸 `Download aborted.` 仍 temporary、远端 `dir` 拼接（含子目录 / 空目录 / 尾斜杠）、
  `aria2_remote_dir` 在 `profiles_of/options_for/merge_incoming` 往返（掩码 secret 保留）
- 既有 3 个 fake 补 `**kwargs`（`tests/pytest/test_aria2_profiles.py:311/341`、`tests/pytest/test_aria2_test_api.py:39/69`）
- 全仓 **214 passed**；`compileall -q src` 通过；两个页面 `node --check` 通过

**部署与真机验证（NAS）**

- `rsync` + `sudo -n systemctl restart quark_auto_download` → `/api/health` = 2.8.1 / pid 860070 / python 3.11.2
- 远端目录探针：`/var/tmp/quark` 与 `/config/quark` 都能被 aria2 自动创建并 `complete`，故「默认 `/downloads` 只读、子目录可写」；
  选 `/var/tmp/quark`（真实磁盘；`/config` 是 aria2 的配置卷，怕小）
- 给「灰机nas」填上 `aria2_remote_dir=/var/tmp/quark` 后重启任务：子进程日志出现 `远端下载目录: /var/tmp/quark`，
  三个文件提交成功（gid 45471d6ff3db6558 / 2180d6e19b7abc01 / b048c3dc590f825b）；
  `tellActive` 显示 `len 2099249152 / 2324135706 speed 23299891 path /var/tmp/quark/198 4K.mp4`，
  页面 `198 4K.mp4` 90.3% / 约 28 MB/s，**此前固定 0%**
- 观察：`/api/jobs/{id}/files` 的 `progress/speed` 来自 5 秒一次的 state 落盘，轮询太密会看到 0.0（不是没在下）

**验证时顺带修掉的两个链路缺陷**

| 缺陷 | 根因 | 修法 |
| --- | --- | --- |
| 填了远端目录，实际仍提交 `/downloads` | `src/server/quark_main.py` 构造 `Aria2Client(... remote=ARIA2_REMOTE)` 时没传 `remote_dir`，配置只到界面为止 | 新增 `ARIA2_REMOTE_DIR`、`Aria2Client(remote_dir=…)`，启动日志加 `远端下载目录: …`（空则「(用对方默认目录)」） |
| `POST /api/aria2/test {name}` 返回 `dir=/downloads` | `Aria2TestRequest.remote/remote_dir` 默认 False/""，接口把 `bool(body.remote)` 直接传下去，已保存的远端设置被丢掉 | 两个字段改成 `None` 缺省；同 host 时沿用同名配置，换 host 不继承；`aria2_target()` 一并暴露 `remote/remote_dir` |

**删除后重新勾选不生效（验证中发现的真 bug）**

- 现象：把三个文件在「文件进度」里删掉、再从目录树勾回来并 `/start`，进程 21 秒就报「所有文件下载成功」，
  `/api/jobs/{id}/files` 为空 —— 子进程用 `_applied_deleted` 把它们全过滤了
- 根因：`deleted` 按路径**持久**记在 `file_override.yaml`；`add_job_selection()` 只把 fid 加回 `jobs.yaml`/`selection.json`，
  没撤删除标记；子进程 `self._applied_deleted |= deleted` 又是只增不减，于是「删掉 → 重勾」永远被挡
- 修法：删除时同时记 `deleted_map`（fid → 路径，来自 `pipeline_state.yaml` 的 `files[]`）；
  新增 `forget_deleted_paths(job_id, paths=…, fids=…)`，`add_job_selection()` 对新增的 fid 调用它撤标记；
  子进程 `_apply_file_override_once` 改成 `self._applied_deleted = set(deleted)`（以文件为准，而不是累积）
- 测试：`test_readd_after_delete_clears_marker`、`test_forget_deleted_paths_by_fid_only`、`test_pause_after_delete_drops_mapping`、
  `test_readded_file_is_not_blocked_by_deleted_marker`（用 `_maybe_reload_selection` 走真流程）→ 全仓 **222 passed**

**真机复验又挖出两处「删了勾不回来」（当轮修掉）**

真机序列：`POST /api/jobs/{id}/files/action {"op":"delete",…}` → `POST /api/jobs/{id}/selection/add {"file_fids":[…]}`，
返回 `added={"files":0}` + `duplicates=[{reason:"in_job_done", state:"downloading"}]`，删除标记原样保留 —— 用户视角就是「删了以后勾不回来」。

| # | 根因 | 修法 |
| --- | --- | --- |
| 1 | `share_usage()` 只看 `jobs.yaml` 的 selection 与 `pipeline_state.yaml` 的 `files[]`；删除只摘了 selection，state 里那条 `state=downloading` 要等子进程下一拍才清，于是被 `add_job_selection()` 当成「已在任务中」挡下，`forget_deleted_paths()` 永远走不到 | `share_usage()` 先读该任务的 `read_file_override(jid)["deleted"]`，遍历 state 时跳过这些路径（目录树也不再显示「已勾选」） |
| 2 | 只撤「新增项」的标记：文件删掉后仍留在 selection 里，重新勾选走 `already_in_job` 分支；且 task 刚重启时 `pipeline_state` 为空，`deleted_map` 建不出来（fid 无从对应路径） | `add_job_selection()` 对**本次请求的全部 fid** 撤标记（不只是新增项）；`apply_file_control()` 的 delete 分支在 state 查不到时按「一次操作一个路径 + 一个 fid」一一配对兜底写 `deleted_map`；`forget_deleted_paths()` 找不到映射时回退查 `pipeline_state` |

新增测试 4 条：`test_share_usage_skips_files_deleted_in_progress`、`test_delete_without_state_records_fid_mapping`、
`test_forget_deleted_paths_falls_back_to_state`、`test_readd_existing_selection_also_clears_marker` → 全仓 **226 passed**。
NAS 真机复验：delete → `deleted_map` 正确写入；`selection/add` → `added={"files":1}`，`file_override.yaml` 里 `deleted` 与 `deleted_map` 都已清空。

**边界**

- 远端目录填错（例如 `/data` 存在但不可写）只会在**提交后**由 aria2 报 16/18，现在能一眼看懂，但**没有预检**：
  aria2 的 RPC 没有「测试写权限」的方法，只能靠试下一个小文件。
- 探针在对方机器的 `/tmp`、`/var/tmp`、`/config`、`/tmp/quark` 留下 `__probe_w.html`（11 KB），
  aria2 RPC 删不掉本地文件，需要对方手动清理。
- 远端模式仍然没有本地副本，完成判定只有 `tellStatus` 的 `totalLength` 比对（23.4），
  对方磁盘写满（errorCode=9）现在也会直接判失败并保留云端副本。


### 23.8 2.8.2：本机配置的下载目录也可以留空（2026-11-18）

**需求**（用户 m05833 第 2 点）：「下载路径是不是可以不要考虑是否是远端，简单来说本地也可以不配置下载路径，
也就是用 aria2 服务默认的下载路径」。此前只有远端配置能留空，本机配置留空会在保存时被
`validate_profiles()` 拦下（「Aria2 配置「X」缺少下载临时目录」）。

**改动**

| 文件 | 改动 |
| --- | --- |
| `src/server/aria2_api.py` | 新增 `resolve_download_dir(client, download_dir, *, remote=False)`：远端或已填 → 原样返回；留空 → `aria2.getGlobalOption()["dir"]`；RPC 异常 → `ValueError("没配置下载目录，且读取 Aria2 默认目录失败: …")`；取到空 → `ValueError("没配置下载目录，Aria2 也没有默认目录（aria2.conf 的 dir）")` |
| `src/server/aria2_profiles.py` | `validate_profiles()` 去掉「非远端必须有 `aria2_download_dir`」，只剩 `缺少 RPC Host`；docstring 同步 |
| `src/server/quark_main.py` | 模块级不再 `_require`；`init_clients()` 里 `ARIA2_DOWNLOAD_DIR = resolve_download_dir(...)`，变化时日志「未配置下载临时目录，改用 Aria2 默认目录: …」 |
| `src/server/jobs_manager.py` | `probe_aria2_profile()` 也调 `resolve_download_dir`，失败包成 `Aria2 配置「X」…`，即启动任务前就拦下 |
| `src/web/static/settings.html` | 标签「下载临时目录（留空 = 用 Aria2 默认目录）」、面板说明与远端勾选文案同步 |

**为什么不直接退回相对路径**：`os.path.join("", "a/b.mkv")` 是相对路径，`shutil.move` 与
`os.path.isfile` 都会按**进程当前目录**解析 —— 2.6.1 修的就是这个坑（文件被挪到 `cwd`）。
所以「留空」必须解析成一个真实绝对目录，拿不到就报错，而不是静默降级。

**语义变化**：`DOWNLOAD_DESTINATION_DIR` 在模块级用 `ARIA2_CONFIG.get("download_destination_dir", ARIA2_DOWNLOAD_DIR)`
计算，下载目录留空后默认值变成 `""`，即「下载完不移动、文件留在下载根目录」——与 2.6.1 的「完成后目录留空 = 不移动」一致。

**验证**：新增 5 条用例（已填不动 / 远端不需要 / 留空取默认 / 默认也为空 / 读 RPC 抛错）；
原 `test_local_profile_still_requires_download_dir` 改名 `test_local_profile_may_omit_download_dir` 并反转断言；
`test_validate_profiles_reports_missing_fields` 去掉「缺少下载临时目录」分支；另加 2 条预检用例
（`test_probe_accepts_empty_download_dir`、`test_probe_reports_profile_when_no_default_dir`）→ 全仓 **233 passed**，`compileall` OK。

**NAS 真机验证**（2.8.2 部署后，pid 910946）

| 配置 | probe | `resolve_download_dir(client, "")` |
| --- | --- | --- |
| 飞牛本机（local） | ok / 1.36.0 | `/vol2/@team/家庭共享_薛成韵/下载/aria2`（来自 `aria2.conf` 的 `dir`） |
| 灰机nas（remote） | ok / 1.36.0 | `""`（远端不需要本机目录） |

顺带发现：`飞牛本机` 的 `aria2_secret` 在 `conf/aria2.yaml` 里是**空**的（`len=0`），所以预检一直报
「RPC 未授权」。用 `/var/apps/aria2/var/aria2.conf` 里的 `rpc-secret`（8 字符，仅 `sudo` 可读）验证可用后，
通过 `save_section` 写回该配置（掩码回显为 `****`），两侧 probe 均通过。

**边界**

- 只有本机 aria2 才需要默认目录；远端配置留空仍然是「用对方默认」，不读也不校验。
- 默认目录由 aria2 的 `aria2.conf` 决定，`getGlobalOption` 读到的是**当前运行值**（命令行 `--dir` 覆盖也生效）。
- 下载根目录若在 `allowed_roots` 之外，只影响设置页保存（白名单只管显式填写的值）；默认目录不经过白名单校验，
  与远端模式一致 —— 它由 aria2 自己的配置决定，本应用只读。

### 23.9 2.8.3：临时目录与完成目录都可选（2026-11-18）

2.8.2 只做了「临时目录留空 → 用 aria2 默认目录」，把「完成目录」当成了独立开关。用户澄清真实语义是**两个目录的
组合**（m05989 原文）：「1. 如果有临时目录，就下载到临时目录，此时如果有完成目录，则下载完成后挪到完成目录
2. 如果没有临时目录，且有完成目录，就直接下载到完成目录 3. 如果两个目录都没有，提交任务时就不用管目录的问题
另外，关于远端的问题，和之前保持一致，远端不校验文件，只通过 aria2 接口看文件是否下载了」。

**语义表**（`plan_download_dirs()` 的返回值）

| 临时目录 | 完成目录 | aria2 提交的 `dir` | `mode` | 下完是否移动 | 最终落点 |
| --- | --- | --- | --- | --- | --- |
| 填了 | 填了 | `<临时>/<job_id>` | `temp` | 是 | `<完成>/<相对路径>` |
| 填了 | 空 | `<临时>/<job_id>` | `temp` | 否 | `<临时>/<job_id>/<相对路径>` |
| 空 | 填了 | `<完成>/<job_id>` | `dest` | 是（原地挪一层） | `<完成>/<相对路径>` |
| 空 | 空 | `getGlobalOption.dir` | `default` | 否 | `<aria2 默认目录>/<相对路径>` |
| 任意 | 任意（远端） | 不提交 | `remote` | 否（本机不碰） | 对方机器 |

「完成目录」单独填时之所以还要 `<job_id>` 这一层：`FileApi` 的判定根是 `download_root`，不隔离 job_id 时
两个任务下同名文件会互相认成「本地已存在」。

**本轮修掉的隐藏 bug（2.8.2 引入）**：`Aria2Client.__init__` 会把传入的 `download_dir` 固化成
`self._download_dir`，`download_dir` 属性在 `job_id` 非空时返回 `<base>/<job_id>`。2.8.2 的顺序是
「建客户端 → 读默认目录 → 回写全局」，客户端里已经固化了空串，之后 `addUri` 提交的 `dir` 会退化成相对路径
（相对进程 cwd，正是 2.6.1 修过的坑）。现在 `init_clients()` 先建**不带 job_id 的探针客户端**把目录定下来，
再建带 `job_id` 的真客户端。

**改动**

| 文件 | 改动 |
| --- | --- |
| `src/server/aria2_api.py` | 新增 `plan_download_dirs(client, download_dir, destination_dir, *, remote=False)`，返回 `{download_dir, destination_dir, mode}`；只有 `default` 分支才调 `resolve_download_dir` |
| `src/server/quark_main.py` | `DOWNLOAD_DESTINATION_DIR` 不再默认等于临时目录；`init_clients()` 探针→plan→回写→建真客户端，并记 `DOWNLOAD_DIR_MODE`；`main()` 按 mode 打不同的目录日志 |
| `src/server/jobs_manager.py` | `probe_aria2_profile()` 改调 `plan_download_dirs`：只填完成目录时不再读 aria2 默认目录（原先会白报错） |
| `src/web/static/settings.html` | 面板说明与字段标签改成「可选」，明确三种组合 |

**验证**：`test_aria2_rpc.py` 新增 6 条 —— 四种组合、远端忽略两个目录、
`test_probe_does_not_need_aria2_default_when_dest_set`（FakeClient 的 `getGlobalOption` 直接 `raise AssertionError`，
证明只填完成目录时根本不会去读默认目录）→ 全仓 **239 passed**，`compileall` OK，设置页 `node --check` 通过。

### 23.10 2.8.4：目录语义收口（`<job_id>` 只当暂存、双空不提交 dir）（2026-11-18）

**需求**（用户 m06081 逐字，三个疑问）：
「`<临时>/<job_id>` 应用不用 job_id 吧」
「临时目录和完成目录都没有填，是不是就不提交，不用管是否是本机。」
「是否是远端只涉及到是否去最终落点校验文件，对吗」

三条全部成立，于是把 2.8.3 的「组合」再收一层语义。

**23.10.1 语义**

| 组合 | 提交给 aria2 的 dir | 最终落点 | 本机行为 |
| --- | --- | --- | --- |
| 有临时目录 | `<临时>/<job_id>/<相对目录>` | 完成目录 `<完成>/<相对>`；没填完成目录则 `<临时>/<相对>` | 下完搬文件、校验本地副本、据此决定能否删云端副本 |
| 只有完成目录 | `<完成>/<job_id>/<相对目录>` | `<完成>/<相对>` | 同上（同一目录内挪一层去掉 job_id） |
| 两个都不填 | **不带 dir**（用 aria2 自己的默认目录） | 由 aria2 决定，本机不知道 | 不扫描、不移动、不校验，完成只看 aria2 状态 |
| 远端 aria2 | 不带 dir（`aria2_remote_dir` 有值则带） | 对方机器 | 同上（本机不参与） |

`<job_id>` 之所以还留着：同一分享加成两个任务时同名文件会互相覆盖（aria2 还会自动改名成 `.1`），
且 `prune_empty_local_dirs(download_root)` 递归清理空目录时可能删掉共享临时目录里用户自己的空文件夹。
它只是「下载中的暂存区」，下完就挪走。

**23.10.2 代码**

| 文件 | 改动 |
| --- | --- |
| `src/server/aria2_api.py` | `plan_download_dirs(download_dir, destination_dir, *, remote=False)` 去掉 client 参数，mode ∈ temp/dest/none/remote；删除 `resolve_download_dir()`（不再需要读 `getGlobalOption`）；`Aria2Client.download_dir` 在根目录为空时返回 `""`（不再拼出裸 job_id 相对路径）；`aria2_add_url` 无目录时提交 `{"out": <相对路径>}` |
| `src/server/file_api.py` | 新增 `local_verifiable` 属性（`bool(aria2_download_dir) and not remote_download`）；`download_root` / `get_local_downloaded_sizes_from_dirs` / `move_file_to_destination` / `resolve_local_copy` / `local_copy_is_intact` / `cleanup_empty_download_dirs` 按它短路；`finish_downloaded_file` 新增「未配置下载目录，按 Aria2 状态判定完成」分支；`cleanup_files_batch` 的本地副本守卫改成 `if self.local_verifiable and not ...` |
| `src/server/pipeline.py` | `verify_completed()` 与「complete 后大小校验」的 `remote_download` 判断改成 `not local_verifiable`；日志/备注措辞去掉「远端」（同一分支也覆盖本机不知道落点的情况） |
| `src/server/verify_download.py` | `verify_remote_downloads` 文档改为「文件不在本机时（远端，或未配置目录）」 |
| `src/server/quark_main.py` | 删掉 probe_client；`plan_download_dirs(...)` 直接算；mode 日志改 temp / dest / none / remote（temp 且完成目录为空时提示「下完挪到 <临时>/」） |
| `src/server/jobs_manager.py` | `probe_aria2_profile()` 只探 `getVersion`（目录不参与预检）；`_aria2_client_from_config()` 的 `or "/tmp"` 兜底去掉 |
| `src/web/static/settings.html` | 面板说明改成「两个目录都可不填」，标签「下载临时目录（可选）」「完成后目录（可选，留空 = 留在临时目录根下）」 |

**23.10.3 验证**

- 新增 `tests/pytest/test_local_verifiable.py` 4 条：无目录时 `local_verifiable` 为假、`download_root` 为空、
  扫盘为空、`move_file_to_destination` 直接 True、`finish_downloaded_file` 直接 True、
  `cleanup_files_batch` 不再被「本地副本缺失」拦住（仍然先删再回查）、
  只填临时目录时文件从 `<临时>/<job_id>/movie/a.mkv` 挪到 `<临时>/movie/a.mkv`
- `tests/pytest/test_aria2_remote.py` 加 2 条：无目录时 `aria2.addUri` 的 options 没有 `dir` 且 `out` 保留子目录；
  `Aria2Client.download_dir` 不会拼出裸 job_id
- `tests/pytest/test_aria2_rpc.py`：删掉 5 条 `resolve_download_dir` 用例与「没有默认目录」预检用例，
  plan 用例改成无 client 签名，新增「plan 不该连 Aria2」「预检不该读 getGlobalOption」
- 全仓 **239 passed**，`compileall -q src` OK

**23.10.4 边界**

- 两个都不填时，本机确实不知道文件落在哪：清理云端副本只能信 aria2 的 `complete`（与远端模式同一风险等级）；
  `totalLength` 与文件大小不符时仍判失败并保留云端副本。
- 下载目录为空时 `<临时>/<job_id>` 这层暂存不存在，因此「下完再挪」也做不了——最终文件由 aria2 直接写在默认目录里。

---

### 23.11 2.8.5：文件进度给「0 字节停摆」打标记（2026-11-18）

**23.11.1 现象与需求**

NAS 上 `凡人修仙` 任务的三个 4K 文件转存成功、提交下载成功，但进度一直 `0.0% · 速度: 0 KB/s`，
30 秒后 `✗ 下载失败，准备重试 … Download aborted.`，重试 3 次后判失败。日志里没有任何一行说明
「为什么一个字节都没下来」，页面上更看不出来。用户要求：**0 字节停摆 5 分钟，在下载行那里做一个标记**。

**23.11.2 实现**

| 位置 | 改动 |
| --- | --- |
| `src/server/pipeline.py` | 模块级 `ZERO_BYTES_STALL_SEC = 300` 与 `track_zero_bytes(task, completed, total, now)`：`completedLength` 连续为 0 时写入 `zero_since`，一旦有字节 `pop` 掉并重新计时；时钟回拨（`zero_since > now`）以本次为准 |
| `src/server/pipeline.py` | `_poll_one_download()` 的 active 分支调 `track_zero_bytes`，连续 ≥300 秒且未告警过时打 `⚠ <文件> 已连续 N 秒 0 字节：直链可能被 CDN 拒绝（412/403）或落盘目录不可写`（`stall_logged` 去重，恢复后清除） |
| `src/server/scheduler.py` | `persist_payload()` 每文件多一个 `zero_since`（epoch 秒），不额外发 RPC |
| `src/server/jobs_manager.py` | `ZERO_STALL_SEC = 300`；`job_files()` 只在「任务 running + state=downloading + downloaded=0 + total>0」时算 `stalled_sec = now - zero_since`，输出 `stalled_sec` 与 `stalled` |
| `src/web/static/index.html` | `.file-stall` 徽标（`.file-row-head` 内、名称与 meta 之间，默认 `hidden`），文本 `⚠ 0 字节 N 分钟`，hover 指向 412/403/23018 与落盘权限；只渲染后端字段 |

**23.11.3 界面**

```
Show/S01E01 停摆.mkv  [⚠ 0 字节 13 分钟]   下载中 · 0.0% · 0 B/8.00 GB      ⏸ ✕
Show/S01E02 正常.mkv                       下载中 · 37.5% · 3.00 GB/… · 2.2 MB/s  ⏸ ✕
```

**23.11.4 验证**

- `tests/pytest/test_stall_marker.py` 11 条：起点只记一次、累加、有字节清零后重新计时、`total<=0` 不计、
  时钟回拨重置、`persist_payload` 透传 `zero_since`（缺省 0.0）、满 5 分钟才 `stalled`、不到阈值（~60s）、
  已有字节、任务未 running、`total=0`、暂停行按 paused 展示不标
- 全仓 **250 passed**，`compileall -q src` OK，`index.html` 脚本块 `node --check` OK
- 真浏览器（本地预演服务，假数据）：停摆行徽标 112×17、其余 4 行 `hidden`（宽高 0）、行高 37 vs 36、
  文件列表 `scrollWidth == clientWidth`（686）无横向溢出；截图 `~/Downloads/quark-stall-badge.png`

**23.11.5 边界**

- 只做显示：不会自动暂停/重试，也不会去探测 412（那需要额外下载请求）。
- 任务被用户暂停后不再判断（`state` 已覆盖成 `paused`）；任务进程退出后 `job_files` 的 `job_running` 判假，
  红标自动消失，避免状态文件停在 `downloading` 时一直挂着。
- 阈值两处常量（`pipeline.ZERO_BYTES_STALL_SEC` 决定日志与起点，`jobs_manager.ZERO_STALL_SEC` 决定红标），
  改的时候要一起改。
- 「0 字节」判定用 aria2 的 `completedLength`：远端 aria2 报了 completed 但文件其实没落盘（errorCode 16）
  也能标出来，因为此时 `completedLength` 仍是 0。

### 23.12 2.8.6：设置页的目录字段收成两套（远端 / 本机互斥）（2026-11-18）

**23.12.1 现象与诉求**

用户：「1. aria2是远端：可以填下载目录 2. aria2不是远端：可以填临时下载目录和下载目录 我现在可以看到三个输入框，
理论上最多只有两个」。截图里一张远端卡片同时出现「远端下载目录」「下载临时目录」「完成后目录」三个输入框。

**23.12.2 根因**

`src/web/static/settings.html` 的 `aria2Card()` 把三个目录字段都 append 进卡片 DOM，远端模式只做两件事：
给两个本机目录 `input.disabled = true`（配合 `.aria2-card.remote input[...]:disabled { opacity: .45 }` 置灰）、
把 `aria2_remote_dir` 的 label/input 设 `hidden`。也就是说「本机目录在远端模式下仍然占位」——`disabled` 只挡输入，
不挡显示，于是三个框都看得见。

**23.12.3 改动（纯前端，后端与 yaml 格式未变）**

| 位置 | 改动 |
| --- | --- |
| `settings.html` CSS | 删掉两条只为置灰服务的 `.aria2-card.remote input[data-field="aria2_download_dir"]:disabled, … { opacity: .45 }` |
| `aria2Card()` | host/secret 的 `fields` 循环提到前面；新增 `dirFields` + `addDirField(key, label, placeholder)`，三个目录字段都走它，label 与 input 一起登记 |
| 目录标签 | 远端：`下载目录（对方机器上的绝对路径；留空 = 用对方的默认目录）`；本机：`临时下载目录（可选）`、`下载目录（完成后移入；留空 = 留在临时目录根下）` |
| `syncRemote()` | 改成按组 `hidden`：远端时藏起 `aria2_download_dir` / `download_destination_dir` 的 label+input，只留 `aria2_remote_dir`；本机时反之 |
| 值保留 | 隐藏字段仍被 `collectAria2()` 读到并保存，切回原模式还能看到原来的目录，不会因为藏起来被清空 |

**23.12.4 为什么不是「隐藏本机两个、只显示远端一个」一刀切**

两种模式的目录语义不同，不能共用一个输入框：远端目录是**对方机器上的绝对路径**，且不过本机 `allowed_roots`
（`aria2_profiles.PROFILE_PATH_KEYS` 只含两个本机目录）；本机的「临时目录 / 完成目录」是两个阶段。
所以只能「同一组字段、按模式整组显示」，用一个字段承载三种含义会回到「留空到底等于什么」的老问题。

**23.12.5 验证**

| 场景 | 结果 |
| --- | --- |
| 本机模式的卡片（本机 conf 两条都是本机） | 可见目录输入框 **2** 个：`临时下载目录（可选）`=/downloads、`下载目录（完成后移入…）`=/downloads_done，`aria2_remote_dir` 不可见 |
| 勾「远端 aria2」 | `visibleDirInputs = 1`，只剩 `下载目录（对方机器上的绝对路径…）`，卡片 class `aria2-card remote` |
| 取消勾选 | 回到 2 个，标签与取值同前 |
| 语法 | 抽 `<script>` 块 `node --check` 通过 |
| 测试 | 无后端改动，`pytest` 仍 **250 passed** |

**23.12.6 边界**

- 远端卡片里被藏起来的本机目录值仍会被保存（`_validate_write_paths` 仍会校验它们）——如果用户先在本机模式填了一个
  白名单外的目录再切远端，保存依旧会被 400 拦下。这是有意保守：切回本机模式就能立刻用上，不必重填。
- 页面上「远端」与「本机」互斥只是显示层，`conf/aria2.yaml` 两种字段可以并存；后端以 `aria2_remote` 决定用哪套。
### 23.13 2.8.7：非远端时那句「对方机器上的绝对路径」不再显示（2026-11-18）

用户反馈（m06718，逐字）：「下载目录（对方机器上的绝对路径；留空 = 用对方的默认目录） 。 当不是远端时，不应该显示这句话」。

#### 23.13.1 根因

- `src/web/static/settings.html:59` 有 `label { display: block; font-size: .8rem; color: var(--muted); margin: .7rem 0 .3rem; }`。
- 2.8.6 用 `label.hidden = true` / `input.hidden = true` 切换两组目录字段：`<input>` 没有声明 `display`，
  所以 `hidden` 的 UA 规则 `display: none` 生效；`<label>` 被作者样式强行 `display: block`，`hidden` 失效。
- 结果：远端那句标签一直占一行文字，只有下面的输入框被藏起来。
- 2.8.6 的验收脚本只统计「可见的 input 数量」（`visibleDirInputs`），没有统计 label，所以没抓到。

#### 23.13.2 改动

| 文件 | 改动 |
| --- | --- |
| `src/web/static/settings.html` | `.aria2-card` 样式区新增 `.aria2-card label[hidden] { display: none; }`，注释说明为什么要压过 `label { display: block }` |
| `src/server/web_app.py` | `APP_VERSION = "2.8.7"` |

首页无需改动：同类问题早有 `.file-stall[hidden] { display: none; }`（`src/web/static/index.html:354`），且首页没有 `label {}` 规则。

#### 23.13.3 验证

本机 dev server（8787）实测，逐个 label 读 `hidden` / `getComputedStyle().display` / 高度：

| 场景 | 远端标签 | 临时下载目录标签 | 下载目录标签 |
| --- | --- | --- | --- |
| 本机配置（未勾远端） | hidden=true, display=none, h=0 | block, h=18 | block, h=18 |
| 勾上远端（同一张卡片，class 变 `aria2-card remote`） | block, h=18 | hidden=true, display=none, h=0 | hidden=true, display=none, h=0 |

无后端改动，`pytest` 仍 **250 passed**。
### 23.14 2.8.8：目录面板可整棵勾选 + 文件行点名字即勾选（2026-11-18）

用户反馈（m06803，逐字）：「关于凡人修仙那个任务，我已经将199的下载任务删了，现在想重新选上后下载，但是现在无法重新选择"下载所选"按钮无法点击。」

用户随后的需求（m06832，逐字）：「将"分享目录"改成"目录"，"目录"可选，选择后就可以点击下载，就是下载整个目录」。

#### 23.14.1 现场核查（NAS 任务 j67bc357015）

- `selection.json` 只剩 `198`/`200` 两个 fid（`199 4K.mp4` 的 `0d6da9f7…` 已被删除动作摘掉），
  `file_override.yaml` 里 `deleted: [199 4K.mp4]`、`deleted_map` 有对应记录，`pipeline_state.yaml` 的 `files` 为空（进程已停）。
- 浏览器实测：`199 4K.mp4` 那一行 `checked=false / disabled=false / 无 taken 徽标`，点复选框后按钮立刻从禁用变可用。
  也就是说后端与勾选状态都正常，用户被挡住的是**交互入口**：只有那个小复选框能勾，点文件名没有任何反应，
  加上没有「整个目录」的入口，删完一个文件想重下时很难找到能点的地方。

#### 23.14.2 改动（只动 `src/web/static/index.html`）

| 位置 | 改动 |
| --- | --- |
| 标题栏 | 「分享目录」→「目录」；默认摘要「勾选「目录」= 下载整个目录」；新增 `input[data-role="root-check"]`（整个目录） |
| `treeSyncHeader()` | 同步根勾选框：`checked = 已勾选根目录 or 已在本任务里`，被别的任务占用时 `disabled` 并提示归属 |
| 标题栏点击 | `ev.target.closest(".tree-check")` 时不再触发折叠 |
| `change` 委托 | 新增根勾选框分支：勾上 = `selectedDirs.add(rootFid)`（提交为 `dir_fid`，整棵目录）；取消时若根目录已在本任务勾选 → `treeRemoveTaken(rootFid, true)` |
| 行点击 | 文件行点名字/尺寸 = 勾选或取消（`tree-badge` 与置灰行除外），复选框仍在 |

根目录被勾选后，所有子行经由既有的 `treeInheritedFrom()`（沿 `treeState.parent` 上溯）显示为「已勾选 + 置灰」，
不会重复提交子项；`treeSelectionPayload()` 只带一个 `dir_fid`，与「勾文件夹 = 整棵子树」同一套后端语义，不需要改接口。

#### 23.14.3 验证

- `node --check`（抽出 `<script>` 块）通过；`pytest` **250 passed**（无后端改动）。
- NAS 2.8.8 真机（任务「凡人修仙」，分享 5ba874e85746）：标题 「目录」、标题栏勾选框存在；
  勾上 → 全部 `.tree-row` 的 `checked=true / disabled=true`、`下载所选` 可用、摘要「本次已勾选 0 文件 / 1 文件夹」；
  取消 → 全部恢复可点、按钮回到禁用；点 `199 4K.mp4` 名字 → 该行选中、按钮可用（用户原先卡住的场景）。
### 23.15 2.8.9：「文件进度」每一行都能删，包括已完成的（2026-11-18）

用户需求（m06960，逐字）：「所有进度都可以点击删除，比如已经完成的任务，只要是不在进度中的，都可以选择并下载」。

#### 23.15.1 旧行为与问题

2.5.3 引入逐文件控制时，按钮是按状态给的：`done` / `cleanup` 的行「已完成，不用管」，一个按钮都不给。
理由是「已经下完的文件没什么好操作的」，但漏掉了「从任务里摘掉这条记录」这个动作：

- 记录摘不掉，fid 就一直留在 `selection.json` 与 `share_usage()` 的汇总里，目录树里这个文件永远是
  「已勾选 · 本任务」，无法重新勾选；
- 于是「下完之后换个目录重下」「当初只是试下一集，现在想删掉记录」这两种场景都无解，
  只能整个任务删掉重建（会连带丢掉其它文件的状态）。

#### 23.15.2 语义：删的是「任务里的记录」，不是磁盘上的文件

| 层 | 删除动作 | 已完成（`done` / `cleanup`）时的差异 |
| --- | --- | --- |
| `file_override.yaml` | 追加到 `deleted`，写 `deleted_map`（fid → 路径） | 无差异（同样写，子进程重启后也不会再下） |
| `selection.json` / `jobs.yaml` | 摘掉 fid | 无差异 |
| Aria2 | `forceRemove` 对应任务 | 已完成时任务已结束，通常是空操作 |
| 网盘转存副本 | `_drop_file()` 删云端副本 | **跳过**（`state == "done"` 时不删，避免删掉已落盘对应的网盘文件） |
| 本地文件 | **不动** | **不动**（这就是「删记录不删文件」的核心） |

删除后 `share_usage()`（先收 selection，再用 `pipeline_state` 覆盖，最后跳过 `override.deleted`）不再把它算成占用，
所以「目录」里这个文件重新可选；但本地文件还在的话，重新勾选后 `initialize()` 会按「本地副本已存在且大小一致」
直接判 `done`——想真正重下必须先删本地文件（README 注意事项已写明）。

#### 23.15.3 改动

| 文件 | 改动 |
| --- | --- |
| `src/server/jobs_manager.py` | `job_files()` 里 `"can_delete": True`（原 `st not in ("done", "cleanup")`）+ 注释说明语义 |
| `src/web/static/index.html` | `fileRowFor()` 的删除按钮 title 按状态区分；`doFileAction()` delete 分支按 `wasDone` 选择提示文案（已完成时强调「本地文件保留在磁盘上」） |
| `tests/pytest/test_file_control.py` | `test_can_flags_per_state` 的 done/cleanup 期望改 `(False, False, True)`；新增 `test_delete_done_file_frees_it_for_reselect` |

后端删除链路本来就和状态无关，因此这是「放开一个前端/接口层的限制」，不是新增逻辑：
`apply_file_control()`（`src/server/jobs_manager.py:892`）→ 运行中 `pipeline._apply_file_override_once()`
（`src/server/pipeline.py:198`）→ `_drop_file()`（`src/server/pipeline.py:266`）→ `scheduler.drop_files()`
（`src/server/scheduler.py:341`，任何状态都能摘）。

#### 23.15.4 验证

- `pytest` **251 passed**。新增用例断言：删 `done` 后 `job_files()` 为空、`selection == {}`、
  `deleted == {"a/done.mp4"}`、`share_usage("j1") == {}`，随后 `add_job_selection()` 能重新加入
  （`added == {"files": 1, "dirs": 0}`、返回的 `deleted` 为空 = 标记已被清掉）。
- 本机真浏览器（端口 8787，假任务 `je374a6bdb5`，手写 4 行状态 done/cleanup/downloading/failed）：
  - `/api/jobs/{id}/files` 四行 `can_delete` 全为 `true`；页面上 done/cleanup 行只有 ✕，downloading 行 ⏸+✕，failed 行 ▶+✕。
  - 点 done 行的 ✕ → 行消失、提示「已删除记录：Show/S01E01.mkv（本地文件保留在磁盘上，可在「目录」里重新勾选）」，
    `counts` 从 4 项变 3 项；再点 cleanup 行的 ✕ 后刷新页面，两条记录都没有回来。
  - `file_override.yaml` 最终为 `deleted: [Show/S01E01.mkv, Show/S01E02.mkv]`、
    `deleted_map: {f-clean1: …, f-done1: …}`。
- 验证用假任务与 `state/`、`log/`、缓存目录已全部清理。

#### 23.15.5 边界

- `done` 状态不删网盘转存副本，配额要等下次清理（`cleanup_files_batch`）或手动删；这是刻意保守的选择。
- 删记录不会碰本地文件与 Aria2 的 `.aria2` 控制文件；若当初没下完（`failed`/`downloading`），
  半成品仍在下载目录里，重新勾选时 Aria2 的 `continue=true` 通常会续传（未在真机逐一验证）。
- 删除仍是「运行中约 1 秒生效」（子进程每 tick 读 `file_override.yaml`），Web 端先本地过滤再重刷一次接口。

### 23.16 2.8.10：「目录」树缩进统一成固定台阶（2026-11-18）

用户反馈（m07146，逐字）：「目录一行和下面的行，缩进不够，下面的行应该再往右边一点点，整体树形结构缩进应该一致」。

#### 23.16.1 实测：改前「差不多但不齐」

在 NAS 页面上量 `getBoundingClientRect()`（1rem = 16px）：

| 元素 | 改前 | 说明 |
| --- | --- | --- |
| `.job-tree-head .tree-check` | 53.8px | 标题栏 padding-left .7rem + `.job-tree-caret` .9rem + gap .45rem |
| 第 1 层 `.tree-row .tree-check` | 57px | 只比标题栏右 3.2px，视觉上「目录」与第 1 层像同级 |
| 标题栏光标宽 / 行内光标宽 | 14.4px / 16px | `.job-tree-caret` 与 `.tree-caret` 各自写死 |
| 标题栏 gap / 行 gap | 7.2px / 5.6px | 同上 |
| 行内勾选框 | 带 UA 默认 margin（约 4px） | `.job-tree-head .tree-check` 被单独 `margin: 0`，两处不同源 |

结论：每层「台阶」只有 .9rem（14.4px），且第 1 层与标题栏几乎重叠，所以看起来「缩进不够、不一致」。

#### 23.16.2 改动（只动 CSS，不改 DOM/JS）

`src/web/static/index.html`：

| 位置 | 改动 |
| --- | --- |
| `.job-tree` | 新增 `--tree-caret: 1rem; --tree-gap: .35rem; --tree-step: 1.15rem; --tree-base: 1.5rem;` |
| `.tree-row` | `gap: var(--tree-gap)`；`padding-left: calc(var(--tree-base) + var(--depth) * var(--tree-step))` |
| `.tree-caret` | `width: var(--tree-caret)`（目录行的 button 与文件行的 span 同宽） |
| `.job-tree-head` | `gap: var(--tree-gap)` |
| `.job-tree-caret` | `width: var(--tree-caret)` |
| `.tree-check` | 统一 `margin: 0`（消掉 UA 默认外边距，标题栏那条单独覆盖不再需要区分） |

`--tree-base: 1.5rem` 让第 1 层相对「目录」正好退一格：标题栏内容起点 = 11.2px(padding) + 16px(光标) + 5.6px(gap) = 32.8px；
行内容起点 = 5.6px(body padding) + 24px(base) + 16px + 5.6px = 51.2px，差 18.4px = 1.15rem。

#### 23.16.3 验证

本机页面（窗口 756×469，用 `treeRowEl()` 造 4 层假行，避免动真实任务）：

| 层级 | 勾选框左边缘 | 光标左边缘 | 与上一层差 |
| --- | --- | --- | --- |
| 标题栏「目录」 | 53.8px | 32.2px | — |
| 第 1 层（目录） | 72.2px | 50.6px | +18.4px |
| 第 1 层（文件） | 72.2px | 50.6px | 同层重合 |
| 第 2 层 | 90.6px | 69.0px | +18.4px |
| 第 3 层 | 109.0px | 87.4px | +18.4px |

- `node --check`（抽出 `<script>` 块，61643 字符）通过；`pytest` **251 passed**（无 Python 改动）。
- 代价：每层缩进从 14.4px 增到 18.4px，第 3 层名字右侧少约 12px；窄面板下 `.tree-name` 本来就有 ellipsis。

#### 23.16.4 没动的

- `.tree-panel` / `.tree-head`（第 86–93 行）是 2.4.2 之前的侧边栏版本残留 CSS，当前 DOM 里没有元素用这两个类；本次未清理，避免无关改动。
- 树的数据结构、勾选逻辑、`--depth` 的写法都没变，只是把「视觉台阶」参数化。


### 23.17 2.8.11：详情区日志默认折叠 + 统计挪进按钮行（2026-11-18）

用户反馈（m07258，逐字）：「目前日志默认展开，日志应该默认关闭，点击可以展开，再次点击可以折叠」/「日志框目前上下边距只有页面一半，展开后做大一点」/「已有文件数 / 总文件数 和 已有文件大小 / 总文件大小 放在 "开始"、"暂停" 的后面，"删除" 的前面」。

#### 23.17.1 日志折叠

| 位置 | 改动 |
| --- | --- |
| 标记 | `.job-log` 初始 `class="job-log folded"`；标题行加 `data-role="log-toggle"`、`aria-expanded="false"`、`<span class="job-log-caret" data-role="log-caret">▸</span>` |
| CSS | `.job-log.folded { flex: 0 0 auto; min-height: 0 }`、`.job-log.folded pre.log { display: none }`、`.job-log.folded .job-log-head { margin-bottom: 0 }`；`.job-log-head { cursor: pointer; user-select: none }` |
| JS 状态 | `let logFolded = true;` + `setLogFolded(folded)`：切 `.folded` 类、翻 ▸/▾、同步 `aria-expanded`，展开时调一次 `fillJobLog({ scrollBottom: true })` |
| 拉日志闸门 | `refreshLogIfNeeded()` 里 `if (logFolded) return;`；`fillJobLog()` 里 `if (logFolded) { logState.id = null; return; }`（折叠时即便有人调也不发请求） |
| 点击 | `treeBindPanel()` 里新增一个 `[data-role="log-toggle"]` 监听，命中 `button[data-act]` 时跳过（「刷新日志」不算折叠） |
| 刷新按钮 | 折叠时点「刷新日志」= `setLogFolded(false)`（展开自带一次加载），展开时才是 `fillJobLog({ scrollBottom: true })`，避免同一次点击拉两遍 |

#### 23.17.2 展开高度

改前 `.job-log { flex: 1 1 auto; min-height: 10rem }`（160px）。改成 `min-height: clamp(14rem, 46vh, 32rem)`：

- 469px 高的小窗口：46vh = 216px 小于 14rem = 224px，取 224px（实测正好 224px）；
- 900px 高的常规窗口：46vh = 414px；
- 1440px 以上的大屏：被 32rem = 512px 封顶，不会把目录树和文件进度压没。

窄屏媒体查询里原来的 `.job-log { min-height: 14rem }` 改成 `.job-log:not(.folded)`，避免折叠态被撑高。

#### 23.17.3 统计挪进按钮行

- DOM：`<div class="stats" data-role="stats">` 从 `.job-meta` 下方移到 `.job-actions` 内、「暂停」之后、「删除」之前；`patchDetail()` 查的 `[data-role="stats"]` 选择器不变（仍在 `#detail` 里）。
- CSS：新增 `.job-actions .stats { display:flex; flex-direction:column; align-items:flex-start; gap:.1rem; margin:0 }`、`.job-actions .stat { display:flex; align-items:baseline; gap:.45rem; border:0; background:transparent; padding:0 }`。
- 为什么纵向两行：先试过一行并排，两个统计的 `nowrap` 文本合计 562px，加上两个 92px 按钮在 756px 窗口就换行（`.job-actions` 是 `flex-wrap: wrap`），「删除」被挤到第二行、垂直居中看起来错位。改纵向后整块 340×34px，与 38px 的按钮同高，一行放得下。

#### 23.17.4 验证（本机 756×469，假任务 jui2811，5 个文件）

| 项 | 结果 |
| --- | --- |
| 按钮行 | 开始 x=20 → 暂停 x=119 → 统计 x=218（340×34，两行）→ 删除 x=644（92×38），同一行、行高 38px |
| 折叠态 | `.job-log` 高 35px、`pre.log` `display:none`、`aria-expanded=false`、光标 ▸ |
| 展开态 | 高 224px、`pre.log` 184px 且框内可滚动、`aria-expanded=true`、光标 ▾ |
| 折叠 6 秒（含一次 5s 轮询） | `/api/jobs/*/logs` 请求 0 次 |
| 点「刷新日志」（折叠时） | 展开 + 拉 1 次 250 行，不会折叠回去、也不会拉两遍 |

- `node --check`（抽出 `<script>` 块）通过；`pytest` **251 passed**（纯前端改动）。
- 假任务用的 `state/jobs.yaml` 与 `state/jobs/jui2811/pipeline_state.yaml` 已删除，工作区不留测试数据。

#### 23.17.5 边界

- 折叠状态是页面内的内存状态（`logFolded` 默认 true），刷新页面回到折叠，不写 localStorage。
- 切换任务不重置该状态：在一个任务里展开日志，切到另一个任务仍是展开（各任务日志独立拉取，`logState.id` 随任务变化重拉）。
- 展开后若窗口很矮，`.main-body` 会整体可滚（`overflow: auto`），日志框自身仍保持框内滚动。
### 23.18 2.8.12：统计块改回左右并排、上下与按钮对齐（2026-11-18）

#### 23.18.1 需求

2.8.11 我把两块统计做成了纵向两行（标签在左、值在右，整块 34px 高）。用户 m07414 逐字：「做成和之前一样的左右分布，位置是够的，上下和暂停按钮上下对齐」——要的是 2.8.10 之前那种「两块并排、标签在上值在下」，并且整块上下边界与「暂停」按钮对齐。

#### 23.18.2 改动（只改 `src/web/static/index.html` 的 4 条 CSS）

| 选择器 | 改动 |
| --- | --- |
| `.job-actions .stats` | `flex-direction: column` → `row`；`align-items: stretch`；新增 `align-self: stretch`（拉满按钮行高度）；`gap: .1rem → .9rem` |
| `.job-actions .stat` | `flex-direction: column`、`justify-content: center`、`align-items: flex-start`（标签在上、值在下并垂直居中）；`line-height: 1.25 → 1.15` |
| `.job-actions .stat-label` | `font-size: .72rem → .7rem` |
| `.job-actions .stat-value` | 不变（`.8rem` 等宽字体、`nowrap`） |

`align-self: stretch` 是关键：`.job-actions` 是 `align-items: center` 的 flex 行，被拉伸的 `.stats` 高度等于行高（按钮决定的 38px），于是统计块与「暂停」按钮同 y、同高；块内两条文字用 `justify-content: center` 居中。

#### 23.18.3 为什么 2.8.11 当时以为「横排放不下」

2.8.11 试的横排是「每块压成一行：标签 值」，两段 nowrap 文本合计 562px，加上两个 92px 按钮，在 756px 窗口触发 `.job-actions` 的 `flex-wrap`，「删除」被挤到第二行并垂直居中，所以改成了纵向。

这次改的是块内结构（标签在上、值在下），每块宽度只取 label 与 value 的较大者：114px + 185px + 14.4px 间距 = 313px，而「暂停」右边界 211px 到「删除」左边界 644px 之间有 426px，够用（用户说的「位置是够的」就是这个意思）。

#### 23.18.4 验证（NAS 真机任务 j974084fb95「脱口秀」，756×469）

| 项 | 结果 |
| --- | --- |
| `.job-actions` | x=20 y=87 716×38（单行，没换行） |
| 「暂停」 | x=119 y=87 92×38 |
| 统计块 | x=218 **y=87 313×38**（与暂停按钮同 y、同高） |
| 内部两块 | 「已有文件数 / 总文件数」114×38、「已有文件大小 / 总文件大小」185×38，左右并排 |
| 「删除」 | x=644 y=87 92×38 |

改前先用注入 `<style>` 的方式在 NAS 活页面上量过候选 CSS，再落盘部署；部署后页面（未注入）复测同一组数字。

#### 23.18.5 边界

- 窗口再窄（约 <430px）时 313px 的统计块 + 两个 92px 按钮会触发 `.job-actions` 的 `flex-wrap`，「删除」换到第二行；此时统计块仍在按钮行里、不与按钮重叠，只是整行变高。
- 数值很长（TB 级、两位小数）时两块宽度会增长，仍是 `nowrap`；真到放不下就整行换行，不会挤压文字。
- 纯 CSS 改动，Python 侧只改版本号；`pytest` 251 passed。
### 23.19 2.8.13：「目录」面板一律默认折叠（2026-11-18）

#### 23.19.1 现象与根因

用户 m07475 逐字：「我点开凡人修人那个任务，目录默认就展开了，但是脱口秀那个确是关闭的，这是为什么，两个逻辑不一致吗」。

不是两套逻辑，是一条按「任务有没有文件进度记录」判定的规则，落在 `src/web/static/index.html` 的 `treeEnsureForJob()`：

```js
// 2.4.2 起的旧代码
treeState.folded = Number(job.total_files || 0) > 0;
```

- 「脱口秀」`total_files = 4`（4 条文件记录都在）→ `folded = true`（折叠）；
- 「凡人修仙」`total_files = 0`（它唯一的 `199 4K.mp4` 记录被逐文件删除，`pipeline_state.files` 为空）→ `folded = false`（展开），被当成了「还没跑过的新任务」。

规则原本有意图（没文件的任务展开目录方便勾选），但判定依据是用户看不见的 `total_files`，于是两个都是「暂停」的任务在界面上表现不同。

#### 23.19.2 改动

`treeEnsureForJob()` 里去掉条件，直接 `treeState.folded = true;`，并留注释说明旧规则为什么会不一致。切任务（`jobId` 变化）时都会重置成折叠；用户点标题行仍是普通的展开/折叠（`treeToggleFold()` 不变）。

#### 23.19.3 验证（NAS 真机 v2.8.13）

| 任务 | total_files | 切换后 |
| --- | --- | --- |
| j67bc357015 凡人修仙 | 0 | 折叠（标题行 + ▸） |
| j974084fb95 脱口秀 | 4 | 折叠（标题行 + ▸） |

- 两个任务来回切，目录树都保持折叠，不再随任务数据变化；
- 点标题行展开 / 再点折叠正常，展开后子目录仍是点开才加载；
- `node --check` 通过（1 个 script 块 62780 字符）、`pytest` 251 passed。

#### 23.19.4 边界

- 新建任务后目录树也是折叠的，需要点一下 ▸ 再勾选（新增成功的提示文案本来就是「在右侧『目录』勾选文件或目录后点『下载所选』」，没提默认展开）。
- 「整个目录」勾选框在折叠态也可见可用（在标题栏里），不受影响。
