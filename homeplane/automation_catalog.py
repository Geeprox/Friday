"""Private observed catalog, independent of execution authority and source graphs."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .geek_inventory import _read
from .runtime_log import observe

LIMIT = 256 * 1024


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(min_length=1, max_length=160)
    source: Literal["geek", "mijia"]
    kind: Literal["automation", "scene"]
    name: str = Field(min_length=1, max_length=160)
    source_id: str | None = Field(default=None, max_length=160)
    observed_at: AwareDatetime = Field(strict=False)
    evidence: Literal["native_backup", "ui_details", "ui_name_index"]
    enabled: bool | None = None
    summary: str = Field(default="", max_length=400)
    triggers: list[str] = Field(default_factory=list, max_length=16)
    conditions: list[str] = Field(default_factory=list, max_length=16)
    actions: list[str] = Field(default_factory=list, max_length=24)
    issues: list[str] = Field(default_factory=list, max_length=8)
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def valid_evidence(self):
        if self.observed_at > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("invalid_config")
        if any(len(v) > 400 for rows in (self.triggers, self.conditions, self.actions, self.issues) for v in rows):
            raise ValueError("invalid_config")
        if self.source == "mijia" and self.source_id is not None:
            raise ValueError("invalid_config")  # current UI adapter has no stable IDs
        if self.evidence == "ui_name_index" and (self.enabled is not None or self.triggers or self.conditions or self.actions):
            raise ValueError("invalid_config")
        return self


class ModeLink(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=32)
    entries: list[str] = Field(max_length=16)
    chain: list[str] = Field(max_length=8)
    gap: str = Field(default="", max_length=400)


class Bundle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    entries: list[Entry] = Field(default_factory=list, max_length=256)
    modes: list[ModeLink] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def unique_and_bound(self):
        keys = {e.key for e in self.entries}
        if len(keys) != len(self.entries) or any(k not in keys for m in self.modes for k in m.entries):
            raise ValueError("invalid_config")
        if any(len(v) > 400 for m in self.modes for v in m.chain):
            raise ValueError("invalid_config")
        return self


class Import(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    bundle: Bundle


class AutomationCatalog:
    def __init__(self, path: Path):
        self.path = Path(path)

    @observe("automation.catalog.read")
    def read(self):
        try:
            return Bundle.model_validate_json(_read(self.path, LIMIT))
        except FileNotFoundError:
            return Bundle()
        except (OSError, ValueError, TypeError):
            raise ValueError("invalid_config") from None

    @observe("automation.catalog.import")
    def replace(self, request: Import):
        current = self.read()
        if current.revision != request.expected_revision:
            raise ValueError("source_conflict")
        updated = request.bundle.model_copy(update={"revision": current.revision + 1})
        raw = updated.model_dump_json().encode()
        if len(raw) > LIMIT:
            raise ValueError("request_budget_exceeded")
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("invalid_config")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=".automation-catalog-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return updated


def create_router(path, manager, geek_inventory):
    router = APIRouter(prefix="/api/home/automations")
    owner = AutomationCatalog(path)
    router.catalog_owner = owner

    @router.get("/catalog")
    async def catalog():
        try:
            bundle = owner.read()
        except ValueError:
            raise HTTPException(409, detail="catalog_unreadable") from None
        try:
            geek = await geek_inventory()
            geek = geek.model_dump(mode="json") if hasattr(geek, "model_dump") else geek
            connection = {"status": geek.get("status", "unavailable"), "observed_at": geek.get("observed_at"), "count": len(geek.get("graphs", []))}
        except (OSError, ValueError, TimeoutError):
            connection = {"status": "unavailable", "observed_at": None, "count": None}
        owned = [{"key": r.rule.id, "name": r.rule.intent, "enabled": r.enabled} for r in manager.records()]
        return {**bundle.model_dump(mode="json"), "owned": owned, "geek_connection": connection,
                "coverage": "observed_catalog_only", "external_rules_intercepted": False,
                "external_writes_supported": False, "mode_state": "unknown"}

    @router.post("/catalog")
    async def import_catalog(request: Import):
        try:
            return owner.replace(request)
        except (OSError, ValueError) as exc:
            code = str(exc) if str(exc) in {"source_conflict", "request_budget_exceeded"} else "invalid_config"
            raise HTTPException(409, detail=code) from None

    return router
