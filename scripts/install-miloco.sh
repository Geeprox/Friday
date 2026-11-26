#!/bin/sh
# 运行用户选定的官方安装阶段，成功后接入本机升级维护。
set -eu
if [ "$#" -lt 1 ] || [ ! -f "$1" ]; then
    echo '用法：install-miloco.sh 官方安装脚本 [安装参数…]' >&2
    exit 2
fi
installer=$1
shift
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
for option in "$@"; do
    case "$option" in
        --help|-h) exec bash "$installer" "$@" ;;
        --uninstall)
            bash "$installer" "$@"
            exec python3 "$root/integrations/miloco_recovery/lifecycle.py" disable
            ;;
    esac
done
bash "$installer" "$@"
exec python3 "$root/integrations/miloco_recovery/lifecycle.py" enable
