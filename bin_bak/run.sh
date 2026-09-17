#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="${PROJECT_ROOT}/state/quark_main.pid"
PYTHON_BIN="$(cd "$PROJECT_ROOT/.." && pwd)/bin/python3"
LOG_DIR="${PROJECT_ROOT}/log"
mkdir -p "$(dirname "$PID_FILE")" "$LOG_DIR"

cd "$PROJECT_ROOT"

if [ -f "$PID_FILE" ]; then
  old_pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
  if [ -n "${old_pid}" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "已在运行中，PID=${old_pid}（${PID_FILE}）"
    exit 1
  fi
  rm -f "$PID_FILE"
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "找不到 Python: $PYTHON_BIN" >&2
  exit 1
fi

nohup "$PYTHON_BIN" src/quark_main.py >>"${LOG_DIR}/run.out" 2>&1 &
pid=$!
echo "$pid" >"$PID_FILE"
echo "已启动，PID=${pid}"
echo "PID 文件: ${PID_FILE}"
