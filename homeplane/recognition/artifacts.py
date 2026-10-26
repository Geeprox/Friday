"""Explicit local artifact checks. No downloading, discovery or graph execution."""

import hashlib
import io
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
WESPEAKER_SOURCE_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
WESPEAKER_REVISION = "f0c48c298fd835726c27956a5d617bad7115627e"


@dataclass(frozen=True)
class LocalArtifact:
    path: Path
    sha256: str

    def __post_init__(self):
        if (not isinstance(self.path, Path) or not self.path.is_absolute()
                or self.path.suffix != ".onnx" or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)):
            raise ValueError("invalid_artifact_configuration")

    def verified_path(self) -> str:
        # Trusted composition must provision immutable files. This is not a
        # sandbox for malicious native graphs or a substitute for file ownership.
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("artifact_symlink")
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 80 * 1024 * 1024:
                raise ValueError("artifact_size_or_type")
            digest = hashlib.sha256()
            total = 0
            while chunk := source.read(65536):
                total += len(chunk)
                if total > 80 * 1024 * 1024:
                    raise ValueError("artifact_size_or_type")
                digest.update(chunk)
            if digest.hexdigest() != self.sha256:
                raise ValueError("artifact_hash_mismatch")
        return str(self.path)


def _varint(source, end):
    value = 0
    for shift in range(0, 70, 7):
        if source.tell() >= end:
            raise ValueError("invalid_onnx_metadata")
        byte = source.read(1)
        if not byte:
            raise ValueError("invalid_onnx_metadata")
        value |= (byte[0] & 127) << shift
        if not byte[0] & 128:
            if value >= 1 << 64:
                raise ValueError("invalid_onnx_metadata")
            return value
    raise ValueError("invalid_onnx_metadata")


def _fields(source, end):
    count = 0
    while source.tell() < end:
        count += 1
        if count > 256:
            raise ValueError("onnx_metadata_budget")
        tag = _varint(source, end)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("invalid_onnx_metadata")
        if wire == 0:
            _varint(source, end)
            continue
        length = {1: 8, 5: 4}.get(wire)
        if wire == 2:
            length = _varint(source, end)
        if length is None or source.tell() + length > end:
            raise ValueError("invalid_onnx_metadata")
        start = source.tell()
        yield number, wire, start, length
        source.seek(start + length)


def speaker_metadata(path: str) -> dict[str, str]:
    """Read only bounded ModelProto metadata_props (field 14); skip graph bytes.

    This is a metadata preflight, not an ONNX graph validator or inference loader.
    Duplicate metadata keys are rejected rather than resolved in library order.
    """
    metadata = {}
    with open(path, "rb") as source:
        end = os.fstat(source.fileno()).st_size
        if not 1 <= end <= 80 * 1024 * 1024:
            raise ValueError("onnx_metadata_budget")
        for number, wire, start, length in _fields(source, end):
            if number != 14:
                continue
            if wire != 2 or length > 4096 or len(metadata) >= 32:
                raise ValueError("onnx_metadata_budget")
            entry = io.BytesIO(source.read(length))
            parts = {}
            for key, entry_wire, offset, size in _fields(entry, length):
                if key not in (1, 2) or key in parts or entry_wire != 2 or size > 2048:
                    raise ValueError("invalid_onnx_metadata")
                parts[key] = entry.read(size).decode("utf-8")
            if set(parts) != {1, 2} or not parts[1] or parts[1] in metadata:
                raise ValueError("invalid_onnx_metadata")
            metadata[parts[1]] = parts[2]
    return metadata
