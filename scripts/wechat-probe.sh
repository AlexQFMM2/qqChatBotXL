#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
probe_python="${WECHAT_PYTHON:-.venv-wechat/bin/python}"
if [[ ! -x "$probe_python" ]]; then
  echo '请先安装独立环境：uv venv .venv-wechat && uv pip install --python .venv-wechat/bin/python -r requirements-wechat.txt' >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
exec "$probe_python" -m app.wechat "$@"
