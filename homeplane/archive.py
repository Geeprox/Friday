"""Bounded metadata-only archive spike. Caller supplies an authorized root and time mapping.

Never infer recording time from mtime, never open/copy media, never modify NAS files.
No directory scan runs automatically. A real path is inspected only after explicit opt-in.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    camera_id: str
    start: float
    end: float
    relative_path: str
    complete: bool = False
    audio_track: bool | None = None
    clock_offset_seconds: float | None = None


class MetadataArchive:
    def __init__(self, root: Path, *, authorized: bool = False, limit: int = 1000):
        self.root = root.resolve()
        self.authorized = authorized
        self.limit = min(max(limit, 1), 10000)
        self._signatures: dict[str, tuple] = {}

    def inspect(self, segment: Segment) -> dict:
        identity = hashlib.sha256(f"{segment.camera_id}:{segment.start}:{segment.end}".encode()).hexdigest()
        answer = {"media_ref": identity, "camera_id": segment.camera_id,
                  "time_range": [segment.start, segment.end], "status": "unavailable",
                  "audio_track": segment.audio_track, "clock_offset_seconds": segment.clock_offset_seconds}
        if not self.authorized:
            return {**answer, "reason": "archive_read_authorization_required"}
        rel = Path(segment.relative_path)
        if rel.is_absolute() or ".." in rel.parts or segment.end <= segment.start:
            return {**answer, "reason": "invalid_segment"}
        path = self.root / rel
        # Never follow symlinks into another tree, including intermediate directories.
        if any(p.is_symlink() for p in [path, *path.parents] if p != self.root.parent):
            return {**answer, "reason": "symlink_rejected"}
        try:
            stat = path.stat()
            if not path.is_file():
                return {**answer, "reason": "not_regular_file"}
        except FileNotFoundError:
            return {**answer, "status": "expired" if identity in self._signatures else "missing"}
        except OSError:
            return {**answer, "reason": "archive_unavailable"}
        signature = (stat.st_size, stat.st_mtime_ns)
        stable = self._signatures.get(identity) == signature
        if identity not in self._signatures and len(self._signatures) >= self.limit:
            return {**answer, "reason": "index_capacity_reached"}
        self._signatures[identity] = signature
        # Stable size alone is not enough: completion must come from a trusted source mapping.
        status = "ready" if stable and segment.complete and stat.st_size else "pending"
        return {**answer, "status": status, "size_bytes": stat.st_size,
                "file_signature": hashlib.sha256(repr(signature).encode()).hexdigest(),
                "reason": "metadata_only_no_decode_validation"}
