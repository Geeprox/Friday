"""Ephemeral, source-bound readonly MP4 byte ranges. No parent NAS file IO."""

import asyncio
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import timedelta

from .archive_contracts import ArchivePlaybackPlan

RANGE_BUDGET = 8 * 1024 * 1024
SESSION_BUDGET = 64 * 1024 * 1024


def byte_range(value, size):
    if value is None:
        if size > RANGE_BUDGET:
            raise ValueError("archive_media_range_required")
        return 0, size, False
    match = re.fullmatch(r"bytes=([0-9]{0,10})-([0-9]{0,10})", value)
    if not match or not any(match.groups()):
        raise ValueError("archive_invalid_media_range")
    a, b = match.groups()
    if a:
        start, end = int(a), min(int(b) if b else size - 1, size - 1)
        if start >= size or start > end:
            raise ValueError("archive_invalid_media_range")
    else:
        count = int(b)
        if not count:
            raise ValueError("archive_invalid_media_range")
        start, end = max(0, size - count), size - 1
    return start, min(end - start + 1, RANGE_BUDGET), True


@dataclass
class Lease:
    plan: ArchivePlaybackPlan
    binding: dict
    deadline: float
    remaining: int = SESSION_BUDGET
    revoked: bool = False


class ArchiveReader:
    def __init__(self, owner, lease, start, length, partial):
        self.owner, self.lease = owner, lease
        self.start, self.length, self.partial = start, length, partial
        self.process, self.closed = None, False
        self.cleanup_task = None
        self.stdout_lock = asyncio.Lock()

    async def read_stdout(self, size, *, line=False):
        async with self.stdout_lock:
            if self.cleanup_task is not None:
                raise ValueError("archive_playback_expired")
            chunk = await (self.process.stdout.readline() if line else self.process.stdout.read(size))
            if self.cleanup_task is not None:
                raise ValueError("archive_playback_expired")
            return chunk

    async def open(self):
        private = self.lease.binding
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "homeplane.archive_worker",
            "--root",
            private["root"],
            "--operation",
            "range",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            limit=65536,
        )
        request = {
            "path": private["path"],
            "signature": private["signature"],
            "start": self.start,
            "length": self.length,
        }
        async with asyncio.timeout(3):
            self.process.stdin.write(json.dumps(request).encode())
            await self.process.stdin.drain()
            self.process.stdin.close()
            header = await self.read_stdout(2048, line=True)
            if len(header) > 2048 or json.loads(header) != {
                "status": "readable",
                "start": self.start,
                "length": self.length,
            }:
                raise ValueError("archive_source_changed_or_unavailable")
            self.owner.valid(self.lease)

    async def chunks(self):
        remaining = self.length
        try:
            async with asyncio.timeout(12):
                while remaining:
                    self.owner.valid(self.lease)
                    chunk = await self.read_stdout(min(65536, remaining))
                    self.owner.valid(self.lease)
                    if not chunk:
                        raise ValueError("archive_media_interrupted")
                    remaining -= len(chunk)
                    yield chunk
                if await self.read_stdout(1):
                    raise ValueError("archive_output_budget")
                await self.process.wait()
                if self.process.returncode:
                    raise ValueError("archive_media_interrupted")
        finally:
            await self.close()

    async def close(self):
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(self._close())
        await asyncio.shield(self.cleanup_task)

    async def _close(self):
        if self.closed:
            return
        if self.process:
            if self.process.returncode is None:
                try:
                    # The range worker has no descendants. Target only our child;
                    # process-group signals can be denied by a macOS sandbox.
                    self.process.kill()
                except ProcessLookupError:
                    pass
            # asyncio's process wait can remain pending after exit when its PIPE
            # transport is paused on unread output. Discard only this killed,
            # bounded range worker's bytes, serializing with any active reader.
            if getattr(self.process, "stdout", None) is not None:
                async with self.stdout_lock:
                    while await self.process.stdout.read(65536):
                        pass
            await self.process.wait()
        self.closed = True
        self.owner.readers.discard(self)


class ArchivePlayer:
    def __init__(self, archive, *, monotonic=None):
        self.archive, self.monotonic = archive, monotonic or time.monotonic
        self.leases, self.readers, self.closed = {}, set(), False

    def valid(self, lease):
        if self.closed or lease.revoked or self.monotonic() >= lease.deadline:
            raise ValueError("archive_playback_expired")
        self.archive._check(lease.plan.context_fingerprint)

    def prune(self):
        for key, lease in list(self.leases.items()):
            if (
                lease.revoked
                or self.monotonic() >= lease.deadline
                or lease.plan.context_fingerprint != self.archive.context()["fingerprint"]
            ):
                lease.revoked = True
                del self.leases[key]

    async def prepare(self, request):
        if self.closed:
            raise ValueError("archive_playback_expired")
        self.prune()
        if len(self.leases) >= 8:
            raise ValueError("archive_playback_capacity")
        binding = await self.archive.playback_binding(request)
        if self.closed or len(self.leases) >= 8:
            raise ValueError("archive_playback_capacity")
        self.archive._check(request.expected_context)
        info, segment = binding["info"], binding["segment"]
        identifier = secrets.token_hex(32)
        plan = ArchivePlaybackPlan(
            plan_id=identifier,
            context_fingerprint=request.expected_context,
            media_ref=request.media_ref,
            camera_id=request.camera_id,
            time_range=request.time_range,
            source_time_range=segment.time_range,
            stream_url="/api/archive/media/" + identifier,
            expires_at=self.archive.clock() + timedelta(seconds=300),
            start_offset_seconds=binding["start"],
            end_offset_seconds=binding["end"],
            previous_keyframe_seconds=info["previous_keyframe_seconds"],
            indexed_keyframes_seconds=info["indexed_keyframes_seconds"],
            duration_seconds=info["duration_seconds"],
            audio_track=info["audio_track"],
            codecs=info["codecs"],
            clock_uncertainty_seconds=segment.clock_uncertainty_seconds,
            simulated=segment.simulated,
        )
        self.leases[identifier] = Lease(plan, binding, self.monotonic() + 300)
        return plan

    async def read(self, identifier, range_header):
        lease = self.leases.get(identifier)
        if lease is None:
            raise ValueError("archive_playback_not_found")
        self.valid(lease)
        start, length, partial = byte_range(range_header, lease.binding["size_bytes"])
        if length > lease.remaining:
            raise ValueError("archive_playback_byte_budget")
        if len(self.readers) >= 2:
            raise ValueError("archive_playback_busy")
        lease.remaining -= length  # Reserve before IO; failure never replenishes a replay budget.
        reader = ArchiveReader(self, lease, start, length, partial)
        self.readers.add(reader)
        try:
            await reader.open()
            return reader
        except BaseException:
            await reader.close()
            raise

    def revoke(self, identifier):
        lease = self.leases.pop(identifier, None)
        if lease:
            lease.revoked = True
        return {"status": "revoked", "media_copied": False, "device_write_attempted": False}

    async def close(self):
        self.closed = True
        for lease in self.leases.values():
            lease.revoked = True
        for reader in list(self.readers):
            await reader.close()
        self.leases.clear()
