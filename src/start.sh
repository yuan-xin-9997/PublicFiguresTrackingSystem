#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
mkdir -p data logs
if [ -f data/runtime.env ]; then
  set -a
  . data/runtime.env
  set +a
fi
if [ ! -f data/password.txt ]; then
  printf '%s\n' '# 格式: username:password:role  (role 取值: admin | user)' 'admin:admin123:admin' > data/password.txt
  chmod 600 data/password.txt 2>/dev/null || true
fi
if [ -f logs/server.pid ] && kill -0 "$(cat logs/server.pid)" 2>/dev/null; then
  echo "服务已运行，PID=$(cat logs/server.pid)"
  exit 0
fi
rm -f logs/server.pid
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt
if [ ! -f app/frontend/dist/index.html ] && command -v npm >/dev/null 2>&1; then
  (cd app/frontend && if [ -f package-lock.json ]; then npm ci; else npm install; fi && npm run build)
fi
PYTHONPATH="$ROOT" nohup .venv/bin/python -m app.backend.main > logs/server.stdout.log 2> logs/server.stderr.log &
echo $! > logs/server.pid
sleep 2
if ! kill -0 "$(cat logs/server.pid)" 2>/dev/null; then
  rm -f logs/server.pid
  echo '服务启动失败，请检查 logs/server.stderr.log' >&2
  exit 1
fi
echo "服务已启动，PID=$(cat logs/server.pid)"
