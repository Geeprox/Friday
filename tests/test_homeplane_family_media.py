"""Family media integration using synthetic bytes and injected F1 protocol only."""

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from homeplane.family_media import FamilyMediaCatalog, FamilyMediaError
from homeplane.family_media.__main__ import demo
from homeplane.family_media.api import create_router
from homeplane.family_media.contracts import (
    FamilyMediaAssetRequest,
    FamilyMediaCrop,
    FamilyMediaQuery,
    FamilyMediaRecipeRequest,
    FamilyMediaRecipeReview,
    FamilyMediaResolveRequest,
    FamilyMediaScanRequest,
    FamilyMediaSourceReview,
)
from homeplane.family_media.fixture import SyntheticNASError, SyntheticNASReader
from homeplane.family_media.reader import RANGE_BYTES
from homeplane.family_media.schemas import generate

PHOTO = b"SIMULATED photo with no household pixels"
VIDEO = b"SIMULATED video, never a real camera recording"


@pytest.fixture
def env(tmp_path):
    reader = SyntheticNASReader()
    reader.put("SIMULATED-photo", PHOTO)
    now = [datetime(2026, 10, 1, tzinfo=UTC)]
    monotonic = [100.0]
    connection = sqlite3.connect(tmp_path / "SIMULATED.sqlite")
    catalog = FamilyMediaCatalog(
        connection,
        reader,
        simulated=True,
        clock=lambda: now[0],
        monotonic=lambda: monotonic[0],
    )
    yield catalog, reader, now, monotonic, connection
    connection.close()


async def scan(catalog, **kwargs):
    return await catalog.scan(
        FamilyMediaScanRequest(expected_context=(await catalog.context()).fingerprint, **kwargs)
    )


async def items(catalog, **kwargs):
    return (
        await catalog.list_items(
            FamilyMediaQuery(expected_context=(await catalog.context()).fingerprint, **kwargs)
        )
    ).items


async def ready(env):
    catalog, _, now, _, _ = env
    await scan(catalog)
    now[0] += timedelta(seconds=3)
    await scan(catalog)
    return next(item for item in await items(catalog) if item.status == "ready")


async def resolution(catalog, item):
    return await catalog.resolve(
        FamilyMediaResolveRequest(
            expected_context=(await catalog.context()).fingerprint,
            item_id=item.item_id,
        )
    )


async def plan(catalog, item):
    return await catalog.prepare_original(
        FamilyMediaResolveRequest(
            expected_context=(await catalog.context()).fingerprint,
            item_id=item.item_id,
        )
    )


async def recipe(catalog, item, operations=None):
    return await catalog.preview_recipe(
        FamilyMediaRecipeRequest(
            expected_context=(await catalog.context()).fingerprint,
            item_id=item.item_id,
            expected_original_hash=item.original_hash,
            operations=operations or [{"operation": "organize", "album": "SIMULATED family"}],
        )
    )


async def test_runnable_synthetic_integration():
    result = await demo()
    assert result["simulated"] and result["duplicate_originals"] == 2
    assert result["bounded_original_read_matches"]
    assert result["reviewed_recipe"]["status"] == "reviewed"
    assert not result["real_nas_accessed"] and not result["original_mutated"] and not result["media_copied"]


async def test_import_context_query_and_router_do_not_enumerate_or_read(env):
    catalog, reader, _, _, _ = env
    assert (await catalog.context()).simulated
    assert await items(catalog) == []
    create_router(catalog)
    assert reader.calls == []


async def test_pending_then_full_original_hash_and_unchanged_incremental_scan(env):
    catalog, reader, _, _, _ = env
    await scan(catalog)
    item = (await items(catalog))[0]
    assert item.status == "pending" and item.original_hash is None and item.captured_at is None
    assert not any(call[0] == "range" for call in reader.calls)
    item = await ready(env)
    assert item.original_hash == hashlib.sha256(PHOTO).hexdigest() == item.asset_id
    assert item.original_ref.object_ref == "SIMULATED-photo"
    assert not item.immutable_source_verified and not item.playback_verified
    reader.calls.clear()
    result = await scan(catalog)
    assert result.bytes_read == result.bytes_requested == result.hashes_completed == 0
    assert not any(call[0] == "range" for call in reader.calls)


async def test_exact_dedup_keeps_each_original_and_does_not_merge_similar_bytes(env):
    catalog, reader, _, _, _ = env
    reader.put("SIMULATED-duplicate", PHOTO)
    reader.put("SIMULATED-similar", PHOTO + b"different")
    reader.put("SIMULATED-video", VIDEO, kind="video")
    await ready(env)
    records = await items(catalog)
    hashes = {item.asset_id for item in records}
    assert len(records) == 4 and len(hashes) == 3
    evidence = await catalog.asset_evidence(
        FamilyMediaAssetRequest(
            expected_context=(await catalog.context()).fingerprint,
            asset_id=hashlib.sha256(PHOTO).hexdigest(),
        )
    )
    assert evidence.original_count == 2 and evidence.status == "ready" and not evidence.partial
    assert not evidence.near_duplicate_inference and evidence.causal_relation == "unknown"
    assert (
        reader.objects["SIMULATED-photo"]["bytes"] == reader.objects["SIMULATED-duplicate"]["bytes"] == PHOTO
    )


