#!/usr/bin/env python3
"""在独占短临时目录运行 CI 回归，不改变生产数据库位置策略。"""

import argparse
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_receipt(path, state):
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def clean_owned(root):
    """只回收本次独占目录；未知类型、引用或跟踪状态保留现场。"""
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("ci_workspace_owner_changed")
    tracked = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, timeout=20
    )
    if tracked.returncode != 128:
        raise ValueError("ci_workspace_git_boundary_changed")
    opened = subprocess.run(
        ["lsof", "-nP", "+D", str(root), "-F", "pn"], capture_output=True, text=True, timeout=20
    )
    if opened.returncode not in {0, 1}:
        raise ValueError("ci_workspace_reference_check_failed")
    pid = None
    for line in opened.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and (pid != os.getpid() or line[1:] != str(root / "running.lock")):
            raise ValueError("ci_workspace_in_use")
    for path in root.rglob("*"):
        entry = path.lstat()
        if entry.st_uid != os.getuid() or not (
            stat.S_ISREG(entry.st_mode)
            or stat.S_ISDIR(entry.st_mode)
            or stat.S_ISLNK(entry.st_mode)
            or stat.S_ISSOCK(entry.st_mode)
            or stat.S_ISFIFO(entry.st_mode)
        ):
            raise ValueError("ci_workspace_unknown_entry")
    shutil.rmtree(root)


@contextmanager
def workspace(receipt, *, parent=Path("/tmp")):
    """持有本轮锁并留下小型回执；本机调用应显式传入任务缓存父目录。"""
    receipt = Path(receipt).absolute()
    root = Path(tempfile.mkdtemp(prefix="fci-", dir=str(parent))).resolve()
    state = dict(
        source_project=str(ROOT),
        root=str(root),
        pid=os.getpid(),
        status="running",
        created_at=datetime.now(timezone.utc).isoformat(),
        purpose="隔离 CI pytest 数据与短 socket",
        retention="退出时核对引用并清理，只保留外部小型回执",
        cleaned=False,
    )
    environment = dict(os.environ, TMPDIR=str(root), PYTHONDONTWRITEBYTECODE="1")
    write_receipt(root / "manifest.json", state)
    write_receipt(receipt, state)
    with (root / "running.lock").open("x") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield root, environment, state
        finally:
            state["ended_at"] = datetime.now(timezone.utc).isoformat()
            try:
                owner = json.loads((root / "manifest.json").read_text())
                if (
                    owner["root"] != str(root)
                    or owner["pid"] != os.getpid()
                    or owner["source_project"] != str(ROOT)
                ):
                    raise ValueError("ci_workspace_manifest_changed")
                clean_owned(root)
                state.update(status="finished", cleaned=not root.exists())
            except BaseException:
                state["status"] = "cleanup_refused"
                raise
            finally:
                write_receipt(receipt, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--temp-parent", type=Path, default=Path("/tmp"))
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    with workspace(args.receipt, parent=args.temp_parent) as (root, environment, state):
        command = [
            sys.executable,
            "-m",
            "pytest",
            "--basetemp=" + str(root / "p"),
            "-o",
            "cache_dir=" + str(root / "pytest-cache"),
            *extra,
        ]
        state["command"] = command
        state["returncode"] = subprocess.run(command, env=environment).returncode
    return state["returncode"]


if __name__ == "__main__":
    sys.exit(main())
