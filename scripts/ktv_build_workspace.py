"""Owned disposable KTV build areas; deliverables remain outside this cache."""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from .workspace_references import workspace_in_use
else:
    from workspace_references import workspace_in_use

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def build_workspace(toolchain: str, purpose: str, *, configuration: str = "release"):
    """Hold ownership until all child tools exit, then remove this run's payload."""
    for value in (toolchain, purpose, configuration):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
            raise ValueError("bounded_build_identity_required")
    project = ROOT.resolve(strict=True)
    base = Path.home() / "Library/Caches/CodexBuilds.noindex"
    base.mkdir(parents=True, exist_ok=True)
    if base.resolve() != base:
        raise ValueError("canonical_cache_root_required")
    namespace = project.name + "-" + hashlib.sha256(str(project).encode()).hexdigest()[:12]
    area = base / namespace / toolchain / configuration / (purpose + "-" + secrets.token_hex(6))
    area.mkdir(parents=True, mode=0o700)
    if area.resolve() != area or not area.is_relative_to(base):
        raise ValueError("isolated_build_path_required")
    def stamp():
        return datetime.datetime.now().astimezone().isoformat()
    record = {"source_project": str(project), "toolchain": toolchain, "purpose": purpose,
              "configuration": configuration, "created_at": stamp(), "ended_at": None,
              "status": "running", "pid": os.getpid(), "retain": False,
              "reproduce_command": [sys.executable, *sys.argv], "actual_output_path": str(area)}
    manifest = area / "manifest.json"
    lock_path = area / "active.lock"
    with lock_path.open("x+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def save():
            manifest.write_text(json.dumps(record, indent=2) + "\n")

        save()
        try:
            yield area, record
            record["status"] = "complete"
        except BaseException:
            record["status"] = "failed"
            raise
        finally:
            record["ended_at"] = stamp()
            # 核对所有子进程退出后的实际句柄，保留仍被引用的文件。
            try:
                if workspace_in_use(area, own_lock=lock_path):
                    raise RuntimeError("build_payload_still_open")
                logical = allocated = 0
                seen = set()
                for item in area.rglob("*"):
                    info = item.lstat()
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                        raise RuntimeError("unidentified_build_object")
                    if stat.S_ISREG(info.st_mode) and item not in (manifest, lock_path):
                        identity = info.st_dev, info.st_ino
                        if identity not in seen:
                            seen.add(identity)
                            logical += info.st_size
                            allocated += info.st_blocks * 512
                for item in area.iterdir():
                    if item in (manifest, lock_path):
                        continue
                    if item.is_dir() and not item.is_symlink():
                        if not shutil.rmtree.avoids_symlink_attacks:
                            raise RuntimeError("safe_build_cleanup_unavailable")
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                record.update(payload_removed=True, logical_bytes_unlinked=logical,
                              allocated_bytes_inventoried=allocated, actual_free_space_delta_measured=False)
            except BaseException as failure:
                record.update(retain=True, cleanup_failure=type(failure).__name__, payload_removed=False)
                raise
            finally:
                record["exclusive_lock_released"] = True
                save()
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