async def test_media_type_and_time_not_guessed_from_capture_metadata(env):
    catalog, reader, _, _, _ = env
    reader.put("SIMULATED-audio", b"SIMULATED", kind="audio")
    reader.put("SIMULATED-unknown", b"SIMULATED", kind="other")
    reader.put("SIMULATED-video", VIDEO, kind="video")
    await ready(env)
    records = await items(catalog)
    assert {item.media_kind for item in records} == {"image", "video"}
    assert all(item.captured_at is None for item in records)


@pytest.mark.parametrize("closed", [None, False])
async def test_stable_metadata_without_completion_evidence_stays_pending(env, closed):
    catalog, reader, now, _, _ = env
    reader.objects["SIMULATED-photo"]["entry"]["closed"] = closed
    await scan(catalog)
    now[0] += timedelta(days=1)
    await scan(catalog)
    assert (await items(catalog))[0].status == "pending"
    assert not any(call[0] == "range" for call in reader.calls)


async def test_reviewed_completion_policy_and_explicit_open_file_veto(env):
    catalog, reader, now, _, _ = env
    reader.objects["SIMULATED-photo"]["entry"]["closed"] = None
    reader.put("SIMULATED-open", VIDEO, kind="video", closed=False)
    await scan(catalog)
    reviewed = await catalog.review_source(
        FamilyMediaSourceReview(
            expected_context=(await catalog.context()).fingerprint,
            completion_convention="closed_files_operator_reviewed",
        )
    )
    assert reviewed.completion_convention == "closed_files_operator_reviewed"
    now[0] += timedelta(seconds=3)
    await scan(catalog)
    records = await items(catalog)
    photo = next(item for item in records if item.media_kind == "image")
    video = next(item for item in records if item.media_kind == "video")
    assert photo.status == "ready" and photo.completion_evidence == "operator_reviewed_stable_metadata"
    assert video.status == "pending" and video.original_hash is None
    await catalog.review_source(
        FamilyMediaSourceReview(
            expected_context=(await catalog.context()).fingerprint,
            completion_convention="unreviewed",
        )
    )
    assert next(item for item in await items(catalog) if item.media_kind == "image").status == "pending"


async def test_zero_byte_partial_and_delayed_arrival(env):
    catalog, reader, now, _, _ = env
    reader.put("SIMULATED-video", b"", kind="video", closed=False)
    await scan(catalog)
    now[0] += timedelta(seconds=3)
    await scan(catalog)
    assert next(item for item in await items(catalog) if item.media_kind == "video").status == "pending"
    reader.put("SIMULATED-video", VIDEO, kind="video", closed=True, revision="SIMULATED-r2")
    reader.put("SIMULATED-late", PHOTO + b"late")
    await scan(catalog)
    assert next(item for item in await items(catalog) if item.media_kind == "video").original_hash is None
    now[0] += timedelta(seconds=3)
    await scan(catalog)
    assert len(await items(catalog)) == 3
    assert all(item.status == "ready" for item in await items(catalog))


async def test_bounded_incremental_large_video_hash(env):
    catalog, reader, now, _, _ = env
    content = b"SIMULATED" * 300000
    reader.objects.clear()
    reader.put("SIMULATED-large-video", content, kind="video")
    await scan(catalog)
    now[0] += timedelta(seconds=3)
    first = await scan(catalog, byte_budget=RANGE_BYTES)
    item = (await items(catalog))[0]
    assert first.bytes_requested == first.bytes_read == RANGE_BYTES
    assert item.status == "pending" and item.hash_progress_bytes == RANGE_BYTES
    second = await scan(catalog, byte_budget=RANGE_BYTES)
    assert second.bytes_read == RANGE_BYTES
    third = await scan(catalog, byte_budget=RANGE_BYTES)
    item = (await items(catalog))[0]
    assert third.hashes_completed == 1 and item.original_hash == hashlib.sha256(content).hexdigest()
    assert all(call[3] <= RANGE_BYTES for call in reader.calls if call[0] == "range")
    assert len(catalog.hash_jobs) == 0 and not item.media_copied


