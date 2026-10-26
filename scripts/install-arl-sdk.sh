#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ "$#" -ne 1 ] || [ ! -f "$1/arl/client.py" ] || [ ! -f "$1/pyproject.toml" ]; then
  echo 'Usage: sh scripts/install-arl-sdk.sh /absolute/path/to/ARL (verified shared checkout)' >&2
  exit 1
fi
command -v uv >/dev/null 2>&1 || { echo 'Install uv first.' >&2; exit 1; }
export UV_CACHE_DIR="$PWD/.venv/uv-cache"
uv pip install --python .venv/bin/python --no-deps --no-build-isolation -e "$1"
printf '%s\n' 'Shared ARL SDK installed. Open Console Settings > 智能服务 to connect. No model was downloaded or started.'
