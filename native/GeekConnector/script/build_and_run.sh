#!/bin/zsh
set -euo pipefail
FRIDAY_TASK_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$FRIDAY_TASK_ROOT"
.venv/bin/python - <<'PY'
import asyncio,shutil
from pathlib import Path
from homeplane.geek_native import build_helper
print(asyncio.run(build_helper(Path('data/homeplane'),shutil.which('node'))))
PY
if [[ "${1:-}" == "--build-only" ]]; then exit 0; fi
# Reap only this named local helper, never Console or another Python worker.
/usr/bin/pkill -x FridayGeekConnector || true
/usr/bin/open -g -n "$FRIDAY_TASK_ROOT/dist/FridayGeekConnector.app"