async def test_restart_resumes_enumeration_but_restarts_unfinished_hash_at_zero(env):
    catalog, reader, now, monotonic, connection = env
    reader.put("SIMULATED-z-video", b"SIMULATED" * 300000, kind="video")
    first = await scan(catalog, limit=1)
    saved_cursor = first.checkpoint.next_cursor
    assert saved_cursor and not first.checkpoint.enumeration_complete
    new_catalog = FamilyMediaCatalog(
        connection, reader, simulated=True, clock=lambda: now[0], monotonic=lambda: monotonic[0]
    )
    reader.calls.clear()
    result = await scan(new_catalog, limit=1)
    assert reader.calls[0][1] == saved_cursor and result.checkpoint.enumeration_complete
    now[0] += timedelta(seconds=3)
    await scan(new_catalog, byte_budget=RANGE_BYTES)
    assert new_catalog.hash_jobs
    newest = FamilyMediaCatalog(connection, reader, simulated=True, clock=lambda: now[0])
    assert not newest.hash_jobs
    pending = next(item for item in await items(newest) if item.media_kind == "video")
    assert pending.hash_progress_bytes == 0
    reader.calls.clear()
    await scan(newest, byte_budget=RANGE_BYTES)
    assert next(call for call in reader.calls if call[0] == "range")[2] == 0


async def test_existing_hash_and_recipes_survive_catalog_restart(env):
    catalog, reader, now, _, connection = env
    item = await ready(env)
    draft = await recipe(catalog, item)
    restarted = FamilyMediaCatalog(connection, reader, simulated=True, clock=lambda: now[0])
    assert (await items(restarted))[0].original_hash == item.original_hash
    assert restarted.recipes(item.item_id)[0].recipe_id == draft.recipe_id
    with pytest.raises(FamilyMediaError, match="context_changed"):
        await restarted.list_items(FamilyMediaQuery(expected_context=draft.context_fingerprint))


async def test_absent_non_atomic_listing_is_stale_then_explicit_stat_missing(env):
    catalog, reader, _, _, _ = env
    item = await ready(env)
    reader.listing = set()
    await scan(catalog)
    record = (await items(catalog))[0]
    assert record.status == "stale" and "non_atomic" in record.reason
    assert record.original_hash == item.original_hash
    exists = await resolution(catalog, record)
    assert exists.item.status == "ready" and exists.source_checked
    del reader.objects["SIMULATED-photo"]
    await scan(catalog)
    absent = await resolution(catalog, record)
    assert absent.item.status == "missing" and absent.item.original_hash == item.original_hash
    assert absent.item.original_ref is None


async def test_missing_is_not_an_empty_success_or_inferred_from_transport_error(env):
    catalog, reader, _, _, _ = env
    item = await ready(env)

    async def stat(*args):
        raise RuntimeError("secret credential at /Volumes/private-family")

    reader.stat = stat
    result = await resolution(catalog, item)
    assert result.item.status == "unavailable" and result.item.reason == "nas_transport_failed"
    assert "secret" not in result.model_dump_json() and "/Volumes" not in result.model_dump_json()


async def test_modified_original_invalidates_hash_and_preserves_prior_sha(env):
    catalog, reader, now, _, _ = env
    item = await ready(env)
    reader.put("SIMULATED-photo", PHOTO + b"new", revision="SIMULATED-r2")
    changed = await resolution(catalog, item)
    assert changed.item.status == "stale" and changed.item.original_ref is None
    await scan(catalog)
    pending = (await items(catalog))[0]
    assert pending.item_id == item.item_id and pending.original_hash is None
    assert item.original_hash in pending.previous_original_hashes
    now[0] += timedelta(seconds=3)
    await scan(catalog)
    updated = (await items(catalog))[0]
    assert updated.original_hash == hashlib.sha256(PHOTO + b"new").hexdigest() != item.original_hash
    assert item.original_hash in updated.previous_original_hashes


async def test_reconnect_expires_locations_but_content_hash_rebinds_after_relist(env):
    catalog, reader, now, _, _ = env
    old = await ready(env)
    old_context = (await catalog.context()).fingerprint
    reader.epoch = "SIMULATED-epoch-2"
    reader.objects["SIMULATED-photo-new-ref"] = reader.objects.pop("SIMULATED-photo")
    reader.objects["SIMULATED-photo-new-ref"]["entry"]["object_ref"] = "SIMULATED-photo-new-ref"
    with pytest.raises(FamilyMediaError, match="context_changed"):
        await catalog.list_items(FamilyMediaQuery(expected_context=old_context))
    assert (await resolution(catalog, old)).item.status == "stale"
    await scan(catalog)
    now[0] += timedelta(seconds=3)
    await scan(catalog)
    records = await items(catalog)
    current = next(item for item in records if item.status == "ready")
    assert current.item_id != old.item_id and current.asset_id == old.asset_id
    assert current.original_ref.connection_epoch == reader.epoch
    evidence = await catalog.asset_evidence(
        FamilyMediaAssetRequest(
            expected_context=(await catalog.context()).fingerprint,
            asset_id=current.asset_id,
        )
    )
    assert evidence.ready_original_count == 1 and evidence.other_original_count == 1


async def test_metadata_freshness_and_clock_rollback_do_not_show_ready(env):
    catalog, _, now, _, _ = env
    item = await ready(env)
    now[0] += timedelta(seconds=61)
    assert (await items(catalog))[0].status == "stale"
    result = await resolution(catalog, item)
    assert result.source_checked and result.item.status == "ready"
    now[0] -= timedelta(days=1)
    assert (await items(catalog))[0].status == "stale"


