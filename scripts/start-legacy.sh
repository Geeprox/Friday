#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
  echo "Please install uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.venv/uv-cache}"
exec uv run --locked python -m friday.launch
