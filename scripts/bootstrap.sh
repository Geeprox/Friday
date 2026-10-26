#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ "${1:-}" = '--check' ]; then
    exec python3 scripts/setup-dependencies.py plan
fi
exec sh scripts/install-home.sh "$@"