@pytest.mark.parametrize(
    "state", ["backoff", "paused", "stopped", "connecting", "disabled", "not_configured"]
)
async def test_offline_states_preserve_catalog_without_false_ready(env, state):
    catalog, reader, _, _, _ = env
    item = await ready(env)
    reader.calls.clear()
    reader.state = state
    context = await catalog.context()
    assert not context.source_online
    result = await resolution(catalog, item)
    assert result.item.status == "unavailable" and not result.source_checked
    assert reader.calls == []
    with pytest.raises(FamilyMediaError, match="source_unavailable"):
        await scan(catalog)


async def test_connection_must_have_all_three_admission_checks(env):
    catalog, reader, _, _, _ = env
    original = reader.status

    async def status():
        value = await original()
        value["authenticated"] = None
        return value

    reader.status = status
    assert not (await catalog.context()).source_online
    with pytest.raises(FamilyMediaError, match="source_unavailable"):
        await scan(catalog)
    assert reader.calls == []


async def test_changed_generation_during_listing_rejects_late_result_and_checkpoint(env):
    catalog, reader, _, _, connection = env
    original = reader.list_page

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        reader.generation += 1
        return result

    reader.list_page = changed
    with pytest.raises(FamilyMediaError, match="source_changed"):
        await scan(catalog)
    assert catalog._meta("last_checkpoint") is None
    assert connection.execute("SELECT COUNT(*) FROM family_media_items").fetchone()[0] == 0


async def test_cancelled_scan_preserves_previous_checkpoint_and_discards_hash_state(env):
    catalog, reader, now, _, _ = env
    await scan(catalog)
    saved = catalog._meta("last_checkpoint")
    now[0] += timedelta(seconds=3)
    started = asyncio.Event()

    async def blocked(*args):
        started.set()
        await asyncio.Future()

    reader.read_range = blocked
    task = asyncio.create_task(scan(catalog))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert catalog._meta("last_checkpoint") == saved
    assert not catalog.hash_jobs and not catalog.lock.locked()
    assert (await items(catalog))[0].original_hash is None


async def test_timeout_does_not_block_event_loop_or_advance_checkpoint(env):
    catalog, reader, _, _, _ = env
    catalog.io_timeout = 0.02
    reader.delay = 0.05
    events = []

    async def unrelated_control_fixture():
        await asyncio.sleep(0)
        events.append("control-fixture-remains-responsive")

    results = await asyncio.gather(scan(catalog), unrelated_control_fixture(), return_exceptions=True)
    assert isinstance(results[0], FamilyMediaError) and results[0].code == "nas_timeout"
    assert events and catalog._meta("last_checkpoint") is None


async def test_short_read_consumes_byte_budget_without_trusting_partial_hash(env):
    catalog, reader, now, _, _ = env
    reader.put("SIMULATED-second", PHOTO)
    await scan(catalog)
    now[0] += timedelta(seconds=3)
    original = reader.read_range

    async def short(*args):
        return (await original(*args))[:-1]

    reader.read_range = short
    reader.calls.clear()
    result = await scan(catalog, byte_budget=10)
    assert result.bytes_requested == 10 and result.bytes_read == 0
    assert sum(call[3] for call in reader.calls if call[0] == "range") == 10
    assert all(item.original_hash is None for item in await items(catalog))


async def test_hash_revision_change_and_source_reconnect_discard_partial_sha(env):
    catalog, reader, now, _, _ = env
    await scan(catalog)
    now[0] += timedelta(seconds=3)
    original = reader.read_range

    async def changed(*args):
        value = await original(*args)
        reader.objects["SIMULATED-photo"]["entry"]["revision"] = "SIMULATED-new"
        return value

    reader.read_range = changed
    result = await scan(catalog)
    assert result.hashes_completed == 0 and not catalog.hash_jobs
    assert (await items(catalog))[0].original_hash is None
    reader.read_range = original
    await scan(catalog)
    now[0] += timedelta(seconds=3)

    async def reconnect(*args):
        value = await original(*args)
        reader.epoch = "SIMULATED-reconnected"
        return value

    reader.read_range = reconnect
    with pytest.raises(FamilyMediaError, match="source_changed"):
        await scan(catalog)
    assert not catalog.hash_jobs


@pytest.mark.parametrize(
    "malformation", ["no_cursor", "false_completion", "wrong_source", "duplicate", "too_many"]
)
async def test_malformed_or_wrong_binding_pages_are_rejected(env, malformation):
    catalog, reader, _, _, _ = env
    original = reader.list_page

    async def invalid(*args, **kwargs):
        value = await original(*args, **kwargs)
        if malformation == "no_cursor":
            value["enumeration_complete"] = False
        elif malformation == "false_completion":
            value["completed_scan_token"] = None
        elif malformation == "wrong_source":
            value["source_ref"] = "SIMULATED-wrong"
        elif malformation == "duplicate":
            value["entries"] *= 2
        else:
            value["entries"] *= 129
        return value

    reader.list_page = invalid
    with pytest.raises(FamilyMediaError):
        await scan(catalog)
    assert await items(catalog) == [] and catalog._meta("last_checkpoint") is None


