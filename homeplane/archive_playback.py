"""Ephemeral, source-bound readonly MP4 byte ranges. No parent NAS file IO."""

import asyncio
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

from .archive_contracts import ArchivePlaybackPlan
from .archive_privacy_previews import PrivacyPreviews, command
from .runtime_log import stage_span
from .media_tools import ffprobe_executable

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
        self.launch_task = None
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
        if private.get("privacy") not in {"local_person_mosaic", "local_full_frame_mosaic"}:
            raise ValueError("archive_privacy_unavailable")
        args = command(
            "homeplane.archive_privacy_range",
            ["--cache", private["root"], "--source-root", private["source_root"]],
        )
        self.launch_task = asyncio.create_task(
            self.owner.range_launcher(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                limit=65536,
            )
        )
        self.process = await asyncio.wait_for(asyncio.shield(self.launch_task), 10)
        if self.cleanup_task is not None:
            raise ValueError("archive_playback_expired")
        request = {
            "path": private["path"],
            "signature": private["signature"],
            "start": self.start,
            "length": self.length,
            "source_path": private["source_path"],
            "source_signature": private["source_signature"],
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
        with stage_span("archive.stream"):
            async for chunk in self._chunks():
                yield chunk

    async def _chunks(self):
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
        process = self.process
        if process is None and self.launch_task is not None:
            try:
                process = await asyncio.wait_for(asyncio.shield(self.launch_task), 12)
            except (OSError, TimeoutError):

                def late(task):
                    if not task.cancelled() and task.exception() is None:
                        asyncio.create_task(self._discard(task.result()))

                self.launch_task.add_done_callback(late)
        await self._discard(process)
        self.closed = True
        self.owner.readers.discard(self)

    async def _discard(self, process):
        if process:
            if process.returncode is None:
                try:
                    # The range worker has no descendants. Target only our child;
                    # process-group signals can be denied by a macOS sandbox.
                    process.kill()
                except ProcessLookupError:
                    pass
            # asyncio's process wait can remain pending after exit when its PIPE
            # transport is paused on unread output. Discard only this killed,
            # bounded range worker's bytes, serializing with any active reader.
            if getattr(process, "stdout", None) is not None:
                async with self.stdout_lock:
                    while await process.stdout.read(65536):
                        pass
            await process.wait()


class ArchivePlayer:
    def __init__(self, archive, *, monotonic=None, privacy=None, range_launcher=None):
        self.archive, self.monotonic = archive, monotonic or time.monotonic
        self.leases, self.readers, self.closed = {}, set(), False
        self.privacy = privacy or PrivacyPreviews(
            archive.settings.path.parent / "archive-previews",
            config=archive.settings.path.parent / "archive-privacy.json",
        )
        self.range_launcher = range_launcher or asyncio.create_subprocess_exec
        self.maintenance = None

    def valid(self, lease):
        if self.closed or lease.revoked or self.monotonic() >= lease.deadline:
            raise ValueError("archive_playback_expired")
        self.archive._check(lease.plan.context_fingerprint)

    def protected_preview(self, identifier):
        """No I/O or preparation: qualify only an existing protected lease."""
        lease = self.leases.get(identifier)
        if lease is None or not self.privacy.owns(lease.binding):
            return False
        try:
            self.valid(lease)
        except (ValueError, OSError, RuntimeError):
            return False
        return lease.plan.privacy_protected is True and lease.plan.privacy_preview_generated is True

    def prune(self):
        for key, lease in list(self.leases.items()):
            if (
                lease.revoked
                or self.monotonic() >= lease.deadline
                or lease.plan.context_fingerprint != self.archive.context()["fingerprint"]
            ):
                lease.revoked = True
                del self.leases[key]
                self.privacy.release(lease.binding["path"])
                self._stop_readers(lease)

    def _stop_readers(self, lease):
        for reader in list(self.readers):
            if reader.lease is lease:
                asyncio.create_task(reader.close())

    async def _maintain(self):
        while not self.closed:
            await asyncio.sleep(15)
            self.prune()

    async def prepare(self, request):
        with stage_span("archive.playback"):
            return await self._prepare(request)

    async def _prepare(self, request):
        if self.closed:
            raise ValueError("archive_playback_expired")
        self.prune()
        if len(self.leases) >= 8:
            raise ValueError("archive_playback_capacity")
        binding = await self.archive.playback_binding(request)
        if self.closed or len(self.leases) >= 8:
            raise ValueError("archive_playback_capacity")
        self.archive._check(request.expected_context)
        segment = binding["segment"]
        protected = await self.privacy.prepare(binding)
        try:
            self.archive._check(request.expected_context)
            if self.closed or len(self.leases) >= 8:
                raise ValueError("archive_playback_expired")
            info = await self.archive.runner(
                protected["root"],
                "playback-info",
                {
                    "path": protected["path"],
                    "ffprobe": ffprobe_executable(),
                    "offset_seconds": binding["start"],
                },
            )
            self.archive._check(request.expected_context)
            if (
                self.closed
                or info.get("status") != "readable"
                or not info.get("media_metadata_valid")
                or info.get("signature") != protected["signature"]
                or info.get("media_start_seconds") != 0
                or abs(info["duration_seconds"] - binding["info"]["duration_seconds"]) > 0.15
            ):
                raise ValueError("archive_privacy_unavailable")
        except BaseException:
            self.privacy.release(protected["path"])
            raise
        binding = {
            **protected,
            "segment": segment,
            "start": binding["start"],
            "end": binding["end"],
            "info": info,
        }
        identifier = secrets.token_hex(32)
        try:
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
                privacy_protected=True,
                privacy_mode=protected["privacy"],
                privacy_degraded_reason=protected["privacy_degraded_reason"],
            )
            self.leases[identifier] = Lease(plan, binding, self.monotonic() + 300)
            if self.maintenance is None:
                self.maintenance = asyncio.create_task(self._maintain())
        except BaseException:
            self.privacy.release(protected["path"])
            raise
        return plan

    async def read(self, identifier, range_header):
        with stage_span("archive.read"):
            return await self._read(identifier, range_header)

    async def _read(self, identifier, range_header):
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
        with stage_span("archive.cancel"):
            return self._revoke(identifier)

    def _revoke(self, identifier):
        lease = self.leases.pop(identifier, None)
        if lease:
            lease.revoked = True
            self.privacy.release(lease.binding["path"])
            self._stop_readers(lease)
        return {"status": "revoked", "media_copied": False, "device_write_attempted": False}

    async def close(self):
        with stage_span("archive.close"):
            await self._close()

    async def _close(self):
        self.closed = True
        if self.maintenance is not None:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
        for lease in self.leases.values():
            lease.revoked = True
        for reader in list(self.readers):
            await reader.close()
        self.leases.clear()
        await self.privacy.close()
