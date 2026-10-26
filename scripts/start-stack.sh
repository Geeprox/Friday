#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ] || [ ! -x data/miloco-venv/bin/miloco-backend ]; then
  echo 'Run ./scripts/deploy.sh first.' >&2
  exit 1
fi
if [ ! -f "${FRIDAY_CONFIG:-friday.home.json}" ]; then
  echo 'Copy friday.live.example.json to friday.home.json and configure your home first.' >&2
  exit 1
fi
exec .venv/bin/python -m friday.stack
