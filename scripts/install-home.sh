#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
command -v uv >/dev/null 2>&1 || { echo 'Install uv first: https://docs.astral.sh/uv/'; exit 1; }
export UV_CACHE_DIR="$PWD/.artifacts/uv-cache"
uv sync --locked --no-dev
./bin/home doctor
