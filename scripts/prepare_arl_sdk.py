#!/usr/bin/env python3
"""显式准备固定公开 SDK 输入；只取源码，不安装或执行 SDK。"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
LOCK_MANIFEST = ROOT / "native/FridayDesktop/Resources/ARL-SDK-MANIFEST.json"
REQUIREMENTS = ROOT / "homeplane/arl_sdk_requirements.json"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def manifest():
    """复用唯一消费契约的摘要，不另立 SDK 接口要求。"""
    raw = LOCK_MANIFEST.read_bytes()
    contract = json.loads(REQUIREMENTS.read_text())
    if sha(raw) != contract["manifest_sha256"]:
        raise ValueError("sdk_manifest_pin_mismatch")
    value = json.loads(raw)
    if (
        value["source"]["commit"] != contract["source_commit"]
        or value["source"]["repository"] != contract["source_repository"]
    ):
        raise ValueError("sdk_provenance_mismatch")
    for name in value["files"]:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or str(path) != name or "\\" in name:
            raise ValueError("sdk_unsafe_path")
    return raw, value, contract


def checked_path(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("sdk_unsafe_path")


def checked_bytes(raw, row):
    if (
        len(raw) != row["size"]
        or sha(raw) != row["sha256"]
        or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != row["git_blob_sha1"]
    ):
        raise ValueError("sdk_source_changed")


def verify(output, *, complete=True):
    """读取完整清单；失败清理只接受已知、摘要仍匹配的部分输入。"""
    raw, value, contract = manifest()
    output = Path(output).absolute()
    checked_path(output)
    expected = {
        **value["files"],
        contract["manifest_name"]: dict(
            size=len(raw),
            sha256=sha(raw),
            git_blob_sha1=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
        ),
    }
    directories = {
        parent.as_posix()
        for name in expected
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    actual = set()
    for path in output.rglob("*"):
        checked_path(path)
        relative = path.relative_to(output).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            if relative not in directories:
                raise ValueError("sdk_inventory_mismatch")
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or relative not in expected:
            raise ValueError("sdk_inventory_mismatch")
        if info.st_size > expected[relative]["size"]:
            raise ValueError("sdk_source_changed")
        checked_bytes(path.read_bytes(), expected[relative])
        actual.add(relative)
    if complete and actual != set(expected):
        raise ValueError("sdk_inventory_mismatch")
    return dict(manifest_sha256=sha(raw), files=len(actual), source_commit=value["source"]["commit"])


def download(url, size):
    request = urllib.request.Request(url, headers={"User-Agent": "Friday-pinned-SDK-preparer"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(size + 1)
    except (OSError, urllib.error.URLError):
        raise ValueError("sdk_fetch_failed") from None


def sidecars(output):
    return output.with_name(output.name + ".prepare.json"), output.with_name(output.name + ".prepare.lock")


@contextmanager
def owned_lock(output):
    checked_path(output)
    receipt, lock = sidecars(output)
    checked_path(receipt)
    checked_path(lock)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        if os.fstat(handle.fileno()).st_uid != os.getuid():
            raise ValueError("sdk_lock_owner_mismatch")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield receipt


def prepare(output, *, fetch=download):
    output = Path(output).absolute()
    raw, value, contract = manifest()
    with owned_lock(output) as receipt:
        if output.exists():
            return {**verify(output), "reused": True}
        output.mkdir(mode=0o700)
        state = dict(
            source_project=str(ROOT),
            output=str(output),
            toolchain="Python standard library/source only",
            purpose="固定公开 ARL SDK 构建输入",
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            pid=os.getpid(),
            status="running",
            manifest_sha256=sha(raw),
            command=["python", "scripts/prepare_arl_sdk.py", "--output", str(output)],
            retention="构建与验证完成后用 --clean 删除；不清理原有正式输入或运行环境",
        )
        receipt.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        try:
            base = "https://raw.githubusercontent.com/Geeprox/ARL/" + value["source"]["commit"] + "/"
            for name, row in value["files"].items():
                data = fetch(base + name, row["size"])
                checked_bytes(data, row)
                target = output / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
            (output / contract["manifest_name"]).write_bytes(raw)
            result = verify(output)
            state["status"] = "complete"
            return {**result, "reused": False}
        except BaseException:
            # 只删除本进程创建且仍匹配已知清单的输入；出现未知写入则保留现场。
            state["status"] = "failed"
            verify(output, complete=False)
            shutil.rmtree(output)
            raise
        finally:
            state["ended_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            receipt.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def clean(output):
    """核对来源、清单和空闲锁后，只清本准备器拥有的可再生输入。"""
    output = Path(output).absolute()
    with owned_lock(output) as receipt:
        state = json.loads(receipt.read_text())
        if state.get("output") != str(output) or state.get("source_project") != str(ROOT):
            raise ValueError("sdk_cleanup_owner_mismatch")
        if output.exists():
            verify(output)
            # 当前引用与 Git 跟踪不能仅靠目录名或创建回执推断。
            if not shutil.which("git") or not shutil.which("lsof"):
                raise ValueError("sdk_cleanup_safety_tool_missing")
            tracked_root = subprocess.run(
                ["git", "-C", str(output), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if tracked_root.returncode == 0:
                base = Path(tracked_root.stdout.strip())
                tracked = subprocess.run(
                    ["git", "-C", str(base), "ls-files", "--", str(output.relative_to(base))],
                    capture_output=True,
                    timeout=20,
                )
                if tracked.returncode or tracked.stdout:
                    raise ValueError("sdk_cleanup_tracked_input")
            elif tracked_root.returncode != 128:
                raise ValueError("sdk_cleanup_git_unavailable")
            opened = subprocess.run(
                ["lsof", "-nP", "+D", str(output), "-F", "pn"], capture_output=True, text=True, timeout=20
            )
            if opened.returncode not in {0, 1} or any(
                line.startswith("n") for line in opened.stdout.splitlines()
            ):
                raise ValueError("sdk_cleanup_input_in_use")
            shutil.rmtree(output)
        state.update(status="cleaned", ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        receipt.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return {"cleaned": not output.exists(), "receipt": str(receipt)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    if args.clean and args.verify_only:
        parser.error("--clean 与 --verify-only 不能同时使用")
    try:
        result = (
            clean(args.output)
            if args.clean
            else verify(args.output)
            if args.verify_only
            else prepare(args.output)
        )
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"ok": False, "code": "sdk_input_preparation_failed"}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