async def test_cursor_expiry_has_explicit_restart_and_never_silently_rescans(env):
    catalog, reader, _, _, _ = env
    reader.put("SIMULATED-z", PHOTO)
    first = await scan(catalog, limit=1)
    assert first.checkpoint.next_cursor
    original = reader.list_page

    async def expired(source_ref, cursor=None, limit=128, **kwargs):
        if cursor:
            raise SyntheticNASError("nas_cursor_expired")
        return await original(source_ref, cursor, limit, **kwargs)

    reader.list_page = expired
    with pytest.raises(FamilyMediaError, match="nas_cursor_expired"):
        await scan(catalog, limit=1)
    assert catalog._meta("last_checkpoint")["next_cursor"] == first.checkpoint.next_cursor
    assert (await scan(catalog, restart_scan=True)).checkpoint.enumeration_complete


async def test_catalog_pagination_filters_expiry_and_mutation_fence(env):
    catalog, reader, _, monotonic, _ = env
    reader.put("SIMULATED-video", VIDEO, kind="video")
    await ready(env)
    context = await catalog.context()
    first = await catalog.list_items(FamilyMediaQuery(expected_context=context.fingerprint, limit=1))
    assert first.next_cursor
    second = await catalog.list_items(
        FamilyMediaQuery(expected_context=context.fingerprint, cursor=first.next_cursor, limit=1)
    )
    assert second.items[0].item_id != first.items[0].item_id and not second.next_cursor
    with pytest.raises(FamilyMediaError, match="cursor_expired"):
        await catalog.list_items(
            FamilyMediaQuery(
                expected_context=context.fingerprint,
                cursor=first.next_cursor,
                media_kind="image",
            )
        )
    monotonic[0] += 61
    with pytest.raises(FamilyMediaError, match="cursor_expired"):
        await catalog.list_items(
            FamilyMediaQuery(expected_context=context.fingerprint, cursor=first.next_cursor)
        )
    await scan(catalog)
    with pytest.raises(FamilyMediaError, match="context_changed"):
        await catalog.list_items(FamilyMediaQuery(expected_context=context.fingerprint))
    assert len(await items(catalog, media_kind="video")) == 1


async def test_multiple_original_plans_and_rechecks_do_not_revoke_each_other(env):
    catalog, _, _, _, _ = env
    item = await ready(env)
    first = await plan(catalog, item)
    second = await plan(catalog, item)
    assert first.context_fingerprint == second.context_fingerprint
    assert await catalog.read_original(first.plan_id, 0, 5) == PHOTO[:5]
    assert await catalog.read_original(second.plan_id, 5, 5) == PHOTO[5:10]
    catalog.revoke_original(first.plan_id)
    with pytest.raises(FamilyMediaError, match="expired"):
        await catalog.read_original(first.plan_id, 0, 5)


@pytest.mark.parametrize(
    "offset,length", [(-1, 1), (0, 0), (0, RANGE_BYTES + 1), (99999, 1), (0.0, 1), (True, 1)]
)
async def test_original_range_limits_rejected_before_reader_io(env, offset, length):
    catalog, reader, _, _, _ = env
    lease = await plan(catalog, await ready(env))
    reader.calls.clear()
    with pytest.raises(FamilyMediaError, match="invalid_range"):
        await catalog.read_original(lease.plan_id, offset, length)
    assert reader.calls == []


async def test_original_expiration_source_change_and_failed_read_reservation(env):
    catalog, reader, _, monotonic, _ = env
    item = await ready(env)
    lease = await plan(catalog, item)
    reader.calls.clear()
    monotonic[0] += 301
    with pytest.raises(FamilyMediaError, match="expired"):
        await catalog.read_original(lease.plan_id, 0, 5)
    assert reader.calls == []
    lease = await plan(catalog, item)
    original = reader.read_range

    async def failed(*args):
        raise SyntheticNASError("nas_timeout")

    reader.read_range = failed
    before = catalog.leases[lease.plan_id].remaining
    with pytest.raises(FamilyMediaError, match="nas_timeout"):
        await catalog.read_original(lease.plan_id, 0, 5)
    assert catalog.leases[lease.plan_id].remaining == before - 5 and catalog.active_readers == 0
    reader.read_range = original
    reader.epoch = "SIMULATED-new"
    with pytest.raises(FamilyMediaError, match="context_changed"):
        await catalog.read_original(lease.plan_id, 0, 5)


async def test_original_late_source_change_and_expiry_during_read_discard_bytes(env):
    catalog, reader, _, monotonic, _ = env
    item = await ready(env)
    lease = await plan(catalog, item)
    original = reader.read_range

    async def expired(*args):
        value = await original(*args)
        monotonic[0] += 301
        return value

    reader.read_range = expired
    with pytest.raises(FamilyMediaError, match="expired"):
        await catalog.read_original(lease.plan_id, 0, 5)
    reader.read_range = original
    lease = await plan(catalog, item)

    async def changed(*args):
        value = await original(*args)
        reader.epoch = "SIMULATED-late-reconnect"
        return value

    reader.read_range = changed
    with pytest.raises(FamilyMediaError, match="source_changed"):
        await catalog.read_original(lease.plan_id, 0, 5)


