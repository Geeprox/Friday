"""Supervise one explicitly started, read-only Geek worker outside Home Core.

No process recovery replay, persistent session token or permanent-login claim.
The PIN stays in the existing private credential file, never in argv or reports.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .geek_setup import ROOT

REVISION = "4fe2c03ce16c365838b58f70e955e987d1a23531"
FailurePhase = Literal["local_setup", "websocket_connect", "authenticate", "inventory"]
FailureCode = Literal[
    "connection_refused", "network_unreachable", "gateway_closed_connection", "connection_timeout",
    "websocket_upgrade_rejected", "authentication_timeout", "protocol_selection_failed",
    "authentication_handshake_failed", "gateway_error_frame", "unexpected_handshake_frame",
    "readonly_request_timeout", "session_not_connected", "check_failed",
]


class Changes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    added: int = Field(ge=0, le=64)
    removed: int = Field(ge=0, le=64)
    modified: int = Field(ge=0, le=64)


class WorkerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    kind: Literal["status"]
    provider: Literal["oh-my-sage-readonly"]
    revision: Literal[REVISION]
    status: Literal["starting", "online", "reconnecting", "blocked", "stopped"]
    simulated: Literal[False]
    writes_attempted: Literal[False]
    graph_semantics: Literal["opaque"]
    checked_at: str = Field(max_length=40)
    reconnects: int = Field(ge=0, le=2147483647)
    device_count: int | None = Field(default=None, ge=0, le=4096)
    graph_count: int | None = Field(default=None, ge=0, le=64)
    scope_count: int | None = Field(default=None, ge=0, le=32)
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    changes: Changes | None = None
    gap: bool = False
    session_seconds: int | None = Field(default=None, ge=0, le=2147483647)
    failure_phase: FailurePhase | None = None
    failure_code: FailureCode | None = None
    network_errno: Literal["ECONNREFUSED", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "EPERM", "EACCES"] | None = None

    @field_validator("checked_at")
    @classmethod
    def aware_time(cls, value):
        if value is None:
            return value
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("worker_time_requires_zone")
        return value


class LastChange(Changes):
    checked_at: str = Field(max_length=40)


class BackgroundAttempt(BaseModel):
    """Last preparation result, distinct from the current owner's state."""
    model_config = ConfigDict(extra="forbid", strict=True)
    checked_at: str = Field(max_length=40)
    outcome: Literal["connected", "failed"]
    failure_phase: FailurePhase | None = None
    failure_code: FailureCode | None = None
    network_errno: Literal["ECONNREFUSED", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "EPERM", "EACCES"] | None = None

    @field_validator("checked_at")
    @classmethod
    def aware_time(cls, value):
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("attempt_time_requires_zone")
        return value


class GeekSessionView(WorkerStatus):
    schema_version: Literal[1] = 1
    kind: Literal["status"] = "status"
    provider: Literal["oh-my-sage-readonly"] = "oh-my-sage-readonly"
    revision: Literal[REVISION] = REVISION
    status: Literal[
        "stopped", "starting", "online", "reconnecting", "blocked", "stale",
        "worker_stopped", "worker_fault", "worker_unavailable", "pairing_required",
        "unsupported_gateway_address",
        "background_unavailable",
    ]
    simulated: Literal[False] = False
    writes_attempted: Literal[False] = False
    graph_semantics: Literal["opaque"] = "opaque"
    checked_at: str | None = Field(default=None, max_length=40)
    reconnects: int = Field(default=0, ge=0, le=2147483647)
    last_change: LastChange | None = None
    session_live: bool
    credential_available: bool
    inventory_stale: bool
    poll_seconds: Literal[60] = 60
    session_resume: Literal["saved_pin_reauthentication"] = "saved_pin_reauthentication"
    permanent_login_verified: Literal[False] = False
    raw_graphs_public: Literal[False] = False
    retention_days: Literal[7] = 7
    coverage: Literal["geek_visible_only"] = "geek_visible_only"
    external_rules_intercepted: Literal[False] = False
    connection_host: Literal["console_child", "background_service"] = "console_child"
    background_service_live: bool = False
    keep_connected: bool = False
    background_last_attempt: BackgroundAttempt | None = None


