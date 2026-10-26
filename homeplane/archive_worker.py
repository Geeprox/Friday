"""Readonly NAS worker. Bounded directory metadata and optional installed ffprobe.

The parent kills this isolated process group on timeout/cancellation. No mounts,
media copies, writes, models, shell commands or credentials in the protocol.
"""

import argparse
import hashlib
import json
import math
import os
import re
import selectors
import stat
import subprocess
import time
from pathlib import Path

MAX_ENTRIES, MAX_FILES, MAX_FOLDERS = 4096, 128, 16
MEDIA = {".mp4", ".mov", ".mkv", ".ts"}


def signature(info):
    return hashlib.sha256(
        repr((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)).encode()
    ).hexdigest()


def open_beneath(root_fd, relative, *, directory=False):
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(p in {".", ".."} for p in path.parts):
        raise ValueError("archive_invalid_path")
    current = os.dup(root_fd)
    try:
        for n, part in enumerate(path.parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if directory or n < len(path.parts) - 1:
                flags |= os.O_DIRECTORY
            nxt = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = nxt
        if not directory and not stat.S_ISREG(os.fstat(current).st_mode):
            raise ValueError("archive_not_regular_file")
        return current
    except BaseException:
        os.close(current)
        raise


def root_handle(root):
    path = Path(root)
    if not path.is_absolute() or ".." in path.parts or str(path) != root:
        raise ValueError("archive_invalid_root")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = nxt
        return current
    except BaseException:
        os.close(current)
        raise


def scan(root_fd):
    files, folders, visited, complete, file_bytes = [], [], 0, True, 0
    with os.scandir(root_fd) as entries:
        for entry in entries:
            visited += 1
            if visited > MAX_ENTRIES or len(folders) >= MAX_FOLDERS:
                complete = False
                break
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            if len(entry.name.encode()) > 256 or any(ord(c) < 32 for c in entry.name):
                complete = False
                continue
            folder = entry.name
            folder_complete = True
            fd = open_beneath(root_fd, folder, directory=True)
            try:
                with os.scandir(fd) as children:
                    for child in children:
                        visited += 1
                        if visited > MAX_ENTRIES or len(files) >= MAX_FILES:
                            folder_complete = complete = False
                            break
                        if child.name.startswith("."):
                            continue
                        if child.is_dir(follow_symlinks=False):
                            # Unknown nested layouts are not silently considered covered.
                            folder_complete = complete = False
                            continue
                        if (
                            Path(child.name).suffix.lower() not in MEDIA
                            or len(child.name.encode()) > 256
                            or any(ord(c) < 32 for c in child.name)
                            or not child.is_file(follow_symlinks=False)
                        ):
                            continue
                        info = child.stat(follow_symlinks=False)
                        record = {
                            "relative_path": str(Path(folder) / child.name),
                            "folder": folder,
                            "size_bytes": info.st_size,
                            "signature": signature(info),
                            "filename": child.name,
                        }
                        file_bytes += len(json.dumps(record).encode()) + 1
                        if file_bytes > 50000:
                            folder_complete = complete = False
                            break
                        files.append(record)
            finally:
                os.close(fd)
            folders.append({"relative_path": folder, "scan_complete": folder_complete})
            if visited > MAX_ENTRIES or len(files) >= MAX_FILES or file_bytes > 50000:
                complete = False
                break
    return {
        "folders": folders,
        "files": files,
        "scan_complete": complete,
        "visited_entries": min(visited, MAX_ENTRIES),
        "media_read": False,
    }


def probe_command(fd, executable):
    return [
        executable,
        "-v",
        "error",
        "-f",
        "mov",
        "-enable_drefs",
        "0",
        "-use_absolute_path",
        "0",
        "-protocol_whitelist",
        "file,pipe,fd",
        "-probesize",
        "1048576",
        "-analyzeduration",
        "1000000",
    ]


def probe(fd, executable):
    if not executable or not Path(executable).is_absolute():
        return {"media_metadata_valid": False, "reason": "archive_probe_unavailable"}
    command = probe_command(fd, executable) + [
        "-show_entries",
        "stream=codec_type,codec_name,time_base,duration,start_time:format=duration,start_time",
        "-of",
        "json",
        f"/dev/fd/{fd}",
    ]
    try:
        code, output = bounded_output(command, pass_fds=(fd,))
        if code:
            raise ValueError("invalid_probe")
        data = json.loads(output)
        streams = data.get("streams", [])
        duration = float(data.get("format", {}).get("duration", 0))
        if not 0 < duration <= 86400 or not 1 <= len(streams) <= 4:
            raise ValueError("invalid_probe")
        if not any(s.get("codec_type") == "video" for s in streams):
            raise ValueError("invalid_probe")
        codecs, bases = [], []
        for stream in streams:
            name, basis = stream.get("codec_name"), stream.get("time_base")
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,40}", name):
                raise ValueError("invalid_probe")
            if (
                not isinstance(basis, str)
                or not re.fullmatch(r"[0-9]{1,10}/[0-9]{1,10}", basis)
                or int(basis.split("/")[1]) == 0
            ):
                raise ValueError("invalid_probe")
            codecs.append(name)
            bases.append(basis)
        start = float(data.get("format", {}).get("start_time", "nan"))
        return {
            "media_metadata_valid": True,
            "audio_track": any(s.get("codec_type") == "audio" for s in streams),
            "codecs": codecs,
            "media_time_bases": bases,
            "duration_seconds": duration,
            "media_start_seconds": start if math.isfinite(start) else None,
        }
    except (OSError, ValueError, TypeError, AttributeError, TimeoutError):
        return {"media_metadata_valid": False, "reason": "archive_media_unvalidated"}


