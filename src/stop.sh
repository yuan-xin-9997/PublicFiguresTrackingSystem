#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PID_FILE="$ROOT/logs/server.pid"
if [ ! -f "$PID_FILE" ]; then echo '服务未运行'; exit 0; fi
PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  WAIT=0
  while kill -0 "$PID" 2>/dev/null && [ "$WAIT" -lt 10 ]; do sleep 1; WAIT=$((WAIT + 1)); done
fi
rm -f "$PID_FILE"
echo "服务已停止，PID=$PID"
