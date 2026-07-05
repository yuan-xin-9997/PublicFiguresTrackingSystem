#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PID_FILE="$ROOT/logs/server.pid"
if [ ! -f "$PID_FILE" ]; then echo STOPPED; exit 1; fi
PID=$(cat "$PID_FILE")
if ! kill -0 "$PID" 2>/dev/null; then echo "STALE PID=$PID"; exit 1; fi
PORT=${PFTS_SERVER__PORT:-8000}
if command -v curl >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:$PORT/api/v1/health/ready" >/dev/null; then
  echo "RUNNING PID=$PID STATUS=ready"
else
  echo "RUNNING PID=$PID HEALTH=UNAVAILABLE"
  exit 2
fi
