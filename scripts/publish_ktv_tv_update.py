#!/usr/bin/env python3
"""复用电视安装预检，向现有 NAS KTV 私有目录登记已签名的主 APK。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from build_ktv_tv import validate_main_manifest
from install_ktv_tv import ROOT, java_environment, verify_build

sys.path.insert(0, str(ROOT))
from integrations.ktv_public.tv_update_inbox import COMMITTED, TvUpdateInbox  # noqa: E402
from integrations.ktv_public.tv_updates import MAX_APK_BYTES, Release, TvUpdateError, TvUpdateStore  # noqa: E402


def verified_release(build_dir, audio_build_dir, sdk, java):
    identity = validate_main_manifest(ROOT / "native/KTVTV/android")
    candidate = build_dir / ("friday-ktv-" + identity["version"] + ".apk")
    if (candidate.resolve() != candidate or not candidate.is_file()
            or not 0 < candidate.stat().st_size <= MAX_APK_BYTES):
        raise TvUpdateError("bounded_main_apk_required")
    main = verify_build("main", build_dir, sdk, java_environment(java), ROOT)
    release = Release(main.identity["version_code"], main.identity["version"], len(main.body),
                      main.receipt["sha256"], main.certificate_sha256)
    return release, main.body


def nas_state_directory(path):
    state = path.absolute()
    marker = state / "state.sqlite"
    if (state.resolve() != state or not state.is_dir() or state.stat().st_mode & 0o077
            or marker.is_symlink() or not marker.is_file()):
        raise TvUpdateError("initialized_private_nas_state_required")
    return state


def worker_key(path):
    path = path.absolute()
    if (path.resolve() != path or not path.is_file() or path.stat().st_mode & 0o077
            or not 0 < path.stat().st_size <= 8192):
        raise TvUpdateError("private_worker_config_required")
    return json.loads(path.read_text())["agent_key"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nas-state", type=Path, required=True, help="NAS KTV 私有配置旁的现有 state 目录")
    parser.add_argument("--build-dir", type=Path, default=ROOT / ".artifacts/ktv-tv", help="主包及构建回执目录")
    parser.add_argument("--audio-build-dir", type=Path, help="兼容旧命令参数，主应用发布时可省略")
    parser.add_argument("--worker-config", type=Path, default=ROOT / "data/ktv/nas.json",
                        help="现有 Mac NAS Worker 私有配置文件，发布时用其签署投递")
    parser.add_argument("--sdk", type=Path, default=ROOT / ".artifacts/embodied-toolchain/android")
    parser.add_argument("--java", type=Path, default=ROOT / ".artifacts/embodied-toolchain/jdk")
    parser.add_argument("--apply", action="store_true", help="投递至现有 NAS 私有收件目录并等待发布回执")
    args = parser.parse_args(argv)

    try:
        store = TvUpdateStore(nas_state_directory(args.nas_state))
        release, body = verified_release(args.build_dir.absolute(), args.audio_build_dir.absolute() if args.audio_build_dir else None,
                                         args.sdk.absolute(), args.java.absolute())
        info = release.public()
        if not args.apply:
            print(json.dumps({"state": store.plan(release), "applied": False, **info}))
            return 0
        inbox = TvUpdateInbox(store, worker_key(args.worker_config))
        request_id = inbox.stage(release, body)
        receipt = inbox.wait_for_receipt(request_id)
        if receipt is None:
            print(json.dumps({"state": "staged", "applied": False, **info}))
            return 2
        if any(receipt.get(field) != value for field, value in info.items()):
            raise TvUpdateError("tv_release_inbox_receipt_mismatch")
        applied = receipt.get("state") in COMMITTED
        print(json.dumps({**receipt, "applied": applied}))
        return 0 if applied else 1
    except TvUpdateError as error:
        print(json.dumps({"state": "refused", "code": str(error)}), file=sys.stderr)
        return 1
    except Exception:
        # 第三方工具异常可能带本机路径或私有预置输入。
        print(json.dumps({"state": "refused", "code": "tv_release_preflight_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
