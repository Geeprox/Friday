"""Room-scoped compressed original for a guest's optional ear monitor. No mic recording."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

CACHE_BUDGET_BYTES = 512 * 1024 * 1024


class GuidePublisher:
    def __init__(self, queue, config_path=None, *, local=False):
        self.queue = queue
        self.local = local
        value = json.loads(Path(config_path).read_text()) if not local else {}
        self.mount = Path(value["mount"]).absolute() if not local else queue.jobs_dir.parent.absolute()
        self.destination = Path(value["directory"]).absolute() if not local else self.mount / "guide-cache"
        if self.destination.resolve() != self.destination or not self.destination.is_relative_to(self.mount):
            raise ValueError("invalid_guide_directory")
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ktv-guide")
        self.futures = {}
        self.retry_after = {}
        self.protected = set()
        self.job_files = {}
        self.verified_files = {}
        self.encodings = {}
        self.closed = False
        self.lock = threading.RLock()
        self.excluded_jobs = set()

    @staticmethod
    def _identity(path):
        try:
            value = path.lstat()
            if not stat.S_ISREG(value.st_mode) or path.resolve() != path:
                return None
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        except OSError:
            return None

    @classmethod
    def _checksum(cls, path):
        before = cls._identity(path)
        if before is None:
            raise ValueError("guide_changed")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            opened = os.fstat(stream.fileno())
            identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            if identity != before:
                raise ValueError("guide_changed")
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if cls._identity(path) != before:
            raise ValueError("guide_changed")
        return checksum, before[2], before

    @classmethod
    def _matches(cls, path, checksum, size):
        try:
            actual, length, _ = cls._checksum(path)
            return actual == checksum and length == size
        except (OSError, ValueError):
            return False

    def _publish_variant(self, folder, variant, spec):
        source = folder / (variant + ".wav")
        source_sha, _, source_identity = self._checksum(source)
        if source_sha != spec["sha256"]:
            raise ValueError("source_changed")
        # A filename containing a source hash does not verify its encoded bytes.
        # Unknown encodings (including files from an earlier process) are rebuilt;
        # known encodings may be reused only after checking their recorded digest.
        local = folder / (".guide-" + variant + "-" + source_sha + ".mp3")
        known = self.encodings.get(local)
        if not known or not self._matches(local, *known):
            part = local.with_suffix(".part.mp3")
            try:
                part.unlink(missing_ok=True)
                subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(source),
                    "-map", "0:a:0", "-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", "-map_metadata", "-1", str(part)],
                    check=True, timeout=90, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if self._identity(source) != source_identity:
                    raise ValueError("source_changed")
                part.chmod(0o600)
                part.replace(local)
            finally:
                part.unlink(missing_ok=True)
        checksum, size, _ = self._checksum(local)
        if not 1024 < size <= 32 * 1024 * 1024:
            raise ValueError("guide_budget_exceeded")
        self.encodings[local] = (checksum, size)
        target = self.destination / (checksum + ".mp3")
        if not self._matches(target, checksum, size):
            part = self.destination / (checksum + ".part")
            try:
                part.unlink(missing_ok=True)
                shutil.copyfile(local, part)
                part.chmod(0o600)
                with part.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                        raise ValueError("guide_copy_changed")
                os.replace(part, target)
            finally:
                part.unlink(missing_ok=True)
        target.touch()
        actual, length, identity = self._checksum(target)
        if actual != checksum or length != size:
            raise ValueError("guide_copy_changed")
        with self.lock:
            self.verified_files[checksum] = identity
        return {"sha256": checksum, "bytes": size, "duration_ms": round(spec["duration_seconds"] * 1000)}, target.name

    def _prepare(self, job_id):
        if (not self.local and not self.mount.is_mount()) or not self.destination.parent.is_dir():
            raise ValueError("guide_offline")
        folder = self.queue.jobs_dir / job_id
        media = self.queue.get(job_id)["media"]
        self.destination.mkdir(mode=0o700, exist_ok=True)
        original, name = self._publish_variant(folder, "original", media["original"])
        names = {name}
        if "instrumental" in media:
            if abs(media["original"]["duration_seconds"] - media["instrumental"]["duration_seconds"]) > 0.1:
                raise ValueError("guide_timeline_mismatch")
            reference, name = self._publish_variant(folder, "instrumental", media["instrumental"])
            names.add(name)
            original["reference"] = {**reference, "kind": "accompaniment"}
        with self.lock:
            self.job_files[job_id] = names
            keep = set().union(*(self.job_files.get(i, set()) for i in self.protected), names)
        files = [p for p in self.destination.glob("*.mp3") if len(p.stem) == 64 and not p.is_symlink()]
        used = sum(p.stat().st_size for p in files)
        for path in sorted(files, key=lambda p: p.stat().st_mtime):
            if used <= CACHE_BUDGET_BYTES:
                break
            if path.name not in keep:
                removed_size = path.stat().st_size
                path.unlink()
                with self.lock:
                    self.verified_files.pop(path.stem, None)
                used -= removed_size
        if self.local:
            original["url"] = f"/v1/jobs/{job_id}/media/guide_original"
            if "reference" in original:
                original["reference"]["url"] = f"/v1/jobs/{job_id}/media/guide_reference"
        return original

    def media(self, job_id, variant):
        if not self.local or variant not in {"guide_original", "guide_reference"}:
            raise ValueError("guide_unavailable")
        value = self.get(job_id)
        spec = value if variant == "guide_original" else (value or {}).get("reference")
        if not spec:
            raise ValueError("guide_unavailable")
        path = self.destination / (spec["sha256"] + ".mp3")
        stream = None
        try:
            if self._identity(path) is None:
                raise ValueError("guide_changed")
            stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb")
            if os.fstat(stream.fileno()).st_size != spec["bytes"]:
                raise ValueError("guide_changed")
            if hashlib.file_digest(stream, "sha256").hexdigest() != spec["sha256"]:
                raise ValueError("guide_changed")
            stream.seek(0)
            return stream, spec
        except BaseException as error:
            if stream is not None:
                stream.close()
            if isinstance(error, (OSError, ValueError)):
                with self.lock:
                    self._rebuild_locked(job_id)
            raise

    def protect(self, jobs):
        with self.lock:
            self.protected = set(jobs)

    def get(self, job_id):
        with self.lock:
            if self.closed or job_id in self.excluded_jobs:
                return None
            future = self.futures.get(job_id)
            if future is None:
                if time.monotonic() < self.retry_after.get(job_id, 0):
                    return None
                self.futures[job_id] = self.pool.submit(self._prepare, job_id)
                return None
            if future.done():
                try:
                    value = future.result()
                except Exception:
                    self.futures.pop(job_id, None)
                    self.retry_after[job_id] = time.monotonic() + 30
                    return None
                specs = [value] + ([value["reference"]] if "reference" in value else [])
                # Only cheap filesystem identity checks run on the polling lane.
                # Hashing, encoding and copying remain in the background pool.
                for spec in specs:
                    identity = self._identity(self.destination / (spec["sha256"] + ".mp3"))
                    if (identity is None or identity[2] != spec["bytes"]
                            or self.verified_files.get(spec["sha256"]) != identity):
                        self._rebuild_locked(job_id)
                        return None
                return value
        return None

    def exclude(self, jobs):
        with self.lock:
            self.excluded_jobs.update(jobs)
            self.protected.difference_update(jobs)

    def _rebuild_locked(self, job_id):
        future = self.futures.get(job_id)
        if not self.closed and (future is None or future.done()):
            self.retry_after.pop(job_id, None)
            self.futures[job_id] = self.pool.submit(self._prepare, job_id)

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
