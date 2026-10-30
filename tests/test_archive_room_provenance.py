"""Reviewed room mappings over synthetic HA registry and temporary NAS fixtures."""

import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from homeplane.archive_contracts import ArchiveCameraEdit
from homeplane.archive_index import ArchiveIndex
from homeplane.event_provenance import registered_room_context
from homeplane.simulator import Simulator
from tests.test_event_provenance import ingress
from tests.test_homeplane_archive_index import query, setup
from tests.test_homeplane_ha_stream import Server


async def review(index, *, room='Simulated Room', camera_id=None):
    context = await index.scan(index.context()['fingerprint'])
    return index.save_camera(ArchiveCameraEdit(
        expected_context=context['fingerprint'], folder_ref=context['folders'][0]['folder_ref'],
        camera_id=camera_id, name='SYNTHETIC reviewed camera', room=room, timing_reviewed=True,
        clock_offset_seconds=0, clock_uncertainty_seconds=0,
        completion_convention='closed_files_operator_reviewed'))


def stored(store):
    return store.connection.execute('SELECT body FROM archive_profiles ORDER BY id').fetchone()[0]


@pytest.mark.asyncio
async def test_registered_room_lookup_is_unique_and_metadata_only():
    server, simulator = Server(), Simulator()
    ha = ingress(server)
    try:
        await ha.snapshot()
        requests, sockets = len(server.requests), len(server.sessions)
        room = registered_room_context(ha, 'Simulated Room')
        assert re.fullmatch(r'[a-f0-9]{64}', room)
        assert registered_room_context(ha, 'SIMULATED-area') == room
        assert registered_room_context(ha, 'simulated room') is None
        assert registered_room_context(ha, 'unknown-room') is None
        assert len(server.requests) == requests and len(server.sessions) == sockets
        ha._areas['SYNTHETIC-duplicate'] = {'area_id': 'SYNTHETIC-duplicate', 'name': 'Simulated Room'}
        assert registered_room_context(ha, 'Simulated Room') is None
        assert registered_room_context(ha, 'SIMULATED-area') == room
        ha._areas['SYNTHETIC-duplicate']['name'] = 'SIMULATED-area'
        assert registered_room_context(ha, 'SIMULATED-area') is None
        ha._registry_ok = False
        assert registered_room_context(ha, 'SIMULATED-area') is None
        assert registered_room_context(SimpleNamespace(), 'Simulated Room') is None
        assert registered_room_context(simulator, 'living-room')
        assert registered_room_context(simulator, 'living room') is None
    finally:
        await ha.close()


@pytest.mark.asyncio
async def test_explicit_camera_room_binding_survives_new_ha_boot_without_rewriting(setup):
    index, _, _, store, settings, current, _ = setup
    server, active = Server(), []
    first = ingress(server)
    await first.snapshot()
    active.append(first)
    index.room_context = lambda room: registered_room_context(active[0], room)
    try:
        context = await review(index)
        camera = context['cameras'][0]
        assert camera['source_binding_current'] is True and camera['room_binding_current'] is True
        saved = stored(store)
        await first.close()
        second = ingress(server)
        active[0] = second
        await second.snapshot()
        assert first._boot != second._boot
        assert index.context()['fingerprint'] == context['fingerprint']
        assert index.all_profiles()[0]['room_binding_current'] is True
        assert stored(store) == saved
        # The persistent profile also qualifies in a new index lifetime.
        reopened = ArchiveIndex(store, settings, lambda: False, runner=index.runner,
                                clock=lambda: current[0], room_context=index.room_context)
        try:
            assert reopened.all_profiles()[0]['room_binding_current'] is True
            assert stored(store) == saved
        finally:
            await reopened.close()
    finally:
        await active[0].close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['instance', 'endpoint', 'area_identity', 'area_missing', 'ambiguous_name'])
