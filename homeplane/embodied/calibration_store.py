"""One private, atomic generation containing both calibration and reference."""

import base64
import fcntl
import hashlib
import json
import os
import secrets
import stat

NAME = "room-calibration.v2.json"
MAX_CONFIG = 65536
MAX_REFERENCE = 2_000_000
MAX_BUNDLE = 2_750_000


def manual_config(config):
    value = dict(config)
    value.pop("_source", None)
    manual = value.get("mode") == "relative_calibrated"
    value.update(
        mode="relative_calibrated" if manual else "approximate",
        calibration_source="manual",
        metric=False,
        note="相对坐标 · 手动标定 · 无米制尺寸" if manual else "相对坐标 · 粗略标定",
    )
    return value


def encode(config, reference):
    config_bytes = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if not isinstance(config, dict) or len(config_bytes) > MAX_CONFIG:
        raise ValueError("calibration_invalid")
    if not isinstance(reference, bytes) or not 0 < len(reference) <= MAX_REFERENCE:
        raise ValueError("calibration_invalid")
    digest = hashlib.sha256(config_bytes + b"\0" + reference).hexdigest()
    payload = json.dumps(
        dict(v=2, revision=digest, config=config, reference_jpeg=base64.b64encode(reference).decode()),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if len(payload) > MAX_BUNDLE:
        raise ValueError("calibration_invalid")
    return payload, digest


def decode(payload):
    if not 0 < len(payload) <= MAX_BUNDLE:
        raise ValueError("calibration_invalid")
    value = json.loads(payload)
    if (
        not isinstance(value, dict)
        or set(value) != {"v", "revision", "config", "reference_jpeg"}
        or type(value["v"]) is not int
        or value["v"] != 2
        or not isinstance(value["reference_jpeg"], str)
    ):
        raise ValueError("calibration_invalid")
    reference = base64.b64decode(value["reference_jpeg"], validate=True)
    _, digest = encode(value["config"], reference)
    if value["revision"] != digest:
        raise ValueError("calibration_invalid")
    return value["config"], reference, digest


def private(info, *, directory=False):
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not kind
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or not directory
        and (info.st_nlink != 1 or info.st_size > MAX_BUNDLE)
    ):
        raise ValueError("calibration_private_path_required")


class CalibrationStore:
    def __init__(self, folder):
        self.folder = folder

    def directory(self):
        fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            private(os.fstat(fd), directory=True)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self):
        directory = self.directory()
        try:
            fd = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                private(os.fstat(stream.fileno()))
                return decode(stream.read(MAX_BUNDLE + 1))
        finally:
            os.close(directory)

    def cleanup(self, nonce, payload, created_after):
        import re

        if not isinstance(nonce, str) or not re.fullmatch("[a-f0-9]{32}", nonce):
            raise ValueError("calibration_invalid")
        directory = self.directory()
        name = ".room-calibration-" + nonce + ".tmp"
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                private(info)
                if getattr(info, "st_birthtime", info.st_ctime) < created_after or not payload.startswith(
                    stream.read(MAX_BUNDLE + 1)
                ):
                    raise ValueError("calibration_private_path_required")
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError("calibration_private_path_required")
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(directory)

    def save(self, config, reference, *, nonce=None, checkpoint=lambda _: None):
        payload, digest = encode(config, reference)
        directory = self.directory()
        nonce = nonce or secrets.token_hex(16)
        import re

        if not re.fullmatch("[a-f0-9]{32}", nonce):
            os.close(directory)
            raise ValueError("calibration_invalid")
        temporary = ".room-calibration-" + nonce + ".tmp"
        lock = None
        created = False
        try:
            lock = os.open(
                "room-calibration.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            private(os.fstat(lock))
            if os.fstat(lock).st_size:
                raise ValueError("calibration_private_path_required")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                private(os.stat(NAME, dir_fd=directory, follow_symlinks=False))
            except FileNotFoundError:
                pass
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            created = True
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            checkpoint("prepared")
            os.replace(temporary, NAME, src_dir_fd=directory, dst_dir_fd=directory)
            checkpoint("replaced")
            os.fsync(directory)
            return digest
        finally:
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
            if lock is not None:
                os.close(lock)
            os.close(directory)
