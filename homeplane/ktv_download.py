"""Bounded, identity-checked handoff from official NetEase downloads to local audio.

Importing or constructing this adapter never opens a client, plays, or loads a
model. Catalog reads only the explicitly selected download directory. NCM song
identity comes from its embedded musicId, never a guessed filename.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import plistlib
import re
import secrets
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .runtime_log import RuntimeLog

MAX_FILES = 256
MAX_DEPTH = 3
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_CATALOG_METADATA_BYTES = 8 * 1024 * 1024
MAX_KEY_BYTES = 65536
MAX_DURATION_SECONDS = 1800
MAX_FAILED_JOBS = 8
MAX_SCAN_SECONDS = 5
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
MAX_CACHE_ENTRIES = 8192
CACHE_METADATA_RESERVE = 1024 * 1024
DECODER_VERSION = "1.5.1"
FFMPEG_VERSION = "9.0.2"
CLIENT_VERSION = "3.1.12"
CLIENT_BUILD = "3444"
CLIENT_RESOURCE_SHA256 = "1bf0665902409a3059d1abd79b01c9a37881de18372053950cf2cc8eebf9263b"
NCM_METADATA_KEY = b"#14ljk_!\\]&0U<'("
ID = re.compile(r"[1-9][0-9]{0,18}\Z", re.ASCII)
SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
CODES = frozenset(
    {
        "none",
        "invalid_request",
        "download_unavailable",
        "download_timeout",
        "download_identity_mismatch",
        "download_cancelled",
        "decode_failed",
        "source_changed",
    }
)


class DownloadError(ValueError):
    def __init__(self, code: str, detail: str = "none"):
        super().__init__(code)
        self.code, self.detail = code, detail


def canonical_song_id(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value) or int(value) > 2**63 - 1:
        raise DownloadError("invalid_request", "numeric_song_id_required")
    return value


def download_uri(song_id: str) -> str:
    payload = {"cmd": "download", "type": "song", "id": canonical_song_id(song_id)}
    data = json.dumps(payload, separators=(",", ":")).encode()
    return "orpheus://" + base64.b64encode(data).decode("ascii")


def _signature(value) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _check(cancel: threading.Event, deadline: float) -> None:
    if cancel.is_set():
        raise DownloadError("download_cancelled")
    if time.monotonic() >= deadline:
        raise DownloadError("download_timeout")


def _directory(path: Path, *, create: bool = False) -> Path:
    path = path.absolute()
    try:
        if path.resolve() != path or path.is_symlink():
            raise DownloadError("download_unavailable", "unsafe_directory")
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        value = path.stat()
    except (OSError, RuntimeError) as error:
        raise DownloadError("download_unavailable", "directory_unavailable") from error
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid():
        raise DownloadError("download_unavailable", "unsafe_directory")
    if create and stat.S_IMODE(value.st_mode) != 0o700:
        raise DownloadError("download_unavailable", "unsafe_cache_permissions")
    return path


def _open_file(path: Path):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise DownloadError("source_changed", "unsafe_source")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or not 1 <= value.st_size <= MAX_FILE_BYTES:
            os.close(descriptor)
            raise DownloadError("download_unavailable", "source_size_or_type_rejected")
        return os.fdopen(descriptor, "rb"), value
    except OSError as error:
        raise DownloadError("source_changed", "source_open_failed") from error


def _hash_file(path: Path) -> str:
    reader, before = _open_file(path)
    digest = hashlib.sha256()
    with reader:
        for data in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(data)
        after = os.fstat(reader.fileno())
    if _signature(before) != _signature(after) or _signature(path.stat()) != _signature(before):
        raise DownloadError("source_changed")
    return digest.hexdigest()


def _cache_capacity(cache: Path, additional_bytes: int = 0) -> int:
    """No eviction: account for staged jobs, decoded songs, indexes and logs alike."""
    total, count, pending = 0, 0, [cache]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_CACHE_ENTRIES:
                    raise DownloadError("download_unavailable", "cache_inventory_exceeded")
                if entry.is_symlink():
                    raise DownloadError("download_unavailable", "unsafe_cache_entry")
                value = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(value.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(value.st_mode):
                    total += value.st_size
                else:
                    raise DownloadError("download_unavailable", "unsafe_cache_entry")
                if total + additional_bytes + CACHE_METADATA_RESERVE > MAX_CACHE_BYTES:
                    raise DownloadError("download_unavailable", "cache_capacity_exceeded")
    if total + additional_bytes + CACHE_METADATA_RESERVE > MAX_CACHE_BYTES:
        raise DownloadError("download_unavailable", "cache_capacity_exceeded")
    return total


def _text(value, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value[:limit] if ord(c) >= 32 and ord(c) != 127)


def read_ncm_metadata(
    path: Path, *, openssl: Path = Path("/usr/bin/openssl"), metadata_budget: list[int] | None = None
) -> dict:
    """Read the bounded public NCM metadata format; never read app credentials."""
    reader, before = _open_file(path)
    try:
        with reader:
            if reader.read(8) != b"CTENFDAM" or len(reader.read(2)) != 2:
                raise DownloadError("download_identity_mismatch", "invalid_ncm_header")
            raw = reader.read(4)
            if len(raw) != 4:
                raise DownloadError("decode_failed", "invalid_ncm_metadata")
            key_bytes = struct.unpack("<I", raw)[0]
            if not 1 <= key_bytes <= MAX_KEY_BYTES or reader.tell() + key_bytes + 4 > before.st_size:
                raise DownloadError("decode_failed", "invalid_ncm_key_length")
            reader.seek(key_bytes, 1)
            raw = reader.read(4)
            if len(raw) != 4:
                raise DownloadError("decode_failed", "invalid_ncm_metadata")
            metadata_bytes = struct.unpack("<I", raw)[0]
            if (
                not 22 < metadata_bytes <= MAX_METADATA_BYTES
                or reader.tell() + metadata_bytes > before.st_size
            ):
                raise DownloadError("decode_failed", "invalid_ncm_metadata_length")
            if metadata_budget is not None:
                if metadata_bytes > metadata_budget[0]:
                    raise DownloadError("download_unavailable", "catalog_metadata_budget_exceeded")
                metadata_budget[0] -= metadata_bytes
            encoded = bytes(value ^ 0x63 for value in reader.read(metadata_bytes))
            after = os.fstat(reader.fileno())
        if _signature(before) != _signature(after) or _signature(path.stat()) != _signature(before):
            raise DownloadError("source_changed")
        if not encoded.startswith(b"163 key(Don't modify):"):
            raise DownloadError("decode_failed", "invalid_ncm_metadata")
        encrypted = base64.b64decode(encoded[22:], validate=True)
        if not encrypted or len(encrypted) % 16:
            raise DownloadError("decode_failed", "invalid_ncm_metadata")
        result = subprocess.run(
            [str(openssl), "enc", "-d", "-aes-128-ecb", "-nosalt", "-K", NCM_METADATA_KEY.hex()],
            input=encrypted,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        if (
            result.returncode != 0
            or len(result.stdout) > MAX_METADATA_BYTES
            or not result.stdout.startswith(b"music:")
        ):
            raise DownloadError("decode_failed", "ncm_metadata_decrypt_failed")
        value = json.loads(result.stdout[6:])
        if not isinstance(value, dict):
            raise DownloadError("decode_failed", "invalid_ncm_metadata")
        music_id = value.get("musicId")
        if type(music_id) is int:
            music_id = str(music_id)
        song_id = canonical_song_id(music_id)
        duration = value.get("duration")
        if (
            type(duration) not in {int, float}
            or not math.isfinite(duration)
            or not 0 < duration <= MAX_DURATION_SECONDS * 1000
        ):
            raise DownloadError("download_identity_mismatch", "ncm_duration_unavailable")
        artists = value.get("artist", [])
        names = (
            [_text(item[0], 100) for item in artists[:8] if isinstance(item, list) and item]
            if isinstance(artists, list)
            else []
        )
        return {
            "song_id": song_id,
            "title": _text(value.get("musicName")),
            "artists": names,
            "duration_seconds": duration / 1000,
            "metadata_bytes": metadata_bytes,
            "signature": _signature(after),
            "identity_basis": "embedded_ncm_musicId",
        }
    except DownloadError:
        raise
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise DownloadError("decode_failed", "ncm_metadata_unreadable") from error


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=2)


def _run(
    argv: list[str], cancel: threading.Event, deadline: float, directory: Path, *, capture: bool = False
) -> bytes:
    _check(cancel, deadline)
    with tempfile.TemporaryFile(dir=directory) as output:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=output if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            while process.poll() is None:
                _check(cancel, deadline)
                if capture and os.fstat(output.fileno()).st_size > 65536:
                    raise DownloadError("decode_failed", "tool_output_exceeded")
                cancel.wait(0.05)
            if process.returncode != 0:
                raise DownloadError("decode_failed", "tool_failed")
            output.seek(0)
            data = output.read(65537) if capture else b""
            if len(data) > 65536:
                raise DownloadError("decode_failed", "tool_output_exceeded")
            _check(cancel, deadline)
            return data
        finally:
            _stop(process)


def _write(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(value, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class NetEaseDownloadAdapter:
    def __init__(
        self,
        download_dir: Path,
        cache_dir: Path,
        *,
        allow_launch: bool = False,
        client: Path = Path("/Applications/NeteaseMusic.app"),
        decoder: Path = Path("/opt/homebrew/bin/ncmdump"),
        ffprobe: Path = Path("/opt/homebrew/bin/ffprobe"),
        ffmpeg: Path = Path("/opt/homebrew/bin/ffmpeg"),
        openssl: Path = Path("/usr/bin/openssl"),
    ):
        if type(allow_launch) is not bool:
            raise DownloadError("invalid_request")
        self.download_dir, self.cache_dir = Path(download_dir).absolute(), Path(cache_dir).absolute()
        self.allow_launch, self.client = allow_launch, client
        self.decoder, self.ffprobe, self.ffmpeg, self.openssl = decoder, ffprobe, ffmpeg, openssl
        self.last_catalog_report = {"status": "not_checked", "truncated": False}

    def _scan(
        self, *, deadline: float | None = None, cancel: threading.Event | None = None
    ) -> tuple[list[tuple[Path, dict]], bool]:
        root = _directory(self.download_dir)
        scan_deadline = min(deadline or float("inf"), time.monotonic() + MAX_SCAN_SECONDS)
        pending, rows, visited, budget, truncated = [(root, 0)], [], 0, [MAX_CATALOG_METADATA_BYTES], False
        while pending:
            directory, depth = pending.pop(0)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancel is not None and cancel.is_set():
                        raise DownloadError("download_cancelled")
                    if time.monotonic() >= scan_deadline:
                        return rows, True
                    visited += 1
                    if visited > MAX_FILES:
                        return rows, True
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < MAX_DEPTH:
                            pending.append((Path(entry.path), depth + 1))
                        else:
                            truncated = True
                        continue
                    if not entry.is_file(follow_symlinks=False) or Path(entry.name).suffix.lower() != ".ncm":
                        continue
                    path = Path(entry.path)
                    try:
                        value = read_ncm_metadata(path, openssl=self.openssl, metadata_budget=budget)
                    except DownloadError as error:
                        if error.detail == "catalog_metadata_budget_exceeded":
                            return rows, True
                        continue
                    rows.append((path, value))
        return rows, truncated

    def catalog(self) -> list[dict]:
        """An explicit local metadata listing, with no paths, launches or download claims."""
        try:
            rows, truncated = self._scan()
            songs = {}
            for _, value in rows:
                songs.setdefault(
                    value["song_id"],
                    {key: value[key] for key in ("song_id", "title", "artists", "duration_seconds")},
                )
            self.last_catalog_report = {
                "status": "ready",
                "code": "none",
                "truncated": truncated,
                "identity_basis": "embedded_ncm_musicId",
                "source_audio_verified": False,
                "client_launched": False,
            }
            return list(songs.values())
        except (DownloadError, OSError):
            self.last_catalog_report = {
                "status": "unavailable",
                "code": "download_unavailable",
                "truncated": False,
                "client_launched": False,
            }
            return []

    def _cached(self, song_id: str, cache: Path) -> dict | None:
        index = cache / "index" / (song_id + ".json")
        try:
            if index.is_symlink() or index.stat().st_size > 16384:
                return None
            value = json.loads(index.read_text())
            key, filename = value["cache_key"], value["filename"]
            if (
                not isinstance(key, str)
                or not SHA256.fullmatch(key)
                or filename not in {"source.mp3", "source.flac"}
            ):
                return None
            if (
                value["song_id"] != song_id
                or value["identity_basis"] != "embedded_ncm_musicId"
                or value["decoder_version"] != DECODER_VERSION
                or value["full_decode_verified"] is not True
                or value["exact_song_id_verified"] is not True
            ):
                return None
            audio = cache / "decoded" / key / filename
            if audio.stat().st_size != value["source_bytes"] or _hash_file(audio) != value["source_sha256"]:
                return None
            return {**value, "source_path": str(audio), "cache_hit": True}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _request(self, song_id: str, cancel: threading.Event, deadline: float, directory: Path) -> None:
        _check(cancel, deadline)
        try:
            client = self.client.absolute()
            if client.resolve() != client or client.is_symlink():
                raise DownloadError("download_unavailable", "client_identity_unavailable")
            info = plistlib.loads((client / "Contents/Info.plist").read_bytes())
            if (
                info.get("CFBundleIdentifier") != "com.netease.163music"
                or info.get("CFBundleShortVersionString") != CLIENT_VERSION
                or info.get("CFBundleVersion") != CLIENT_BUILD
                or _hash_file(client / "Contents/Resources/resources.pack") != CLIENT_RESOURCE_SHA256
            ):
                raise DownloadError("download_unavailable", "client_version_or_source_changed")
            _run(
                ["/usr/bin/open", "-a", str(client), download_uri(song_id)],
                cancel,
                min(deadline, time.monotonic() + 5),
                directory,
            )
        except DownloadError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DownloadError("download_unavailable", "client_identity_unavailable") from error

    def _decode(self, source, expected, cache, cancel, deadline):
        descriptor = os.open(cache / "decode.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            while True:
                _check(cancel, deadline)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    cancel.wait(0.1)
            return self._decode_locked(source, expected, cache, cancel, deadline)
        finally:
            os.close(descriptor)

    def _decode_locked(
        self, source: Path, expected: dict, cache: Path, cancel: threading.Event, deadline: float
    ) -> dict:
        current = source.stat()
        if _signature(current) != expected["signature"]:
            raise DownloadError("source_changed")
        # The pinned NCM decoder copies/decrypts audio payload; allow one source
        # copy, an output no larger than source + bounded tag overhead, and the
        # separate global reserve for logs/manifests. Refuse before staging when
        # that room is unavailable; never evict either existing or new sources.
        _cache_capacity(cache, 2 * current.st_size + MAX_METADATA_BYTES)
        jobs = _directory(cache / "jobs", create=True)
        with os.scandir(jobs) as entries:
            if sum(1 for _ in entries) >= MAX_FAILED_JOBS:
                raise DownloadError("download_unavailable", "failed_job_capacity")
        job = Path(tempfile.mkdtemp(prefix="download-", dir=jobs))
        # Failed jobs remain private diagnostic evidence; never modify/delete the original download.
        copied = job / "source.ncm"
        reader, before = _open_file(source)
        if _signature(before) != expected["signature"]:
            reader.close()
            raise DownloadError("source_changed")
        digest, copied_bytes = hashlib.sha256(), 0
        with reader, copied.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                _check(cancel, deadline)
                if copied_bytes + len(chunk) > before.st_size:
                    raise DownloadError("source_changed")
                writer.write(chunk)
                digest.update(chunk)
                copied_bytes += len(chunk)
            after = os.fstat(reader.fileno())
        os.chmod(copied, 0o400)
        if (
            copied_bytes != before.st_size
            or _signature(before) != _signature(after)
            or _signature(source.stat()) != _signature(before)
            or expected["signature"] != _signature(before)
        ):
            raise DownloadError("source_changed")
        value = read_ncm_metadata(copied, openssl=self.openssl)
        if value["song_id"] != expected["song_id"]:
            raise DownloadError("download_identity_mismatch")
        source_hash = digest.hexdigest()
        version = _run(
            [str(self.decoder), "--version"], cancel, min(deadline, time.monotonic() + 5), job, capture=True
        )
        if version.decode(errors="replace").strip() != "ncmdump version " + DECODER_VERSION:
            raise DownloadError("decode_failed", "decoder_version_mismatch")
        for tool, name in [(self.ffprobe, "ffprobe"), (self.ffmpeg, "ffmpeg")]:
            version = _run(
                [str(tool), "-version"], cancel, min(deadline, time.monotonic() + 5), job, capture=True
            )
            if not version.startswith((name + " version " + FFMPEG_VERSION + " ").encode()):
                raise DownloadError("decode_failed", "validation_tool_version_mismatch")
        output = _directory(job / "decoded", create=True)
        _run([str(self.decoder), str(copied), "-o", str(output)], cancel, deadline, job)
        files = list(output.iterdir())
        if len(files) != 1 or files[0].name not in {"source.mp3", "source.flac"} or files[0].is_symlink():
            raise DownloadError("decode_failed", "decoded_output_unqualified")
        audio = files[0]
        if audio.stat().st_size > before.st_size + MAX_METADATA_BYTES:
            raise DownloadError("decode_failed", "decoded_output_size_exceeded")
        if _hash_file(copied) != source_hash:
            raise DownloadError("source_changed", "staged_source_changed")
        probe = _run(
            [
                str(self.ffprobe),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type,codec_name,sample_rate,channels:format=duration,format_name",
                "-of",
                "json",
                str(audio),
            ],
            cancel,
            min(deadline, time.monotonic() + 30),
            job,
            capture=True,
        )
        metadata = json.loads(probe)
        streams = metadata.get("streams", [])
        duration = float(metadata["format"]["duration"])
        if (
            len(streams) != 1
            or streams[0].get("codec_type") != "audio"
            or not 1 <= int(streams[0].get("channels", 0)) <= 2
            or not 8000 <= int(streams[0].get("sample_rate", 0)) <= 192000
            or not math.isfinite(duration)
            or not 0 < duration <= MAX_DURATION_SECONDS
            or abs(duration - value["duration_seconds"]) > max(0.5, duration * 0.01)
        ):
            raise DownloadError("decode_failed", "decoded_audio_or_duration_mismatch")
        _run(
            [
                str(self.ffmpeg),
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                str(audio),
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            cancel,
            deadline,
            job,
        )
        if _signature(source.stat()) != _signature(before):
            raise DownloadError("source_changed")
        audio_hash = _hash_file(audio)
        _cache_capacity(cache)
        key = hashlib.sha256((source_hash + DECODER_VERSION).encode()).hexdigest()
        destination = cache / "decoded" / key
        _directory(cache / "decoded", create=True)
        if destination.exists():
            # Do not silently replace a damaged or ambiguous cache entry.
            raise DownloadError("decode_failed", "decoded_cache_conflict")
        destination.mkdir(mode=0o700)
        final = destination / audio.name
        audio.rename(final)
        os.chmod(final, 0o600)
        result = {
            "song_id": value["song_id"],
            "identity_basis": "embedded_ncm_musicId",
            "exact_song_id_verified": True,
            "cache_key": key,
            "filename": final.name,
            "source_sha256": audio_hash,
            "source_bytes": final.stat().st_size,
            "ncm_sha256": source_hash,
            "decoder_version": DECODER_VERSION,
            "full_decode_verified": True,
            "duration_seconds": duration,
            "sample_rate_hz": int(streams[0]["sample_rate"]),
            "cache_hit": False,
        }
        _write(destination / "manifest.json", result)
        index = _directory(cache / "index", create=True)
        _write(index / (value["song_id"] + ".json"), result)
        copied.unlink()
        output.rmdir()
        job.rmdir()
        return {**result, "source_path": str(final)}

    def acquire(
        self, song_id: str, *, timeout: float = 180, cancel: threading.Event | None = None, progress=None
    ) -> dict:
        cancel = cancel if cancel is not None else threading.Event()
        result = {
            "status": "unavailable",
            "code": "download_unavailable",
            "detail_code": "none",
            "source_path": None,
            "request_sent": False,
            "download_confirmed": False,
            "exact_song_id_verified": False,
            "full_decode_verified": False,
            "audio_uploaded": False,
        }
        lock, journal, correlation = None, None, secrets.token_hex(16)
        started = time.monotonic()
        try:
            song_id = canonical_song_id(song_id)
            if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 600:
                raise DownloadError("invalid_request")
            deadline = time.monotonic() + timeout
            _check(cancel, deadline)
            cache = _directory(self.cache_dir, create=True)
            _cache_capacity(cache)
            journal = RuntimeLog(cache / "logs")
            journal.record("service.start", "start", correlation=correlation)
            locks = _directory(cache / "locks", create=True)
            lock = os.open(
                locks / (song_id + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
            )
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise DownloadError("download_unavailable", "unsafe_lock")
            while True:
                _check(cancel, deadline)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    cancel.wait(0.1)
            hit = self._cached(song_id, cache)
            if hit is not None:
                _check(cancel, deadline)
                if progress is not None:
                    progress({"phase": "source_cache_hit", "bytes": hit["source_bytes"]})
                result.update(hit, status="ready", code="none", download_confirmed=True)
                return result
            signatures = {}
            while True:
                _check(cancel, deadline)
                rows, truncated = self._scan(deadline=deadline, cancel=cancel)
                candidates = [(path, meta) for path, meta in rows if meta["song_id"] == song_id]
                if len(candidates) > 1:
                    raise DownloadError("download_identity_mismatch", "multiple_matching_downloads")
                if candidates:
                    path, meta = candidates[0]
                    if progress is not None:
                        progress({"phase": "file_detected", "bytes": meta["signature"][2]})
                    if signatures.get(path) == meta["signature"]:
                        if progress is not None:
                            progress({"phase": "decoding", "bytes": meta["signature"][2]})
                        decoded = self._decode(path, meta, cache, cancel, deadline)
                        result.update(decoded, status="ready", code="none", download_confirmed=True)
                        return result
                    signatures[path] = meta["signature"]
                elif not result["request_sent"]:
                    if not self.allow_launch:
                        result.update(
                            status="waiting_download",
                            detail_code="automatic_download_disabled",
                            catalog_truncated=truncated,
                        )
                        return result
                    self._request(song_id, cancel, deadline, cache)
                    result["request_sent"] = True
                    if progress is not None:
                        progress({"phase": "official_request_sent", "bytes": 0})
                if cancel.wait(min(0.5, max(0, deadline - time.monotonic()))):
                    raise DownloadError("download_cancelled")
        except DownloadError as error:
            result.update(
                status="cancelled" if error.code == "download_cancelled" else "unavailable",
                code=error.code if error.code in CODES else "download_unavailable",
                detail_code=error.detail,
            )
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            result.update(status="unavailable", code="decode_failed", detail_code="local_processing_failed")
        finally:
            if lock is not None:
                os.close(lock)
            if journal is not None:
                journal.record(
                    "service.stop",
                    "result",
                    correlation=correlation,
                    result="ok"
                    if result["status"] == "ready"
                    else "cancelled"
                    if result["code"] == "download_cancelled"
                    else "timeout"
                    if result["code"] == "download_timeout"
                    else "unavailable",
                    code="none"
                    if result["status"] == "ready"
                    else "flow_cancelled"
                    if result["code"] == "download_cancelled"
                    else "deadline_exceeded"
                    if result["code"] == "download_timeout"
                    else "internal_error",
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
        return result
