"""只保存本应用明确配置的连接，不搜索其他应用的凭据。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


def default_directory():
    configured = os.environ.get("FRIDAY_INTELLIGENCE_DIRECTORY")
    if configured:
        return Path(configured).absolute()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Friday/intelligence"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "friday/intelligence"


class IntelligenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: Literal["arl", "openai", "auto", "local"] = "arl"
    allow_arl_fallback: StrictBool = True
    cloud_text: StrictBool = False
    cloud_images: StrictBool = False
    cloud_voice: StrictBool = False
    background_with_mv: StrictBool = False
    cached_background_limit: int = Field(default=128, ge=0, le=2048)
    responses_model: str = Field(default="gpt-6-luna", pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    decisions_model: Literal["gpt-6-luna"] = "gpt-6-luna"
    image_model: str = Field(default="gpt-image-2.5-flare", pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    realtime_model: str = Field(default="gpt-realtime-2.1", pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    daily_requests: int = Field(default=100, ge=0, le=100000)
    daily_text_tokens: int = Field(default=100000, ge=0, le=100000000)
    daily_images: int = Field(default=5, ge=0, le=1000)
    daily_voice_seconds: int = Field(default=300, ge=0, le=86400)
    max_output_tokens: int = Field(default=2048, ge=128, le=8192)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("invalid_usage_timezone") from None
        return value


def private_path(path: Path):
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("private_canonical_path_required")


def atomic_json(path: Path, value):
    private_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    part = path.with_name("." + path.name + "." + secrets.token_hex(8))
    try:
        with part.open("x", encoding="utf-8") as stream:
            part.chmod(0o600)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


class IntelligenceSettings:
    def __init__(self, directory, *, key_resolver=None):
        self.directory = Path(directory).absolute()
        private_path(self.directory)
        self.path = self.directory / "intelligence.json"
        self.credential = self.directory / "openai-credential.json"
        self.key_resolver = key_resolver

    @contextmanager
    def lock(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / "intelligence.lock"
        private_path(path)
        with path.open("a+") as stream:
            path.chmod(0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def read(self):
        private_path(self.path)
        if not self.path.exists():
            return IntelligenceConfig()
        if self.path.stat().st_size > 16384:
            raise ValueError("intelligence_settings_unavailable")
        return IntelligenceConfig.model_validate_json(self.path.read_text())

    def update(self, patch):
        with self.lock():
            config = IntelligenceConfig.model_validate({**self.read().model_dump(), **patch})
            atomic_json(self.path, config.model_dump())
        return config

    def key(self):
        if self.key_resolver is not None:
            value = self.key_resolver()
        else:
            value = os.environ.get("OPENAI_API_KEY")
            if not value:
                private_path(self.credential)
                if not self.credential.exists():
                    return None
                if self.credential.stat().st_size > 4096 or self.credential.stat().st_mode & 0o077:
                    raise ValueError("private_credential_permissions_required")
                stored = json.loads(self.credential.read_text())
                if not isinstance(stored, dict):
                    raise ValueError("invalid_api_credential")
                value = stored.get("api_key")
        if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{8,512}", value):
            return None
        return value

    def save_key(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{8,512}", value):
            raise ValueError("invalid_api_key")
        with self.lock():
            atomic_json(self.credential, {"api_key": value})

    def remove_key(self):
        with self.lock():
            private_path(self.credential)
            self.credential.unlink(missing_ok=True)
