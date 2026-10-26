"""Finite orchestration tests: synthetic tempfile bytes, no actual NAS/model/provider IO."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from homeplane.family_media import FamilyMediaCatalog
from homeplane.family_media.catalog import digest
from homeplane.family_media.contracts import FamilyMediaQuery, FamilyMediaScanRequest
from homeplane.family_media.fixture import SyntheticNASReader
from homeplane.file_steward import FileSteward, StewardError
from homeplane.file_steward.api import create_router
from homeplane.file_steward.contracts import (
    StewardBatchApproval,
    StewardDestinationInventory,
    StewardJobRequest,
    StewardPageRequest,
    StewardPreviewRequest,
    StewardRelocationSelection,
    StewardSourceAnchor,
    StewardSuggestionRequest,
    StewardUnit,
    relative_path,
)
from homeplane.file_steward.policy import StewardPolicyEdit, StewardPolicyRule, resolve_policy
from homeplane.file_steward.providers import ExtractionStep, ProviderSpec


class FixtureProvider:
    spec = ProviderSpec("fixture_text", "1", "合成文档", "fixture", "fixture_only")

    async def step(self, job, budget):
        ordinal = job.completed_units
        unit = StewardUnit(
            unit_id=digest([job.job_id, ordinal]),
            ordinal=ordinal,
            title="科学：宇宙尺度",
            text="一份合成的科学课程内容，供有限状态测试。",
            anchor=StewardSourceAnchor(
                content_id=job.content_id, original_ref=job.original_ref, kind="page", page=ordinal + 1
            ),
            provider=job.provider,
            provider_version=job.provider_version,
        )
        return ExtractionStep([unit], "fixture-page-2" if ordinal == 0 else None, ordinal > 0)


@pytest.fixture
async def env(tmp_path):
    reader = SyntheticNASReader()
    for name, content, kind in (
        ("fixture-doc", b"SYNTHETIC document A", "other"),
        ("fixture-copy", b"SYNTHETIC document A", "other"),
        ("fixture-camera", b"SYNTHETIC camera image", "image"),
    ):
        path = tmp_path / (name + ".txt")
        path.write_bytes(content)
        reader.put(name, path.read_bytes(), kind=kind)
    now = [datetime(2026, 10, 2, tzinfo=UTC)]
    connection = sqlite3.connect(tmp_path / "fixture.sqlite")
    catalog = FamilyMediaCatalog(
        connection,
        reader,
        simulated=True,
        clock=lambda: now[0],
        indexed_kinds=("image", "video", "audio", "other"),
    )
    for _ in range(2):
        await catalog.scan(
            FamilyMediaScanRequest(
                expected_context=(await catalog.context()).fingerprint, directory_ref="fixture-downloads"
            )
        )
        now[0] += timedelta(seconds=3)
    items = (
        await catalog.list_items(FamilyMediaQuery(expected_context=(await catalog.context()).fingerprint))
    ).items
    item = next(i for i in items if i.original_ref.object_ref == "fixture-doc")
    paths = {i.item_id: "downloads/" + i.original_ref.object_ref + ".pdf" for i in items}
    steward = FileSteward(
        connection, catalog, clock=lambda: now[0], fixture_provider=FixtureProvider(), fixture_paths=paths
    )
    yield steward, catalog, reader, now, connection, item
    await catalog.close()
    connection.close()


async def context(env):
    return (await env[1].context()).fingerprint


async def job(env, provider="fixture_text"):
    return (
        await env[0].plan_jobs(
            StewardJobRequest(
                expected_context=await context(env), item_ids=[env[5].item_id], provider=provider
            )
        )
    )[0]


async def pin(env, **fields):
    ctx = await env[1].context()
    rule = StewardPolicyRule(
        rule_id="downloads",
        source_ref=ctx.source_ref,
        config_generation=ctx.config_generation,
        connection_epoch=ctx.connection_epoch,
        directory_ref="fixture-downloads",
        label="下载收藏目录",
        status="user_pinned",
        origin="downloaded",
        rename_preview=True,
        move_preview=True,
        structure_preview=True,
        dedup_review=True,
    )
    rule = rule.model_copy(update=fields)
    await env[0].set_policy(StewardPolicyEdit(expected_context=ctx.fingerprint, rule=rule))
    return rule


async def preview(
    env,
    destination="downloads/new.pdf",
    *,
    root_ref=None,
    operation="rename",
    trust_inventory=True,
    **inventory_fields,
):
    inventory = StewardDestinationInventory(
        root_ref=root_ref or env[5].original_ref.source_ref,
        namespace_revision="1",
        checked_at=env[3][0],
        complete=True,
        paths=inventory_fields.pop("paths", []),
        basis="synthetic_fixture",
        **inventory_fields,
    )
    if trust_inventory:
        # Server-owned synthetic setup, independent of the HTTP request.
        env[0].register_fixture_inventory(inventory)
    return await env[0].preview(
        StewardPreviewRequest(
            expected_context=await context(env),
            selections=[
                StewardRelocationSelection(
                    item_id=env[5].item_id,
                    expected_content_id=env[5].original_hash,
                    destination=destination,
                    operation=operation,
                )
            ],
            inventory=inventory,
        )
    )


async def approval(env, plan, **changes):
    fields = dict(
        expected_context=await context(env),
        plan_id=plan.plan_id,
        expected_plan_fingerprint=plan.plan_fingerprint,
        expected_inventory_fingerprint=plan.inventory_fingerprint,
        selected_entry_ids=[e.entry_id for e in plan.entries],
        decision="approve_preview",
    )
    fields.update(changes)
    return await env[0].approve(StewardBatchApproval(**fields))


async def test_incremental_offline_retains_catalog_and_never_scans(env):
    steward, catalog, reader, _, conn, _ = env
    reader.calls.clear()
    result = await steward.reconcile(StewardPageRequest(expected_context=await context(env)))
    assert result["changes"]["new"] == 3
    assert (await steward.reconcile(StewardPageRequest(expected_context=await context(env))))["changes"][
        "unchanged"
    ] == 3
    reader.state = "backoff"
    result = await steward.reconcile(StewardPageRequest(expected_context=await context(env)))
    assert result["changes"]["offline"] == 3 and result["records_removed"] == 0
    assert not reader.calls
    assert conn.execute("SELECT COUNT(*) FROM family_media_items").fetchone()[0] == 3


async def test_cached_session_authentication_status_does_not_retry_credentials(env):
    steward, _, reader, _, _, _ = env
    steward.session_status = lambda: {"state": "paused", "failure": "nas_auth_failed"}
    reader.calls.clear()
    projected = (await steward.context())["session"]
    assert projected["label"] == "核对 NAS 授权" and not projected["retry_credentials"]
    assert projected["reason"] == "authentication_required" and not reader.calls


async def test_expired_offline_preview_can_be_rejected_but_never_approved(env):
    await pin(env)
    plan = await preview(env)
    env[3][0] = plan.expires_at + timedelta(seconds=1)
    env[2].state = "backoff"
    with pytest.raises(StewardError, match="stale_plan"):
        await approval(env, plan)
    env[2].calls.clear()
    rejected = await approval(env, plan, decision="reject")
    assert rejected.status == "rejected" and not env[2].calls
    phases = [row[0] for row in env[4].execute("SELECT body FROM steward_journal")]
    assert any('"phase":"rejected"' in row for row in phases)
    with pytest.raises(StewardError, match="review_already_recorded"):
        await approval(env, plan, decision="reject")


async def test_review_cross_root_rename_cannot_hide_movement(env):
    await pin(env, move_preview=False, structure_preview=False)
    plan = await preview(env, root_ref="fixture-output")
    assert plan.counts["move"] == 1
    assert plan.status == "blocked" and "policy_preserve_location" in plan.entries[0].blockers
    assert plan.entries[0].operation == "rename_and_move"
    import json

    recorded = [json.loads(row[0]) for row in env[4].execute("SELECT body FROM steward_journal")]
    assert recorded[0]["operation"] == "rename_and_move"
    with pytest.raises(StewardError, match="plan_blocked"):
        await approval(env, plan)


async def test_review_target_namespace_requires_server_evidence_and_is_rechecked(env):
    await pin(env)
    unknown = await preview(env, trust_inventory=False)
    assert unknown.status == "blocked" and "destination_namespace_unverified" in unknown.entries[0].blockers
    trusted = await preview(env)
    inventory = env[0].fixture_inventories[trusted.target_root_ref]
    env[0].register_fixture_inventory(inventory.model_copy(update={"paths": ["downloads/new.pdf"]}))
    with pytest.raises(StewardError, match="destination_changed"):
        await approval(env, trusted)
    spoofed = await preview(env, trust_inventory=False)
    assert (
        spoofed.counts["collision"] == 1 and "destination_namespace_unverified" in spoofed.entries[0].blockers
    )
    valid_move = await preview(env, root_ref="fixture-output", operation="rename_and_move")
    assert valid_move.counts["move"] == 1 and valid_move.status == "awaiting_review"
    assert (await approval(env, valid_move)).status == "reviewed_preview"


async def test_review_saved_legacy_root_diff_is_recomputed_at_approval(env):
    await pin(env, move_preview=False)
    plan = await preview(env, root_ref="fixture-output")
    # Emulate a saved 742d90b preview with the formerly accepted cross-root rename.
    legacy_entry = plan.entries[0].model_copy(update={"operation": "rename", "blockers": []})
    legacy = plan.model_copy(
        update={
            "entries": [legacy_entry],
            "status": "awaiting_review",
            "counts": {**plan.counts, "move": 0, "blocked": 0},
        }
    )
    legacy.plan_fingerprint = digest(
        [
            legacy.plan_id,
            legacy.context_fingerprint,
            legacy.inventory_fingerprint,
            [legacy_entry.model_dump(mode="json")],
        ]
    )
    with env[4]:
        env[0]._save("plans", legacy.plan_id, legacy, 64)
    with pytest.raises(StewardError, match="plan_diff_changed"):
        await approval(env, legacy)
    assert (await approval(env, legacy, decision="reject")).status == "rejected"


async def test_review_cursor_revisit_after_recreation_keeps_committed_units(env):
    class Cycling(FixtureProvider):
        async def step(self, job, budget):
            result = await super().step(job, budget)
            return ExtractionStep(
                result.units, "cursor-A" if job.completed_units % 2 == 0 else "cursor-B", False
            )

    steward, catalog, _, now, connection, _ = env
    steward.fixture_provider = Cycling()
    record = await job(env)
    for _ in range(2):
        await steward.run_fixture_step(record.job_id, await context(env))
    replacement = FileSteward(connection, catalog, clock=lambda: now[0], fixture_provider=Cycling())
    with pytest.raises(StewardError, match="cursor_revisited"):
        await replacement.run_fixture_step(record.job_id, await context(env))
    saved = replacement.jobs()[0]
    assert saved.cursor == "cursor-B" and saved.completed_units == 2
    assert saved.attempts == 3 and saved.charged_source_bytes == 3 * 1048576
    assert len(replacement.units(record.job_id)) == 2


async def test_review_legacy_resume_without_cursor_history_is_held(env):
    record = await job(env)
    await env[0].run_fixture_step(record.job_id, await context(env))
    # An old sidecar has a committed cursor but no historical cursor table.
    with env[4]:
        env[4].execute("DROP TABLE steward_cursors")
    replacement = FileSteward(env[4], env[1], clock=lambda: env[3][0], fixture_provider=FixtureProvider())
    saved = replacement.jobs()[0]
    assert saved.status == "stale" and saved.reason == "cursor_history_unverified"
    with pytest.raises(StewardError, match="cursor_history_recheck_required"):
        await replacement.run_fixture_step(record.job_id, await context(env))
    assert saved.completed_units == 1 and saved.attempts == 1 and len(replacement.units(record.job_id)) == 1


async def test_review_completed_provider_wins_over_new_disabled_provider(env):
    record = await job(env)
    for _ in range(2):
        await env[0].run_fixture_step(record.job_id, await context(env))
    await job(env, "docling")
    page = await env[0].page(StewardPageRequest(expected_context=await context(env)))
    row = next(row for row in page["items"] if row["item"].item_id == env[5].item_id)
    assert row["extraction_status"] == "done" and row["reference_count"] == 2
    assert {state["provider"]: state["status"] for state in row["provider_states"]} == {
        "docling": "disabled",
        "fixture_text": "done",
    }


async def test_review_private_alias_does_not_supply_allowed_title_search_or_status(tmp_path):
    from homeplane.file_steward.agent_loop import AgentLoopFiles, StewardAgentReadScope

    reader = SyntheticNASReader()
    for name in ("allowed-copy", "private-doc"):
        reader.put(name, b"SYNTHETIC same content", kind="other")
    now = [datetime(2026, 10, 2, tzinfo=UTC)]
    connection = sqlite3.connect(tmp_path / "scoped-fixture.sqlite")
    catalog = FamilyMediaCatalog(
        connection, reader, simulated=True, clock=lambda: now[0], indexed_kinds=("other",)
    )
    try:
        for directory, name in (("allowed", "allowed-copy"), ("private", "private-doc")):
            reader.listing = [name]
            for _ in range(2):
                await catalog.scan(
                    FamilyMediaScanRequest(
                        expected_context=(await catalog.context()).fingerprint, directory_ref=directory
                    )
                )
                now[0] += timedelta(seconds=3)
        steward = FileSteward(connection, catalog, clock=lambda: now[0], fixture_provider=FixtureProvider())
        ctx = await catalog.context()
        items = (await catalog.list_items(FamilyMediaQuery(expected_context=ctx.fingerprint))).items
        private = next(item for item in items if item.original_ref.object_ref == "private-doc")
        record = (
            await steward.plan_jobs(
                StewardJobRequest(
                    expected_context=ctx.fingerprint, item_ids=[private.item_id], provider="fixture_text"
                )
            )
        )[0]
        await steward.run_fixture_step(record.job_id, ctx.fingerprint)
        scope = StewardAgentReadScope(
            grant_id="fixture-grant",
            subject_ref="fixture-peer",
            source_ref=ctx.source_ref,
            config_generation=ctx.config_generation,
            connection_epoch=ctx.connection_epoch,
            directory_refs=["allowed"],
            approved_at=now[0],
            expires_at=now[0] + timedelta(minutes=5),
        )
        loop = AgentLoopFiles(steward, scope_authorizer=lambda _: True, clock=lambda: now[0])
        reader.calls.clear()
        matched = await loop.query(scope, StewardPageRequest(expected_context=ctx.fingerprint, query="宇宙"))
        assert matched["items"] == []
        visible = (await loop.query(scope, StewardPageRequest(expected_context=ctx.fingerprint)))["items"]
        assert len(visible) == 1 and visible[0]["title"] == ""
        assert visible[0]["extraction_status"] == "needs_extraction" and not visible[0]["references"]
        assert "jobs" not in visible[0] and not visible[0]["provider_states"] and not reader.calls
        # Even a current alias job binding cannot relabel older private references.
        allowed = next(item for item in items if item.original_ref.object_ref == "allowed-copy")
        with connection:
            steward._save_job(steward.jobs()[0].model_copy(update={"original_ref": allowed.original_ref}))
        rebound = (await loop.query(scope, StewardPageRequest(expected_context=ctx.fingerprint)))["items"][0]
        assert rebound["title"] == "" and rebound["extraction_status"] == "needs_extraction"

        class AllowedProvider(FixtureProvider):
            spec = ProviderSpec("fixture_text", "2", "合成文档", "fixture", "fixture_only")

            async def step(self, job, budget):
                result = await super().step(job, budget)
                return ExtractionStep(
                    [
                        unit.model_copy(update={"title": "公开标题", "text": "获准原件的正文"})
                        for unit in result.units
                    ],
                    result.next_cursor,
                    result.done,
                )

        qualified = FileSteward(connection, catalog, clock=lambda: now[0], fixture_provider=AllowedProvider())
        allowed_job = (
            await qualified.plan_jobs(
                StewardJobRequest(
                    expected_context=ctx.fingerprint, item_ids=[allowed.item_id], provider="fixture_text"
                )
            )
        )[0]
        for _ in range(2):
            await qualified.run_fixture_step(allowed_job.job_id, ctx.fingerprint)
        qualified_loop = AgentLoopFiles(qualified, scope_authorizer=lambda _: True, clock=lambda: now[0])
        private_match = await qualified_loop.query(
            scope, StewardPageRequest(expected_context=ctx.fingerprint, query="宇宙")
        )
        assert not private_match["items"]
        public_match = await qualified_loop.query(
            scope, StewardPageRequest(expected_context=ctx.fingerprint, query="公开")
        )
        public = public_match["items"][0]
        assert public["title"] == "公开标题" and public["extraction_status"] == "done"
        assert public["reference_count"] == 2 and len(public["provider_states"]) == 1
        assert all(unit.anchor.original_ref == allowed.original_ref for unit in public["references"])
        assert not reader.calls
    finally:
        await catalog.close()
        connection.close()


async def test_changed_content_invalidates_job_and_keeps_prior_units(env):
    steward, catalog, reader, now, _, _ = env
    await steward.reconcile(StewardPageRequest(expected_context=await context(env)))
    record = await job(env)
    await steward.run_fixture_step(record.job_id, await context(env))
    reader.put("fixture-doc", b"SYNTHETIC changed B", kind="other", revision="fixture-r2")
    for _ in range(2):
        await catalog.scan(
            FamilyMediaScanRequest(expected_context=await context(env), directory_ref="fixture-downloads")
        )
        now[0] += timedelta(seconds=3)
    result = await steward.reconcile(StewardPageRequest(expected_context=await context(env)))
    assert result["changes"]["changed"] == 1
    changed = next(j for j in steward.jobs() if j.job_id == record.job_id)
    # A still-ready duplicate supplies the old content; it may safely rebind old work.
    assert changed.content_id == record.content_id and len(steward.units(record.job_id)) == 1
    new_record = await job(env)
    assert new_record.content_id != record.content_id and new_record.cursor is None


async def test_content_id_survives_new_location_and_exact_hash_uses_primary(env):
    steward, catalog, reader, now, _, item = env
    aliases = await catalog.cached_items(
        await context(env),
        [
            i.item_id
            for i in (await catalog.list_items(FamilyMediaQuery(expected_context=await context(env)))).items
            if i.original_hash == item.original_hash
        ],
    )
    result = await steward.plan_jobs(
        StewardJobRequest(
            expected_context=await context(env),
            item_ids=[i.item_id for i in aliases],
            provider="fixture_text",
        )
    )
    assert len(result) == 1
    evidence = await steward.compare(await context(env), *[i.item_id for i in aliases])
    assert evidence["relation"] == "exact_hash" and evidence["evidence"].original_count == 2
    assert not evidence["delete_suggested"] and not evidence["review_allowed"]
    reader.epoch = "SIMULATED-epoch-2"
    for _ in range(2):
        await catalog.scan(
            FamilyMediaScanRequest(expected_context=await context(env), directory_ref="fixture-downloads")
        )
        now[0] += timedelta(seconds=3)
    await steward.reconcile(StewardPageRequest(expected_context=await context(env)))
    rebound = steward.jobs()[0]
    assert rebound.content_id == item.original_hash and rebound.item_id != item.item_id


async def test_resume_atomic_cursor_budget_and_source_only_calls(env):
    steward, catalog, reader, now, conn, _ = env
    record = await job(env)
    reader.calls.clear()
    first = await steward.run_fixture_step(record.job_id, await context(env))
    assert first.status == "paused" and first.completed_units == 1
    replacement = FileSteward(conn, catalog, clock=lambda: now[0], fixture_provider=FixtureProvider())
    last = await replacement.run_fixture_step(record.job_id, await context(env))
    assert last.status == "done" and [u.ordinal for u in replacement.units(record.job_id)] == [0, 1]
    assert last.charged_source_bytes == 2097152 and not reader.calls
    assert all(u.anchor.kind == "page" for u in replacement.units(record.job_id))


async def test_cancel_keeps_cursor_and_does_not_refund_reserved_budget(env):
    steward, _, _, _, _, _ = env
    started = asyncio.Event()

    class Waiting(FixtureProvider):
        async def step(self, job, budget):
            started.set()
            await asyncio.Event().wait()

    steward.fixture_provider = Waiting()
    record = await job(env)
    task = asyncio.create_task(steward.run_fixture_step(record.job_id, await context(env)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = steward.jobs()[0]
    assert saved.status == "paused" and saved.cursor is None and saved.completed_units == 0
    assert saved.charged_source_bytes == 1048576
    steward.fixture_provider = FixtureProvider()
    assert (await steward.run_fixture_step(record.job_id, await context(env))).completed_units == 1


async def test_bad_provider_result_rolls_back_units_and_cursor(env):
    steward = env[0]

    class Bad(FixtureProvider):
        async def step(self, job, budget):
            result = await super().step(job, budget)
            return ExtractionStep(result.units, None, False)

    steward.fixture_provider = Bad()
    record = await job(env)
    with pytest.raises(StewardError, match="invalid_provider_result"):
        await steward.run_fixture_step(record.job_id, await context(env))
    assert not steward.units(record.job_id)
    assert steward.jobs()[0].cursor is None and steward.jobs()[0].charged_source_bytes == 1048576


async def test_step_timeout_is_finite_and_keeps_budget(env):
    from homeplane.file_steward.contracts import StewardBudget

    class Slow(FixtureProvider):
        async def step(self, job, budget):
            await asyncio.Event().wait()

    env[0].fixture_provider = Slow()
    record = await job(env)
    with pytest.raises(StewardError, match="provider_step_failed"):
        await env[0].run_fixture_step(record.job_id, await context(env), StewardBudget(seconds=0.01))
    assert env[0].jobs()[0].status == "failed" and env[0].jobs()[0].charged_source_bytes == 1048576


async def test_near_version_text_is_distinct_from_exact_hash(env):
    record = await job(env)
    await env[0].run_fixture_step(record.job_id, await context(env))
    page = await env[1].list_items(FamilyMediaQuery(expected_context=await context(env)))
    other = next(i for i in page.items if i.media_kind == "image")
    other_job = (
        await env[0].plan_jobs(
            StewardJobRequest(
                expected_context=await context(env), item_ids=[other.item_id], provider="fixture_text"
            )
        )
    )[0]
    await env[0].run_fixture_step(other_job.job_id, await context(env))
    result = await env[0].compare(await context(env), env[5].item_id, other.item_id)
    assert result["relation"] == "possible_version" and result["basis"] == "bounded_extracted_text"
    assert not result["delete_suggested"] and result["evidence_unit_ids"]


async def test_intrabatch_collision_and_source_change_veto(env):
    await pin(env)
    aliases = [
        i
        for i in (await env[1].list_items(FamilyMediaQuery(expected_context=await context(env)))).items
        if i.original_hash == env[5].original_hash
    ]
    selections = [
        StewardRelocationSelection(
            item_id=i.item_id,
            expected_content_id=i.original_hash,
            destination="downloads/new.pdf",
            operation="rename",
        )
        for i in aliases
    ]
    plan = await env[0].preview(
        StewardPreviewRequest(
            expected_context=await context(env),
            selections=selections,
            inventory=StewardDestinationInventory(
                root_ref="fixture-output",
                namespace_revision="1",
                checked_at=env[3][0],
                complete=True,
                paths=[],
                basis="synthetic_fixture",
            ),
        )
    )
    assert plan.counts["collision"] == 2
    plan = await preview(env)
    env[2].put("fixture-doc", b"CHANGED synthetic", kind="other", revision="fixture-new")
    await env[1].scan(
        FamilyMediaScanRequest(expected_context=await context(env), directory_ref="fixture-downloads")
    )
    with pytest.raises(StewardError, match="stale_plan"):
        await approval(env, plan)


def test_preview_schema_generation_matches_checked_in_source(tmp_path):
    import json
    from pathlib import Path

    from homeplane.file_steward.schemas import generate

    generate(tmp_path)
    for path in tmp_path.glob("Steward*.json"):
        assert json.loads(path.read_text()) == json.loads(
            (Path(__file__).parents[1] / "schemas" / path.name).read_text()
        )


async def test_nonfixture_execution_and_cloud_are_disabled_independently_of_policy(env):
    await pin(env)
    cloud = await job(env, "cloud_semantic")
    assert cloud.status == "disabled"
    with pytest.raises(StewardError, match="executor_disabled"):
        await env[0].run_fixture_step(cloud.job_id, await context(env))
    with pytest.raises(StewardError, match="executor_disabled"):
        env[0].execute(cloud.job_id)
    env[1].simulated = False
    with pytest.raises(StewardError, match="fixture_only"):
        FileSteward(env[4], env[1], fixture_provider=FixtureProvider())


async def test_preserve_display_title_then_explicit_download_suggestion(env):
    record = await job(env)
    await env[0].run_fixture_step(record.job_id, await context(env))
    request = StewardSuggestionRequest(expected_context=await context(env), item_id=env[5].item_id)
    default = await env[0].suggest(request)
    assert (
        default.status == "display_title_only" and default.title and not default.stem and not default.folder
    )
    assert default.policy.source == "default_preserve"
    await pin(env)
    allowed = await env[0].suggest(request)
    assert allowed.status == "suggested" and allowed.folder == "知识/科学"
    assert allowed.policy.matched_rule_ids == ["downloads"]


async def test_preserve_policy_blocks_physical_plan_and_counts(env):
    plan = await preview(env)
    assert plan.status == "blocked" and plan.counts["protected"] == 1
    assert "policy_preserve_name" in plan.entries[0].blockers
    with pytest.raises(StewardError, match="plan_blocked"):
        await approval(env, plan)


async def test_separate_rename_switch_and_declared_diff_cannot_hide_move(env):
    await pin(env, move_preview=False, structure_preview=False)
    assert (await preview(env)).status == "awaiting_review"  # rename within same directory
    moved = await preview(env, "new-folder/new.pdf")
    assert "policy_preserve_location" in moved.entries[0].blockers
    assert "declared_operation_mismatch" in moved.entries[0].blockers


@pytest.mark.parametrize(
    "target,occupied",
    [("downloads/New.pdf", "downloads/new.pdf"), ("downloads/é.pdf", "downloads/e\u0301.pdf")],
)
async def test_casefold_and_unicode_collision(env, target, occupied):
    await pin(env)
    plan = await preview(env, target, paths=[occupied])
    assert plan.counts["collision"] == 1 and "destination_collision" in plan.entries[0].blockers


async def test_stale_context_and_policy_change_veto_review(env):
    await pin(env)
    plan = await preview(env)
    await pin(env, rename_preview=False)
    with pytest.raises(StewardError, match="policy_changed"):
        await approval(env, plan)
    await pin(env)
    plan = await preview(env)
    env[2].state = "backoff"
    with pytest.raises(StewardError, match="stale_plan"):
        await approval(env, plan)


async def test_preview_approval_journal_and_replay_veto(env):
    await pin(env)
    plan = await preview(env)
    with pytest.raises(StewardError, match="batch_selection_changed"):
        await approval(env, plan, selected_entry_ids=["a" * 64])
    reviewed = await approval(env, plan)
    assert reviewed.status == "reviewed_preview" and not reviewed.execution_enabled
    phases = [
        __import__("json").loads(r[0])["phase"] for r in env[4].execute("SELECT body FROM steward_journal")
    ]
    assert sorted(phases) == ["planned", "preview_approved"]
    with pytest.raises(StewardError, match="already_recorded"):
        await approval(env, plan)


async def test_expired_inventory_and_finite_plan(env):
    await pin(env)
    plan = await preview(env)
    env[3][0] += timedelta(seconds=61)
    with pytest.raises(StewardError, match="stale_plan"):
        await approval(env, plan)


async def test_bundle_cannot_be_split_by_file_exception(env):
    await pin(env, unit="dependent_bundle", origin="bundle")
    await pin(env, rule_id="member", item_id=env[5].item_id, label="成员例外")
    plan = await preview(env)
    assert "dependent_bundle_review_required" in plan.entries[0].blockers


async def test_api_has_no_scan_run_upload_or_delete(env):
    app = FastAPI()
    app.include_router(create_router(env[0]))
    env[2].calls.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        report = (await client.get("/api/files/context")).json()
        assert not report["executor_enabled"] and all(not p["enabled"] for p in report["providers"])
        response = await client.post(
            "/api/files/query", json={"expected_context": report["catalog"]["fingerprint"]}
        )
        assert response.status_code == 200 and len(response.json()["items"]) == 3
        for path in ("scan", "run", "jobs/run", "upload", "relocations/execute", "delete"):
            assert (await client.post("/api/files/" + path, json={})).status_code == 404
    assert not env[2].calls


def test_pinned_policy_precedence_conflicts_proposals_and_binding_expiry():
    base = dict(source_ref="source", config_generation=1, connection_epoch="epoch", status="user_pinned")
    parent = StewardPolicyRule(
        rule_id="camera", directory_ref="root", label="相机原片", origin="camera_phone", **base
    )
    child = StewardPolicyRule(
        rule_id="downloads", directory_ref="child", label="下载收藏", rename_preview=True, **base
    )
    exception = StewardPolicyRule(rule_id="exception", item_id="a" * 64, label="此文件保留", **base)
    args = dict(
        source_ref="source",
        config_generation=1,
        connection_epoch="epoch",
        item_id="a" * 64,
        ancestor_refs=["root", "child"],
    )
    assert resolve_policy([parent, child], **args).rename_preview
    resolved = resolve_policy([parent, child, exception], **args)
    assert resolved.source == "file_exception" and not resolved.rename_preview
    conflicting = child.model_copy(update={"rule_id": "conflict", "rename_preview": False})
    assert resolve_policy([child, conflicting], **args).source == "policy_conflict"
    proposed = child.model_copy(update={"status": "proposed"})
    assert resolve_policy([proposed], **args).source == "default_preserve"
    assert resolve_policy([child], **{**args, "connection_epoch": "new"}).source == "default_preserve"
    assert not resolve_policy([child], **args).automatic_cloud_allowed


@pytest.mark.parametrize("path", ["../x", "a/../x", "/root", "a\\b", "CON.pdf", "x/", "name.", "a\x00b"])
def test_unsafe_destinations_rejected(path):
    with pytest.raises(ValueError):
        relative_path(path)


def test_time_page_and_region_references_are_typed(env=None):
    from homeplane.family_media.contracts import FamilyMediaOriginalRef

    reference = FamilyMediaOriginalRef(
        source_ref="source", object_ref="object", config_generation=1, connection_epoch="epoch", revision="r1"
    )
    base = dict(content_id="a" * 64, original_ref=reference)
    with pytest.raises(ValidationError):
        StewardSourceAnchor(kind="video_time", start_ms=10, end_ms=5, **base)
    with pytest.raises(ValidationError):
        StewardSourceAnchor(kind="page", **base)
    assert StewardSourceAnchor(kind="video_time", start_ms=10, end_ms=20, **base).end_ms == 20


async def test_agent_loop_scope_default_off_whitelist_and_auth_pause(env):
    from homeplane.file_steward.agent_loop import AgentLoopFiles, StewardAgentReadScope, session_projection

    ctx = await env[1].context()
    scope = StewardAgentReadScope(
        grant_id="fixture-grant",
        subject_ref="fixture-peer",
        source_ref=ctx.source_ref,
        config_generation=ctx.config_generation,
        connection_epoch=ctx.connection_epoch,
        directory_refs=["fixture-downloads"],
        approved_at=env[3][0],
        expires_at=env[3][0] + timedelta(minutes=5),
    )
    request = StewardPageRequest(expected_context=ctx.fingerprint)
    with pytest.raises(StewardError, match="scope_unbound"):
        await AgentLoopFiles(env[0], clock=lambda: env[3][0]).query(scope, request)
    permitted = [True]
    loop = AgentLoopFiles(env[0], scope_authorizer=lambda _: permitted[0], clock=lambda: env[3][0])
    env[2].calls.clear()
    assert len((await loop.query(scope, request))["items"]) == 3
    assert not (
        await loop.query(scope.model_copy(update={"directory_refs": ["different-directory"]}), request)
    )["items"]
    assert not env[2].calls
    paused = AgentLoopFiles(
        env[0],
        scope_authorizer=lambda _: True,
        clock=lambda: env[3][0],
        session_status=lambda: {"state": "paused", "failure": "nas_auth_failed"},
    )
    with pytest.raises(StewardError, match="authentication_required"):
        await paused.step_fixture(scope, ctx.fingerprint, directory_ref="fixture-downloads")
    assert (
        not env[2].calls
        and not session_projection({"state": "paused", "failure": "nas_auth_failed"})["retry_credentials"]
    )
    with pytest.raises(StewardError, match="source_read_executor_disabled"):
        loop.read_original(scope, "arbitrary-path")


async def test_agent_loop_finite_checkpoint_resume_and_revocation(env):
    from homeplane.file_steward.agent_loop import AgentLoopFiles, StewardAgentReadScope

    ctx = await env[1].context()
    scope = StewardAgentReadScope(
        grant_id="finite-window",
        subject_ref="fixture-peer",
        source_ref=ctx.source_ref,
        config_generation=ctx.config_generation,
        connection_epoch=ctx.connection_epoch,
        directory_refs=["fixture-downloads"],
        approved_at=env[3][0],
        expires_at=env[3][0] + timedelta(minutes=5),
        max_observed_entries=2,
        max_read_bytes=0,
    )
    loop = AgentLoopFiles(env[0], scope_authorizer=lambda _: True, clock=lambda: env[3][0])
    first = await loop.step_fixture(scope, ctx.fingerprint, directory_ref="fixture-downloads", limit=1)
    second_loop = AgentLoopFiles(env[0], scope_authorizer=lambda _: True, clock=lambda: env[3][0])
    second = await second_loop.step_fixture(
        scope, first.context.fingerprint, directory_ref="fixture-downloads", limit=1
    )
    assert first.checkpoint.scan_id == second.checkpoint.scan_id
    assert (
        first.checkpoint.next_cursor != second.checkpoint.next_cursor and second.checkpoint.entries_seen == 2
    )
    with pytest.raises(StewardError, match="window_exhausted"):
        await second_loop.step_fixture(
            scope, second.context.fingerprint, directory_ref="fixture-downloads", limit=1
        )
    calls = [0]

    def once(_):
        calls[0] += 1
        return calls[0] == 1

    revoked = AgentLoopFiles(env[0], scope_authorizer=once, clock=lambda: env[3][0])
    with pytest.raises(StewardError, match="scope_denied"):
        await revoked.query(scope, StewardPageRequest(expected_context=second.context.fingerprint))


def test_files_console_discards_late_responses_and_scopes_styles():
    import subprocess
    from pathlib import Path

    root = Path(__file__).parents[1]
    result = subprocess.run(
        ["node", str(root / "tests/file_steward_console_fixture.cjs"), str(root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("enabled", [False, True])
async def test_shared_hook_opt_in_does_not_expand_catalog_scope(tmp_path, enabled):
    from homeplane.api import create_app

    app = create_app(
        db_path=tmp_path / "synthetic.sqlite",
        settings_path=tmp_path / "settings.json",
        file_steward_enabled=enabled,
    )
    try:
        assert app.state.family_media.indexed_kinds == frozenset({"image", "video"})
        assert app.state.nas_connection._task is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            html = (await client.get("/")).text
            assert ('data-file-steward-enabled="true"' in html) is enabled
            response = await client.get("/api/files/context")
            assert response.status_code == (200 if enabled else 404)
            if enabled:
                assert not response.json()["executor_enabled"]
        table = app.state.core.store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='steward_jobs'"
        ).fetchone()
        assert bool(table) is enabled
    finally:
        await app.state.family_media.close()
        await app.state.nas_connection.close()
        app.state.core.store.close()
