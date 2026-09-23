#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_PID_FILE="${PROJECT_ROOT}/state/web.pid"
JOBS_DIR="${PROJECT_ROOT}/state/jobs"

# 对单个 PID 发 TERM，等待后必要时 KILL。name 仅用于日志。
stop_one_pid() {
  name="$1"
  pid="$2"
  wait_secs="${3:-15}"

  if [ -z "${pid}" ]; then
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "${name} 进程不存在，PID=${pid}"
    return 0
  fi

  echo "正在停止 ${name} PID=${pid} ..."
  kill -TERM "$pid" 2>/dev/null || true

  i=0
  while [ "$i" -lt "$wait_secs" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "${name} 已停止"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done

  kill -KILL "$pid" 2>/dev/null || true
  echo "${name} 已强制停止"
}

# 读 pid 文件并停止；始终删除 pid 文件。
stop_from_pid_file() {
  name="$1"
  pid_file="$2"
  wait_secs="${3:-15}"

  if [ ! -f "$pid_file" ]; then
    return 0
  fi

  pid="$(tr -d '[:space:]' < "$pid_file" || true)"
  if [ -n "${pid}" ]; then
    stop_one_pid "$name" "$pid" "$wait_secs"
  else
    echo "${name} PID 文件为空，已清理"
  fi
  rm -f "$pid_file"
}

echo "=== 停止 Web ==="
stop_from_pid_file "Web" "$WEB_PID_FILE" 20

echo "=== 停止分享任务 ==="
if [ -d "$JOBS_DIR" ]; then
  # 无匹配时 glob 保持字面量；用 -f 跳过
  for pid_file in "$JOBS_DIR"/*/job.pid; do
    [ -f "$pid_file" ] || continue
    job_id="$(basename "$(dirname "$pid_file")")"
    stop_from_pid_file "任务 ${job_id}" "$pid_file" 10
  done
fi

echo "=== 兜底清理残留进程 ==="
# 先尽量精确 PID，再按命令行兜底（无匹配时 pkill 非 0，忽略）
pkill -f 'uvicorn web_app:app' 2>/dev/null || true
pkill -f 'quark_main.py' 2>/dev/null || true

# 再扫一遍，清理可能残留的空/过期 pid 文件
rm -f "$WEB_PID_FILE"
if [ -d "$JOBS_DIR" ]; then
  for pid_file in "$JOBS_DIR"/*/job.pid; do
    [ -f "$pid_file" ] || continue
    rm -f "$pid_file"
  done
fi

echo "全部相关进程已停止（Web + 分享任务）；下次 start.sh 仅启动 Web，任务需在页面重新「开始」"
