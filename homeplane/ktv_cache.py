"""Owner-only accompaniment cache inventory and explicit, revision-bound removal."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import threading
from pathlib import Path

from fastapi import HTTPException


class CacheManager:
    def __init__(self, queue, prepared, protected=lambda: set(), protected_lock=None):
        self.protected_lock = protected_lock or threading.RLock()
        self.queue, self.prepared, self.protected = queue, Path(prepared).absolute(), protected
        self.archive = None
        if self.prepared.resolve() != self.prepared:
            raise ValueError("unsafe_cache")

    def inventory(self):
        with self.protected_lock, self.queue.lock:
            protected = set(self.protected())
            busy = any(
                j["state"] in {"queued", "acquiring", "separating", "transcoding"}
                for j in self.queue.jobs.values()
            )
            groups = {}
            for value in self.queue.jobs.values():
                if value["state"] != "ready":
                    continue
                key = "song:" + value["song_id"]
                group = groups.setdefault(
                    key,
                    {
                        "id": key,
                        "title": value["title"],
                        "jobs": [],
                        "paths": [],
                        "last_used": 0,
                        "protected": False,
                        "kind": "song",
                    },
                )
                folder = self.queue.jobs_dir / value["id"]
                group["jobs"].append(value["id"])
                group["paths"].append(folder)
                group["protected"] |= value["id"] in protected
                group["last_used"] = max(group["last_used"], value.get("last_used", value["created"]))
                report = folder / "preparation.json"
                if report.is_file() and not report.is_symlink() and report.stat().st_size < 65536:
                    try:
                        cache_key = json.loads(report.read_text()).get("cache_key", "")
                        if re.fullmatch("[0-9a-f]{64}", cache_key):
                            group["paths"].append(self.prepared / "prepared" / cache_key)
                    except (ValueError, OSError):
                        pass
            linked = {p for v in groups.values() for p in v["paths"]}
            prepared = self.prepared / "prepared"
            if prepared.is_dir() and not prepared.is_symlink():
                for entry in sorted(prepared.iterdir())[:512]:
                    if (
                        re.fullmatch("[0-9a-f]{64}", entry.name)
                        and entry not in linked
                        and entry.is_dir()
                        and not entry.is_symlink()
                    ):
                        key = "stem:" + entry.name
                        groups[key] = {
                            "id": key,
                            "title": "未关联曲目的伴奏",
                            "jobs": [],
                            "paths": [entry],
                            "last_used": int(entry.stat().st_mtime),
                            "protected": False,
                            "kind": "stem",
                        }
            result = []
            for group in groups.values():
                unique = {}
                manifest = []
                unsafe = False
                for path in set(group["paths"]):
                    if not path.exists():
                        continue
                    if path.is_symlink() or path.resolve() != path:
                        unsafe = True
                        continue
                    for item in path.rglob("*"):
                        if item.is_symlink():
                            unsafe = True
                            continue
                        if not item.is_file():
                            continue
                        stat = item.stat()
                        unique[(stat.st_dev, stat.st_ino)] = stat.st_size
                        manifest.append(
                            (str(item.relative_to(path)), stat.st_ino, stat.st_size, stat.st_mtime_ns)
                        )
                fingerprint = hashlib.sha256(json.dumps(sorted(manifest)).encode()).hexdigest()
                result.append(
                    {k: group[k] for k in ("id", "title", "last_used", "kind")}
                    | {
                        "bytes": sum(unique.values()),
                        "revision": fingerprint,
                        "copies": len(group["jobs"]),
                        "can_delete": not busy and not unsafe and not group["protected"],
                        "reason": "preparing"
                        if busy
                        else "in_queue"
                        if group["protected"]
                        else "unsafe"
                        if unsafe
                        else "",
                    }
                )
            archived = self.archive.summaries() if self.archive else {}
            for song_id, info in archived.items():
                if not info["bytes"]:
                    continue
                row = next((r for r in result if r["id"] == "song:" + song_id), None)
                if row is None:
                    row = {"id": "song:" + song_id, "title": info["title"], "last_used": info["updated"],
                           "kind": "song", "bytes": 0, "revision": "", "copies": 0,
                           "can_delete": not busy, "reason": "preparing" if busy else ""}
                    result.append(row)
                row["local_bytes"], row["nas_bytes"] = row["bytes"], info["bytes"]
                row["bytes"] += info["bytes"]
                row["revision"] = hashlib.sha256((row["revision"] + info["revision"]).encode()).hexdigest()
            return {
                "entries": sorted(result, key=lambda r: r["last_used"], reverse=True),
                "bytes": sum(r["bytes"] for r in result),
                "preparing": busy,
                "original_downloads_included": False,
                "models_included": False,
                "archive": self.archive.status() if self.archive else {"configured": False},
            }

    def delete(self, entry_id, revision, *, local_only=False):
        with self.protected_lock, self.queue.lock:
            current = next((r for r in self.inventory()["entries"] if r["id"] == entry_id), None)
            if not current or current["revision"] != revision:
                raise HTTPException(409, "cache_changed")
            if not current["can_delete"]:
                raise HTTPException(409, "cache_in_use")
            lock_path = self.prepared / "prepare.lock"
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise HTTPException(409, "cache_in_use") from None
                paths = set()
                if entry_id.startswith("song:"):
                    song_id = entry_id[5:]
                    if self.archive:
                        if local_only:
                            summary = self.archive.summaries().get(song_id)
                            if not summary or not summary["bytes"] or not self.archive.verify_prepared(song_id):
                                raise HTTPException(409, "archive_not_ready")
                        else:
                            try:
                                self.archive.remove_prepared(song_id)
                            except (OSError, ValueError):
                                raise HTTPException(409, "archive_not_ready") from None
                    for job in list(self.queue.jobs.values()):
                        if job["song_id"] != song_id or job["state"] != "ready":
                            continue
                        folder = self.queue.jobs_dir / job["id"]
                        report = folder / "preparation.json"
                        if report.is_file() and report.stat().st_size < 65536:
                            try:
                                key = json.loads(report.read_text()).get("cache_key", "")
                                if re.fullmatch("[0-9a-f]{64}", key):
                                    paths.add(self.prepared / "prepared" / key)
                            except (ValueError, OSError):
                                pass
                        self.queue.remove(job["id"])
                elif re.fullmatch("stem:[0-9a-f]{64}", entry_id):
                    paths.add(self.prepared / "prepared" / entry_id[5:])
                else:
                    raise HTTPException(400, "invalid_cache")
                for path in paths:
                    if path.exists():
                        if path.resolve() != path or path.is_symlink():
                            raise HTTPException(409, "unsafe_cache")
                        shutil.rmtree(path)
                if local_only and self.archive and entry_id.startswith("song:"):
                    self.archive.prune_source_local(entry_id[5:])
                return {"removed": True, "original_downloads_deleted": False, "models_deleted": False}
            finally:
                os.close(descriptor)
