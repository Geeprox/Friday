#!/bin/sh
set -eu
repo_dir="$(CDPATH= cd "$(dirname "$0")/../../.." && pwd)"
# Compatibility name: defaults to a plan and never opens or installs an App.
mkdir -p "$repo_dir/.artifacts/tmp"
exec /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin   TMPDIR="$repo_dir/.artifacts/tmp" PYTHONDONTWRITEBYTECODE=1   /usr/bin/python3 -I -B "$repo_dir/native/FridayDesktop/script/build_desktop.py" "$@"
