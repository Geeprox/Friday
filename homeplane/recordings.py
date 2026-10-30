"""Read-only NAS metadata and short-lived protected playback derivatives.

Filename time is a label from the recorder; it is not a calibrated event clock.
The worker opens paths beneath the configured mount with no-follow directory FDs.
"""

import asyncio
import heapq
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .archive_playback import SESSION_BUDGET, ArchiveReader, byte_range
from .archive_privacy_previews import PrivacyPreviews
from .archive_worker import open_beneath, root_handle, signature
from .runtime_log import stage_span
from .media_tools import ffprobe_executable
from .worker_runtime import module_command

FOLDER = re.compile(r"XiaomiCamera_[0-9]{2}_[A-Fa-f0-9]{12}")
FILE = re.compile(r"[0-9]{2}_([0-9]{14})_([0-9]{14})\.(?:mp4|mov)", re.I)


def listing(root, selected=None, date=None, limit=80):
    candidates, folders, visited = [], [], 0
    fd = root_handle(root)
    try:
        with os.scandir(fd) as entries:
            names = [e.name for e in entries if FOLDER.fullmatch(e.name) and e.is_dir(follow_symlinks=False)][
                :8
            ]
        for folder in sorted(names):
            folders.append(folder)
            if selected and selected != folder:
                continue
            sub = open_beneath(fd, folder, directory=True)
            try:
                with os.scandir(sub) as entries:
                    for e in entries:
                        visited += 1
                        if visited > 20000:
                            break
                        m = FILE.fullmatch(e.name)
                        if not m or date and m[1][:8] != date:
                            continue
                        row = dict(folder=folder, name=e.name, recorded_start=m[1], recorded_end=m[2])
                        heapq.heappush(candidates, (m[1] + "/" + folder + "/" + e.name, row))
                        if len(candidates) > limit:
                            heapq.heappop(candidates)
            finally:
                os.close(sub)
        result = []
        # SMB directory enumeration is cheap relative to one stat per historic
        # file. Inspect only the selected newest candidates and never follow links.
        for _, row in sorted(candidates, reverse=True):
            try:
                candidate = open_beneath(fd, row["folder"] + "/" + row["name"])
                try:
                    info = os.fstat(candidate)
                finally:
                    os.close(candidate)
                if not stat.S_ISREG(info.st_mode) or info.st_size < 1:
                    continue
                result.append(dict(row, size=info.st_size, revision=signature(info)))
            except OSError:
                continue
        return dict(folders=folders, files=result, visited=visited, partial=visited > 20000)
    finally:
        os.close(fd)


@dataclass
class RecordingLease:
    binding: dict
    context: str
    deadline: float
    remaining: int = SESSION_BUDGET
    revoked: bool = False
    privacy_protected: bool = False
    privacy_preview_generated: bool = False