class GeekSession:
    def __init__(self, setup, *, launcher=None, clock=time.monotonic):
        self.setup, self.launcher, self.clock = setup, launcher or self._launch, clock
        self.gate = asyncio.Lock()
        self.process = self.task = None
        self.url = None
        self.generation = 0
        self.last_seen = self.last_inventory = None
        self.report = {"status": "stopped"}
        self.last_change = None
        self.inventory_root = None
        self.inventory_gate = asyncio.Lock()
        self.evidence_lease = None

    async def _launch(self, url, output):
        return await asyncio.create_subprocess_exec(
            "node", "--max-old-space-size=192", str(ROOT / "workers/geek/session.mjs"),
            "--url", url, "--passcode-file", str(self.setup.secret.absolute()),
            "--output", str(output.absolute()), cwd=ROOT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=8192,
            env={k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ},
        )

    def live(self):
        return bool(self.process and self.process.returncode is None and self.task and not self.task.done())

    def status(self):
        report = self.report.copy()
        live = self.live()
        recent = self.last_inventory is not None and 0 <= self.clock() - self.last_inventory <= 180
        if report["status"] == "online" and (not live or not recent):
            report["status"] = "stale" if live else "worker_stopped"
        return GeekSessionView.model_validate({
            **report, "last_change": self.last_change, "session_live": live,
            "credential_available": self.setup.available(), "inventory_stale": report["status"] != "online",
        }).model_dump(exclude_none=True)

    async def connect(self, url):
        async with self.gate:
            if self.live() and self.url == url:
                return self.status()
            await self._stop()
            if not url or not self.setup.available():
                self.report = {"status": "pairing_required"}
                return self.status()
            # Share the existing URL validation; no new discovery or credential destination.
            if not self.setup.valid_url(url):
                self.report = {"status": "unsupported_gateway_address"}
                return self.status()
            try:
                self.setup.prune()
                self.setup.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
                if any(p.is_symlink() for p in (self.setup.evidence, *self.setup.evidence.parents)):
                    raise ValueError("evidence_symlink")
                os.chmod(self.setup.evidence, 0o700)
                output = self.setup.evidence / ("session-" + str(uuid4()))
                self.evidence_lease = self.setup.lease(output)
                launch = asyncio.create_task(self.launcher(url, output))
                try:
                    process = await asyncio.wait_for(asyncio.shield(launch), timeout=10)
                except (asyncio.CancelledError, TimeoutError):
                    # A process appearing after its HTTP caller has gone away is
                    # never admitted; close only that owned late process.
                    def clean_late(task):
                        if not task.cancelled() and task.exception() is None:
                            asyncio.create_task(self._terminate(task.result()))
                    launch.add_done_callback(clean_late)
                    raise
                self.process, self.url = process, url
                self.inventory_root = output
                self.last_seen = self.clock()
                self.last_inventory = None
                self.report = {"status": "starting"}
                self.generation += 1
                self.task = asyncio.create_task(self._read(process, self.generation))
            except (OSError, ValueError, TimeoutError):
                self._release_evidence()
                self.report = {"status": "worker_unavailable"}
            except asyncio.CancelledError:
                self._release_evidence()
                raise
            return self.status()

    async def _read(self, process, generation):
        window, messages = self.clock(), 0
        try:
            while generation == self.generation:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=190)
                if not line:
                    break
                if len(line) > 4096 or not line.endswith(b"\n"):
                    raise ValueError("worker_output_budget")
                if self.clock() - window >= 10:
                    window, messages = self.clock(), 0
                messages += 1
                if messages > 12:
                    raise ValueError("worker_rate_budget")
                report = WorkerStatus.model_validate_json(line).model_dump(exclude_none=True)
                if report["status"] == "online" and any(report.get(k) is None for k in (
                    "device_count", "graph_count", "scope_count", "fingerprint", "changes", "session_seconds"
                )):
                    raise ValueError("incomplete_inventory")
                if generation != self.generation:
                    break
                self.report = report
                self.last_seen = self.clock()
                if report["status"] == "online":
                    self.last_inventory = self.clock()
                    changes = report["changes"]
                    if any(changes.values()):
                        self.last_change = {"checked_at": report["checked_at"], **changes}
            if generation == self.generation and self.report["status"] not in {"blocked", "stopped"}:
                self.report = {"status": "worker_stopped"}
        except (ValueError, OSError, TimeoutError):
            if generation == self.generation:
                self.report = {"status": "worker_fault", "failure_code": "check_failed"}
        finally:
            await self._terminate(process)
            if generation == self.generation:
                self._release_evidence()

    @staticmethod
    async def _terminate(process):
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.stdin:
            process.stdin.close()

    async def _stop(self):
        self.generation += 1
        task, process = self.task, self.process
        self.task = self.process = None
        self.url = None
        self.inventory_root = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # A just-created reader can be cancelled before its coroutine enters
        # try/finally. Always reap the owned process as well.
        if process:
            await self._terminate(process)
        self._release_evidence()
        self.report = {"status": "stopped"}
        self.last_seen = self.last_inventory = None
        self.last_change = None

    def _release_evidence(self):
        if self.evidence_lease is not None:
            os.close(self.evidence_lease)
            self.evidence_lease = None

    async def close(self):
        async with self.gate:
            await self._stop()

    async def release(self):
        await self.close()

    async def refresh(self):
        return self.status()

    async def inventory(self):
        from .geek_inventory import GeekInventoryView, read_inventory

        def unavailable(status):
            return GeekInventoryView(session=self.status(), status=status)

        before = self.status()
        if before["status"] != "online" or not self.inventory_root:
            return unavailable("stale" if self.last_inventory is not None else "unavailable")
        if self.inventory_gate.locked():
            return unavailable("unavailable")
        generation, root, fingerprint = self.generation, self.inventory_root, before["fingerprint"]
        async with self.inventory_gate:
            try:
                result = await asyncio.wait_for(asyncio.to_thread(
                    read_inventory, root, fingerprint, before["graph_count"]
                ), timeout=2)
            except (OSError, ValueError, TypeError, KeyError, RecursionError, TimeoutError):
                return unavailable("integrity_failed")
        after = self.status()
        # A result from an old session/endpoint or prior content is never shown
        # as current, even when local IO finishes after disconnect/reconnect.
        if generation != self.generation or root != self.inventory_root or after["status"] != "online" or fingerprint != after.get("fingerprint"):
            return unavailable("stale")
        return GeekInventoryView(session=after, status="ready", **result)
