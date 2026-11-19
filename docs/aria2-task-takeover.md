# Aria2 任务接管：重启后不要重复提交

## 问题

重启 Web 服务（或重启任务）后，Aria2 里同一个文件出现两份任务：

- 原任务继续在下，文件名正常；
- 新任务被 `auto-file-renaming=true` 改名成 `xxx.1.mkv`，从 0 开始下一份；
- 两个任务各占连接数，速度被摊薄，磁盘上还会留下两份半成品。

## 机制

提交下载时本项目的约定是：

- `dir = <根目录>/<相对目录>`（例如 `…/downloading/<任务名>/Show/S01`）
- `out = <文件名>`

Aria2 的 `tellActive/tellStopped` 回报的是绝对路径 `files[0].path` 与 `dir`。还原相对路径的函数在
`src/server/aria2_api.py` 的 `_extract_task_filename()`：

旧实现先按 `task["dir"]` 剥前缀。但 `dir` 里已经包含相对子目录，剥完只剩**文件名**；
而 `src/server/file_api.py` 的 `submit_download()` 用**完整相对路径**（`Show/S01/a.mkv`）去查已有任务表
（`aria2_get_existing_task_infos_by_name()` 的键）。两者对不上 → 每次启动都判定「无已有任务」→ 重新 `addUri`。

远端模式此前有「完整路径 → 文件名」的别名兜底，所以远端不容易踩到；本机模式没有别名，于是每次重启都重下。

## 修法

还原顺序改成（`src/server/aria2_api.py`）：

1. 先按**根目录**剥：本机 `download_dir`（`<aria2_download_dir>/<work_dir>`），远端 `remote_dir`；剥完得到「相对目录 + 文件名」，正好是提交时用的键；
2. 路径恰好等于根目录 → 返回空串（不是文件）；
3. 根目录不匹配时退回按 `task["dir"]` 剥（兼容换过根目录的老任务）；
4. 远端再退回文件名；
5. 都不中返回空串（宁可不认领，也不要拿绝对路径和别的任务撞键）。

日志对照：

- 修好：`[Aria2分支=接管] 启动时接管已有任务(active): Nintendo Switch运动…/upd-v1.5.0/…nsz | gid=b81e335759578bde`
- 出问题：`[Aria2分支=新建] 无已有任务→addUri: …` + `下载任务提交成功 … gid=<新 gid>`

## 回归用例

`tests/pytest/test_aria2_remote.py`：

- `test_local_extract_uses_download_root_not_task_dir`（dir 含子目录时仍还原成完整相对路径）
- `test_local_extract_single_level_file`（根目录下的单层文件）
- `test_remote_extract_uses_remote_dir_when_set`
- `test_remote_extract_without_remote_dir_falls_back_to_basename`
- 旧用例 `test_local_extract_filename_requires_own_dir`（别的目录下的任务不被认领）

## 运维提示

升级到这个版本之前已经产生过重复任务的环境，需要手工清理一次：

1. `aria2.tellActive` 找出同一个文件的两条任务，保留较老（进度较高）的那条；
2. 对重复的 gid 执行 `aria2.remove` + `aria2.removeDownloadResult`；
3. 删掉对应的 `*.1.mkv` / `*.1.nsz` 半成品与 `.aria2` 控制文件；
4. 重启任务，确认日志出现 `[Aria2分支=接管]`，进度接回原值。