async def test_same_room_name_cannot_reuse_old_ha_mapping(change, setup):
    index, _, _, store, _, _, _ = setup
    server = Server()
    first = ingress(server)
    await first.snapshot()
    active = [first]
    index.room_context = lambda room: registered_room_context(active[0], room)
    context = await review(index)
    saved = stored(store)
    await first.close()
    options = {}
    if change == 'instance':
        options['instance'] = 'SYNTHETIC-other-instance'
    elif change == 'endpoint':
        options['url'] = 'https://another-simulated.invalid'
    elif change == 'area_identity':
        server.registry['config/area_registry/list'][0]['area_id'] = 'SYNTHETIC-other-area'
        server.registry['config/device_registry/list'][0]['area_id'] = 'SYNTHETIC-other-area'
    elif change == 'area_missing':
        server.registry['config/area_registry/list'].clear()
    elif change == 'ambiguous_name':
        server.registry['config/area_registry/list'].append({'area_id': 'SYNTHETIC-duplicate', 'name': 'Simulated Room'})
    second = ingress(server, **options)
    active[0] = second
    try:
        await second.snapshot()
        changed = index.context()
        assert changed['cameras'][0]['source_binding_current'] is True
        assert changed['cameras'][0]['room_binding_current'] is False
        assert changed['fingerprint'] != context['fingerprint']
        assert stored(store) == saved
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_legacy_profile_stays_unqualified_until_explicit_review(setup):
    index, _, _, store, _, _, _ = setup
    server = Server()
    ha = ingress(server)
    await ha.snapshot()
    index.room_context = lambda room: registered_room_context(ha, room)
    try:
        first = await review(index)
        identifier = first['cameras'][0]['id']
        body = json.loads(stored(store))
        body.pop('room_source_context')
        # Old/imported files may contain presentation flags; they are not proof.
        body['room_binding_current'] = True
        legacy = json.dumps(body)
        with store.connection:
            store.connection.execute('UPDATE archive_profiles SET body=? WHERE id=?', (legacy, identifier))
        assert index.all_profiles()[0]['room_binding_current'] is False
        assert index.context()['cameras'][0]['room_binding_current'] is False
        assert stored(store) == legacy
        reviewed = await review(index, camera_id=identifier)
        assert reviewed['cameras'][0]['room_binding_current'] is True
        assert json.loads(stored(store))['room_source_context'] == registered_room_context(ha, 'Simulated Room')
    finally:
        await ha.close()


@pytest.mark.asyncio
async def test_room_change_without_review_does_not_inherit_old_room_evidence(setup):
    index, _, _, store, _, _, _ = setup
    server = Server()
    server.registry['config/area_registry/list'].append({'area_id': 'SYNTHETIC-other-area', 'name': 'SYNTHETIC other room'})
    ha = ingress(server)
    await ha.snapshot()
    index.room_context = lambda room: registered_room_context(ha, room)
    try:
        first = await review(index)
        identifier = first['cameras'][0]['id']
        body = json.loads(stored(store))
        body['room'] = 'SYNTHETIC other room'
        with store.connection:
            store.connection.execute('UPDATE archive_profiles SET body=? WHERE id=?', (json.dumps(body), identifier))
        assert index.context()['cameras'][0]['room_binding_current'] is False
        reviewed = await review(index, room='SYNTHETIC other room', camera_id=identifier)
        assert reviewed['cameras'][0]['room_binding_current'] is True
    finally:
        await ha.close()


@pytest.mark.asyncio
async def test_missing_or_failed_room_callback_keeps_standalone_nas_readback_available(setup):
    index, _, _, store, _, current, _ = setup
    assert index.room_context is None
    await review(index)
    saved = stored(store)
    current[0] += timedelta(seconds=3)
    assert index.context()['cameras'][0]['room_binding_current'] is False
    assert (await index.resolve(query(index))).status == 'ready'
    for invalid in (lambda _room: 'not-a-digest', lambda _room: True):
        index.room_context = invalid
        assert index.context()['cameras'][0]['room_binding_current'] is False
        assert stored(store) == saved
    def failed(_room):
        raise RuntimeError('SYNTHETIC-private-source-detail')
    index.room_context = failed
    assert index.context()['cameras'][0]['room_binding_current'] is False
    assert 'SYNTHETIC-private-source-detail' not in json.dumps(index.context())
    assert (await index.resolve(query(index))).status == 'ready'


def test_simulated_camera_declares_its_binding_and_context_tracks_callback_changes(setup):
    index, _, _, _, _, _, calls = setup
    index.simulated = lambda: True
    simulator = Simulator()
    index.room_context = lambda room: registered_room_context(simulator, room)
    first = index.context()
    camera = first['cameras'][0]
    assert camera['simulated'] is True and camera['source_binding_current'] is True
    assert camera['room_binding_current'] is True
    index.room_context = None
    changed = index.context()
    assert changed['cameras'][0]['room_binding_current'] is False
    assert changed['fingerprint'] != first['fingerprint']
    assert calls == []
