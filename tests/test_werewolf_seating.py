"""Correct mistaken seats through original host API without releasing players."""

import httpx
import pytest

from homeplane.werewolf_service import phone_app
from tests.test_werewolf_camera_view import host


def binding(party, seat, track, event):
    return dict(seat=seat, camera='cam_1234567890', session='SYNTHETIC', track=track,
                game_session=party.session, revision=party.game.revision, event_id=event)


async def bind(client, party, seat, track, event):
    response = await client.post('/api/games/werewolf/camera-bind', json=binding(party, seat, track, event))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_swapped_seats_can_be_corrected_without_removing_players(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, state, _, client):
        players, credentials = party.game.public()['players'], dict(party.credentials)
        await bind(client, party, 1, 7, 'SYNTHETIC-first-binding')
        await bind(client, party, 2, 8, 'SYNTHETIC-second-binding')
        request = binding(party, 2, 8, 'SYNTHETIC-clear-second')
        calls = len(state.calls)
        response = await client.post('/api/games/werewolf/camera-unbind', json=request)
        assert response.status_code == 200
        assert len(state.calls) == calls, 'releasing an association must not query or start a camera'
        assert 2 not in party.bindings
        await bind(client, party, 1, 8, 'SYNTHETIC-correct-first')
        final = await bind(client, party, 2, 7, 'SYNTHETIC-correct-second')
        assert final['camera_bindings'] == [dict(seat=1, camera='cam_1234567890', session='SYNTHETIC', track=8),
                                            dict(seat=2, camera='cam_1234567890', session='SYNTHETIC', track=7)]
        assert party.game.public()['players'] == players and party.credentials == credentials
        assert party.game.phase == 'lobby' and not party.game.choices


@pytest.mark.asyncio
async def test_stale_clear_cannot_erase_the_same_track_after_a_clear_and_rebind(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, camera, _, _, client):
        before = party.game.revision
        await bind(client, party, 1, 7, 'SYNTHETIC-before-clear')
        assert party.game.revision == before + 1
        stale = binding(party, 1, 7, 'SYNTHETIC-held-clear')
        clear = binding(party, 1, 7, 'SYNTHETIC-current-clear')
        assert (await client.post('/api/games/werewolf/camera-unbind', json=clear)).status_code == 200
        await bind(client, party, 1, 7, 'SYNTHETIC-rebound-track')
        report = party.host()
        rejected = await client.post('/api/games/werewolf/camera-unbind', json=stale)
        assert rejected.status_code == 409 and rejected.json()['detail'] == 'phase_changed'
        assert party.host() == report
        # Acknowledged retry is inert even after another mapping exists.
        assert (await client.post('/api/games/werewolf/camera-unbind', json=clear)).json() == report
        camera.process.returncode = 1
        fresh = binding(party, 1, 7, 'SYNTHETIC-offline-clear')
        assert (await client.post('/api/games/werewolf/camera-unbind', json=fresh)).status_code == 200
        assert not party.bindings


@pytest.mark.asyncio
async def test_event_id_cannot_be_reused_for_the_opposite_binding_action(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, _, _, client):
        body = binding(party, 1, 7, 'SYNTHETIC-same-event')
        assert (await client.post('/api/games/werewolf/camera-bind', json=body)).status_code == 200
        response = await client.post('/api/games/werewolf/camera-unbind', json=body)
        assert response.status_code == 409 and response.json()['detail'] == 'event_conflict'
        assert party.bindings[1][2] == 7


@pytest.mark.asyncio
async def test_unchanged_binding_does_not_advance_the_room_version_again(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, _, _, client):
        first = await bind(client, party, 1, 7, 'SYNTHETIC-original-bind')
        same = await bind(client, party, 1, 7, 'SYNTHETIC-same-mapping')
        assert first['game']['revision'] == same['game']['revision']


@pytest.mark.asyncio
async def test_unbinding_needs_its_exact_current_mapping_and_current_lobby(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, state, _, client):
        await bind(client, party, 1, 7, 'SYNTHETIC-initial-bind')
        original = dict(party.bindings)
        wrong = binding(party, 1, 8, 'SYNTHETIC-wrong-mapping')
        response = await client.post('/api/games/werewolf/camera-unbind', json=wrong)
        assert response.status_code == 409 and response.json()['detail'] == 'camera_binding_changed'
        assert party.bindings == original
        body = binding(party, 1, 7, 'SYNTHETIC-origin-test')
        assert (await client.post('/api/games/werewolf/camera-unbind', json=body,
                                 headers={'Origin': 'http://elsewhere.invalid'})).status_code == 403
        calls = len(state.calls)
        await party.control('start')
        late = binding(party, 1, 7, 'SYNTHETIC-after-start')
        response = await client.post('/api/games/werewolf/camera-unbind', json=late)
        assert response.status_code == 409 and party.bindings == original and len(state.calls) == calls


@pytest.mark.asyncio
async def test_old_room_or_private_phone_cannot_clear_host_seats(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, _, _, client):
        await bind(client, party, 1, 7, 'SYNTHETIC-mapped-player')
        old = binding(party, 1, 7, 'SYNTHETIC-old-room-clear')
        old['game_session'] = 'SYNTHETIC-retired-room'
        assert (await client.post('/api/games/werewolf/camera-unbind', json=old)).status_code == 409
        app = phone_app(party, 'http://phone.invalid')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://phone.invalid',
                                    headers={'Origin': 'http://phone.invalid'}) as phone:
            assert (await phone.post('/api/games/werewolf/camera-unbind', json=old)).status_code == 404
        assert party.bindings[1][2] == 7


@pytest.mark.asyncio
async def test_live_empty_observation_is_distinct_from_waiting_for_the_camera(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, state, clock, _):
        await party._camera()
        assert party.host()['camera_available'] is True
        state.report['people'] = []
        await party._camera()
        assert party.host()['camera_available'] is True and party.host()['camera_candidates'] == []
        clock.now += 1.1
        assert party.host()['camera_available'] is False and party.host()['camera_candidates'] == []
