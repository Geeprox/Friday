"""Durable continuations of existing jobs, with one finite fixture-only claim per call."""

import asyncio
import secrets
from contextlib import nullcontext
from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from homeplane.contracts import Contract
from homeplane.family_media.contracts import Digest, OpaqueRef

from .contracts import StewardBudget, StewardJob
from .service import StewardError
from .settings_contracts import StewardSettingsRequest


class StewardQueueEntry(Contract):
    job_id: Digest
    settings_fingerprint: Digest
    budget: StewardBudget
    status: Literal[
        "held", "waiting", "claimed", "paused", "done", "needs_ocr", "cancelled", "failed", "offline", "stale"
    ]
    reason: str = Field(max_length=120)
    revision: int = Field(default=0, ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    lease_token: OpaqueRef | None = None
    lease_expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def lease_pair(self):
        if (self.status == "claimed") != bool(self.lease_token and self.lease_expires_at):
            raise ValueError("steward_queue_invalid_lease")
        if self.status != "claimed" and (self.lease_token or self.lease_expires_at):
            raise ValueError("steward_queue_unexpected_lease")
        return self


class StewardQueueControl(Contract):
    expected_context: Digest
    job_id: Digest
    expected_revision: int = Field(ge=0)
    expected_settings_fingerprint: Digest
    decision: Literal["pause", "cancel", "resume_preview"]


class StewardExtractionQueue:
    def __init__(self, steward):
        self.owner = steward
        self.active_tasks = {}
        with steward.connection:
            steward.connection.execute(
                "CREATE TABLE IF NOT EXISTS steward_queue (id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )

    def records(self):
        return [
            StewardQueueEntry.model_validate_json(row[0])
            for row in self.owner.connection.execute("SELECT body FROM steward_queue ORDER BY id LIMIT 256")
        ]

    def public_records(self):
        result = []
        for entry in self.records():
            job = self.owner._load("jobs", entry.job_id, StewardJob)
            if job.status in {"done", "needs_ocr"} and entry.status not in {"claimed", "cancelled"}:
                entry = entry.model_copy(update={"status": job.status, "reason": job.reason})
            result.append(entry.model_dump(mode="json", exclude={"lease_token", "lease_expires_at"}))
        return result

    def active_lease(self, job_id):
        try:
            entry = self.owner._load("queue", job_id, StewardQueueEntry)
        except StewardError:
            return False
        return entry.status == "claimed" and entry.lease_expires_at > self.owner.clock()

    def _replace(self, entry, **changes):
        old = entry.model_dump_json()
        value = StewardQueueEntry.model_validate(
            entry.model_copy(
                update={"revision": entry.revision + 1, "updated_at": self.owner.clock(), **changes}
            ).model_dump()
        )
        with nullcontext() if self.owner.connection.in_transaction else self.owner.connection:
            updated = self.owner.connection.execute(
                "UPDATE steward_queue SET body=? WHERE id=? AND body=?",
                (value.model_dump_json(), entry.job_id, old),
            ).rowcount
            if updated != 1:
                raise StewardError("steward_queue_changed")
        return value

    def recover(self):
        for entry in self.records():
            if entry.status == "claimed" and entry.lease_expires_at <= self.owner.clock():
                job = self.owner._load("jobs", entry.job_id, StewardJob)
                status = job.status if job.status in {"done", "needs_ocr"} else "paused"
                self._replace(
                    entry,
                    status=status,
                    reason="interrupted_resume_available",
                    lease_token=None,
                    lease_expires_at=None,
                )

    async def _settings(self, job, context):
        return await self.owner.settings.query(
            StewardSettingsRequest(expected_context=context, sample_item_id=job.item_id)
        )

    async def record_jobs(self, jobs, context):
        entries = []
        for job in jobs:
            try:
                entry = self.owner._load("queue", job.job_id, StewardQueueEntry)
            except StewardError:
                view = await self._settings(job, context)
                entry = StewardQueueEntry(
                    job_id=job.job_id,
                    settings_fingerprint=view.fingerprint,
                    budget=view.settings.budget,
                    status=job.status if job.status in {"done", "needs_ocr"} else "held",
                    reason="provider_qualification_required",
                    created_at=self.owner.clock(),
                    updated_at=self.owner.clock(),
                )
                with self.owner.connection:
                    self.owner._save("queue", job.job_id, entry, 256)
            entries.append(entry)
        return entries

    async def control(self, request):
        await self.owner._check(request.expected_context)
        entry = self.owner._load("queue", request.job_id, StewardQueueEntry)
        if entry.revision != request.expected_revision:
            raise StewardError("steward_queue_changed")
        if entry.status in {"done", "needs_ocr"} or (
            entry.status == "cancelled" and request.decision != "resume_preview"
        ):
            raise StewardError("steward_queue_terminal")
        changes = {"lease_token": None, "lease_expires_at": None}
        if request.decision == "resume_preview":
            if entry.status == "claimed":
                raise StewardError("steward_queue_busy")
            job = self.owner._load("jobs", entry.job_id, StewardJob)
            view = await self._settings(job, request.expected_context)
            if view.fingerprint != request.expected_settings_fingerprint:
                raise StewardError("steward_settings_changed")
            changes.update(
                status="held",
                reason="provider_qualification_required",
                settings_fingerprint=view.fingerprint,
                budget=view.settings.budget,
            )
        else:
            changes.update(
                status="cancelled" if request.decision == "cancel" else "paused",
                reason="operator_cancelled" if request.decision == "cancel" else "operator_paused",
            )
        with self.owner.connection:
            self.owner.connection.execute("BEGIN IMMEDIATE")
            job = self.owner._load("jobs", entry.job_id, StewardJob)
            if job.status in {"done", "needs_ocr"}:
                raise StewardError("steward_queue_terminal")
            result = self._replace(entry, **changes)
            if request.decision in {"pause", "cancel"}:
                self.owner._save_job(job.model_copy(update={"status": "paused", "reason": result.reason}))
        task = self.active_tasks.get(entry.job_id)
        if task:
            task.cancel()
        return result

    async def activate_fixture(self, job_id, expected_context):
        """Synthetic harness only. No API, setting or provider JSON can call this."""
        adapter = self.owner.fixture_provider
        if not self.owner.catalog.simulated or not adapter or adapter.spec.environment != "fixture":
            raise StewardError("steward_executor_disabled")
        entry = self.owner._load("queue", job_id, StewardQueueEntry)
        job = self.owner._load("jobs", job_id, StewardJob)
        if job.provider != adapter.spec.key or job.provider_version != adapter.spec.version:
            raise StewardError("steward_executor_disabled")
        if entry.status not in {"held", "paused", "failed", "offline", "stale"}:
            raise StewardError("steward_queue_resume_recheck_required")
        await self.owner._items(expected_context, [job.item_id], ready=True)
        view = await self._settings(job, expected_context)
        if view.fingerprint != entry.settings_fingerprint:
            raise StewardError("steward_settings_changed")
        return self._replace(entry, status="waiting", reason="fixture_window_ready")

    def _claim_current(self, job_id, token, revision):
        entry = self.owner._load("queue", job_id, StewardQueueEntry)
        return (
            entry.status == "claimed"
            and entry.lease_token == token
            and entry.revision == revision
            and entry.lease_expires_at > self.owner.clock()
        )

    def _claim(self, entry):
        """The one-worker gate and ticket CAS share one SQLite transaction across owners."""
        with self.owner.connection:
            self.owner.connection.execute("BEGIN IMMEDIATE")
            if any(self.active_lease(candidate.job_id) for candidate in self.records()):
                return None
            value = StewardQueueEntry.model_validate(
                entry.model_copy(
                    update={
                        "status": "claimed",
                        "reason": "fixture_step_running",
                        "lease_token": secrets.token_hex(16),
                        "lease_expires_at": self.owner.clock() + timedelta(seconds=entry.budget.seconds + 1),
                        "revision": entry.revision + 1,
                        "updated_at": self.owner.clock(),
                    }
                ).model_dump()
            )
            count = self.owner.connection.execute(
                "UPDATE steward_queue SET body=? WHERE id=? AND body=?",
                (value.model_dump_json(), entry.job_id, entry.model_dump_json()),
            ).rowcount
            return value if count == 1 else None

    async def step_fixture(self, expected_context):
        """At most one bounded step. No loop, automatic retry or real provider dispatch."""
        adapter = self.owner.fixture_provider
        if not self.owner.catalog.simulated or not adapter or adapter.spec.environment != "fixture":
            raise StewardError("steward_executor_disabled")
        await self.owner._check(expected_context)
        self.recover()
        if any(self.active_lease(entry.job_id) for entry in self.records()):
            return None
        pending = sorted(
            (entry for entry in self.records() if entry.status == "waiting"),
            key=lambda entry: (entry.created_at, entry.job_id),
        )
        if not pending:
            return None
        entry = pending[0]
        job = self.owner._load("jobs", entry.job_id, StewardJob)
        context = await self.owner.context()
        if context["session"]["reason"] == "authentication_required":
            return self._replace(entry, status="held", reason="authentication_required")
        if context["session"].get("handoff_service"):
            # ARL connection evidence explicitly does not supply the steward content grant.
            return self._replace(entry, status="held", reason="content_grant_required")
        if not context["catalog"].source_online:
            return self._replace(entry, status="offline", reason="source_offline")
        view = await self._settings(job, expected_context)
        if view.fingerprint != entry.settings_fingerprint:
            return self._replace(entry, status="held", reason="settings_changed")
        claimed = self._claim(entry)
        if claimed is None:
            return None
        token = claimed.lease_token
        task = asyncio.create_task(
            self.owner.run_fixture_step(
                entry.job_id,
                expected_context,
                entry.budget,
                commit_guard=lambda: self._claim_current(entry.job_id, token, claimed.revision),
                approval_view=view,
            )
        )
        self.active_tasks[entry.job_id] = task
        status, reason = "paused", "interrupted_resume_available"
        try:
            result = await task
            status = result.status if result.status in {"done", "needs_ocr"} else "paused"
            reason = result.reason
        except StewardError as error:
            if error.code in {"steward_settings_changed", "steward_settings_policy_rebound"}:
                status, reason = "held", "settings_changed"
            elif error.code == "steward_policy_changed":
                status, reason = "held", "policy_changed"
            elif error.code in {"authentication_required", "content_grant_required"}:
                status, reason = "held", error.code
            else:
                status, reason = "failed", error.code
        finally:
            self.active_tasks.pop(entry.job_id, None)
            current = self.owner._load("queue", entry.job_id, StewardQueueEntry)
            if current.lease_token == claimed.lease_token and current.revision == claimed.revision:
                self._replace(current, status=status, reason=reason, lease_token=None, lease_expires_at=None)
        return self.owner._load("queue", entry.job_id, StewardQueueEntry)