class Recordings:
    def __init__(self, archive, *, directory=None, privacy=None, range_launcher=None):
        self.archive = archive
        self.files, self.leases = {}, {}
        self.lock = asyncio.Lock()
        directory = (
            Path(directory) if directory is not None else archive.settings.path.parent / "recording-previews"
        )
        self.privacy = privacy or PrivacyPreviews(directory, config=directory.parent / "archive-privacy.json")
        self.range_launcher = range_launcher or asyncio.create_subprocess_exec
        self.readers = set()
        self.closed = False
        self.maintenance = None

    async def browse(self, folder=None, date=None):
        if self.closed:
            raise ValueError("archive_playback_expired")
        if folder is not None and not FOLDER.fullmatch(folder):
            raise ValueError("invalid_recording_folder")
        if date is not None and not re.fullmatch(r"[0-9]{8}", date):
            raise ValueError("invalid_recording_date")
        async with self.lock:
            context = self.archive.context()["fingerprint"]
            root = await self.archive._root()
            self.archive._check(context)
            proc = await asyncio.create_subprocess_exec(
                *module_command("homeplane.recordings"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(
                    proc.communicate(json.dumps(dict(root=root, folder=folder, date=date)).encode()), 25
                )
                if len(out) > 256000 or proc.returncode:
                    raise ValueError("recordings_unavailable")
                result = json.loads(out)
                self.archive._check(context)
                if self.closed:
                    raise ValueError("archive_playback_expired")
                self.files.clear()
                for row in result["files"]:
                    ref = secrets.token_hex(16)
                    self.files[ref] = dict(row, root=root, context=context)
                    row["id"] = ref
                return dict(
                    connected=True,
                    **result,
                    clock_basis="recorder_filename",
                    read_only=True,
                    context_fingerprint=context,
                )
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

    def valid(self, lease):
        if self.closed or lease.revoked or time.monotonic() >= lease.deadline:
            raise ValueError("archive_playback_expired")
        self.archive._check(lease.context)

    def protected_preview(self, token):
        """Only a completed protected lease can cross the desktop media guard."""
        lease = self.leases.get(token)
        if lease is None or not self.privacy.owns(lease.binding):
            return False
        try:
            self.valid(lease)
        except (ValueError, OSError, RuntimeError):
            return False
        return lease.privacy_protected is True and lease.privacy_preview_generated is True

    def prune(self):
        for token, lease in list(self.leases.items()):
            try:
                self.valid(lease)
            except ValueError:
                self.revoke(token)

    async def _maintain(self):
        while not self.closed:
            await asyncio.sleep(15)
            self.prune()

    async def prepare(self, ref, offset_seconds=0):
        with stage_span("archive.playback"):
            if type(offset_seconds) not in (int, float) or not math.isfinite(offset_seconds) or not 0 <= offset_seconds < 600:
                raise ValueError("recording_invalid_offset")
            if self.closed:
                raise ValueError("archive_playback_expired")
            self.prune()
            item = self.files.get(ref)
            if item is None or not FOLDER.fullmatch(item["folder"]) or not FILE.fullmatch(item["name"]):
                raise ValueError("recording_refresh_required")
            context = self.archive.context()["fingerprint"]
            if item.get("context") != context:
                raise ValueError("archive_context_changed")
            source_root = await self.archive._root()
            self.archive._check(context)
            if source_root != item["root"]:
                raise ValueError("archive_source_changed_or_unavailable")
            path = item["folder"] + "/" + item["name"]
            info = await self.archive.runner(
                source_root,
                "playback-info",
                dict(path=path, ffprobe=ffprobe_executable(), offset_seconds=0),
            )
            self.archive._check(context)
            if (
                info.get("status") != "readable"
                or not info.get("media_metadata_valid")
                or info.get("signature") != item["revision"]
                or info.get("media_start_seconds") != 0
            ):
                raise ValueError("archive_source_changed_or_unavailable")
            duration = info["duration_seconds"]
            if not 0 <= offset_seconds < duration:
                raise ValueError("recording_invalid_offset")
            window_duration = min(30, duration - offset_seconds)
            protected = await self.privacy.prepare(
                dict(
                    root=source_root,
                    path=path,
                    signature=item["revision"],
                    size_bytes=info["size_bytes"],
                    info=info,
                    preview_window=dict(start_seconds=offset_seconds, duration_seconds=window_duration),
                )
            )
            try:
                self.archive._check(context)
                if self.closed or self.files.get(ref) is not item:
                    raise ValueError("archive_context_changed")
                preview = await self.archive.runner(
                    protected["root"],
                    "playback-info",
                    dict(
                        path=protected["path"],
                        ffprobe=ffprobe_executable(),
                        offset_seconds=0,
                    ),
                )
                self.archive._check(context)
                if (
                    self.closed
                    or self.files.get(ref) is not item
                    or preview.get("status") != "readable"
                    or not preview.get("media_metadata_valid")
                    or preview.get("signature") != protected["signature"]
                    or preview.get("media_start_seconds") != 0
                    or abs(preview["duration_seconds"] - window_duration) > 0.15
                ):
                    raise ValueError("archive_privacy_unavailable")
                token = secrets.token_hex(24)
                self.leases[token] = RecordingLease(
                    protected,
                    context,
                    time.monotonic() + 300,
                    privacy_protected=True,
                    privacy_preview_generated=True,
                )
                if self.maintenance is None:
                    self.maintenance = asyncio.create_task(self._maintain())
                return dict(
                    url="/api/home/recordings/media/" + token,
                    token=token,
                    expires_in=300,
                    name=item["name"],
                    size=protected["size_bytes"],
                    duration_seconds=preview["duration_seconds"],
                    source_duration_seconds=duration,
                    media_start_seconds=offset_seconds,
                    privacy_protected=True,
                    privacy_mode=protected["privacy"],
                    privacy_preview_generated=True,
                    privacy_degraded_reason=protected["privacy_degraded_reason"],
                    original_modified=False,
                    original_copied=False,
                )
            except BaseException:
                self.privacy.release(protected["path"])
                raise

    async def media(self, token, header):
        with stage_span("archive.read"):
            lease = self.leases.get(token)
            if lease is None:
                raise ValueError("archive_playback_not_found")
            self.valid(lease)
            start, length, partial = byte_range(header or "bytes=0-", lease.binding["size_bytes"])
            if lease.remaining < length:
                raise ValueError("archive_playback_byte_budget")
            if len(self.readers) >= 2:
                raise ValueError("archive_playback_busy")
            lease.remaining -= length
            reader = ArchiveReader(self, lease, start, length, partial)
            self.readers.add(reader)
            try:
                await reader.open()
                return reader
            except BaseException:
                await reader.close()
                raise

    def revoke(self, token):
        with stage_span("archive.cancel"):
            lease = self.leases.pop(token, None)
            if lease:
                lease.revoked = True
                self.privacy.release(lease.binding["path"])
                for reader in list(self.readers):
                    if reader.lease is lease:
                        asyncio.create_task(reader.close())
            return dict(status="revoked", original_modified=False)

    async def close(self):
        with stage_span("archive.close"):
            self.closed = True
            if self.maintenance is not None:
                self.maintenance.cancel()
                await asyncio.gather(self.maintenance, return_exceptions=True)
            for token in list(self.leases):
                self.revoke(token)
            for reader in list(self.readers):
                await reader.close()
            self.files.clear()
            await self.privacy.close()


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.read(4096))
        print(json.dumps(listing(request["root"], request.get("folder"), request.get("date"))))
    except (OSError, ValueError, KeyError, TypeError):
        sys.exit(1)
