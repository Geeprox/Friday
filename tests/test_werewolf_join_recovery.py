"""Original LAN ASGI surface, inert gateway/effects; no listener or devices."""

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from homeplane.werewolf_service import phone_app
from tests.test_werewolf_service import owner


def attempt(service, *, seat=1, marker="1"):
    return dict(
        code=service.join_code,
        session=service.session,
        seat=seat,
        name=f"SYNTHETIC {seat}",
        attempt_id=marker * 64,
    )


@pytest.mark.asyncio
async def test_lost_join_reply_can_recover_the_same_private_credential_once():
    service = await owner()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=phone_app(service, service.origin)), base_url=service.origin
        ) as client:
            payload = attempt(service)
            first = await client.post("/api/join", json=payload)
            assert first.status_code == 200
            revision = service.game.revision
            retry = await client.post("/api/join", json=payload)
            assert retry.status_code == 200
            assert retry.json()["token"] == first.json()["token"]
            assert retry.json()["session"] == service.session
            assert payload["attempt_id"] not in str(service.join_retries)
            assert first.json()["token"] not in str(service.join_retries)
            assert (
                service.game.revision == revision
                and len(service.game.players) == len(service.credentials) == 1
            )
            private = await client.get(
                "/api/player", headers={"Authorization": "Bearer " + retry.json()["token"]}
            )
            assert private.status_code == 200 and private.json()["seat"] == 1
            public = service.public()
            assert payload["attempt_id"] not in str(public) and first.json()["token"] not in str(public)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_recovery_cannot_change_seat_or_reclaim_a_removed_player():
    service = await owner()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=phone_app(service, service.origin)), base_url=service.origin
        ) as client:
            payload = attempt(service)
            assert (await client.post("/api/join", json=payload)).status_code == 200
            conflict = await client.post("/api/join", json={**payload, "seat": 2})
            assert conflict.status_code == 409 and conflict.json()["detail"] == "event_conflict"
            other_phone = await client.post("/api/join", json={**payload, "attempt_id": "2" * 64})
            assert other_phone.status_code == 409 and other_phone.json()["detail"] == "seat_unavailable"
            assert len(service.credentials) == 1
            await service.control("remove", seat=1)
            revoked = await client.post("/api/join", json=payload)
            assert revoked.status_code == 403
            assert not service.game.players and not service.credentials
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_an_old_room_attempt_cannot_join_a_new_room_with_the_same_short_code():
    service = await owner()
    try:
        payload = attempt(service)
        await service.close_game()
        await service.create(6, address="192.168.50.20")
        service.join_code = payload["code"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=phone_app(service, service.origin)), base_url=service.origin
        ) as client:
            response = await client.post("/api/join", json=payload)
            assert response.status_code == 409 and not service.game.players
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_original_phone_can_reload_a_lost_committed_join(tmp_path):
    service = await owner()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=phone_app(service, service.origin)), base_url=service.origin
        ) as client:
            command = attempt(service, marker="0")
            command["attempt_id"] = "01" * 32
            lobby = (await client.post("/api/lobby", json={"code": service.join_code})).json()
            assert lobby["session"] == service.session
            joined = await client.post("/api/join", json=command)
            assert joined.status_code == 200
            revision = service.game.revision
            retried = await client.post("/api/join", json=command)
            assert retried.status_code == 200 and service.game.revision == revision
            packet = tmp_path / "SYNTHETIC-join.json"
            packet.write_text(json.dumps(dict(lobby=lobby, command=command, retried=retried.json())))
            result = subprocess.run(
                ["node", "tests/werewolf_join_fixture.cjs", str(packet)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=12,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert all(json.loads(result.stdout).values())
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_twelve_phones_behind_one_address_share_a_bounded_verified_room_budget():
    service = await owner()
    try:
        await service.close_game()
        await service.create(12, address="192.168.50.20")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=phone_app(service, service.origin)), base_url=service.origin
        ) as client:
            for seat in range(1, 13):
                payload = attempt(service, seat=seat)
                payload["attempt_id"] = f"{seat:064x}"
                first = await client.post("/api/join", json=payload)
                replay = await client.post("/api/join", json=payload)
                assert first.status_code == replay.status_code == 200
                assert first.json()["token"] == replay.json()["token"]
            assert len(service.game.players) == len(service.credentials) == 12
            for _ in range(6):
                assert (await client.post("/api/join", json=payload)).status_code == 200
            assert (await client.post("/api/join", json=payload)).status_code == 429
            for _ in range(10):
                assert (await client.post("/api/join", json={**payload, "code": "000000"})).status_code == 409
            assert (await client.post("/api/join", json={**payload, "code": "000000"})).status_code == 429
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", [{"session": None}, {"attempt_id": True}, {"attempt_id": "1" * 63}, {"session": "x" * 32}]
)
async def test_invalid_recovery_context_cannot_fall_back_to_an_unscoped_join(bad):
    service = await owner()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=phone_app(service, service.origin)), base_url=service.origin
        ) as client:
            response = await client.post("/api/join", json={**attempt(service), **bad})
            assert response.status_code == 422 and not service.game.players and not service.credentials
    finally:
        await service.close()


def test_original_phone_and_host_state_regressions():
    result = subprocess.run(
        [
            "node",
            "--test",
            "tests/werewolf_session.test.cjs",
            "tests/werewolf_host_state.test.cjs",
            "tests/werewolf_camera_view.test.cjs",
            "tests/werewolf_host_effects.test.cjs",
            "tests/werewolf_script.test.cjs",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
