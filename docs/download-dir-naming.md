# 下载目录里的子目录：用任务名，不用内部 job id（2.8.20）

## 1. 需求

用户 2026-11-19 反馈两件事：

1. NAS 上 `/vol2/@team/家庭共享_薛成韵/下载/aria2/downloading/` 里留着一个空目录 `jbb615ea738`（任务「土匪」早已下完），为什么没删掉？
2. 下载目录里不要用 job id，换成任务名 —— 任务名不能重复，天然唯一。

## 2. 原来的行为与根因

### 2.1 目录名是内部 job id

```
<临时下载目录>/<job_id>/<分享内的相对路径>
```

`job_id` 由 `jobs_manager._make_job_id(share_url)` 生成（`j` + 分享 URL 摘要），用户在自己的 Aria2 下载目录里只看到一串随机字符，认不出是哪个任务，删起来也不放心。

### 2.2 空目录不删的原因

收尾时 `FileApi.cleanup_empty_download_dirs()` 调 `prune_empty_local_dirs(download_root)`，而后者从 `download_root` **内部**自底向上删空目录，并且用 `_is_path_under()` 明确排除 `root` 自己：

```python
def _is_path_under(path, root):
    if abs_path == abs_root:
        return False        # root 本身不算「root 之下」
```

所以 `<任务目录>/` 里面清干净了，`<任务目录>` 这个空壳留着，正是用户看到的 `jbb615ea738`。

## 3. 方案

### 3.1 子目录名 = 任务名（清洗后）

新增 `src/server/job_paths.py`：

```python
def work_dir_name(name: str, fallback: str = "") -> str
```

- 路径分隔符 `/ \\`、Windows 保留字符 `<>:"|?*`、控制字符 → 下划线，其余（中文、空格、括号、表情）原样保留；
- 连续空白压成一个空格，去掉首尾空格与点号；
- 超过 80 字符截断（NAS 文件名上限 255 字节，中文 3 字节/字，80 字留够余量）；
- 清洗后只剩 `.`、空格、下划线（例如名字是 `..` 或 `./`）→ 退回 job id，绝不让目录跑出上一级。

任务名唯一由 `jobs_manager.add_job()` 保证（重名直接 `任务名已存在`），所以同一份 `state/jobs.yaml` 里的任务不会撞目录。

### 3.2 两边必须算出同一个名字

| 位置 | 取值方式 |
| --- | --- |
| Web 进程 `start_job()` | `work_dir_name(job["name"], job_id)` 写进环境变量 `QUARK_AUTO_DL_WORK_DIR` |
| 子进程 `quark_main` | 读 `QUARK_AUTO_DL_WORK_DIR`；没有（老任务 / 手工跑 CLI）退回 `JOB_ID` |
| Web 进程建 Aria2 客户端（pause / 清理按路径对齐） | 现算 `work_dir_name(job["name"], job_id)` |

`Aria2Client` 与 `FileApi` 的参数名从 `job_id` 改成 `work_dir`（语义就是「子目录名」，不再假装是 id），属性也改成 `client.work_dir` / `api.work_dir`。

### 3.3 收尾时连空的任务目录一起删

`cleanup_empty_download_dirs()` 在 prune 之后补一步：

```python
if self.work_dir and root and os.path.isdir(root):
    try:
        os.rmdir(root)     # 非空会抛 OSError，那就保留
    except OSError:
        pass
```

只删 `<临时>/<任务名>` 这一层，不会碰到 `<临时>` 根下用户自己的目录（例如 NAS 上的 `06.Magic Marker（73篇完结）`）。

## 4. 兼容性

- **老任务的目录仍叫 job id**：不做自动改名。任务正在跑时改目录会让 aria2 写到旧路径（目录被重建），甚至和进度记录对不上。新任务一律用任务名；老任务想统一，暂停后手工把 `<临时>/<job_id>` 改名成任务名即可（或直接删掉重下）。
- `aria2_remote`（远端 Aria2）与「两个目录都空」两种模式本来就不落本机，子目录名只影响提交给对方的 `dir`，同样跟着任务名走。
- `pipeline_state.yaml` 里的路径都是相对路径，目录改名的任务会当作「本地没有副本」重新下载 —— 这也是不自动改名的原因。

## 5. 验证

- `tests/pytest/test_job_paths.py`（8 条）：清洗规则（中文保留、非法字符替换、`..`/`./`/空白回落 job id、80 字符截断）、`Aria2Client.download_dir` 与 `FileApi.download_root` 都拼任务名、收尾删空任务目录、有文件时保留、文件已挪到 `<临时>/` 根下时任务目录被删。另有 1 条在 `tests/pytest/test_file_control.py`：`start_job()` 写出的 `QUARK_AUTO_DL_WORK_DIR` 是清洗后的任务名。
- 全仓 `pytest` 260 passed。
- NAS 真机：清掉遗留的空 `downloading/jbb615ea738`；新起任务后确认下载目录里出现以任务名命名的子目录、收尾后自动消失（见 README 2.8.20 变更记录）。

## 6. 遗留 / 后续

- 老任务的 `<临时>/<job_id>` 目录不会自动改名（见第 4 节）。
- 任务**改名**功能目前不存在；将来加改名时，需要决定「改名前后的下载目录是否跟着改」（建议只在任务 paused 且目录为空时跟着改）。
