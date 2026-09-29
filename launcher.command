#!/bin/bash
# Agent Hub desktop launcher (macOS). Double-click to start.
# Starts the local server once, then opens the dashboard -- a second
# double-click just opens the browser against the running instance.
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || {
  echo "需要 Python 3：请先安装（brew install python），然后重新双击本文件。"
  read -r -p "按回车关闭…"
  exit 1
}

URL="http://127.0.0.1:8787/dashboard.html"

# already running? just open the browser
if curl -s -m 2 "http://127.0.0.1:8787/health" >/dev/null 2>&1; then
  open "$URL"
  exit 0
fi

DATA_DIR="$HOME/Library/Application Support/AgentHub"
mkdir -p "$DATA_DIR"
nohup "$PY" serve.py >> "$DATA_DIR/launcher.log" 2>&1 &

# wait for the server to come up, then open the browser
for _ in $(seq 1 20); do
  sleep 0.3
  curl -s -m 1 "http://127.0.0.1:8787/health" >/dev/null 2>&1 && break
done
open "$URL"
