"""Local atomic configuration/checkpoint store and one-owner process lock."""

import fcntl
import json
import os
import tempfile
from pathlib import Path

from .contracts import NASConnectionConfig, NASConnectionError


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        self.fd = None
        self._check_path()

    def _check_path(self):
        if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents):
            raise NASConnectionError("nas_invalid_request")

    def load(self):
        self._check_path()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None, 0, None
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(32769)
        if len(data) > 32768:
            raise NASConnectionError("nas_budget_exceeded")
        try:
            value = json.loads(data)
            if set(value) != {"schema_version", "config", "config_generation", "paused_reason"}:
                raise ValueError()
            if value["schema_version"] != 1 or type(value["config_generation"]) is not int:
                raise ValueError()
            if value["config_generation"] < 1:
                raise ValueError()
            reason = value["paused_reason"]
            if reason not in {None, "nas_auth_failed", "nas_access_denied", "nas_host_key_mismatch",
                              "nas_dependency_missing"}:
                raise ValueError()
            return NASConnectionConfig.model_validate(value["config"]), value["config_generation"], reason
        except (ValueError, TypeError, KeyError):
            raise NASConnectionError("nas_invalid_request") from None

    def claim(self):
        if self.fd is not None:
            return
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".owner", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise NASConnectionError("nas_busy") from None
        self.fd = fd

    def save(self, config, generation, paused):
        self.claim()
        self._check_path()
        fd, name = tempfile.mkstemp(prefix=".nas-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"schema_version": 1, "config": config.model_dump(),
                           "config_generation": generation, "paused_reason": paused}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
