#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
chat_python="${WECHAT_PYTHON:-.venv-wechat/bin/python}"
if [[ ! -x "$chat_python" ]]; then
  echo '请先安装 requirements-wechat.txt 到 .venv-wechat。' >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
exec "$chat_python" -m app.wechat.chat "$@"