async def test_original_capacity_session_budget_and_cancellation(env):
    catalog, reader, _, _, _ = env
    item = await ready(env)
    leases = [await plan(catalog, item) for _ in range(8)]
    with pytest.raises(FamilyMediaError, match="capacity"):
        await plan(catalog, item)
    lease = leases[0]
    catalog.leases[lease.plan_id].remaining = 1
    with pytest.raises(FamilyMediaError, match="byte_budget"):
        await catalog.read_original(lease.plan_id, 0, 2)
    catalog.leases[lease.plan_id].remaining = 100
    started = asyncio.Event()

    async def blocked(*args):
        started.set()
        await asyncio.Future()

    reader.read_range = blocked
    task = asyncio.create_task(catalog.read_original(lease.plan_id, 0, 5))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert catalog.active_readers == 0 and catalog.leases[lease.plan_id].remaining == 95


async def test_recipes_are_reviewable_local_records_preserve_original_and_hash(env):
    catalog, reader, _, _, _ = env
    item = await ready(env)
    before = {key: value["bytes"] for key, value in reader.objects.items()}
    draft = await recipe(
        catalog,
        item,
        [
            {"operation": "organize", "album": "SIMULATED holiday", "labels": ["SIMULATED"]},
            {"operation": "crop", "x": 0.1, "y": 0.2, "width": 0.8, "height": 0.7},
            {"operation": "rotate", "degrees_clockwise": 90},
            {"operation": "exposure", "stops": 0.5},
        ],
    )
    assert draft.status == "awaiting_review" and len(draft.operations) == 4
    reviewed = await catalog.review_recipe(
        FamilyMediaRecipeReview(
            expected_context=draft.context_fingerprint,
            recipe_id=draft.recipe_id,
            decision="reviewed",
        )
    )
    assert reviewed.status == "reviewed" and reviewed.original_hash == item.original_hash
    assert (
        not reviewed.original_mutated and not reviewed.automatic_delete and not reviewed.rendered_derivative
    )
    again = await catalog.review_recipe(
        FamilyMediaRecipeReview(
            expected_context=(await catalog.context()).fingerprint,
            recipe_id=draft.recipe_id,
            decision="reviewed",
        )
    )
    assert again == reviewed
    assert {key: value["bytes"] for key, value in reader.objects.items()} == before
    assert (await items(catalog))[0].original_hash == item.original_hash
    with pytest.raises(FamilyMediaError, match="already_reviewed"):
        await catalog.review_recipe(
            FamilyMediaRecipeReview(
                expected_context=(await catalog.context()).fingerprint,
                recipe_id=draft.recipe_id,
                decision="rejected",
            )
        )


@pytest.mark.parametrize("mutation", ["revision", "missing", "expire", "scan", "clock_backwards"])
async def test_recipe_review_rejects_changed_stale_or_expired_source(env, mutation):
    catalog, reader, now, _, _ = env
    item = await ready(env)
    draft = await recipe(catalog, item)
    if mutation == "revision":
        reader.objects["SIMULATED-photo"]["entry"]["revision"] = "SIMULATED-replaced"
    elif mutation == "missing":
        reader.objects.clear()
    elif mutation == "expire":
        now[0] += timedelta(seconds=301)
    elif mutation == "clock_backwards":
        now[0] -= timedelta(days=1)
    else:
        await scan(catalog)
    with pytest.raises(FamilyMediaError):
        await catalog.review_recipe(
            FamilyMediaRecipeReview(
                expected_context=draft.context_fingerprint,
                recipe_id=draft.recipe_id,
                decision="reviewed",
            )
        )
    assert catalog.recipes(item.item_id)[0].status == "awaiting_review"


async def test_video_only_organize_and_duplicate_operations_are_rejected(env):
    catalog, reader, _, _, _ = env
    reader.objects.clear()
    reader.put("SIMULATED-video", VIDEO, kind="video")
    item = await ready(env)
    assert (await recipe(catalog, item)).status == "awaiting_review"
    with pytest.raises(FamilyMediaError, match="image_recipe_required"):
        await recipe(catalog, item, [{"operation": "rotate", "degrees_clockwise": 90}])
    with pytest.raises(FamilyMediaError, match="duplicate_recipe_operation"):
        await recipe(
            catalog,
            item,
            [
                {"operation": "organize", "album": "SIMULATED-a"},
                {"operation": "organize", "album": "SIMULATED-b"},
            ],
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"x": 0.7, "y": 0, "width": 0.5, "height": 1},
        {"x": -0.1, "y": 0, "width": 0.5, "height": 1},
        {"x": 0, "y": 0, "width": float("nan"), "height": 1},
        {"x": 0, "y": 0, "width": 1, "height": float("inf")},
    ],
)
def test_crop_geometry_and_nonfinite_values_rejected(bad):
    with pytest.raises(ValidationError):
        FamilyMediaCrop(**bad)


