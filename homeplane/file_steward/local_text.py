"""Selected UTF-8 content → existing bounded reader → local derived index.

No paths, transport, native parser, model, cloud, directory scan or original-file
mutation. A POST authorizes one known original for at most five seconds; saved
settings and ARL connectivity alone never initiate a read.
"""

import asyncio
import hashlib
import time

from homeplane.contracts import Contract
from homeplane.family_media.catalog import FamilyMediaError, digest
from homeplane.family_media.contracts import Digest, FamilyMediaResolveRequest
from homeplane.family_media.reader import RANGE_BYTES

from .contracts import StewardJobRequest, StewardSourceAnchor, StewardUnit
from .providers import ExtractionStep, ProviderSpec
from .service import StewardError, fingerprint
from .settings_contracts import StewardSettingsRequest

MAX_TEXT_BYTES = 131072
LOCAL_TEXT = ProviderSpec(
    "local_utf8_text", "utf8_selected_v1", "本机文字", "local", "selected_utf8_128k_no_models"
)


class SelectedTextPreparation(Contract):
    expected_context: Digest
    item_id: Digest
    expected_content_id: Digest


class SelectedTextRequest(SelectedTextPreparation):
    expected_settings_fingerprint: Digest


class SelectedTextSource:
    """Request-owned capability, never accepted from JSON or persisted as authority."""

    def __init__(self, owner, request, item, approval):
        self.owner, self.request, self.item, self.approval = owner, request, item, approval
        self.deadline = time.monotonic() + 5
        self.active = True
        self.plan_id = None
        self.policy_epoch = owner._policy_epoch()

    def current_sync(self):
        return bool(
            self.active and time.monotonic() < self.deadline
            and self.owner._policy_epoch() == self.policy_epoch
            and self.owner.settings.approval_current(self.approval, self.item.item_id)
        )

    async def current(self, job):
        if not self.current_sync() or (
            job.item_id, job.content_id, job.source_fingerprint, job.original_ref
        ) != (
            self.item.item_id, self.item.original_hash, fingerprint(self.item), self.item.original_ref
        ):
            return False
        current = (await self.owner._items(self.request.expected_context, [job.item_id], ready=True))[0]
        return self.current_sync() and fingerprint(current) == job.source_fingerprint

    async def read_complete(self, job, budget):
        if not await self.current(job):
            raise StewardError("content_grant_required")
        try:
            plan = await self.owner.catalog.prepare_original(FamilyMediaResolveRequest(
                expected_context=self.request.expected_context, item_id=job.item_id,
            ))
            self.plan_id = plan.plan_id
            if (
                plan.context_fingerprint != self.request.expected_context
                or plan.original_hash != job.content_id or plan.item_id != job.item_id
                or not 0 < plan.size_bytes <= min(MAX_TEXT_BYTES, budget.source_bytes)
            ):
                raise StewardError("steward_text_source_limit")
            data = bytearray()
            while len(data) < plan.size_bytes:
                if not await self.current(job):
                    raise StewardError("content_grant_required")
                chunk = await self.owner.catalog.read_original(
                    plan.plan_id, len(data), min(RANGE_BYTES, plan.size_bytes - len(data))
                )
                data.extend(chunk)
                if not await self.current(job):
                    raise StewardError("content_grant_required")
            return bytes(data)
        except FamilyMediaError as error:
            raise StewardError(error.code) from None
        finally:
            if self.plan_id is not None:
                self.owner.catalog.revoke_original(self.plan_id)
                self.plan_id = None

    def close(self):
        self.active = False
        if self.plan_id is not None:
            self.owner.catalog.revoke_original(self.plan_id)
            self.plan_id = None


class LocalTextProvider:
    spec = LOCAL_TEXT

    def __init__(self, source):
        self.source = source

    async def step(self, job, budget):
        if (job.provider, job.provider_version) != (self.spec.key, self.spec.version):
            raise StewardError("steward_text_provider_changed")
        if not await self.source.current(job):
            raise StewardError("content_grant_required")
        data = await self.source.read_complete(job, budget)
        if not isinstance(data, bytes) or not 0 < len(data) <= min(MAX_TEXT_BYTES, budget.source_bytes):
            raise StewardError("steward_text_source_limit")
        if hashlib.sha256(data).hexdigest() != job.content_id:
            raise StewardError("steward_stale_fingerprint")
        if data.startswith((b"%PDF", b"%!PS", b"PK\x03\x04", b"\x89PNG", b"GIF8")):
            raise StewardError("steward_text_format_unsupported")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise StewardError("steward_text_format_unsupported") from None
        if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
            raise StewardError("steward_text_format_unsupported")
        if not text.strip():
            raise StewardError("steward_text_empty")
        if len(text) > min(budget.text_chars, budget.units * 4000):
            # Never report a silently truncated file as fully indexed.
            raise StewardError("steward_text_output_limit")
        if job.cursor is not None or job.completed_units:
            raise StewardError("steward_text_progress_changed")
        if not await self.source.current(job):
            raise StewardError("content_grant_required")
        title = next(line.strip() for line in text.splitlines() if line.strip())[:120]
        anchor = StewardSourceAnchor(content_id=job.content_id, original_ref=job.original_ref, kind="file")
        units = [StewardUnit(
            unit_id=digest([job.job_id, ordinal]), ordinal=ordinal, title=title,
            text=text[offset:offset + 4000], anchor=anchor,
            provider=job.provider, provider_version=job.provider_version,
        ) for ordinal, offset in enumerate(range(0, len(text), 4000))]
        return ExtractionStep(units, None, True)


