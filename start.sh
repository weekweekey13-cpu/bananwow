#!/usr/bin/env bash
# Render start — exit 127 = команда не найдена (часто Runtime=Node из‑за package.json)
set -e
cd "$(dirname "$0")"

echo "=== bananwow start ==="
echo "pwd=$(pwd)"
echo "PORT=${PORT:-unset}"
echo "BOT_TOKEN set? $([ -n "${BOT_TOKEN:-}" ] && echo yes || echo NO)"
echo "which python3: $(command -v python3 || true)"
echo "which python:  $(command -v python || true)"
ls -la bot.py requirements.txt 2>&1 || true

if command -v python3 >/dev/null 2>&1; then
  exec python3 -u bot.py
fi
if command -v python >/dev/null 2>&1; then
  exec python -u bot.py
fi

echo "FATAL: python/python3 not found. In Render → Settings → Runtime language = Python 3" >&2
echo "Start Command must be: bash start.sh   OR   python3 -u bot.py" >&2
exit 127
