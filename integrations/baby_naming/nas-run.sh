#!/bin/sh
# 由 Friday NAS 的普通服务用户运行。凭据只从 NAS 文件路径传给应用。
set -eu
umask 077
: "${NAMING_PYTHON:=/usr/bin/python3}"
: "${NAMING_KEY_FILE:?需要现有 NAS Key 的私有路径}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
STATE="$ROOT/state"
SEED="$ROOT/history.json"
PROOF="$ROOT/proxy-proof"
PORT="${NAMING_PORT:-19146}"
MODEL="${NAMING_MODEL:-gpt-6-luna}"
DAY_LIMIT="${NAMING_DAILY_REQUESTS:-24}"
ACTION="${1:-}"
DIRECTION="${2:-}"
[ "$(id -u)" -ne 0 ]
[ -d "$STATE" ] && [ ! -L "$STATE" ]
cd "$ROOT/current"
case "${NAMING_API_PROXY:-}" in
  '') set -- ;;
  *) set -- --api-proxy "$NAMING_API_PROXY" ;;
esac

case "$ACTION" in
  smoke)
    case "$DIRECTION" in girl|boy) ;; *) exit 2 ;; esac
    exec "$NAMING_PYTHON" -B -m integrations.baby_naming.service --data "$STATE" --seed "$SEED" \
      --key-file "$NAMING_KEY_FILE" --model "$MODEL" --daily-requests "$DAY_LIMIT" --smoke "$DIRECTION" \
      --decisions-shadow "$@"
    ;;
  serve)
    exec "$NAMING_PYTHON" -B -m integrations.baby_naming.service --data "$STATE" \
      --key-file "$NAMING_KEY_FILE" --model "$MODEL" --daily-requests "$DAY_LIMIT" \
      --origins http://friday.local --proxy-proof "$PROOF" --port "$PORT" "$@"
    ;;
  *) exit 2 ;;
esac