@pytest.mark.parametrize("operation", ["delete", "overwrite", "export", "shell", "trim"])
def test_unimplemented_or_destructive_recipes_never_accepted(operation):
    with pytest.raises(ValidationError):
        FamilyMediaRecipeRequest(
            expected_context="a" * 64,
            item_id="b" * 64,
            expected_original_hash="c" * 64,
            operations=[{"operation": operation, "path": "/private"}],
        )


def test_path_inputs_and_schema_extras_and_scan_budgets_rejected():
    for kwargs in (
        {"directory_ref": "../../private"},
        {"limit": 129},
        {"byte_budget": 8388609},
        {"path": "/private"},
    ):
        with pytest.raises(ValidationError):
            FamilyMediaScanRequest(expected_context="a" * 64, **kwargs)
    with pytest.raises(ValidationError):
        FamilyMediaQuery(expected_context="a" * 64, limit=101)


async def test_capacity_limits_are_metadata_only_backpressure_not_original_deletion(env, monkeypatch):
    catalog, reader, _, _, _ = env
    from homeplane.family_media import catalog as module

    monkeypatch.setattr(module, "MAX_ITEMS", 1)
    await scan(catalog)
    reader.put("SIMULATED-second", PHOTO)
    with pytest.raises(FamilyMediaError, match="capacity"):
        await scan(catalog)
    assert len(reader.objects) == 2 and len(await items(catalog)) == 1
    monkeypatch.setattr(module, "MAX_ITEMS", 10)
    item = await ready(env)
    monkeypatch.setattr(module, "MAX_RECIPES", 1)
    await recipe(catalog, item)
    with pytest.raises(FamilyMediaError, match="recipe_capacity"):
        await recipe(catalog, item)


async def test_close_revokes_leases_and_never_closes_shared_nas_or_database(env):
    catalog, reader, _, _, connection = env
    item = await ready(env)
    await plan(catalog, item)
    await catalog.close()
    assert not catalog.leases and not catalog.hash_jobs
    with pytest.raises(FamilyMediaError, match="closed"):
        await catalog.context()
    assert (await reader.status())["state"] == "online"
    assert connection.execute("SELECT 1").fetchone()[0] == 1


async def test_actual_sync_status_getter_accepted_without_source_io(env):
    catalog, reader, _, _, _ = env
    reader.status = lambda: {
        "source_ref": reader.source_ref,
        "config_generation": reader.generation,
        "connection_epoch": reader.epoch,
        "state": "online",
        "reachable": True,
        "authenticated": True,
        "readable": True,
    }
    assert (await catalog.context()).source_online and reader.calls == []
    assert (await scan(catalog)).files_observed == 1


async def test_old_asset_hash_remains_addressable_after_many_original_revisions(env):
    catalog, reader, now, _, _ = env
    initial = await ready(env)
    for revision in range(12):
        reader.put("SIMULATED-photo", PHOTO + str(revision).encode(), revision=f"SIMULATED-r-{revision}")
        await scan(catalog)
        now[0] += timedelta(seconds=3)
        await scan(catalog)
    current = (await items(catalog))[0]
    assert len(current.previous_original_hashes) == 8
    historical = await catalog.asset_evidence(
        FamilyMediaAssetRequest(
            expected_context=(await catalog.context()).fingerprint,
            asset_id=initial.original_hash,
        )
    )
    assert historical.original_hash == initial.original_hash and historical.status == "stale"
    assert (
        historical.originals[0].original_ref is None
        and historical.originals[0].original_hash == initial.original_hash
    )
    assert current.original_hash != initial.original_hash


async def test_hash_history_capacity_rolls_back_metadata_without_losing_original_hash(env, monkeypatch):
    catalog, reader, _, _, _ = env
    from homeplane.family_media import catalog as module

    item = await ready(env)
    monkeypatch.setattr(module, "MAX_HISTORY", 0)
    reader.put("SIMULATED-photo", PHOTO + b"changed", revision="SIMULATED-r2")
    with pytest.raises(FamilyMediaError, match="history_capacity"):
        await scan(catalog)
    assert (await items(catalog))[0].original_hash == item.original_hash
    assert reader.objects["SIMULATED-photo"]["bytes"] == PHOTO + b"changed"


async def test_hash_aggregate_over_all_duplicates_including_beyond_first_128(env):
    catalog, reader, _, _, _ = env
    item = await ready(env)
    template = catalog._load(item.item_id)
    with catalog.connection:
        for index in range(140):
            row = dict(template)
            row["item_id"] = hashlib.sha256(f"SIMULATED-{index}".encode()).hexdigest()
            row["status"], row["reason"] = "missing", "nas_missing"
            catalog._save(row)
    evidence = await catalog.asset_evidence(
        FamilyMediaAssetRequest(
            expected_context=(await catalog.context()).fingerprint,
            asset_id=item.original_hash,
        )
    )
    assert evidence.status == "ready" and evidence.original_count == 141 and evidence.partial
    assert len(evidence.originals) == 128 and evidence.originals[0].status == "ready"
    assert reader.objects["SIMULATED-photo"]["bytes"] == PHOTO


