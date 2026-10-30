"""Persistent household intent and supervision, independent of any UI consumer.

No import or GET opens a camera. The owning application's lifespan explicitly
starts this coordinator on a configured real household. Mi Home owns shutter intent.
"""

import asyncio
import json
import os
import time

import httpx
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .runtime_log import observe


class ServicePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(default=0, ge=0)
    enabled: bool = False
    recognition: bool = True
    interaction: bool = True
    auto_open_cameras: bool = False

    @field_validator("auto_open_cameras")
    @classmethod
    def preserve_manual_shutters(cls, value):
        # Accept old saved policies without retaining permission to reopen a
        # camera after a newer manual close or Mi Home scene.
        return False


class HouseholdInfrastructure:
    def __init__(self, service, path, *, admitted, people=None, prepare=None, interval=5):
        self.service, self.path = service, Path(path)
        self.admitted, self.people, self.interval = admitted, people, interval
        self.prepare = prepare
        self.policy = ServicePolicy()
        self.failure = None
        try:
            if self.path.exists():
                if self.path.is_symlink() or self.path.stat().st_size > 4096:
                    raise ValueError("invalid_config")
                self.policy = ServicePolicy.model_validate_json(self.path.read_bytes())
        except (ValueError, OSError):
            # A corrupt saved pause must never become implicit permission.
            self.policy = ServicePolicy(enabled=False)
            self.failure = "invalid_config"
        self.task = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.retry_at = 0
        self.failures = 0
        self.service.capture_enabled = self.policy.enabled and self.policy.recognition
        self.service.recovery_enabled = bool(self.admitted())

    async def start(self):
        if self.task is None and not self.closed:
            self.task = asyncio.create_task(self._run(), name="friday-household-supervisor")

    async def _run(self):
        while not self.closed:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep recovery alive; no exception text or household content.
                self.failure = "service_unavailable"
                self.failures += 1
                self.retry_at = time.monotonic() + min(60, 2 ** min(self.failures, 6))
            await asyncio.sleep(self.interval)

    @observe("infrastructure.reconcile")
    async def reconcile(self):
        async with self.lock:
            if self.closed or not self.admitted():
                return
            if not self.policy.enabled or not self.policy.recognition:
                await self.service.stop()
                return
            if time.monotonic() < self.retry_at or not self.service.configured():
                return
            if self.prepare is not None:
                await self.prepare()
            await self.service.ensure_background(interaction=self.policy.interaction,
                open_privacy=False)
            if self.people is not None:
                await self.service.sync_people(self.people.page().people)
            self.failure, self.failures, self.retry_at = None, 0, 0

    @observe("infrastructure.configure")
    async def configure(self, requested):
        async with self.lock:
            requested = ServicePolicy.model_validate(requested.model_dump())
            if requested.revision != self.policy.revision:
                raise ValueError("source_conflict")
            updated = requested.model_copy(update={"revision": requested.revision + 1})
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            import tempfile
            fd, name = tempfile.mkstemp(prefix=".infrastructure-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(updated.model_dump(), stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self.policy = updated
            # Fence direct game/preview starts before waiting for worker cleanup.
            self.service.capture_enabled = updated.enabled and updated.recognition
            self.failure, self.retry_at = None, 0
            try:
                if not updated.enabled or not updated.recognition:
                    preview=getattr(self.service,'manual_previews',None)
                    if preview is not None:await preview.close()
                    await self.service.stop()
                else:
                    await self.service.set_interaction(updated.interaction)
            except (OSError, ValueError, TimeoutError, httpx.HTTPError):
                self.failure = "service_unavailable"
        # Reconciliation owns recovery even if the immediate attempt fails.
        return self.report()

    def report(self):
        return dict(policy=self.policy.model_dump(), state="unavailable" if self.failure else
                    "paused" if not self.policy.enabled or not self.policy.recognition else
                    "supervising" if self.task and not self.task.done() and self.admitted() else "waiting",
                    failure=self.failure, owner="household_service", requires_open_window=False)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
