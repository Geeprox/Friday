"""从 NAS 本机核对已发布电视 APK。默认只读，可显式修正该包的权限。"""

import argparse
import fcntl
import hashlib
import json
import os
import stat
from contextlib import ExitStack
from pathlib import Path

try:
    from integrations.ktv_public.tv_updates import (
        MANIFEST_LIMIT, MAX_APK_BYTES, SHA256, Release, TvUpdateError, TvUpdateStore,
    )
except ImportError:
    from tv_updates import MANIFEST_LIMIT, MAX_APK_BYTES, SHA256, Release, TvUpdateError, TvUpdateStore


def describe(file, *, directory):
    item = file.lstat()
    return {"mode": format(stat.S_IMODE(item.st_mode), "04o"),
            "owner_matches_process": item.st_uid == os.geteuid(), "private": not bool(item.st_mode & 0o077),
            "type_matches": stat.S_ISDIR(item.st_mode) if directory else stat.S_ISREG(item.st_mode)}


def read_regular(file):
    fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("regular_file_required")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def inspect(config, version, size, digest):
    state = config.parent / "state"
    updates = TvUpdateStore(state)
    paths = {"config": config, "state": state, "release": updates.directory,
             "manifest": updates.manifest, "apk": updates.directory / (digest + ".apk"),
             "publish_lock": updates.directory / ".publish.lock"}
    facts = {}
    for name, path in paths.items():
        try:
            facts[name] = describe(path, directory=name in {"state", "release"})
        except OSError as error:
            facts[name] = {"error": type(error).__name__, "errno": error.errno}
    result = {"files": facts, "version_code": version, "current_matches_receipt": False,
              "apk_matches_receipt": False, "readable_by_live_contract": False}
    for root in ("config", "state", "release"):
        if (paths[root].resolve() != paths[root] or not facts[root].get("type_matches")):
            return {**result, "code": "invalid_" + root + "_path"}
    if not facts["config"]["owner_matches_process"] or not facts["state"]["owner_matches_process"]:
        return {**result, "code": "nas_service_owner_required"}
    try:
        with read_regular(paths["manifest"]) as stream:
            data = stream.read(MANIFEST_LIMIT + 1)
        if len(data) > MANIFEST_LIMIT:
            raise ValueError
        current = Release(**json.loads(data)["current"])
    except (OSError, KeyError, TypeError, ValueError, RecursionError):
        return {**result, "code": "unreadable_release_manifest"}
    result["current_matches_receipt"] = (current.version_code, current.bytes, current.sha256) == (version, size, digest)
    if not result["current_matches_receipt"]:
        return {**result, "code": "release_receipt_mismatch"}
    try:
        with read_regular(paths["apk"]) as stream:
            if os.fstat(stream.fileno()).st_size == size:
                value = hashlib.sha256()
                remaining = size
                while remaining:
                    chunk = stream.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    value.update(chunk)
                    remaining -= len(chunk)
                result["apk_matches_receipt"] = remaining == 0 and value.hexdigest() == digest
    except OSError as error:
        return {**result, "code": "unreadable_release_apk", "errno": error.errno}
    except ValueError:
        return {**result, "code": "unreadable_release_apk"}
    if not result["apk_matches_receipt"]:
        return {**result, "code": "release_apk_mismatch"}
    try:
        actual = updates._state()["current"]
        if actual is not None:
            with updates._verified_apk(actual):
                result["readable_by_live_contract"] = actual == current
    except TvUpdateError as error:
        result["live_contract_code"] = str(error)
    except OSError as error:
        result["live_contract_code"] = type(error).__name__
        result["live_contract_errno"] = error.errno
    if result["readable_by_live_contract"]:
        code = "ready"
    elif any(not facts[name].get("private") for name in ("release", "manifest", "apk")):
        code = "nas_release_modes_not_private"
    else:
        code = "release_withheld_by_live_contract"
    return {**result, "code": code}


def repair_modes(config, version, size, digest):
    args = config, version, size, digest
    view = inspect(*args)
    if view["code"] != "nas_release_modes_not_private":
        return view
    root = TvUpdateStore(config.parent / "state").directory
    targets = {"release": (root, 0o700), "manifest": (root / "release.json", 0o600),
               "apk": (root / (digest + ".apk"), 0o600), "publish_lock": (root / ".publish.lock", 0o600)}
    with ExitStack() as opened:
        descriptors = {}
        for name, (path, _) in targets.items():
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if name == "release":
                flags |= os.O_DIRECTORY
            fd = os.open(path, flags)
            opened.callback(os.close, fd)
            item = os.fstat(fd)
            correct_type = stat.S_ISDIR(item.st_mode) if name == "release" else stat.S_ISREG(item.st_mode)
            if not correct_type or item.st_uid != os.geteuid() or (name != "release" and item.st_nlink != 1):
                return {**view, "code": "nas_release_owned_entries_required"}
            descriptors[name] = fd
        fcntl.flock(descriptors["publish_lock"], fcntl.LOCK_EX | fcntl.LOCK_NB)
        checked = inspect(*args)
        if checked["code"] != "nas_release_modes_not_private":
            return checked
        for name, (path, _) in targets.items():
            before, now = os.fstat(descriptors[name]), path.lstat()
            if (before.st_dev, before.st_ino) != (now.st_dev, now.st_ino):
                return {**checked, "code": "nas_release_changed"}
        for name, (_, mode) in targets.items():
            if stat.S_IMODE(os.fstat(descriptors[name]).st_mode) != mode:
                os.fchmod(descriptors[name], mode)
        return {**inspect(*args), "private_modes_applied": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--version-code", type=int, required=True)
    parser.add_argument("--bytes", type=int, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--repair-modes", action="store_true", help="仅对摘要匹配的当前成品与其发布文件修正权限")
    args = parser.parse_args(argv)
    if not 0 < args.version_code <= 2147483647 or not 0 < args.bytes <= MAX_APK_BYTES or not SHA256.fullmatch(args.sha256):
        parser.error("需要已校验回执中的版本号、尺寸和 SHA-256")
    try:
        check = repair_modes if args.repair_modes else inspect
        result = check(args.config.absolute(), args.version_code, args.bytes, args.sha256)
    except (OSError, RuntimeError, ValueError):
        result = {"code": "nas_release_check_failed"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["code"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
