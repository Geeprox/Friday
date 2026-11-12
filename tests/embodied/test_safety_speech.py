"""Real incident/Console/Speaker paths over silent HTTP and AirPlay adapters."""

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from homeplane.embodied import airplay, safety_api, synthesis
from homeplane.embodied.safety import SafetyPolicy, SafetyRuntime, SafetyStore
from tests.embodied.test_speech_owner import rig as rig


@pytest.fixture
async def incident(rig, monkeypatch):
    store = SafetyStore(rig.private / "safety.sqlite")
    policy = store.configure(SafetyPolicy(enabled=True, announce=True))
    event = store.create(dict(kind="fall", message="SYNTHETIC fall reminder", simulated=False))
    runtime = SafetyRuntime(store, speaker=rig.speaker())
    monkeypatch.setattr(safety_api, "PRIVATE", rig.private)
    router = APIRouter()
    console = safety_api.install(router, SimpleNamespace(process=None))
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        try:
            yield SimpleNamespace(store=store, policy=policy, event=event, runtime=runtime, client=client)
        finally:
            await runtime.close()
            await console.close()


async def revoke(incident, change, monkeypatch):
    if change in {"false_alarm", "restored"}:
        response = await incident.client.post(
            "/api/embodied/safety/events/" + incident.event["id"] + "/review", json={"review": "false_alarm"}
        )
        assert response.status_code == 200 and response.json()["review"] == "false_alarm"
        if change == "restored":
            response = await incident.client.post(
                "/api/embodied/safety/events/" + incident.event["id"] + "/review",
                json={"review": "acknowledged"},
            )
            assert response.status_code == 200 and response.json()["review"] == "acknowledged"
    elif change == "policy":
        policy = incident.policy.model_copy(update={"announce": False})
        response = await incident.client.post("/api/embodied/safety/policy", json=policy.model_dump())
        assert response.status_code == 200
    elif change == "removed":
        with incident.store.connect() as db:
            db.execute("DELETE FROM safety_events WHERE id=?", (incident.event["id"],))
    elif change == "storage":

        def unavailable(*args, **kwargs):
            raise OSError("SYNTHETIC private storage failure")

        monkeypatch.setattr(incident.store, "output_current", unavailable, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["notify", "media_player", "homepod"])
@pytest.mark.parametrize("change", [None, "false_alarm", "restored", "policy", "removed", "storage"])
async def test_incident_scope_is_rechecked_after_audio_preparation(
    incident, rig, monkeypatch, transport, change
):
    entered, release = asyncio.Event(), asyncio.Event()
    stream = AsyncMock()
    if transport == "notify":
        session = json.loads(rig.session.read_text())
        rig.session.write_text(json.dumps({**session, "expires_at": 0}))

        async def hook(request):
            if request.url.path == "/auth/token":
                entered.set()
                await release.wait()

        rig.hook = hook
    else:
        rig.config["speech_output"] = transport
        rig.states.extend(["idle", "idle", "playing", "idle"])

        async def synth(text, **options):
            entered.set()
            await release.wait()
            return await rig.synthesizer(text, **options)

        monkeypatch.setattr(synthesis, "synthesize", synth)
        if transport == "homepod":
            target = rig.private / "homepod.json"
            target.write_text(json.dumps(dict(host="192.168.50.123", identifier="SYNTHETIC-homepod")))
            target.chmod(0o600)

            @asynccontextmanager
            async def receiver():
                yield SimpleNamespace(stream=SimpleNamespace(stream_file=stream))

            monkeypatch.setattr(airplay, "_receiver", receiver)
            monkeypatch.setattr(airplay, "status", lambda: dict(configured=True, dependency_available=True))
            monkeypatch.setattr(airplay, "_availability", AsyncMock(return_value="ready"))
    work = asyncio.create_task(incident.runtime.announce(incident.event, revision=incident.policy.revision))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await revoke(incident, change, monkeypatch)
        release.set()
        await asyncio.wait_for(work, 3)
        writes = sum(request.url.path.startswith("/api/services/") for request in rig.calls)
        if change is None:
            assert writes == (0 if transport == "homepod" else 1)
            assert stream.await_count == (1 if transport == "homepod" else 0)
            assert incident.store.event(incident.event["id"])["announcement"] == dict(
                status="accepted", physical_audio_confirmed=False
            )
        else:
            assert writes == 0
            stream.assert_not_awaited()
        if change not in {None, "removed"}:
            assert incident.store.event(incident.event["id"])["announcement"]["status"] == "cancelled"
        assert not rig.audio.exists()
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_withdrawal_after_external_dispatch_remains_unconfirmed(incident, rig):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(request):
        if request.url.path == "/api/services/notify/send_message":
            entered.set()
            await release.wait()

    rig.hook = hook
    work = asyncio.create_task(incident.runtime.announce(incident.event, revision=incident.policy.revision))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await incident.client.post(
            "/api/embodied/safety/events/" + incident.event["id"] + "/review", json={"review": "false_alarm"}
        )
        assert response.status_code == 200
        release.set()
        await asyncio.wait_for(work, 3)
        saved = incident.store.event(incident.event["id"])
        assert saved["announcement"] == dict(status="unconfirmed", physical_audio_confirmed=False)
        assert sum(request.url.path.startswith("/api/services/") for request in rig.calls) == 1
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_pending_event_read_does_not_block_camera_observation(incident, rig, monkeypatch):
    await incident.runtime.sync_policy()
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = incident.store.output_current

    def held(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(incident.store, "output_current", held)
    work = asyncio.create_task(incident.runtime.announce(incident.event, revision=incident.policy.revision))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        started = time.perf_counter()
        for _ in range(100):
            now = time.monotonic()
            incident.runtime.offer("SYNTHETIC camera", "session", [], now, now, stable=True)
        await asyncio.sleep(0.01)
        assert time.perf_counter() - started < 0.15
        assert not work.done() and not rig.calls
        assert len(incident.runtime.pending) <= 1
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_event_read_keeps_output_owned_until_storage_finishes(incident, rig, monkeypatch):
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = incident.store.output_current

    def held(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(incident.store, "output_current", held)
    work = asyncio.create_task(incident.runtime.announce(incident.event, revision=incident.policy.revision))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        work.cancel()
        await asyncio.sleep(0.02)
        work.cancel()
        await asyncio.sleep(0.02)
        assert (await rig.speaker().say("SYNTHETIC next reminder"))["status"] == "busy"
        assert not rig.calls
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
    await incident.runtime.io.drain()
    await asyncio.sleep(0.02)
    assert not rig.calls, "The retired event cannot send after its storage read returns"
    assert (await rig.speaker().say("SYNTHETIC next reminder"))["status"] == "accepted"
