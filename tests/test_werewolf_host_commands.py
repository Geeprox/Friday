"""Original host ASGI commands with inert output; no listener or household I/O."""

import asyncio
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from homeplane.werewolf import WerewolfGame
from homeplane.werewolf_api import Control, create_router
from homeplane.werewolf_effects import HostEffects
from homeplane.werewolf_service import Join, WerewolfService
from tests.test_werewolf import Clock, FixedRoles
from tests.test_werewolf_service import SilentLights


async def populate(service):
    await service.create(6)
    service.pump.cancel()
    await asyncio.gather(service.pump, return_exceptions=True)
    service.pump = None
    for seat in range(1, 7):
        joined = await service.join(Join(code=service.join_code, seat=seat, name=f"合成玩家{seat}"))
        await service.ready(joined["token"], True)


@asynccontextmanager
async def host(directory):
    service = WerewolfService(
        HostEffects(SilentLights()), clock=Clock(), game_factory=partial(WerewolfGame, rng=FixedRoles())
    )
    await populate(service)
    app = FastAPI()
    app.include_router(create_router(None, None, None, directory, owner=service))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host.invalid",
            headers={"Origin": "http://host.invalid"},
        ) as client:
            yield service, client
    finally:
        await service.close()


def command(service, action, event="SYNTHETIC-command-0001", **extra):
    return dict(action=action, session=service.session, revision=service.game.revision, event_id=event, **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "advance", "pause", "resume", "end", "remove", "close"])
async def test_unbound_host_commands_never_mutate_the_current_room(tmp_path, action):
    async with host(tmp_path) as (service, client):
        before = service.host()
        response = await client.post("/api/games/werewolf/control", json=dict(action=action))
        assert response.status_code == 422
        assert service.host() == before


@pytest.mark.asyncio
async def test_acknowledgement_retry_never_starts_or_advances_a_phase_twice(tmp_path):
    async with host(tmp_path) as (service, client):
        for index, action in enumerate(("start", "advance")):
            body = command(service, action, f"SYNTHETIC-command-{index:04d}")
            first = await client.post("/api/games/werewolf/control", json=body)
            assert first.status_code == 200
            after = service.game.public()
            for _ in range(3):
                retry = await client.post("/api/games/werewolf/control", json=body)
                assert retry.status_code == 200
                assert service.game.public() == after
                assert retry.json()["game"] == after


@pytest.mark.asyncio
async def test_an_old_phase_cannot_advance_or_close_the_current_phase(tmp_path):
    async with host(tmp_path) as (service, client):
        await service.control("start")
        old_advance = command(service, "advance")
        old_close = command(service, "close", "SYNTHETIC-old-close")
        await service.control("advance")
        after = service.host()
        for body in (old_advance, old_close):
            response = await client.post("/api/games/werewolf/control", json=body)
            assert response.status_code == 409 and response.json()["detail"] == "phase_changed"
            assert service.host() == after


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "advance", "end", "close", "remove"])
async def test_old_room_commands_never_affect_a_replacement_room(tmp_path, action):
    async with host(tmp_path) as (service, client):
        body = command(service, action, **({"seat": 1} if action == "remove" else {}))
        await service.close_game()
        await populate(service)
        after = service.host()
        response = await client.post("/api/games/werewolf/control", json=body)
        assert response.status_code == 409 and response.json()["detail"] == "room_changed"
        assert service.host() == after


@pytest.mark.asyncio
async def test_close_retry_survives_room_replacement_without_closing_the_new_room(tmp_path):
    async with host(tmp_path) as (service, client):
        body = command(service, "close")
        assert (await client.post("/api/games/werewolf/control", json=body)).json() == {"active": False}
        await populate(service)
        after = service.host()
        retry = await client.post("/api/games/werewolf/control", json=body)
        assert retry.status_code == 200 and retry.json() == after
        assert service.host() == after


