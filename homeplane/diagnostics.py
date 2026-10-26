"""Bounded, allowlisted local support evidence. Never copies logs or source payloads."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import io
import json
import platform
import re
import sqlite3
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .geek_session import FailureCode, FailurePhase

MAX_BYTES = 65536
ROW_LIMIT = 1000
SNAPSHOT_LIMIT = 4096
EXCLUDED = (
    "credentials",
    "addresses_and_paths",
    "device_room_member_names_and_ids",
    "raw_logs_and_exceptions",
    "graphs_rules_prompts_and_transcripts",
    "audio_video_images_and_biometric_templates",
    "database_and_settings_files",
)
VERSION = re.compile(r"^\d{1,4}\.\d{1,4}(?:\.\d{1,4})?(?:(?:a|b|rc)\d{1,4})?$")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DiagnosticsExportRequest(Contract):
    redacted: Literal[True]

    @field_validator("redacted", mode="before")
    @classmethod
    def explicitly_redacted(cls, value):
        if value is not True:
            raise ValueError("redacted_export_required")
        return value


class DiagnosticsPolicy(Contract):
    schema_version: Literal[1] = 1
    redacted: Literal[True] = True
    sensitivity: Literal["operational_metadata_redacted"] = "operational_metadata_redacted"
    server_retention_seconds: Literal[0] = 0
    downloaded_file_retention: Literal["user_managed"] = "user_managed"
    max_bytes: Literal[65536] = MAX_BYTES
    collection_timeout_seconds: Literal[2] = 2
    journal_row_limit_per_table: Literal[1000] = ROW_LIMIT
    collection_basis: Literal["cached_local_state_no_upstream_refresh"] = (
        "cached_local_state_no_upstream_refresh"
    )
    excluded: tuple[str, ...] = EXCLUDED
    automatic_upload: Literal[False] = False


class QualityCounts(Contract):
    healthy: int = Field(default=0, ge=0, le=SNAPSHOT_LIMIT)
    suspect: int = Field(default=0, ge=0, le=SNAPSHOT_LIMIT)
    stale: int = Field(default=0, ge=0, le=SNAPSHOT_LIMIT)
    faulty: int = Field(default=0, ge=0, le=SNAPSHOT_LIMIT)
    unknown: int = Field(default=0, ge=0, le=SNAPSHOT_LIMIT)


class SnapshotSummary(Contract):
    mode: Literal["simulator", "ha-read-only", "unavailable"]
    simulated: bool | None
    cached_snapshot_at: datetime | None = None
    snapshot_available: bool = False
    sampled_entities: int = Field(default=0, ge=0, le=SNAPSHOT_LIMIT)
    sampled_devices: int = Field(default=0, ge=0, le=SNAPSHOT_LIMIT)
    sampled_states: int = Field(default=0, ge=0, le=SNAPSHOT_LIMIT)
    truncated: bool = False
    quality: QualityCounts = Field(default_factory=QualityCounts)


class HAConnectionSummary(Contract):
    transport: Literal["online", "offline", "unknown", "unavailable"] = "unavailable"
    version: str | None = Field(default=None, pattern=VERSION.pattern, max_length=32)
    continuous_subscription: bool | None = None
    observation_gap: bool | None = None
    journal_available: bool | None = None

    @field_validator("version")
    @classmethod
    def canonical_version(cls, value):
        if value is not None and not VERSION.fullmatch(value):
            raise ValueError("invalid_version")
        return value


class GeekConnectionSummary(Contract):
    status: Literal[
        "stopped",
        "starting",
        "online",
        "reconnecting",
        "blocked",
        "stale",
        "worker_stopped",
        "worker_fault",
        "worker_unavailable",
        "pairing_required",
        "unsupported_gateway_address",
        "background_unavailable",
        "unknown",
    ] = "unknown"
    connection_host: Literal["console_child", "background_service", "unknown"] = "unknown"
    session_live: bool | None = None
    inventory_stale: bool | None = None
    background_service_live: bool | None = None
    keep_connected: bool | None = None
    failure_phase: FailurePhase | None = None
    failure_code: FailureCode | None = None
    network_errno: (
        Literal["ECONNREFUSED", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "EPERM", "EACCES"] | None
    ) = None
    permanent_login_verified: Literal[False] = False


class JournalSummary(Contract):
    status: Literal["available", "missing", "unavailable"]
    sampled_rows: dict[
        Literal["traces", "events", "light_actions"], Annotated[int, Field(ge=0, le=ROW_LIMIT)]
    ]
    truncated: bool = False
    sampled_action_stages: dict[
        Literal[
            "planned",
            "authorized",
            "sent",
            "accepted",
            "reported_match",
            "physically_confirmed",
            "failed",
            "expired",
            "unknown_outcome",
            "rejected",
            "unknown",
        ],
        Annotated[int, Field(ge=0, le=ROW_LIMIT)],
    ] = Field(default_factory=dict)


class DiagnosticsReport(Contract):
    schema_version: Literal[1] = 1
    generated_at: datetime
    policy: DiagnosticsPolicy = Field(default_factory=DiagnosticsPolicy)
    versions: dict[Literal["python", "macos", "fastapi", "pydantic", "httpx", "websockets"], str | None]
    source: SnapshotSummary
    ha: HAConnectionSummary
    geek: GeekConnectionSummary
    journal: JournalSummary
    hardware_offline_qualification: Literal["not_established_by_diagnostics"] = (
        "not_established_by_diagnostics"
    )
    real_writes_attempted: Literal[False] = False

    @field_validator("versions")
    @classmethod
    def canonical_versions(cls, values):
        if any(value is not None and not VERSION.fullmatch(value) for value in values.values()):
            raise ValueError("invalid_version")
        return values


def version(value):
    return value if isinstance(value, str) and len(value) <= 32 and VERSION.fullmatch(value) else None


def boolean(value):
    return value if type(value) is bool else None


def choice(value, allowed, default=None):
    return value if isinstance(value, str) and value in allowed else default


def project(source, mode, ha, geek, journal):
    """Construct every field; extra upstream text never enters the export."""
    mode = choice(mode, {"simulator", "ha-read-only"}, "unavailable")
    snapshot = SnapshotSummary(mode=mode, simulated=mode == "simulator" if mode != "unavailable" else None)
    if source is not None and source.mode == mode:
        collections = [source.entities, source.devices, source.states]
        qualities = Counter(
            s.quality if s.quality in QualityCounts.model_fields else "unknown"
            for s in source.states[:SNAPSHOT_LIMIT]
        )
        snapshot = SnapshotSummary(
            mode=mode,
            simulated=mode == "simulator",
            snapshot_available=True,
            cached_snapshot_at=source.generated_at,
            sampled_entities=min(len(source.entities), SNAPSHOT_LIMIT),
            sampled_devices=min(len(source.devices), SNAPSHOT_LIMIT),
            sampled_states=min(len(source.states), SNAPSHOT_LIMIT),
            truncated=any(len(items) > SNAPSHOT_LIMIT for items in collections),
            quality=QualityCounts(**qualities),
        )
    ha = ha if isinstance(ha, dict) else {}
    geek = geek if isinstance(geek, dict) else {}
    transport = ha.get("transport")
    ha_summary = HAConnectionSummary(
        transport=choice(transport, {"online", "offline", "unknown"}, "unavailable"),
        version=version(ha.get("ha_version")),
        continuous_subscription=boolean(ha.get("continuous_subscription")),
        observation_gap=boolean(ha.get("observation_gap")),
        journal_available=boolean(ha.get("diagnostic_journal_available")),
    )
    fields = GeekConnectionSummary.model_fields
    allowed = {
        key: geek[key]
        for key in ("status", "connection_host")
        if isinstance(geek.get(key), str) and geek[key] in get_args(fields[key].annotation)
    }
    for key in ("session_live", "inventory_stale", "background_service_live", "keep_connected"):
        allowed[key] = boolean(geek.get(key))
    # Optional Literal unions need a separate fixed allowlist, not arbitrary error strings.
    allowed["failure_phase"] = choice(geek.get("failure_phase"), get_args(FailurePhase))
    allowed["failure_code"] = choice(geek.get("failure_code"), get_args(FailureCode))
    errors = {"ECONNREFUSED", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "EPERM", "EACCES"}
    allowed["network_errno"] = choice(geek.get("network_errno"), errors)
    versions = {"python": version(platform.python_version()), "macos": version(platform.mac_ver()[0])}
    for dependency in ("fastapi", "pydantic", "httpx", "websockets"):
        try:
            versions[dependency] = version(importlib.metadata.version(dependency))
        except importlib.metadata.PackageNotFoundError:
            versions[dependency] = None
    return DiagnosticsReport(
        generated_at=datetime.now(UTC),
        versions=versions,
        source=snapshot,
        ha=ha_summary,
        geek=GeekConnectionSummary(**allowed),
        journal=journal,
    )


def journal_summary(path):
    """Separate read-only connection: no prune/checkpoint/migration or body selection."""
    empty = {table: 0 for table in ("traces", "events", "light_actions")}
    if not path.exists():
        return JournalSummary(status="missing", sampled_rows=empty)
    connection = None
    try:
        deadline = time.monotonic() + 1
        connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.05)
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        counts, stages, truncated = {}, Counter(), False
        permitted = get_args(JournalSummary.model_fields["sampled_action_stages"].annotation)[0]
        for table in empty:
            column = "status" if table == "light_actions" else "1"
            rows = connection.execute(
                f"SELECT {column} FROM {table} ORDER BY rowid DESC LIMIT ?", (ROW_LIMIT + 1,)
            ).fetchall()
            truncated = truncated or len(rows) > ROW_LIMIT
            counts[table] = min(len(rows), ROW_LIMIT)
            if table == "light_actions":
                for (stage,) in rows[:ROW_LIMIT]:
                    stages[
                        stage if isinstance(stage, str) and stage in get_args(permitted) else "unknown"
                    ] += 1
        return JournalSummary(
            status="available", sampled_rows=counts, truncated=truncated, sampled_action_stages=dict(stages)
        )
    except (sqlite3.Error, OSError):
        return JournalSummary(status="unavailable", sampled_rows=empty)
    finally:
        if connection is not None:
            connection.close()


def bundle(report):
    body = report.model_dump_json(indent=2).encode()
    if len(body) > 32768:
        raise ValueError("diagnostics_budget_exceeded")
    manifest = json.dumps(
        {
            "schema_version": 1,
            "redacted": True,
            "files": [
                {"name": "report.json", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            ],
        },
        separators=(",", ":"),
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in (("report.json", body), ("manifest.json", manifest)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    data = output.getvalue()
    if len(data) > MAX_BYTES:
        raise ValueError("diagnostics_budget_exceeded")
    return data


class Diagnostics:
    def __init__(self, path):
        self.path = Path(path)
        self.task = None
        self.next_export = 0

    async def export(self, source, mode, ha, geek):
        if (self.task and not self.task.done()) or time.monotonic() < self.next_export:
            raise ValueError("diagnostics_busy")
        self.next_export = time.monotonic() + 1

        def collect():
            return bundle(project(source, mode, ha, geek, journal_summary(self.path)))

        self.task = asyncio.create_task(asyncio.to_thread(collect))
        # Keep an overdue owned task referenced: timeout never admits a new queued collector.
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return await asyncio.wait_for(asyncio.shield(self.task), 2)
