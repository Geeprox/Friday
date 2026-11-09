"""Real local UTF-8 parser/commit/search over synthetic bounded reader bytes only."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from homeplane.family_media import FamilyMediaCatalog
from homeplane.family_media.contracts import FamilyMediaQuery, FamilyMediaScanRequest
from homeplane.family_media.fixture import SyntheticNASReader
from homeplane.file_steward import FileSteward, StewardError
from homeplane.file_steward.api import create_router
from homeplane.file_steward.extraction_queue import StewardQueueControl
from homeplane.file_steward.local_text import SelectedTextPreparation, SelectedTextRequest
from homeplane.file_steward.settings_contracts import StewardSettingsRequest


@pytest.fixture
async def local_env(tmp_path):
    created = []

    async def make(content, *, enabled=True, copies=False):
        reader = SyntheticNASReader()
        reader.put("selected-text", content, kind="other")
        if copies:
            reader.put("separate-copy", content, kind="other")
        now = [datetime(2026, 11, 5, tzinfo=UTC)]
        connection = sqlite3.connect(tmp_path / f"local-{len(created)}.sqlite")
        catalog = FamilyMediaCatalog(connection, reader, simulated=True, clock=lambda: now[0],
                                     indexed_kinds=("other",))
        for _ in range(2):
            await catalog.scan(FamilyMediaScanRequest(
                expected_context=(await catalog.context()).fingerprint, directory_ref="selected-directory",
            ))
            now[0] += timedelta(seconds=3)
        items = (await catalog.list_items(FamilyMediaQuery(
            expected_context=(await catalog.context()).fingerprint,
        ))).items
        steward = FileSteward(connection, catalog, clock=lambda: now[0], local_text_enabled=enabled)
        reader.calls.clear()
        created.append((connection, catalog))
        return steward, catalog, reader, items

    yield make
    for connection, catalog in created:
        await catalog.close()
        connection.close()


async def request_for(steward, catalog, item):
    expected = (await catalog.context()).fingerprint
    approval = await steward.settings.query(StewardSettingsRequest(
        expected_context=expected, sample_item_id=item.item_id,
    ))
    return SelectedTextRequest(expected_context=expected, item_id=item.item_id,
                               expected_content_id=item.original_hash,
                               expected_settings_fingerprint=approval.fingerprint)


async def test_selected_http_read_commits_and_literal_search_keeps_exact_original(local_env):
    content = ("合成课程标题\n" + "甲" * 4100 + "可检索的宇宙课程\n").encode()
    steward, catalog, reader, items = await local_env(content, copies=True)
    app = FastAPI()
    app.include_router(create_router(steward))
    request = await request_for(steward, catalog, items[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
        report = (await client.get("/api/files/context")).json()
        assert report["content_extraction_enabled"] and not report["executor_enabled"]
        assert [p["key"] for p in report["providers"] if p["enabled"]] == ["local_utf8_text"]
        assert not reader.calls  # Browsing/settings are not source-read approval.
        response = await client.post("/api/files/content/text", json=request.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "done"
        result = (await client.post("/api/files/query", json={
            "expected_context": request.expected_context, "query": "宇宙课程", "search_mode": "indexed_cache",
        })).json()
    row = result["items"][0]
    assert row["item"]["item_id"] == items[0].item_id
    assert row["references"][0]["anchor"]["original_ref"] == items[0].original_ref.model_dump(mode="json")
    assert row["references"][0]["ordinal"] == 1  # A later matching chunk, not the first unrelated text.
    assert "宇宙课程" in row["references"][0]["text"]
    assert not result["coverage"]["source_read_enabled"]
    reads = [call for call in reader.calls if call[0] == "range"]
    assert reads == [("range", items[0].original_ref.object_ref, 0, len(content))]
    assert not catalog.leases and steward.queue.records()[0].status == "done"
    calls = list(reader.calls)
    await steward.local_extraction.start(request)
    assert reader.calls == calls  # Idempotent completed selection does not reread.
    second = await steward.local_extraction.start(await request_for(steward, catalog, items[1]))
    assert second.job_id != response.json()["job_id"]
    assert second.original_ref == items[1].original_ref  # Equal bytes cannot borrow another original's grant.


@pytest.mark.parametrize("change,code", [
    ({"expected_context": "0" * 64}, "context_changed"),
    ({"expected_content_id": "0" * 64}, "stale_fingerprint"),
    ({"expected_settings_fingerprint": "0" * 64}, "settings_changed"),
])
async def test_selection_rejects_stale_scope_before_content_io(local_env, change, code):
    steward, catalog, reader, items = await local_env(b"Selected synthetic text")
    request = (await request_for(steward, catalog, items[0])).model_copy(update=change)
    with pytest.raises(StewardError, match=code):
        await steward.local_extraction.start(request)
    assert not reader.calls and not catalog.leases


@pytest.mark.parametrize("content,code", [
    (b"%PDF-1.7\nASCII container is not plain text", "format_unsupported"),
    (b"\xff\xfeinvalid UTF-8", "format_unsupported"),
    (b"binary\x00data", "format_unsupported"),
    (b" \n\t", "text_empty"),
    (b"x" * 32001, "output_limit"),
    (b"x" * 131073, "source_limit"),
])
async def test_binary_empty_and_oversize_never_create_truncated_index(local_env, content, code):
    steward, catalog, reader, items = await local_env(content)
    request = await request_for(steward, catalog, items[0])
    with pytest.raises(StewardError, match=code):
        await steward.local_extraction.start(request)
    assert not catalog.leases
    assert not steward.connection.execute("SELECT 1 FROM steward_units").fetchone()
    assert all(entry.status != "claimed" for entry in steward.queue.records())
    if code == "source_limit":
        assert not reader.calls


async def test_changed_bytes_are_not_indexed_even_if_transport_reuses_metadata_revision(local_env):
    steward, catalog, reader, items = await local_env(b"Original synthetic text")
    reader.objects["selected-text"]["bytes"] = b"Changed! synthetic text"
    with pytest.raises(StewardError, match="stale_fingerprint"):
        await steward.local_extraction.start(await request_for(steward, catalog, items[0]))
    assert not steward.connection.execute("SELECT 1 FROM steward_units").fetchone()
    assert not catalog.leases and steward.jobs()[0].charged_source_bytes > 0


async def test_cancel_and_new_source_epoch_cannot_commit_late_read(local_env):
    steward, catalog, reader, items = await local_env(b"Cancellation fixture text")
    request = await request_for(steward, catalog, items[0])
    entered, release = asyncio.Event(), asyncio.Event()
    read = reader.read_range

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await read(*args)

    reader.read_range = delayed
    running = asyncio.create_task(steward.local_extraction.start(request))
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(StewardError, match="busy"):
        await steward.local_extraction.start(request)
    entry = steward.queue.records()[0]
    await steward.queue.control(StewardQueueControl(
        expected_context=request.expected_context, job_id=entry.job_id, expected_revision=entry.revision,
        expected_settings_fingerprint=entry.settings_fingerprint, decision="cancel",
    ))
    with pytest.raises(asyncio.CancelledError):
        await running
    assert steward.queue.records()[0].status == "cancelled"
    assert not catalog.leases and not steward.connection.execute("SELECT 1 FROM steward_units").fetchone()
    reader.read_range = read
    reader.epoch = "SIMULATED-reconnected-epoch"
    with pytest.raises(StewardError, match="context_changed"):
        await steward.local_extraction.start(request)


async def test_disabled_host_has_no_content_execution_port(local_env):
    steward, catalog, reader, items = await local_env(b"No implicit read", enabled=False)
    request = await request_for(steward, catalog, items[0])
    app = FastAPI()
    app.include_router(create_router(steward))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
        response = await client.post("/api/files/content/text", json=request.model_dump(mode="json"))
    assert response.status_code == 409
    assert not reader.calls


async def test_selected_metadata_refresh_recovers_expiry_without_implicit_content_read(local_env):
    steward, catalog, reader, items = await local_env(b"Selected text after sixty seconds")
    request = await request_for(steward, catalog, items[0])
    old_clock = catalog.clock
    catalog.clock = lambda: old_clock() + timedelta(seconds=61)
    with pytest.raises(StewardError, match="source_recheck_required"):
        await steward.local_extraction.start(request)
    assert not reader.calls
    prepared = await steward.local_extraction.prepare(SelectedTextPreparation(
        expected_context=request.expected_context, item_id=request.item_id,
        expected_content_id=request.expected_content_id,
    ))
    assert reader.calls and all(call[0] == "stat" for call in reader.calls)
    assert not steward.jobs() and not catalog.leases
    assert prepared.expected_context != request.expected_context
    assert (await steward.local_extraction.start(prepared)).status == "done"
