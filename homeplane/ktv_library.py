"""Owner song library, revision-bound batch deletion and managed-media LRU limits.

Only Friday-owned copies are included. Account sessions, model weights, tools,
official-client downloads and arbitrary user input files are never deletion roots.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

import httpx
from fastapi import HTTPException

from .ktv_service import ACTIVE, atomic_json

ID = re.compile(r"[1-9][0-9]{0,17}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
JOB = re.compile(r"[a-f0-9-]{36}\Z")
DEFAULT_POLICY = {"enabled": False, "limit_bytes": 20 * 1024**3}
DEFAULT_HOT_CACHE_BYTES = 2 * 1024**3


def read_record(path, limit=262144):
    try:
        if path.resolve() != path or path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def signature(paths):
    records, unique, unsafe = [], {}, False
    for root in sorted(paths):
        if not root.exists() and not root.is_symlink():
            continue
        if root.resolve() != root or root.is_symlink():
            unsafe = True
            continue
        items = [root, *root.rglob("*")] if root.is_dir() else [root]
        for item in items:
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                unsafe = True
            records.append([str(item), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
            if stat.S_ISREG(info.st_mode):
                unique[(info.st_dev, info.st_ino)] = info.st_size
    revision = hashlib.sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest()
    return revision, unique, unsafe


class SongLibrary:
    def __init__(self, queue, prepared, *, bridge=None, source_api=None, archive=None):
        self.queue, self.prepared = queue, Path(prepared).absolute()
        if self.prepared.resolve() != self.prepared:
            raise ValueError("unsafe_cache")
        self.bridge, self.source_api, self.archive = bridge, source_api, archive
        self.guide = getattr(bridge, "guide", None)
        self.direct = callable(getattr(bridge, "manage", None))
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.thread = None
        self.path = queue.root / "storage-policy.json"
        self.plans = {}
        self.last_cleanup = {"state": "not_run", "removed_bytes": 0, "removed": 0}
        self.nas_usage = None
        self.reservations = {}
        self.legacy_guide_hashes = {}
        self.legacy_scan_lock = threading.Lock()
        self.listing_lock = threading.Lock()
        self.listing_thread = None
        self.listing_snapshot = None
        self.listing_updated = float("-inf")
        self.listing_generation = 0
        self.listing_failed = False
        self.trim_wake = threading.Event()
        self.trim_request_lock = threading.Lock()
        self.trim_bytes_requested = 0

    def policy(self, value=None):
        if value is not None:
            with self.lock:
                if (set(value) != {"enabled", "limit_bytes"} or type(value["enabled"]) is not bool
                        or type(value["limit_bytes"]) is not int or not 2 * 1024**3 <= value["limit_bytes"] <= 100 * 1024**4):
                    raise HTTPException(400, "invalid_policy")
                atomic_json(self.path, value)
                self.request_trim()
                return dict(value)
        # 清单或清理可能在等待 NAS。容量策略采用原子替换，可独立读取。
        data = read_record(self.path)
        return data if set(data) == set(DEFAULT_POLICY) else dict(DEFAULT_POLICY)

    def remote(self, operation, value=None):
        if not self.direct:
            raise HTTPException(503, "nas_management_unavailable")
        return self.bridge.manage(operation, value or {})

    @contextmanager
    def state_lock(self):
        with ExitStack() as stack:
            if self.bridge:
                stack.enter_context(self.bridge.lock)
            stack.enter_context(self.queue.lock)
            if self.source_api:
                stack.enter_context(self.source_api.lock)
            if self.guide:
                stack.enter_context(self.guide.lock)
            yield

    def _collect(self, remote_protected=()):
        groups, linked = {}, set()
        active = any(v["state"] in ACTIVE for v in self.queue.jobs.values())
        protected_jobs = set()
        if self.bridge:
            party = self.bridge.party or {}
            protected_jobs = set(party.get("order", [])) - set((self.bridge.playback or {}).get("played", []))
            protected_jobs.add((self.bridge.playback or {}).get("current", ""))

        def group(sid, title=None):
            return groups.setdefault("song:" + sid, {"id": "song:" + sid, "title": title or "歌曲 " + sid,
                "paths": set(), "jobs": set(), "last_used": 0, "protected": sid in remote_protected,
                "busy": False, "kind": "song"})

        def attach(row, path):
            if path.exists() or path.is_symlink():
                row["paths"].add(path)
                linked.add(path)

        for value in self.queue.jobs.values():
            row = group(value["song_id"], value["title"])
            row["title"] = value["title"]
            row["jobs"].add(value["id"])
            row["last_used"] = max(row["last_used"], value.get("last_used", value["created"]))
            future = self.queue.futures.get(value["id"])
            row["busy"] |= value["state"] in ACTIVE or bool(future and not future.done())
            row["protected"] |= value["id"] in protected_jobs
            folder = self.queue.jobs_dir / value["id"]
            attach(row, folder)
            report = read_record(folder / "preparation.json")
            key = report.get("cache_key", "")
            if isinstance(key, str) and SHA.fullmatch(key):
                attach(row, self.prepared / "prepared" / key)
            if self.guide:
                future = self.guide.futures.get(value["id"])
                row["busy"] |= bool(future and not future.done())
                if self.guide.local:
                    for name in self.guide.job_files.get(value["id"], set()):
                        if re.fullmatch(r"[a-f0-9]{64}\.mp3", name):
                            attach(row, self.guide.destination / name)
            if self.direct and value["state"] == "ready":
                from .ktv_worker import manifest_version
                version = manifest_version(self.queue.snapshot(value))
                metadata = self.bridge.metadata_root / (version + ".json")
                attach(row, metadata)
                data = read_record(metadata).get("guide") or {}
                if self.guide and self.guide.local:
                    for spec in (data, data.get("reference", {})):
                        sha = spec.get("sha256", "")
                        if isinstance(sha, str) and SHA.fullmatch(sha):
                            attach(row, self.guide.destination / (sha + ".mp3"))

        for name in ("api-downloads", "cloud-downloads"):
            root = self.queue.root / name
            if root.is_dir() and not root.is_symlink():
                for folder in root.iterdir():
                    if ID.fullmatch(folder.name):
                        row = group(folder.name)
                        attach(row, folder)
                        row["last_used"] = max(row["last_used"], int(folder.lstat().st_mtime))
        root = self.queue.root / "downloads/decoded"
        if root.is_dir() and not root.is_symlink():
            for folder in root.iterdir():
                data = read_record(folder / "manifest.json")
                sid = data.get("song_id", "")
                if SHA.fullmatch(folder.name) and isinstance(sid, str) and ID.fullmatch(sid):
                    row = group(sid)
                    attach(row, folder)
                    row["last_used"] = max(row["last_used"], int(folder.lstat().st_mtime))
        root = self.queue.root / "downloads/index"
        if root.is_dir() and not root.is_symlink():
            for path in root.glob("*.json"):
                if ID.fullmatch(path.stem):
                    data = read_record(path)
                    row = group(path.stem)
                    attach(row, path)
                    key = data.get("cache_key", "")
                    if isinstance(key, str) and SHA.fullmatch(key):
                        attach(row, self.queue.root / "downloads/decoded" / key)
        lyrics = self.queue.root / "source-account/cloud-lyrics"
        if lyrics.is_dir() and not lyrics.is_symlink():
            for path in lyrics.glob("*.json"):
                if ID.fullmatch(path.stem):
                    attach(group(path.stem), path)
        if self.source_api:
            for sid, future in self.source_api.lyric_futures.items():
                if not future.done():
                    group(sid)["busy"] = True

        orphan_roots = [(self.prepared / "prepared", "未关联歌曲的伴奏", lambda p: bool(SHA.fullmatch(p.name))),
                        (self.queue.root / "downloads/jobs", "未完成的原曲处理", lambda p: p.name.startswith("download-"))]
        if self.guide and self.guide.local:
            orphan_roots.append((self.guide.destination, "未关联歌曲的伴唱", lambda p: bool(re.fullmatch(r"[a-f0-9]{64}\.mp3", p.name))))
        for root, title, valid in orphan_roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for path in root.iterdir():
                if path not in linked and valid(path):
                    key = "cache:" + hashlib.sha256(str(path).encode()).hexdigest()
                    groups[key] = {"id": key, "title": title, "paths": {path}, "jobs": set(),
                                   "last_used": int(path.lstat().st_mtime), "protected": False, "busy": active, "kind": "orphan"}
        return groups

    def _hot_cache_limit(self, policy):
        return policy["limit_bytes"] if policy["enabled"] else getattr(self.archive, "hot_limit", DEFAULT_HOT_CACHE_BYTES)

    def _hot_cache_usage(self, jobs):
        files = {}
        for job in jobs:
            if job["state"] != "ready":
                continue
            folder = self.queue.jobs_dir / job["id"]
            names = ["original.wav", "instrumental.wav"]
            if job.get("video", {}).get("status") == "ready":
                names.append("video.mp4")
            for name in names:
                try:
                    info = (folder / name).lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    files[info.st_dev, info.st_ino] = info.st_size
        return sum(files.values())

    def _local_capacity(self, jobs):
        policy = self.policy()
        hot_cache = {"bytes": self._hot_cache_usage(jobs), "limit_bytes": self._hot_cache_limit(policy)}
        cleanup = dict(self.last_cleanup)
        if not policy["enabled"]:
            cleanup = {"state": "disabled", "removed_bytes": 0, "removed": 0}
        elif hot_cache["bytes"] <= hot_cache["limit_bytes"]:
            cleanup["state"] = "within_limit"
        elif cleanup["state"] != "unavailable" and (
            cleanup.get("limit_bytes") != hot_cache["limit_bytes"] or cleanup["state"] != "retained_over_target"
        ):
            cleanup = {"state": "pending", "removed_bytes": 0, "removed": 0}
        return {"available": True, "policy": policy, "hot_cache": hot_cache, "last_cleanup": cleanup}

    def capacity(self, host):
        if host == "mac":
            with self.queue.lock:
                jobs = [dict(job) for job in self.queue.jobs.values() if job["state"] == "ready"]
            return self._local_capacity(jobs)
        if not self.direct and not self.archive:
            return {"configured": False, "available": False}
        if not self.direct and not self.archive.available():
            raise HTTPException(503, "nas_management_unavailable")
        return {"configured": True, "available": True, "policy": self.nas_policy(), "bytes": self.nas_usage}

    def local_inventory(self, remote_protected=()):
        with self.lock, self.state_lock():
            entries, unique = [], {}
            for key, row in self._collect(remote_protected).items():
                revision, files, unsafe = signature(row["paths"])
                unique.update(files)
                entries.append({"id": key, "title": row["title"], "last_used": row["last_used"], "kind": row["kind"],
                                "bytes": sum(files.values()), "revision": revision, "copies": len(row["jobs"]),
                                "can_delete": not (row["busy"] or row["protected"] or unsafe),
                                "reason": "preparing" if row["busy"] else "in_queue" if row["protected"] else "unsafe" if unsafe else ""})
            return {**self._local_capacity(self.queue.jobs.values()),
                    "entries": sorted(entries, key=lambda r: r["last_used"], reverse=True), "bytes": sum(unique.values())}

    def _index_legacy_guides(self):
        if not self.archive or not self.archive.guide_root:
            return
        with self.queue.lock:
            jobs = [dict(v) for v in self.queue.jobs.values() if v["state"] == "ready"]
        links, seen = {}, set()
        for value in jobs:
            if value["id"] in self.archive.exclusions.get(value["song_id"], []):
                continue
            for kind, spec in value.get("media", {}).items():
                path = self.queue.jobs_dir / value["id"] / (".guide-" + kind + "-" + spec["sha256"] + ".mp3")
                if path.resolve() != path or path.is_symlink() or not path.is_file() or not 1024 < path.stat().st_size <= 32 * 1024**2:
                    continue
                seen.add(path)
                info = path.stat()
                identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                previous = self.legacy_guide_hashes.get(path)
                if previous and previous[0] == identity:
                    checksum = previous[1]
                else:
                    with path.open("rb") as stream:
                        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                    after = path.stat()
                    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        continue
                    self.legacy_guide_hashes[path] = (identity, checksum)
                links.setdefault(checksum, []).append({"song_id": value["song_id"], "title": value["title"],
                    "last_used": value.get("last_used", value["created"])})
        self.legacy_guide_hashes = {p: v for p, v in self.legacy_guide_hashes.items() if p in seen}
        with self.archive.lock:
            self.archive.guide_links = links

    def _nas_inventory(self, *, listing=False):
        data = {"available": False, "configured": self.direct or bool(self.archive), "entries": [], "bytes": None}
        if self.direct:
            try:
                data = {**self.remote("library"), "configured": True}
            except HTTPException:
                pass
        elif self.archive:
            data.update(available=True, bytes=0, policy=self.nas_policy(), protected_song_ids=[])
        guide_hashes = data.get("protected_guide_hashes")
        guide_protection_known = data["available"] and isinstance(guide_hashes, list) and all(
            isinstance(checksum, str) and SHA.fullmatch(checksum) for checksum in guide_hashes
        )
        protected_guides = set(guide_hashes) if guide_protection_known else set()
        rows = {r["id"]: {**r, "remote": r, "legacy": None} for r in data["entries"]}
        if self.archive:
            try:
                with self.legacy_scan_lock:
                    self._index_legacy_guides()
                    if listing:
                        legacy = self.archive.managed_inventory(cache_catalogs=True)
                    else:
                        legacy = self.archive.managed_inventory()
            except (OSError, ValueError):
                data.update(available=False, entries=[], bytes=None)
                return data
            if data["available"]:
                data["bytes"] += legacy["bytes"]
            for key, item in legacy["entries"].items():
                public = {k: v for k, v in item.items() if k != "paths"}
                guides = {p.stem for p in item["paths"] if p.parent == self.archive.guide_root and p.suffix == ".mp3"}
                if public["can_delete"]:
                    if guides & protected_guides:
                        public.update(can_delete=False, reason="in_queue")
                    elif self.direct and guides and not guide_protection_known:
                        public.update(can_delete=False, reason="usage_unverified")
                    elif not data["available"]:
                        public.update(can_delete=False, reason="nas_offline")
                row = rows.setdefault(key, {**public, "bytes": 0, "remote": None, "legacy": None})
                row["legacy"] = public
                row["bytes"] += public["bytes"]
                row["last_used"] = max(row["last_used"], public["last_used"])
                row["can_delete"] &= public["can_delete"]
                row["reason"] = row["reason"] or public["reason"]
                row["revision"] = hashlib.sha256(json.dumps([row["remote"], public], sort_keys=True).encode()).hexdigest()
            data["legacy_bytes"] = legacy["bytes"]
            data["legacy_guide_files"] = legacy.get("legacy_guide_files", 0)
            data["legacy_guide_linked_files"] = legacy.get("legacy_guide_linked_files", 0)
        protected = set(data.get("protected_song_ids", ()))
        for row in rows.values():
            if row["id"].startswith("song:") and row["id"][5:] in protected:
                row.update(can_delete=False, reason="in_queue")
        data["entries"] = list(rows.values())
        return data

    def nas_policy(self, value=None):
        if self.direct:
            if value is not None:
                self.remote("external-usage", {"bytes": self.archive.managed_inventory()["bytes"] if self.archive else 0, "delegated": bool(self.archive)})
            return self.remote("policy", value)
        path = self.queue.root / "nas-storage-policy.json"
        if value is not None:
            if (set(value) != {"enabled", "limit_bytes"} or type(value["enabled"]) is not bool
                    or type(value["limit_bytes"]) is not int or not 2 * 1024**3 <= value["limit_bytes"] <= 100 * 1024**4):
                raise HTTPException(400, "invalid_policy")
            atomic_json(path, value)
        return read_record(path) or {"enabled": False, "limit_bytes": 100 * 1024**3}

    def inventory(self, *, listing=False):
        if listing:
            return self._combine_inventory(self._nas_inventory(listing=True))
        with self.lock:
            return self._combine_inventory(self._nas_inventory())

    def _combine_inventory(self, nas):
        if nas["available"]:
            self.nas_usage = nas["bytes"]
        mac = self.local_inventory(nas.get("protected_song_ids", ()))
        if nas["configured"] and not nas["available"]:
            for row in mac["entries"]:
                row.update(can_delete=False, reason="nas_offline")
        combined = {r["id"]: {"id": r["id"], "title": r["title"], "kind": r["kind"], "last_used": r["last_used"], "mac": r, "nas": None} for r in mac["entries"]}
        for row in nas["entries"]:
            item = combined.setdefault(row["id"], {"id": row["id"], "title": row["title"], "kind": row["kind"], "last_used": row["last_used"], "mac": None, "nas": None})
            item["nas"] = row
            item["last_used"] = max(item["last_used"], row["last_used"])
        for item in combined.values():
            if item["mac"] and item["nas"] and item["mac"]["reason"] in {"preparing", "in_queue"}:
                item["nas"].update(can_delete=False, reason=item["mac"]["reason"])
        return {"mac": {k: v for k, v in mac.items() if k != "entries"},
                "nas": {k: v for k, v in nas.items() if k not in {"entries", "protected_song_ids", "protected_guide_hashes"}},
                "entries": sorted(combined.values(), key=lambda r: r["last_used"], reverse=True),
                "original_downloads_included": True, "models_included": False}

    def refresh_listing(self):
        with self.listing_lock:
            if (self.stop.is_set() or self.listing_thread is not None
                    or time.monotonic() - self.listing_updated < 5):
                return
            generation = self.listing_generation

            def run():
                snapshot = None
                try:
                    snapshot = self.inventory(listing=True)
                except (OSError, ValueError, KeyError, HTTPException):
                    pass
                finally:
                    with self.listing_lock:
                        if generation == self.listing_generation:
                            if snapshot is not None:
                                self.listing_snapshot = snapshot
                            self.listing_failed = snapshot is None
                            self.listing_updated = time.monotonic()
                        self.listing_thread = None

            self.listing_thread = threading.Thread(target=run, name="ktv-library-list", daemon=True)
            self.listing_thread.start()

    def invalidate_listing(self):
        with self.listing_lock:
            self.listing_updated = float("-inf")
            self.listing_generation += 1
            self.listing_snapshot = None
            self.listing_failed = False

    def inventory_page(self, *, offset, limit, query, presence, sort, selectable):
        self.refresh_listing()
        with self.listing_lock:
            data = self.listing_snapshot
            failed = self.listing_failed
        if data is None:
            if failed:
                raise HTTPException(503, "library_unavailable")
            return {"indexing": True, "entries": [], "total": 0}

        def text(value):
            return unicodedata.normalize("NFKC", value).casefold()

        def matches(row):
            return (presence == "all" or presence == "orphan" and row["kind"] == "orphan"
                    or presence == "both" and row["mac"] and row["nas"]
                    or presence == "mac" and data["nas"]["available"] and row["mac"] and not row["nas"]
                    or presence == "nas" and data["mac"]["available"] and row["nas"] and not row["mac"])

        pattern = text(query.strip())
        rows = [row for row in data["entries"] if matches(row) and (not pattern or pattern in text(row["title"]))]
        if selectable:
            hosts = ("mac", "nas") if selectable == "both" else (selectable,)
            rows = [row for row in rows if (copies := [row[host] for host in hosts if row[host]])
                    and all(copy["can_delete"] for copy in copies)]
        if sort == "size":
            rows.sort(key=lambda row: -sum(copy["bytes"] for copy in (row["mac"], row["nas"]) if copy))
        if sort == "title":
            rows.sort(key=lambda row: text(row["title"]))
        return {**data, "entries": rows[offset:offset + limit], "total": len(rows), "indexing": False}

    def _validate_legacy_deletion(self, selected):
        current = {r["id"]: r["nas"] for r in self.inventory()["entries"]}
        for item in selected:
            row = current.get(item["id"])
            if row and row["reason"] == "usage_unverified":
                raise HTTPException(409, "cache_usage_unverified")
            if not row or not row["can_delete"]:
                raise HTTPException(409, "cache_in_use")
            if not row["legacy"] or row["legacy"]["revision"] != item["revision"]:
                raise HTTPException(409, "cache_changed")

    def _delete_nas(self, items, plan):
        stores = plan.setdefault("nas_stores", {})
        legacy = [{"id": row["legacy"]["id"], "revision": row["legacy"]["revision"]}
                  for row in items if row.get("legacy")]
        if legacy and "legacy" not in stores:
            self._validate_legacy_deletion(legacy)
        for name in ("remote", "legacy"):
            selected = [{"id": row[name]["id"], "revision": row[name]["revision"]} for row in items if row.get(name)]
            if not selected or name in stores:
                continue
            if name == "remote":
                stores[name] = self.remote("delete", {"items": selected, "request_id": plan["request_id"]})
            else:
                if "remote" in stores:
                    self._validate_legacy_deletion(selected)
                try:
                    stores[name] = self.archive.remove_all(selected)
                except (OSError, ValueError):
                    raise HTTPException(409, "archive_changed") from None
        if self.direct and not plan.get("queue_reconciled"):
            # A purged, already-sung entry must not remain as an unplayable
            # ready item. Remove only completed history, never a new request.
            scene = self.remote("room")
            if scene.get("active"):
                selected_songs = {row["id"][5:] for row in items if row["id"].startswith("song:")}
                status = scene["status"]
                ids = [row["id"] for row in status["songs"] if row["song_id"] in selected_songs
                       and row["id"] != status["playback"]["current"]
                       and (row["id"] in status["playback"]["played"] or row["state"] in {"failed", "cancelled"})]
                if ids:
                    self.remote("queue-remove", {"room_id": scene["room_id"], "value": {"ids": ids, "revision": status["queue_revision"]}})
            plan["queue_reconciled"] = True
        if self.direct and self.archive:
            self.remote("external-usage", {"bytes": self.archive.managed_inventory()["bytes"], "delegated": True})
        return {"removed": len(items), "removed_bytes": sum(r["removed_bytes"] for r in stores.values())}

    def preview(self, value):
        if (set(value) != {"ids", "scope"} or not isinstance(value["scope"], str) or value["scope"] not in {"mac", "nas", "both"}
                or not isinstance(value["ids"], list) or not 1 <= len(value["ids"]) <= 128
                or any(not isinstance(i, str) for i in value["ids"]) or len(set(value["ids"])) != len(value["ids"])):
            raise HTTPException(400, "invalid_selection")
        with self.lock:
            data = self.inventory()
            rows = {r["id"]: r for r in data["entries"]}
            selected = []
            for key in value["ids"]:
                row = rows.get(key)
                if not row:
                    raise HTTPException(409, "cache_changed")
                hosts = [h for h in ("mac", "nas") if value["scope"] in (h, "both") and row[h]]
                if not hosts or any(not row[h]["can_delete"] for h in hosts):
                    reason = "cache_usage_unverified" if any(row[h]["reason"] == "usage_unverified" for h in hosts) else "cache_in_use"
                    raise HTTPException(409, reason)
                selected.append({"id": key, "title": row["title"], **{h: row[h] if h in hosts else None for h in ("mac", "nas")}})
            if value["scope"] in {"nas", "both"} and not data["nas"]["available"]:
                raise HTTPException(503, "nas_management_unavailable")
            token = secrets.token_urlsafe(32)
            self.plans = {k: v for k, v in self.plans.items() if time.monotonic() < v["expires"]}
            if len(self.plans) >= 16:
                self.plans.pop(next(iter(self.plans)))
            self.plans[token] = {"rows": selected, "expires": time.monotonic() + 300, "request_id": str(uuid.uuid4()), "results": {}}
            return {"plan": token, "songs": [{"id": r["id"], "title": r["title"]} for r in selected], "scope": value["scope"],
                    "bytes": {h: sum((r[h] or {}).get("bytes", 0) for r in selected) for h in ("mac", "nas")}, "expires_in": 300}

    @contextmanager
    def file_locks(self, song_ids):
        with ExitStack() as stack:
            paths = [self.prepared / "prepare.lock", self.queue.root / "downloads/decode.lock"]
            for name in ("api-downloads", "cloud-downloads", "downloads"):
                paths.extend(self.queue.root / name / "locks" / (sid + ".lock") for sid in sorted(song_ids))
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if path.parent.resolve() != path.parent:
                    raise HTTPException(409, "unsafe_cache")
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                stack.callback(os.close, fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise HTTPException(409, "cache_in_use") from None
            yield

    def _delete_local(self, items, protected):
        with self.state_lock(), self.file_locks({r["id"][5:] for r in items if r["id"].startswith("song:")}):
            rows = {r["id"]: r for r in self.local_inventory(protected)["entries"]}
            for item in items:
                current = rows.get(item["id"])
                if not current or current["revision"] != item["revision"]:
                    raise HTTPException(409, "cache_changed")
                if not current["can_delete"]:
                    raise HTTPException(409, "cache_in_use")
            groups = self._collect(protected)
            ids = {r["id"] for r in items}
            paths = set().union(*(groups[key]["paths"] for key in ids))
            keep = set().union(*(r["paths"] for key, r in groups.items() if key not in ids)) if len(groups) > len(ids) else set()
            removed_jobs = set().union(*(groups[key]["jobs"] for key in ids))
            before = self.local_inventory(protected)["bytes"]
            if not shutil.rmtree.avoids_symlink_attacks:
                raise HTTPException(409, "unsafe_cache")
            for job_id in removed_jobs:
                self.queue.remove(job_id)
            for path in sorted(paths - keep, key=lambda p: len(p.parts), reverse=True):
                if path.is_symlink() or path.resolve() != path:
                    raise HTTPException(409, "unsafe_cache")
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.is_file():
                    path.unlink()
            if self.direct:
                self.bridge.mapping = {k: v for k, v in self.bridge.mapping.items() if v not in removed_jobs}
                self.bridge.archive_pending -= removed_jobs
                self.bridge.terminal_acked -= removed_jobs
                self.bridge.published_metadata.clear()
            if self.guide:
                for job_id in removed_jobs:
                    self.guide.futures.pop(job_id, None)
                    self.guide.job_files.pop(job_id, None)
                    self.guide.retry_after.pop(job_id, None)
            after = self.local_inventory(protected)["bytes"]
        if self.direct:
            self.bridge._save()
        return {"removed": len(items), "removed_bytes": max(0, before - after), "original_downloads_deleted": True, "models_deleted": False}

    def delete(self, value):
        if set(value) != {"plan"} or not isinstance(value["plan"], str):
            raise HTTPException(400, "invalid_plan")
        with self.lock:
            plan = self.plans.get(value["plan"])
            if not plan or time.monotonic() >= plan["expires"]:
                raise HTTPException(409, "plan_expired")
            items = {h: [r[h] for r in plan["rows"] if r[h]] for h in ("mac", "nas")}
            # Check the local selection before any irreversible remote action.
            data = self.inventory()
            current = {r["id"]: r for r in data["entries"]}
            for item in items["mac"] if "mac" not in plan["results"] else []:
                row = (current.get(item["id"]) or {}).get("mac")
                if not row or row["revision"] != item["revision"] or not row["can_delete"]:
                    raise HTTPException(409, "cache_changed")
            for host in ("nas", "mac"):
                if not items[host] or host in plan["results"]:
                    continue
                try:
                    result = self._delete_nas(items[host], plan) if host == "nas" else self._delete_local(items[host], self.remote("library")["protected_song_ids"] if self.direct else ())
                    plan["results"][host] = {"state": "removed", **result}
                except (HTTPException, OSError) as error:
                    code = error.detail if isinstance(error, HTTPException) else "delete_failed"
                    return {"complete": False, "results": dict(plan["results"]), "nas_stores": dict(plan.get("nas_stores", {})), "failed_host": host, "code": code,
                            "retryable": code not in {"cache_changed", "archive_changed", "cache_in_use", "plan_expired"}}
            return {"complete": True, "results": dict(plan["results"])}

    def enforce(self, reserve=0):
        with self.lock:
            with self.queue.lock:
                self.reservations = {k: v for k, v in self.reservations.items() if self.queue.jobs.get(k, {}).get("state") in ACTIVE}
            reserve += sum(self.reservations.values())
            policy = self.policy()
            if not policy["enabled"]:
                return {"state": "disabled", "removed_bytes": 0, "removed": 0}
            protected = self.remote("library")["protected_song_ids"] if self.direct else ()
            data, removed, total = self.local_inventory(protected), 0, 0
            for row in sorted(data["entries"], key=lambda r: (r["last_used"], r["id"])):
                if data["bytes"] + reserve <= policy["limit_bytes"]:
                    break
                if not row["can_delete"]:
                    continue
                current = next((r for r in self.local_inventory(protected)["entries"] if r["id"] == row["id"]), None)
                if current and current["can_delete"]:
                    try:
                        result = self._delete_local([current], protected)
                    except HTTPException:
                        continue
                    removed += result["removed"]
                    total += result["removed_bytes"]
                    data = self.local_inventory(protected)
            self.last_cleanup = {"state": "within_limit" if data["bytes"] + reserve <= policy["limit_bytes"] else "protected_over_limit",
                                 "removed_bytes": total, "removed": removed, "at": int(time.time())}
            return dict(self.last_cleanup)

    def enforce_nas(self):
        if not self.direct and not self.archive:
            return
        # 先在锁外检查容量。只有达到清理条件时才读取完整的最新校验信息。
        nas = self._nas_inventory(listing=True)
        if not nas["available"]:
            return
        self.nas_usage = nas["bytes"]
        if self.direct:
            self.remote("external-usage", {"bytes": nas.get("legacy_bytes", 0), "delegated": bool(self.archive)})
        policy = nas["policy"]
        if not policy["enabled"] or nas["bytes"] <= policy["limit_bytes"]:
            return
        with self.lock:
            data = self.inventory()
            if not data["nas"]["available"] or not data["nas"]["policy"]["enabled"]:
                return
            for row in sorted(data["entries"], key=lambda r: (r["last_used"], r["id"])):
                if data["nas"]["bytes"] <= data["nas"]["policy"]["limit_bytes"]:
                    break
                if not row["nas"] or not row["nas"]["can_delete"] or row["mac"] and not row["mac"]["can_delete"]:
                    continue
                current = next((r for r in self.inventory()["entries"] if r["id"] == row["id"]), None)
                if current and current["nas"] and current["nas"]["can_delete"]:
                    try:
                        self._delete_nas([current["nas"]], {"request_id": str(uuid.uuid4())})
                    except HTTPException:
                        continue
                    data = self.inventory()

    def reserve(self, job_id, amount):
        with self.lock:
            policy = self.policy()
            if not policy["enabled"]:
                return False
            self.reservations[job_id] = amount
            if self.enforce()["state"] == "protected_over_limit":
                self.reservations.pop(job_id, None)
                raise ValueError("storage_full")
            return True

    def release(self, job_id):
        with self.lock:
            self.reservations.pop(job_id, None)

    def request_trim(self, required_bytes=0):
        with self.trim_request_lock:
            self.trim_bytes_requested = max(self.trim_bytes_requested, required_bytes)
            self.trim_wake.set()

    def _hot_cache_protected(self):
        protected = set(self.archive.active) if self.archive else set()
        if self.bridge:
            getter = getattr(self.bridge, "protected_jobs", None)
            if callable(getter):
                protected.update(getter())
            else:
                report = self.bridge.playback or {}
                protected.update(set((self.bridge.party or {}).get("order", [])) - set(report.get("played", [])))
                protected.add(report.get("current"))
        return protected

    def _archived_on_nas(self, jobs):
        # 直连 NAS 按成品 SHA 和大小确认。兼容挂载路径还核对音源版本。
        verified = set()
        if self.direct and callable(getattr(self.bridge, "request", None)):
            payload = []
            for value in jobs:
                specs = list(value["media"].values())
                if value.get("video", {}).get("status") == "ready":
                    specs.append(value["video"])
                payload.append({"id": value["id"], "files": [
                    {"sha256": spec["sha256"], "bytes": spec["bytes"]} for spec in specs]})
            try:
                result = self.bridge.request("/agent/archive-check", {"items": payload})
                verified.update(row["id"] for row in result["items"] if row["state"] == "ready")
            except (AttributeError, KeyError, TypeError, OSError, ValueError, RuntimeError, HTTPException, httpx.HTTPError):
                pass
        if self.archive:
            for job in jobs:
                if job["id"] not in verified and self.archive.verify_prepared(job["song_id"], job=job):
                    verified.add(job["id"])
        return verified

    def trim_hot_cache(self, required_bytes=0):
        """仅淘汰已核对 NAS 成品的本机缓存，按最近使用时间回收。"""
        with self.lock:
            policy = self.policy()
            limit = self._hot_cache_limit(policy)
            with self.state_lock():
                protected = self._hot_cache_protected()
                ready = [dict(job) for job in self.queue.jobs.values() if job["state"] == "ready"]
                used = self._hot_cache_usage(ready)
            target = max(0, used - limit, required_bytes)
            removed = removed_bytes = 0
            candidates = sorted((job for job in ready if job["id"] not in protected),
                                key=lambda job: (job.get("last_used", job["created"]), job["id"]))
            # 分批验证，磁盘有足够空间便结束。每轮只检查有限的备选项。
            for start in range(0, min(32, len(candidates)), 8):
                if removed_bytes >= target or self.stop.is_set():
                    break
                batch = candidates[start:start + 8]
                verified = self._archived_on_nas(batch)
                for job in batch:
                    if removed_bytes >= target:
                        break
                    job_id = job["id"]
                    if job_id not in verified:
                        continue
                    with self.state_lock():
                        current = self.queue.jobs.get(job_id)
                        if current != job or job_id in self._hot_cache_protected():
                            continue
                        try:
                            folder = self.queue.jobs_dir / job_id
                            files = [p.lstat() for p in folder.rglob("*") if p.is_file() and not p.is_symlink()]
                            self.queue.remove(job_id)
                        except (HTTPException, OSError):
                            continue
                        if self.direct:
                            self.bridge.mapping = {k: v for k, v in self.bridge.mapping.items() if v != job_id}
                            self.bridge.archive_pending.discard(job_id)
                            self.bridge.terminal_acked.discard(job_id)
                        removed += 1
                        removed_bytes += sum(info.st_size for info in files if info.st_nlink == 1)
            if removed and self.direct:
                self.bridge._save()
            with self.state_lock():
                remaining = self._hot_cache_usage(self.queue.jobs.values()) if removed else used
            self.last_cleanup = {
                "state": ("disabled" if not policy["enabled"] else
                          "within_limit" if remaining <= limit else "retained_over_target"),
                "limit_bytes": limit, "remaining_bytes": remaining,
                "removed_bytes": removed_bytes, "removed": removed, "at": int(time.time()),
            }
            return dict(self.last_cleanup)

    def start(self):
        self.refresh_listing()

        def run():
            next_nas_check = time.monotonic() + 30
            while not self.stop.is_set():
                self.trim_wake.wait(30)
                if self.stop.is_set():
                    break
                with self.trim_request_lock:
                    pressure = self.trim_bytes_requested
                    self.trim_bytes_requested = 0
                    self.trim_wake.clear()
                try:
                    self.trim_hot_cache(pressure)
                except Exception:
                    self.last_cleanup = {"state": "unavailable", "removed_bytes": 0, "removed": 0}
                if time.monotonic() >= next_nas_check:
                    next_nas_check = time.monotonic() + 30
                    try:
                        self.enforce_nas()
                    except Exception:
                        pass
        self.thread = threading.Thread(target=run, name="ktv-storage", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.trim_wake.set()
        if self.thread:
            self.thread.join(2)
