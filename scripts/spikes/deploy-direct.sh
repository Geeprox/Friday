#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
export UV_CACHE_DIR="$PWD/data/uv-cache"
export HF_HOME="$PWD/data/huggingface"
export HF_HUB_DISABLE_PROGRESS_BARS=1
uv sync --locked --extra local-runtime
.venv/bin/python -m scripts.install_miloco
.venv/bin/python -m scripts.download_models
printf '%s\n' 'Models and Miloco installed. Start: ./scripts/start-stack.sh'
