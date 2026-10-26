"""Synthetic bytes and declared PDF receipts only. No PDFKit/OCR/NAS execution."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from homeplane.family_media import FamilyMediaCatalog
from homeplane.family_media.contracts import FamilyMediaQuery, FamilyMediaScanRequest
from homeplane.family_media.fixture import SyntheticNASReader
from homeplane.file_steward import FileSteward, StewardError
from homeplane.file_steward.contracts import (
    StewardBudget,
    StewardJobRequest,
    StewardPageOutcome,
    StewardPageRequest,
)
from homeplane.file_steward.extraction_queue import StewardQueueControl, StewardQueueEntry
from homeplane.file_steward.pdf_text import PDFTextProvider, StewardPDFPageReceipt
from homeplane.file_steward.providers import ProviderSpec
from homeplane.file_steward.settings_contracts import StewardSettingsEdit, StewardSettingsRequest

SYNTHETIC_BYTES = b"SYNTHETIC receipt source, not an actual PDF or OCR result"


class FixtureSource:
    def __init__(self):
        self.calls = []
        self.allowed = True
        self.data = SYNTHETIC_BYTES

    async def current(self, job):
        self.calls.append("check")
        return self.allowed

    async def read_complete(self, job, budget):
        self.calls.append("fixture_bytes")
        return self.data


class ReceiptFixture:
    def __init__(self):
        self.calls = []
        self.pages = ["Fixture embedded text on page one.", ""]
        self.wait = None

    async def page(self, document, page, max_chars):
        self.calls.append(page)
        if self.wait:
            await self.wait.wait()
        text = self.pages[page - 1]
        return StewardPDFPageReceipt(
            page=page,
            page_count=len(self.pages),
            status="text" if text else "needs_ocr",
            text=text,
            engine="synthetic_fixture",
            engine_version="fixture_v1",
            execution="fixture",
        )


class FixturePDFProvider(PDFTextProvider):
    spec = ProviderSpec("fixture_pdf_receipts", "fixture_v1", "合成页回执", "fixture", "fixture_only")

    def __init__(self):
        # This boolean qualifies ONLY a declared test double, never native extraction.
        super().__init__(source=FixtureSource(), bridge=ReceiptFixture(), native_qualified=True)


class SliceEnv:
    def __init__(self, tmp_path):
        self.path = tmp_path / "synthetic.sqlite"
        self.now = [datetime(2026, 10, 2, 13, tzinfo=UTC)]
        self.reader = SyntheticNASReader()
        self.reader.put("fixture-document", SYNTHETIC_BYTES, kind="other")
        self.adapter = FixturePDFProvider()
        self.open()

    def open(self):
        self.connection = sqlite3.connect(self.path)
        self.catalog = FamilyMediaCatalog(
            self.connection,
            self.reader,
            simulated=True,
            clock=lambda: self.now[0],
            indexed_kinds=("image", "video", "audio", "other"),
        )
        self.steward = FileSteward(
            self.connection, self.catalog, clock=lambda: self.now[0], fixture_provider=self.adapter
        )

    async def context(self):
        return (await self.catalog.context()).fingerprint

    async def plan(self, provider=None):
        self.job = (
            await self.steward.plan_jobs(
                StewardJobRequest(
                    expected_context=await self.context(),
                    item_ids=[self.item.item_id],
                    provider=provider or self.adapter.spec.key,
                )
            )
        )[0]
        return self.job

    async def tick(self):
        await self.steward.queue.activate_fixture(self.job.job_id, await self.context())
        return await self.steward.queue.step_fixture(await self.context())

    async def reopen(self):
        await self.catalog.close()
        self.connection.close()
        self.open()


@pytest.fixture
async def slice_env(tmp_path):
    env = SliceEnv(tmp_path)
    path = tmp_path / "synthetic-receipt-source.fixture"
    path.write_bytes(SYNTHETIC_BYTES)
    for _ in range(2):
        await env.catalog.scan(
            FamilyMediaScanRequest(expected_context=await env.context(), directory_ref="fixture-downloads")
        )
        env.now[0] += timedelta(seconds=3)
    env.item = (await env.catalog.list_items(FamilyMediaQuery(expected_context=await env.context()))).items[0]
    env.reader.calls.clear()
    yield env
    await env.catalog.close()
    env.connection.close()


async def test_default_pdf_and_queue_do_not_read_or_start(slice_env):
    env = slice_env
    record = await env.plan("pdfkit_text")
    provider = PDFTextProvider(source=env.adapter.source, bridge=env.adapter.bridge)
    with pytest.raises(StewardError, match="qualification_required"):
        await provider.step(record, StewardBudget())
    assert not env.adapter.source.calls and not env.adapter.bridge.calls
    assert env.steward.queue.records()[0].status == "held"
    assert await env.steward.queue.step_fixture(await env.context()) is None
    with pytest.raises(StewardError, match="executor_disabled"):
        await env.steward.queue.activate_fixture(record.job_id, await env.context())
    assert not env.reader.calls


async def test_page_receipts_resume_across_closed_sqlite_without_fake_ocr(slice_env):
    env = slice_env
    record = await env.plan()
    assert (await env.tick()).status == "paused"
    assert env.steward.jobs()[0].cursor == "pdf-page-2"
    await env.reopen()
    assert (await env.tick()).status == "needs_ocr"
    saved = env.steward.jobs()[0]
    assert (saved.completed_pages, saved.source_pages, saved.completed_units) == (2, 2, 1)
    assert saved.charged_source_bytes == 2097152 and saved.attempts == 2
    assert env.adapter.bridge.calls == [1, 2]
    units = env.steward.units(record.job_id)
    assert len(units) == 1 and units[0].anchor.page == 1 and units[0].text == env.adapter.bridge.pages[0]
    outcomes = env.steward.page_outcomes(record.job_id)
    assert [p.status for p in outcomes] == ["text", "needs_ocr"] and outcomes[1].unit_id is None
    page = await env.steward.page(StewardPageRequest(expected_context=await env.context()))
    assert page["items"][0]["extraction_status"] == "needs_ocr"
    assert page["items"][0]["references"] == units
    assert not env.reader.calls


async def test_queue_claim_recovery_retains_pages_cursor_and_reserved_attempt(slice_env):
    env = slice_env
    await env.plan()
    await env.tick()
    ticket = env.steward.queue.records()[0]
    env.steward.queue._replace(
        ticket,
        status="claimed",
        reason="fixture_crash",
        lease_token="fixture-lease",
        lease_expires_at=env.now[0] + timedelta(seconds=6),
    )
    saved = env.steward.jobs()[0]
    saved.status = "running"
    saved.attempts += 1
    saved.charged_source_bytes += 1048576
    with env.connection:
        env.steward._save_job(saved)
    env.now[0] += timedelta(seconds=7)
    await env.reopen()
    assert env.steward.queue.records()[0].status == "paused"
    assert env.steward.jobs()[0].status == "paused"
    assert (await env.tick()).status == "needs_ocr"
    assert env.steward.jobs()[0].attempts == 3 and env.steward.jobs()[0].charged_source_bytes == 3145728
    assert env.adapter.bridge.calls == [1, 2]


async def test_second_owner_cannot_claim_while_first_has_live_lease(slice_env):
    env = slice_env
    await env.plan()
    env.adapter.bridge.wait = asyncio.Event()
    await env.steward.queue.activate_fixture(env.job.job_id, await env.context())
    task = asyncio.create_task(env.steward.queue.step_fixture(await env.context()))
    while not env.adapter.bridge.calls:
        await asyncio.sleep(0)
    connection = sqlite3.connect(env.path)
    other = FileSteward(connection, env.catalog, clock=lambda: env.now[0], fixture_provider=env.adapter)
    try:
        assert other.jobs()[0].status == "running"
        assert await other.queue.step_fixture(await env.context()) is None
        assert env.adapter.bridge.calls == [1]
    finally:
        env.adapter.bridge.wait.set()
        await task
        connection.close()


async def control(env, decision, owner=None):
    owner = owner or env.steward
    ticket = owner.queue.records()[0]
    return await owner.queue.control(
        StewardQueueControl(
            expected_context=await env.context(),
            job_id=ticket.job_id,
            expected_revision=ticket.revision,
            expected_settings_fingerprint=ticket.settings_fingerprint,
            decision=decision,
        )
    )


async def test_cross_owner_cancel_discards_late_page_and_keeps_charge(slice_env):
    env = slice_env
    await env.plan()
    env.adapter.bridge.wait = asyncio.Event()
    await env.steward.queue.activate_fixture(env.job.job_id, await env.context())
    task = asyncio.create_task(env.steward.queue.step_fixture(await env.context()))
    while not env.adapter.bridge.calls:
        await asyncio.sleep(0)
    other = FileSteward(env.connection, env.catalog, clock=lambda: env.now[0], fixture_provider=env.adapter)
    assert (await control(env, "cancel", other)).status == "cancelled"
    env.adapter.bridge.wait.set()
    assert (await task).status == "cancelled"
    assert not env.steward.units(env.job.job_id) and not env.steward.page_outcomes(env.job.job_id)
    assert env.steward.jobs()[0].charged_source_bytes == 1048576
    assert env.connection.execute("SELECT COUNT(*) FROM family_media_items").fetchone()[0] == 1


async def test_pause_then_resume_preview_never_activates_worker(slice_env):
    env = slice_env
    await env.plan()
    await env.tick()
    assert (await control(env, "pause")).status == "paused"
    assert (await control(env, "resume_preview")).status == "held"
    assert await env.steward.queue.step_fixture(await env.context()) is None
    assert env.steward.jobs()[0].completed_pages == 1 and env.adapter.bridge.calls == [1]
    assert (await env.tick()).status == "needs_ocr"


async def test_cancelled_plan_can_be_replanned_without_refund_or_automatic_start(slice_env):
    env = slice_env
    await env.plan()
    await env.tick()
    assert (await control(env, "cancel")).status == "cancelled"
    assert (await control(env, "resume_preview")).status == "held"
    assert await env.steward.queue.step_fixture(await env.context()) is None
    assert (
        env.steward.jobs()[0].completed_pages == 1 and env.steward.jobs()[0].charged_source_bytes == 1048576
    )


async def test_atomic_gate_blocks_second_job_and_hides_claim_token_from_public_metadata(slice_env):
    env = slice_env
    await env.plan()
    env.reader.put("fixture-second", SYNTHETIC_BYTES + b" second", kind="other")
    for _ in range(2):
        await env.catalog.scan(
            FamilyMediaScanRequest(expected_context=await env.context(), directory_ref="fixture-downloads")
        )
        env.now[0] += timedelta(seconds=3)
    items = (await env.catalog.list_items(FamilyMediaQuery(expected_context=await env.context()))).items
    second = next(item for item in items if item.original_ref.object_ref == "fixture-second")
    await env.steward.plan_jobs(
        StewardJobRequest(
            expected_context=await env.context(), item_ids=[second.item_id], provider=env.adapter.spec.key
        )
    )
    for entry in env.steward.queue.records():
        await env.steward.queue.activate_fixture(entry.job_id, await env.context())
    entries = env.steward.queue.records()
    first_claim = env.steward.queue._claim(entries[0])
    assert first_claim is not None
    connection = sqlite3.connect(env.path)
    other = FileSteward(connection, env.catalog, clock=lambda: env.now[0], fixture_provider=env.adapter)
    try:
        assert other.queue._claim(entries[1]) is None
        public = (await other.context())["queue"]
        assert all("lease_token" not in entry and "lease_expires_at" not in entry for entry in public)
        assert not env.adapter.source.calls and not env.adapter.bridge.calls
    finally:
        connection.close()


async def test_auth_and_offline_hold_without_content_retry_or_delete(slice_env):
    env = slice_env
    await env.plan()
    await env.steward.queue.activate_fixture(env.job.job_id, await env.context())
    env.steward.session_status = lambda: {"state": "paused", "failure": "nas_auth_failed"}
    assert (await env.steward.queue.step_fixture(await env.context())).reason == "authentication_required"
    assert not env.adapter.source.calls
    env.steward.session_status = None
    await env.steward.queue.activate_fixture(env.job.job_id, await env.context())
    env.reader.state = "backoff"
    assert (await env.steward.queue.step_fixture(await env.context())).status == "offline"
    assert not env.adapter.source.calls and not env.reader.calls
    assert env.connection.execute("SELECT COUNT(*) FROM family_media_items").fetchone()[0] == 1


async def test_settings_change_holds_old_budget_before_content_read(slice_env):
    env = slice_env
    await env.plan()
    await env.steward.queue.activate_fixture(env.job.job_id, await env.context())
    request = StewardSettingsRequest(expected_context=await env.context(), sample_item_id=env.item.item_id)
    view = await env.steward.settings.query(request)
    await env.steward.settings.save(
        StewardSettingsEdit(
            **request.model_dump(),
            expected_fingerprint=view.fingerprint,
            watched=True,
            extraction_mode="pdf_text_first",
            budget=StewardBudget(source_bytes=2097152, units=1),
            rule=view.rule,
        )
    )
    assert (await env.steward.queue.step_fixture(await env.context())).reason == "settings_changed"
    assert not env.adapter.source.calls


async def test_source_change_and_revocation_reject_all_late_results(slice_env):
    env = slice_env
    await env.plan()
    env.adapter.bridge.wait = asyncio.Event()
    await env.steward.queue.activate_fixture(env.job.job_id, await env.context())
    task = asyncio.create_task(env.steward.queue.step_fixture(await env.context()))
    while not env.adapter.bridge.calls:
        await asyncio.sleep(0)
    env.adapter.source.allowed = False
    env.adapter.bridge.wait.set()
    result = await task
    assert result.status == "failed" and result.reason == "steward_pdf_source_grant_denied"
    assert not env.steward.units(env.job.job_id) and not env.steward.page_outcomes(env.job.job_id)


@pytest.mark.parametrize("failure", ["wrong_hash", "oversized", "denied"])
async def test_pdf_input_gates_before_bridge(slice_env, failure):
    env = slice_env
    await env.plan()
    if failure == "wrong_hash":
        env.adapter.source.data = b"CHANGED fixture bytes"
    elif failure == "oversized":
        env.adapter.source.data = b"x" * 1048577
    else:
        env.adapter.source.allowed = False
    result = await env.tick()
    assert result.status == "failed" and not env.adapter.bridge.calls
    assert env.steward.jobs()[0].charged_source_bytes == 1048576
    assert not env.steward.page_outcomes(env.job.job_id)


async def test_page_count_change_does_not_commit_cursor_or_receipt(slice_env):
    env = slice_env
    await env.plan()
    await env.tick()
    original = env.adapter.bridge.page

    async def changed(document, page, max_chars):
        receipt = await original(document, page, max_chars)
        return receipt.model_copy(update={"page_count": 3})

    env.adapter.bridge.page = changed
    assert (await env.tick()).reason == "steward_invalid_page_progress"
    saved = env.steward.jobs()[0]
    assert saved.cursor == "pdf-page-2" and saved.completed_pages == 1 and saved.source_pages == 2
    assert len(env.steward.page_outcomes(env.job.job_id)) == 1


async def test_timeout_keeps_reservation_and_no_partial_page(slice_env):
    env = slice_env
    await env.plan()
    entry = env.steward.queue.records()[0]
    env.steward.queue._replace(entry, budget=StewardBudget(seconds=0.01))
    env.adapter.bridge.wait = asyncio.Event()
    result = await env.tick()
    assert result.status == "failed" and env.steward.jobs()[0].charged_source_bytes == 1048576
    assert not env.steward.page_outcomes(env.job.job_id)


def test_receipts_never_attach_fake_text_to_nontext_pages():
    with pytest.raises(ValidationError):
        StewardPDFPageReceipt(
            page=1,
            page_count=1,
            status="needs_ocr",
            text="invented OCR",
            engine="synthetic_fixture",
            engine_version="fixture_v1",
            execution="fixture",
        )
    with pytest.raises(ValidationError):
        StewardPDFPageReceipt(
            page=1,
            page_count=1,
            status="text",
            text="",
            engine="synthetic_fixture",
            engine_version="fixture_v1",
            execution="fixture",
        )
    assert (
        StewardPDFPageReceipt(
            page=1,
            status="locked",
            engine="synthetic_fixture",
            engine_version="fixture_v1",
            execution="fixture",
        ).page_count
        is None
    )
    with pytest.raises(ValidationError):
        StewardQueueEntry(
            job_id="a" * 64,
            settings_fingerprint="b" * 64,
            budget=StewardBudget(),
            status="claimed",
            reason="bad lease",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )


@pytest.mark.parametrize("status", ["text", "failed"])
async def test_frozen_v2_ocr_or_failed_receipt_cannot_activate_indexing(slice_env, status):
    env = slice_env
    await env.plan()
    original = env.adapter.step

    async def unqualified(job, budget):
        from homeplane.file_steward.providers import ExtractionStep

        result = await original(job, budget)
        old = result.page_outcomes[0]
        outcome = StewardPageOutcome.model_validate(
            {
                **old.model_dump(),
                "outcome_version": 2,
                "status": status,
                "unit_id": old.unit_id if status == "text" else None,
                "reason": "ocr_text" if status == "text" else "provider_failure",
                "provenance": {
                    "engine": "synthetic_fixture",
                    "engine_version": "fixture_v2",
                    "execution": "fixture",
                    "method": "ocr",
                },
                "quality": {
                    "assessment": "review_required",
                    "character_count": len(result.units[0].text) if status == "text" else 0,
                },
                "failure_code": "provider_failed" if status == "failed" else None,
            }
        )
        return ExtractionStep(result.units if status == "text" else [], None, True, [outcome], 1)

    env.adapter.step = unqualified
    assert (await env.tick()).reason == "steward_page_outcome_qualification_required"
    saved = env.steward.jobs()[0]
    assert saved.completed_units == saved.completed_pages == 0 and saved.cursor is None
    assert saved.charged_source_bytes == 1048576 and not env.steward.units(saved.job_id)


@pytest.mark.parametrize("mixed_public_evidence", [False, True])
async def test_page_only_private_observation_cannot_supply_allowed_alias_status(
    slice_env, mixed_public_evidence
):
    from homeplane.family_media.catalog import digest
    from homeplane.file_steward.agent_loop import AgentLoopFiles, StewardAgentReadScope
    from homeplane.file_steward.contracts import StewardSourceAnchor, StewardUnit

    env = slice_env
    env.reader.put("fixture-private-copy", SYNTHETIC_BYTES, kind="other")
    env.reader.listing = ["fixture-private-copy"]
    for _ in range(2):
        await env.catalog.scan(
            FamilyMediaScanRequest(expected_context=await env.context(), directory_ref="fixture-private")
        )
        env.now[0] += timedelta(seconds=3)
    items = (await env.catalog.list_items(FamilyMediaQuery(expected_context=await env.context()))).items
    private = next(item for item in items if item.original_ref.object_ref == "fixture-private-copy")
    record = (
        await env.steward.plan_jobs(
            StewardJobRequest(
                expected_context=await env.context(),
                item_ids=[private.item_id],
                provider=env.adapter.spec.key,
            )
        )
    )[0]
    env.adapter.bridge.pages = [""]
    await env.steward.run_fixture_step(record.job_id, await env.context())
    record = env.steward.jobs()[0].model_copy(
        update={"item_id": env.item.item_id, "original_ref": env.item.original_ref}
    )
    with env.connection:
        if mixed_public_evidence:
            anchor = StewardSourceAnchor(
                content_id=record.content_id, original_ref=env.item.original_ref, kind="page", page=2
            )
            unit = StewardUnit(
                unit_id=digest([record.job_id, 0]),
                ordinal=0,
                text="PUBLIC fixture evidence",
                anchor=anchor,
                provider=record.provider,
                provider_version=record.provider_version,
            )
            outcome = StewardPageOutcome(
                anchor=anchor,
                status="text",
                provider=record.provider,
                provider_version=record.provider_version,
                unit_id=unit.unit_id,
                reason="embedded_text",
            )
            env.connection.execute(
                "INSERT INTO steward_units VALUES (?,?,?)",
                (unit.unit_id, record.job_id, unit.model_dump_json()),
            )
            env.connection.execute(
                "INSERT INTO steward_pages VALUES (?,?,?)",
                (digest([record.job_id, "page", 2]), record.job_id, outcome.model_dump_json()),
            )
            record.completed_units, record.completed_pages, record.source_pages = 1, 2, 2
        env.steward._save_job(record)
    ctx = await env.catalog.context()
    scope = StewardAgentReadScope(
        grant_id="fixture-grant",
        subject_ref="fixture-peer",
        source_ref=ctx.source_ref,
        config_generation=ctx.config_generation,
        connection_epoch=ctx.connection_epoch,
        directory_refs=["fixture-downloads"],
        approved_at=env.now[0],
        expires_at=env.now[0] + timedelta(minutes=5),
    )
    env.reader.calls.clear()
    result = await AgentLoopFiles(
        env.steward, scope_authorizer=lambda _: True, clock=lambda: env.now[0]
    ).query(scope, StewardPageRequest(expected_context=ctx.fingerprint))
    assert len(result["items"]) == 1
    public = result["items"][0]
    if mixed_public_evidence:
        assert public["extraction_status"] == "paused" and public["provider_states"][0]["status"] == "paused"
        assert [page.anchor.page for page in public["page_outcomes"]] == [2]
        assert public["reference_count"] == 1
    else:
        assert public["extraction_status"] == "needs_extraction" and not public["provider_states"]
        assert not public["page_outcomes"] and not public["references"]
    assert not env.reader.calls