async def test_close_cancels_and_joins_active_read_without_closing_shared_manager(env):
    catalog, reader, _, _, _ = env
    lease = await plan(catalog, await ready(env))
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    reader.read_range = blocked
    task = asyncio.create_task(catalog.read_original(lease.plan_id, 0, 5))
    await started.wait()
    await catalog.close()
    assert cancelled.is_set() and task.cancelled() and catalog.active_readers == 0 and not catalog.tasks
    assert (await reader.status())["state"] == "online"


async def test_resolve_and_scan_serialize_without_holding_core_control_lock(env):
    catalog, reader, _, _, _ = env
    item = await ready(env)
    original_stat = reader.stat
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        started.set()
        await release.wait()
        return await original_stat(*args)

    reader.stat = blocked
    task = asyncio.create_task(resolution(catalog, item))
    await started.wait()
    with pytest.raises(FamilyMediaError, match="busy"):
        await scan(catalog)
    assert (await catalog.context()).source_online
    release.set()
    assert (await task).item.status == "ready"


async def test_opt_in_http_router_synthetic_query_recipe_original_lifecycle(env):
    catalog, reader, now, _, _ = env
    app = FastAPI()
    app.include_router(create_router(catalog))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://SIMULATED"
    ) as client:
        context = (await client.get("/api/family-media/context")).json()
        result = await client.post(
            "/api/family-media/scan", json={"expected_context": context["fingerprint"]}
        )
        assert result.status_code == 200
        now[0] += timedelta(seconds=3)
        context = result.json()["context"]
        result = await client.post(
            "/api/family-media/scan", json={"expected_context": context["fingerprint"]}
        )
        context = result.json()["context"]
        page = (
            await client.post("/api/family-media/query", json={"expected_context": context["fingerprint"]})
        ).json()
        item = page["items"][0]
        prepared = await client.post(
            "/api/family-media/original-plans",
            json={
                "expected_context": page["context_fingerprint"],
                "item_id": item["item_id"],
            },
        )
        assert prepared.status_code == 200
        original_plan = prepared.json()
        returned = await client.get(original_plan["stream_url"], headers={"Range": "bytes=0-8"})
        assert returned.status_code == 206 and returned.content == PHOTO[:9]
        assert returned.headers["cache-control"] == "no-store" and returned.headers[
            "content-range"
        ].startswith("bytes 0-8/")
        assert (
            await client.get(original_plan["stream_url"], headers={"Range": "bytes=0-1,2-3"})
        ).status_code == 416
        context = (await client.get("/api/family-media/context")).json()
        drafted = await client.post(
            "/api/family-media/recipes/preview",
            json={
                "expected_context": context["fingerprint"],
                "item_id": item["item_id"],
                "expected_original_hash": item["original_hash"],
                "operations": [{"operation": "crop", "x": 0, "y": 0, "width": 0.8, "height": 0.8}],
            },
        )
        assert drafted.status_code == 200
        recipe_plan = drafted.json()
        reviewed = await client.post(
            "/api/family-media/recipes/review",
            json={
                "expected_context": recipe_plan["context_fingerprint"],
                "recipe_id": recipe_plan["recipe_id"],
                "decision": "reviewed",
            },
        )
        assert reviewed.status_code == 200 and reviewed.json()["status"] == "reviewed"
        await client.delete("/api/family-media/original-plans/" + original_plan["plan_id"])
        assert (await client.get(original_plan["stream_url"])).status_code == 404
        bad = await client.post(
            "/api/family-media/scan", json={"expected_context": "a" * 64, "path": "/private"}
        )
        assert bad.status_code == 422
    assert reader.objects["SIMULATED-photo"]["bytes"] == PHOTO


async def test_http_large_original_requires_single_bounded_range(env):
    catalog, reader, _, _, _ = env
    reader.objects.clear()
    reader.put("SIMULATED-big", b"SIMULATED" * 150000)
    item = await ready(env)
    lease = await plan(catalog, item)
    app = FastAPI()
    app.include_router(create_router(catalog))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://SIMULATED"
    ) as client:
        assert (await client.get(lease.stream_url)).status_code == 416
        response = await client.get(lease.stream_url, headers={"Range": "bytes=0-"})
        assert response.status_code == 206 and len(response.content) == RANGE_BYTES


def test_generated_schemas_match_runtime_contracts(tmp_path):
    generate(tmp_path)
    root = Path(__file__).resolve().parents[1]
    generated = sorted(tmp_path.glob("FamilyMedia*.json"))
    assert len(generated) >= 20
    for path in generated:
        expected = json.loads((root / "schemas" / path.name).read_text())
        assert json.loads(path.read_text()) == expected
        assert expected["additionalProperties"] is False
