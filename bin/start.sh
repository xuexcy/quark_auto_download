#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="$(cd "$PROJECT_ROOT/.." && pwd)/bin/python3"
HOST="${QUARK_AUTO_DL_WEB_HOST:-0.0.0.0}"
PORT="${QUARK_AUTO_DL_WEB_PORT:-8787}"
PID_FILE="${PROJECT_ROOT}/state/web.pid"
RUN_DATE="$(date '+%Y_%m_%d')"
RUN_TIME="$(date '+%H_%M_%S')"
LOG_DIR="${PROJECT_ROOT}/log/${RUN_DATE}/web"
LOG_FILE="${LOG_DIR}/${RUN_TIME}.log"

cd "$PROJECT_ROOT"
mkdir -p "$(dirname "$PID_FILE")" "$LOG_DIR"

if [ -f "$PID_FILE" ]; then
  old_pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
  if [ -n "${old_pid}" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Web 已在运行，PID=${old_pid}" >&2
    exit 1
  fi
  rm -f "$PID_FILE"
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "找不到 Python: $PYTHON_BIN" >&2
  exit 1
fi

export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
export QUARK_AUTO_DL_WEB_LOG_FILE="$LOG_FILE"

{
  echo "===== web start $(date '+%Y-%m-%d %H:%M:%S') ====="
  echo "listen=http://${HOST}:${PORT}/"
  echo "log=${LOG_FILE}"
} >>"$LOG_FILE"

# 标准输出/错误全部进 web 日志，终端不再刷日志
nohup "$PYTHON_BIN" -m uvicorn web_app:app \
  --host "$HOST" \
  --port "$PORT" \
  --no-access-log \
  </dev/null >>"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" >"$PID_FILE"
# 仅返回启动结果，不输出运行日志
echo "PID=${pid}"
echo "LOG=${LOG_FILE}"
