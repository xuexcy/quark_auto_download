#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="${PROJECT_ROOT}/state/quark_main.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "未找到 PID 文件: ${PID_FILE}（进程可能未启动）"
  exit 0
fi

pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
if [ -z "${pid}" ]; then
  echo "PID 文件为空，已清理: ${PID_FILE}"
  rm -f "$PID_FILE"
  exit 0
fi

if ! kill -0 "$pid" 2>/dev/null; then
  echo "进程不存在，清理 PID 文件: PID=${pid}"
  rm -f "$PID_FILE"
  exit 0
fi

echo "正在停止进程 PID=${pid} ..."
kill -TERM "$pid" 2>/dev/null || true

# 等待优雅退出
i=0
while [ "$i" -lt 30 ]; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "已停止，PID=${pid}"
    exit 0
  fi
  sleep 1
  i=$((i + 1))
done

echo "优雅退出超时，强制结束 PID=${pid}"
kill -KILL "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "已强制停止，PID=${pid}"
