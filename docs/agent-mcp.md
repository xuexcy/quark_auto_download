# Agent MCP 接入指南

给 NAS 上的 DSH agent（或其他 MCP 客户端）接入本下载服务的完整说明。
架构：本服务内嵌 MCP server，挂载在 Web 服务的 `/mcp` 路径（streamable-http），
工具内部复用 `jobs_manager` 现有逻辑；另有任务完成 webhook 通知（可选）。

## 1. 服务端配置

### 1.1 MCP token（必须）

MCP 入口不走 Web 的 HTTP Basic 登录，用独立 Bearer token。二选一：

- 环境变量：`QUARK_AUTO_DL_MCP_TOKEN=<随机串>`
- 或 conf/web.yaml 加一行：

```yaml
mcp_token: "请生成一个足够长的随机串"
```

生成随机串：`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`

未配置 token 时 `/mcp` 不挂载、Web 日志会写明原因。

### 1.2 完成通知 webhook（可选，推荐配）

```yaml
# conf/web.yaml
notify_webhook_url: "http://<agent地址>/notify/quark"   # agent 收回调的端点
notify_webhook_token: "可选，回调请求会带 Authorization: Bearer <token>"
```

或环境变量 `QUARK_AUTO_DL_NOTIFY_WEBHOOK_URL` / `QUARK_AUTO_DL_NOTIFY_WEBHOOK_TOKEN`。

行为：后台线程每 5s 对账任务状态；任务从「运行中」变为「全部完成」时 POST：

```json
{
  "event": "job_completed",
  "job_id": "xxxx",
  "name": "MCP-xxxxxxxx",
  "status": "completed",
  "total_files": 12,
  "done_files": 12,
  "failed_files": []
}
```

`status` 为 `completed` 或 `completed_with_failures`（failed_files 非空）。
失败重试 30s/60s/120s 三次；接收方按 job_id 幂等处理（服务重启后会重发一次）。
手动暂停等「停了但没下完」的情况不通知。

依赖：`pip install fastmcp`（requirements 需加 `fastmcp`，已在本机 venv 安装验证）。

## 2. MCP 工具一览

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `submit_download` | 提交链接开始下载 | `share_url`（必须含 /s/）、`share_pwd`、`paths`（部分下载的相对路径列表，空=整棵目录）、`name`、`aria2_profile` |
| `job_status` | 查单个任务进度 | `job_id` |
| `list_downloads` | 列出全部任务 | 无 |
| `job_failures` | 失败文件明细 | `job_id` |

- 同一链接重复提交**复用已有任务**（勾选追加语义，不重复下载已完成的文件）。
- 返回摘要：`status ∈ {downloading, completed, completed_with_failures, paused}`、
  `done_files / total_files / percent / failed_files`。
- 部分下载用 `paths`：相对分享根的路径，如 `["Season 1"]`（目录）或
  `["第01集.mp4"]`（文件名，整树唯一时可只给文件名）。

## 3. DSH 侧接入

用 MCP bundle（configuration-only，transport 为 streamable-http）指向本服务：

```yaml
# cordis.patch.yml（要点示例；以 DSH 文档 templates/mcp 为准）
# url: http://<nas-ip>:8787/mcp
# headers: Authorization: Bearer <mcp_token>
# transport: streamable-http, serverName: quark-download
```

安装 bundle 后，DSH agent 的工具列表里应出现
`mcp__quark-download__submit_download` 等四个工具。

网络注意：DSH 所在机器要能访问 NAS 的 8787 端口；若配置了 webhook，
本服务要能反向访问 agent 的回调端点（不通也不影响下载，只丢主动通知，
agent 可改用轮询 `job_status`）。

## 4. 给 agent 的 system prompt 片段（可直接粘贴）

```
用户发来夸克网盘链接（pan.quark.cn/s/...）时：
1. 调用 submit_download 提交下载；链接带提取码就填 share_pwd，用户只要部分文件时填 paths。
2. 汇报任务 ID 和当前状态；同一链接已提交过时说明「复用已有任务」。
3. 配置了完成回调时，收到 job_completed 通知后向用户汇报结果；否则用户询问进度时调 job_status。
4. 汇报时明确说出失败文件（调 job_failures）与已完成数量，不要只说「完成」。
不要对同一链接反复创建任务。
```

## 5. 验证

```bash
# token 缺失应 401
curl -i -X POST http://127.0.0.1:8787/mcp -H "Content-Type: application/json"

# 带token 的 initialize（MCP 协议握手；/mcp 会 307 到 /mcp/，用 -L 跟随）
curl -s -L -X POST http://127.0.0.1:8787/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

已实测（2026-11，fastmcp 4.2.2）：无/错 token → 401；正确 token → 200 initialize 握手
（serverInfo `quark-download`）、tools/list 返回 4 工具、tools/call `list_downloads` 正常返回。
MCP 客户端按 streamable-http 标准会自动处理 307 重定向与 `mcp-session-id`，无需特殊配置。

之后对 agent 直接说一句自然语言（如「下载 https://pan.quark.cn/s/xxxx」）即可验证闭环。