class LocalTextExtraction:
    spec = LOCAL_TEXT

    def __init__(self, owner):
        self.owner = owner

    async def prepare(self, request):
        """Recheck only the selected original's metadata; no content bytes or job."""
        owner = self.owner
        await owner._check(request.expected_context)
        try:
            resolution = await owner.catalog.resolve(FamilyMediaResolveRequest(
                expected_context=request.expected_context, item_id=request.item_id,
            ))
        except FamilyMediaError as error:
            raise StewardError(error.code) from None
        item = resolution.item
        if (not resolution.source_checked or item.status != "ready"
                or item.original_hash != request.expected_content_id or not item.original_ref):
            raise StewardError("steward_source_recheck_required")
        approval = await owner.settings.query(StewardSettingsRequest(
            expected_context=resolution.context_fingerprint, sample_item_id=item.item_id,
        ))
        return SelectedTextRequest(
            expected_context=resolution.context_fingerprint, item_id=item.item_id,
            expected_content_id=request.expected_content_id,
            expected_settings_fingerprint=approval.fingerprint,
        )

    async def start(self, request):
        owner = self.owner
        if owner.lock.locked():
            raise StewardError("steward_busy")
        item = (await owner._items(request.expected_context, [request.item_id], ready=True))[0]
        if item.original_hash != request.expected_content_id:
            raise StewardError("steward_stale_fingerprint")
        if item.media_kind != "other":
            raise StewardError("steward_text_format_unsupported")
        approval = await owner.settings.query(StewardSettingsRequest(
            expected_context=request.expected_context, sample_item_id=item.item_id,
        ))
        if approval.fingerprint != request.expected_settings_fingerprint:
            raise StewardError("steward_settings_changed")
        budget = approval.settings.budget
        if not 0 < item.size_bytes <= min(MAX_TEXT_BYTES, budget.source_bytes):
            raise StewardError("steward_text_source_limit")
        owner.queue.recover()
        job = (await owner.plan_jobs(StewardJobRequest(
            expected_context=request.expected_context, item_ids=[item.item_id], provider=self.spec.key,
        )))[0]
        if job.status == "done":
            return job
        queue = owner.queue
        entry = next(entry for entry in queue.records() if entry.job_id == job.job_id)
        if entry.status == "claimed":
            raise StewardError("steward_queue_busy")
        # A cancelled/failed job needs a new explicit request and current approval.
        entry = queue._replace(
            entry, status="held", reason="selected_content_approved",
            settings_fingerprint=approval.fingerprint, budget=budget,
            lease_token=None, lease_expires_at=None,
        )
        claimed = queue._claim(entry, reason="local_text_running")
        if claimed is None:
            raise StewardError("steward_queue_busy")
        source = SelectedTextSource(owner, request, item, approval)
        def current():
            return queue._claim_current(job.job_id, claimed.lease_token, claimed.revision)

        task = asyncio.create_task(owner._run_provider_step(
            LocalTextProvider(source), job.job_id, request.expected_context, budget,
            approval_view=approval, commit_guard=current, content_guard=source.current_sync,
        ))
        queue.active_tasks[job.job_id] = task
        status, reason = "paused", "interrupted_resume_available"
        try:
            result = await task
            status, reason = result.status, result.reason
            return result
        except StewardError as error:
            status, reason = "failed", error.code
            raise
        finally:
            source.close()
            if queue.active_tasks.get(job.job_id) is task:
                queue.active_tasks.pop(job.job_id, None)
            saved = next(entry for entry in queue.records() if entry.job_id == job.job_id)
            if (saved.lease_token, saved.revision) == (claimed.lease_token, claimed.revision):
                queue._replace(saved, status=status, reason=reason, lease_token=None, lease_expires_at=None)