def bounded_output(command, *, pass_fds=(), timeout=2, limit=16384):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=pass_fds)
    deadline, output = time.monotonic() + timeout, bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("archive_probe_timeout")
                if not selector.select(min(0.1, remaining)):
                    continue
                chunk = os.read(process.stdout.fileno(), min(4096, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise ValueError("archive_probe_output_budget")
        return process.wait(timeout=max(0.001, deadline - time.monotonic())), bytes(output)
    except subprocess.TimeoutExpired:
        raise TimeoutError("archive_probe_timeout") from None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def playback_info(root_fd, request):
    if set(request) != {"path", "ffprobe", "offset_seconds"}:
        raise ValueError("archive_invalid_job")
    offset, executable = request["offset_seconds"], request["ffprobe"]
    if not isinstance(offset, (int, float)) or not math.isfinite(offset) or not 0 <= offset < 86400:
        raise ValueError("archive_invalid_seek")
    fd = open_beneath(root_fd, request["path"])
    try:
        before = os.fstat(fd)
        info = probe(fd, executable)
        if info.get("media_start_seconds") != 0 or not info.get("media_metadata_valid"):
            raise ValueError("archive_media_timeline_unqualified")
        # Packet timestamps are metadata, not decoded frames or an accurate seek receipt.
        command = probe_command(fd, executable) + [
            "-select_streams",
            "v:0",
            "-read_intervals",
            f"{offset:.6f}%+#64",
            "-show_packets",
            "-show_entries",
            "packet=pts_time,flags",
            "-of",
            "json",
            f"/dev/fd/{fd}",
        ]
        code, output = bounded_output(command, pass_fds=(fd,))
        if code:
            raise ValueError("archive_keyframe_probe_failed")
        packets = json.loads(output).get("packets", [])
        if len(packets) > 64:
            raise ValueError("archive_keyframe_budget")
        keyframes = []
        for item in packets:
            if "K" in item.get("flags", ""):
                value = float(item["pts_time"])
                if not math.isfinite(value) or not 0 <= value <= info["duration_seconds"]:
                    raise ValueError("archive_invalid_keyframe")
                keyframes.append(value)
        prior = [v for v in keyframes if v <= offset]
        if not prior:
            raise ValueError("archive_keyframe_basis_unknown")
        after = os.fstat(fd)
        current_fd = open_beneath(root_fd, request["path"])
        try:
            current = os.fstat(current_fd)
        finally:
            os.close(current_fd)
        if signature(before) != signature(after) or signature(after) != signature(current):
            raise ValueError("archive_file_still_changing")
        return {
            "signature": signature(current),
            "size_bytes": current.st_size,
            **info,
            "indexed_keyframes_seconds": sorted(set(keyframes)),
            "previous_keyframe_seconds": max(prior),
        }
    finally:
        os.close(fd)


def run_range(root, request, output):
    """Binary stdout only after a bounded JSON header. No source writes/copies."""
    if set(request) != {"path", "signature", "start", "length"}:
        raise ValueError("archive_invalid_job")
    start, length = request["start"], request["length"]
    if (
        type(start) is not int
        or type(length) is not int
        or not 0 <= start <= 1073741824
        or not 0 < length <= 8388608
    ):
        raise ValueError("archive_range_budget")
    root_fd = root_handle(root)
    sent_header = False
    try:
        fd = open_beneath(root_fd, request["path"])
        try:
            before = os.fstat(fd)
            if signature(before) != request["signature"] or start + length > before.st_size:
                raise ValueError("archive_source_changed")
            output.write(
                (json.dumps({"status": "readable", "start": start, "length": length}) + "\n").encode()
            )
            output.flush()
            sent_header = True
            os.lseek(fd, start, os.SEEK_SET)
            remaining = length
            while remaining:
                current_fd = open_beneath(root_fd, request["path"])
                try:
                    if (
                        signature(os.fstat(current_fd)) != request["signature"]
                        or signature(os.fstat(fd)) != request["signature"]
                    ):
                        raise ValueError("archive_source_changed")
                finally:
                    os.close(current_fd)
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    raise ValueError("archive_source_changed")
                check_fd = open_beneath(root_fd, request["path"])
                try:
                    if (
                        signature(os.fstat(check_fd)) != request["signature"]
                        or signature(os.fstat(fd)) != request["signature"]
                    ):
                        raise ValueError("archive_source_changed")
                finally:
                    os.close(check_fd)
                output.write(chunk)
                output.flush()
                remaining -= len(chunk)
            return True
        finally:
            os.close(fd)
    except (OSError, ValueError):
        if not sent_header:
            output.write(b'{"status":"unavailable"}\n')
            output.flush()
        return False
    finally:
        os.close(root_fd)


def inspect(root_fd, paths, executable):
    if not isinstance(paths, list) or len(paths) > 16:
        raise ValueError("archive_job_budget")
    rows = []
    for path in paths:
        if not isinstance(path, str) or len(path.encode()) > 1024:
            raise ValueError("archive_invalid_path")
        try:
            fd = open_beneath(root_fd, path)
            try:
                before = os.fstat(fd)
                metadata = probe(fd, executable)
                after = os.fstat(fd)
            finally:
                os.close(fd)
            check = open_beneath(root_fd, path)
            try:
                current = os.fstat(check)
            finally:
                os.close(check)
            rows.append(
                {
                    "relative_path": path,
                    "present": True,
                    "signature": signature(current),
                    "size_bytes": current.st_size,
                    "unchanged_during_probe": signature(before) == signature(after) == signature(current),
                    **metadata,
                }
            )
        except FileNotFoundError:
            rows.append({"relative_path": path, "present": False, "reason": "archive_file_absent"})
        except (OSError, ValueError):
            rows.append({"relative_path": path, "present": None, "reason": "archive_file_unavailable"})
    return {"files": rows, "media_copied": False}


def run(root, operation, request=None):
    fd = root_handle(root)
    try:
        if operation == "scan":
            return {"status": "readable", **scan(fd)}
        if operation == "inspect":
            request = request or {}
            if set(request) != {"paths", "ffprobe"}:
                raise ValueError("archive_invalid_job")
            return {"status": "readable", **inspect(fd, request["paths"], request["ffprobe"])}
        if operation == "playback-info":
            return {"status": "readable", **playback_info(fd, request or {})}
        raise ValueError("archive_invalid_job")
    finally:
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--operation", choices=["scan", "inspect", "playback-info", "range"], required=True)
    args = parser.parse_args()
    try:
        import sys

        text = sys.stdin.read(16385)
        if len(text.encode()) > 16384:
            raise ValueError("archive_job_budget")
        request = json.loads(text) if text else None
        if args.operation == "range":
            sys.exit(0 if run_range(args.root, request, sys.stdout.buffer) else 1)
        output = run(args.root, args.operation, request)
        encoded = json.dumps(output, allow_nan=False)
        if len(encoded.encode()) > 65536:
            raise ValueError("archive_output_budget")
    except (OSError, ValueError, TypeError):
        encoded = json.dumps({"status": "unavailable", "reason": "archive_read_failed"})
    print(encoded)
