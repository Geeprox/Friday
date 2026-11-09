"""Interrupted selected-file reads recover without another service restart."""

import asyncio
import sqlite3
import time
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI

from homeplane.file_steward import FileSteward, StewardError
from homeplane.file_steward.api import create_router
from homeplane.file_steward.contracts import StewardJobRequest
from tests.test_file_steward_local_text import local_env as local_env
from tests.test_file_steward_local_text import request_for


async def interrupted_owner(local_env):
    owner, catalog, reader, items = await local_env("中断后仍可检索的课程".encode())
    request = await request_for(owner, catalog, items[0])
    job = (await owner.plan_jobs(StewardJobRequest(
        expected_context=request.expected_context, item_ids=[items[0].item_id], provider="local_utf8_text",
    )))[0]
    entry = owner.queue.records()[0]
    claimed = owner.queue._claim(entry, reason="local_text_running")
    with owner.connection:
        owner._save_job(job.model_copy(update={
            "status": "running", "attempts": 1, "charged_source_bytes": entry.budget.source_bytes,
        }))
    path = owner.connection.execute("PRAGMA database_list").fetchone()[2]
    connection = sqlite3.connect(path)
    now = [owner.clock()]
    restarted = FileSteward(connection, catalog, clock=lambda: now[0], local_text_enabled=True)
    assert restarted.jobs()[0].status == "running"
    assert restarted.queue.records()[0].status == "claimed"
    reader.calls.clear()
    return restarted, connection, catalog, reader, items[0], now, claimed


@pytest.mark.asyncio
async def test_expired_claim_in_restarted_service_can_index_on_new_explicit_request(local_env):
    owner, connection, catalog, reader, item, now, claimed = await interrupted_owner(local_env)
    try:
        # A different owner must not steal the still-current lease.
        request = await request_for(owner, catalog, item)
        with pytest.raises(StewardError, match="queue_busy"):
            await owner.local_extraction.start(request)
        assert not [call for call in reader.calls if call[0] == "range"]
        now[0] = claimed.lease_expires_at + timedelta(seconds=1)
        request = await request_for(owner, catalog, item)
        app = FastAPI()
        app.include_router(create_router(owner))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            response = await client.post("/api/files/content/text", json=request.model_dump(mode="json"))
            assert response.status_code == 200, response.text
            result = owner.jobs()[0]
            search = await client.post("/api/files/query", json={
                "expected_context": request.expected_context, "query": "课程", "search_mode": "indexed_cache",
            })
        assert result.status == "done" and search.status_code == 200
        match = search.json()["items"][0]
        assert match["item"]["item_id"] == item.item_id
        assert match["references"][0]["anchor"]["original_ref"] == item.original_ref.model_dump(mode="json")
        assert result.attempts == 2 and result.charged_source_bytes == 2 * claimed.budget.source_bytes
        assert owner.queue.records()[0].status == "done" and not catalog.leases
        assert len(owner.units(result.job_id)) == 1
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_context_recovery_updates_job_and_queue_without_reading_original(local_env):
    owner, connection, catalog, reader, item, now, claimed = await interrupted_owner(local_env)
    try:
        now[0] = claimed.lease_expires_at + timedelta(seconds=1)
        app = FastAPI()
        app.include_router(create_router(owner))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            response = await client.get("/api/files/context")
        assert response.status_code == 200
        assert response.json()["queue"][0]["status"] == "paused"
        job = owner.jobs()[0]
        assert job.status == "paused" and job.reason == "interrupted_resume_available"
        assert job.attempts == 1 and job.charged_source_bytes == claimed.budget.source_bytes
        assert not [call for call in reader.calls if call[0] == "range"]
        assert not catalog.leases
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_late_old_read_cannot_overwrite_recovered_selected_file(local_env):
    old, catalog, reader, items = await local_env("恢复后只保留一份课程索引".encode())
    now = [old.clock()]
    old.clock = lambda: now[0]
    request = await request_for(old, catalog, items[0])
    entered, release = asyncio.Event(), asyncio.Event()
    read_range = reader.read_range
    first = [True]

    async def delayed(*args, **kwargs):
        value = await read_range(*args, **kwargs)
        if first[0]:
            first[0] = False
            entered.set()
            await release.wait()
        return value

    reader.read_range = delayed
    work = asyncio.create_task(old.local_extraction.start(request))
    connection = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        claimed = old.queue.records()[0]
        connection = sqlite3.connect(old.connection.execute("PRAGMA database_list").fetchone()[2])
        current = FileSteward(connection, catalog, clock=lambda: now[0], local_text_enabled=True)
        now[0] = claimed.lease_expires_at + timedelta(seconds=1)
        result = await current.local_extraction.start(await request_for(current, catalog, items[0]))
        before = (current.jobs(), current.queue.records(), current.units(result.job_id))
        assert result.status == "done" and len(before[2]) == 1
        release.set()
        with pytest.raises(StewardError):
            await asyncio.wait_for(work, 1)
        assert (current.jobs(), current.queue.records(), current.units(result.job_id)) == before
        assert not catalog.leases and not old.queue.active_tasks
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
        if connection:
            connection.close()


@pytest.mark.asyncio
async def test_status_defers_locked_recovery_without_blocking_or_changing_timeout(local_env):
    owner, connection, catalog, reader, item, now, claimed = await interrupted_owner(local_env)
    writer = sqlite3.connect(connection.execute("PRAGMA database_list").fetchone()[2])
    try:
        now[0] = claimed.lease_expires_at + timedelta(seconds=1)
        connection.execute("PRAGMA busy_timeout=600")
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        await owner.context()
        assert time.monotonic() - started < .2, "status read waited for another writer"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 600
        assert owner.jobs()[0].status == "running"
        assert owner.queue.records()[0].status == "claimed"
        assert not [call for call in reader.calls if call[0] == "range"]
        writer.rollback()
        await owner.context()
        assert owner.jobs()[0].status == owner.queue.records()[0].status == "paused"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 600
    finally:
        writer.close()
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["job", "queue"])
async def test_recovery_failure_does_not_split_job_and_queue_state(local_env, monkeypatch, failure):
    owner, connection, catalog, reader, item, now, claimed = await interrupted_owner(local_env)
    try:
        now[0] = claimed.lease_expires_at + timedelta(seconds=1)
        before_job = owner.jobs()[0]
        before_queue = owner.queue.records()[0]

        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic unavailable write")

        monkeypatch.setattr(owner if failure == "job" else owner.queue,
                            "_save_job" if failure == "job" else "_replace", unavailable)
        with pytest.raises(sqlite3.OperationalError):
            owner.queue.recover()
        assert owner.jobs()[0] == before_job and owner.queue.records()[0] == before_queue
        assert not reader.calls
    finally:
        connection.close()
