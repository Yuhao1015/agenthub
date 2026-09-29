#!/bin/bash
# Agent Hub desktop launcher (Linux). Run from a terminal or via a .desktop file.
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || {
  echo "需要 Python 3：请先安装，然后重新运行本脚本。"
  exit 1
}

URL="http://127.0.0.1:8787/dashboard.html"

if curl -s -m 2 "http://127.0.0.1:8787/health" >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 || echo "已运行：请打开 $URL"
  exit 0
fi

DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/AgentHub"
mkdir -p "$DATA_DIR"
nohup "$PY" serve.py >> "$DATA_DIR/launcher.log" 2>&1 &

for _ in $(seq 1 20); do
  sleep 0.3
  curl -s -m 1 "http://127.0.0.1:8787/health" >/dev/null 2>&1 && break
done
xdg-open "$URL" >/dev/null 2>&1 || echo "已启动：请打开 $URL"