@pytest.mark.asyncio
async def test_changed_payload_cannot_reuse_an_acknowledged_host_event(tmp_path):
    async with host(tmp_path) as (service, client):
        body = command(service, "start")
        assert (await client.post("/api/games/werewolf/control", json=body)).status_code == 200
        after = service.host()
        response = await client.post("/api/games/werewolf/control", json={**body, "action": "end"})
        assert response.status_code == 409 and response.json()["detail"] == "event_conflict"
        assert service.host() == after


@pytest.mark.asyncio
async def test_cancelled_close_keeps_a_tombstone_before_gateway_cleanup_finishes(tmp_path):
    async with host(tmp_path) as (service, client):
        entered = asyncio.Event()
        async def closing():
            entered.set()
            await asyncio.Event().wait()
        service.gateway = SimpleNamespace(close=closing)
        body = command(service, "close")
        task = asyncio.create_task(service.close_game(proof=Control(**body)))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.game is None and not service.credentials
        await populate(service)
        after = service.host()
        assert (await client.post("/api/games/werewolf/control", json=body)).json() == after
        assert service.host() == after


@pytest.mark.asyncio
async def test_host_retry_memory_is_bounded_and_evicted_old_revisions_stay_inert(tmp_path):
    async with host(tmp_path) as (service, client):
        first = command(service, "start")
        assert (await client.post("/api/games/werewolf/control", json=first)).status_code == 200
        for index in range(260):
            body = command(service, "resume" if index % 2 else "pause", f"SYNTHETIC-event-{index:04d}")
            assert (await client.post("/api/games/werewolf/control", json=body)).status_code == 200
        assert len(service.host_retries) == 256
        after = service.host()
        response = await client.post("/api/games/werewolf/control", json=first)
        assert response.status_code == 409 and service.host() == after


@pytest.mark.asyncio
async def test_camera_binding_is_game_scoped_and_deduplicated_after_acknowledgement(tmp_path):
    async with host(tmp_path) as (service, client):
        camera = "cam_1234567890"
        sample = dict(
            status="live", tracking_current=True, scene_visible=True, frame_age_ms=10,
            camera_id=camera, session_id="SYNTHETIC-camera",
            room=dict(registration=dict(status="locked"), seats=[], pointing=[]),
            people=[dict(id=7, box=dict(x=.1, y=.1, w=.3, h=.8, confidence=.95), joints={})],
        )
        service.embodied = SimpleNamespace(process=SimpleNamespace(returncode=None), request=AsyncMock(return_value=sample))
        service.camera_enabled = True
        body = dict(seat=1, camera=camera, session="SYNTHETIC-camera", track=7,
                    game_session=service.session, revision=service.game.revision, event_id="SYNTHETIC-binding-0001")
        route = "/api/games/werewolf/camera-bind"
        assert (await client.post(route, json={**body, "game_session": "SYNTHETIC-old-room"})).status_code == 409
        assert not service.bindings and service.embodied.request.await_count == 0
        response = await client.post(route, json=body)
        assert response.status_code == 200
        assert service.bindings == {1: (camera, "SYNTHETIC-camera", 7)}
        service.embodied.process.returncode = 1
        assert (await client.post(route, json=body)).status_code == 200
        assert service.embodied.request.await_count == 1
        await service.control("start")
        late = await client.post(route, json={**body, "event_id": "SYNTHETIC-binding-0002"})
        assert late.status_code == 409 and late.json()["detail"] == "phase_changed"


@pytest.mark.asyncio
async def test_closing_room_reads_inactive_while_gateway_cleanup_is_pending(tmp_path):
    async with host(tmp_path) as (service, client):
        entered, released = asyncio.Event(), asyncio.Event()
        async def closing():
            entered.set()
            await released.wait()
        service.gateway = SimpleNamespace(close=closing)
        body = command(service, "close")
        task = asyncio.create_task(service.close_game(proof=Control(**body)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            response = await client.get("/api/games/werewolf/host")
            assert response.status_code == 200 and response.json() == {"active": False}
            assert service.public() == {"active": False}
        finally:
            released.set()
            await task
