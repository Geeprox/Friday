"""Persistent producer provenance over synthetic HA transports; no household I/O."""

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from homeplane.contracts import Binding, Event
from homeplane.event_provenance import entity_source_context
from homeplane.ha import HAReadOnlyAdapter
from homeplane.simulator import FIXTURE, Simulator
from homeplane.store import TraceStore
from tests.test_homeplane_ha_stream import Server, until


def ingress(server, *, url='http://simulated.invalid', instance='SYNTHETIC-instance'):
    return HAReadOnlyAdapter(url, 'SYNTHETIC-provenance-secret', instance,
                             ws_connect=server.connect, transport=httpx.MockTransport(server.http))


def observed(ha, entity):
    ha._event('state-transition', {'entity_id': entity.id, 'upstream_event': 'state_changed',
                                 'from': 'off', 'to': 'on', 'trigger_eligible': False}, datetime.now(UTC))
    return ha._events[-1]


@pytest.mark.asyncio
async def test_live_ha_producer_source_context_survives_store_and_new_boot(tmp_path):
    server, store = Server(), TraceStore(tmp_path / 'journal.sqlite')
    first = ingress(server)
    try:
        assert (await first.subscribe(event_sink=store.append_events))['status'] == 'connected'
        server.change()
        await until(lambda: any(event.kind == 'state-transition' for event in store.recent_events(simulated=False)))
        event = next(event for event in store.recent_events(simulated=False) if event.kind == 'state-transition')
        assert re.fullmatch(r'[a-f0-9]{64}', event.source_context)
        assert event.time_basis == 'upstream_event'
        assert first.url not in event.model_dump_json()
        assert 'SYNTHETIC-provenance-secret' not in event.model_dump_json()
        assert 'SIMULATED-private-user' not in event.model_dump_json()
    finally:
        first._event_sink = None
        await first.close()
        store.close()

    store, second = TraceStore(tmp_path / 'journal.sqlite'), ingress(server)
    try:
        snapshot = await second.snapshot()
        entity = next(item for item in snapshot.entities if item.id == event.payload['entity_id'])
        persisted = next(item for item in store.recent_events(simulated=False) if item.event_id == event.event_id)
        assert first._boot != second._boot
        assert persisted.source_context == entity_source_context(second, entity)
        assert all(request.method == 'GET' for request in server.requests)
    finally:
        store.close()
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['instance', 'endpoint', 'endpoint_port_zero', 'registry', 'device', 'unique', 'platform', 'entity_area', 'device_area'])
async def test_reused_entity_id_does_not_match_another_source_or_binding(change):
    server = Server()
    first = ingress(server)
    initial = await first.snapshot()
    entity = next(item for item in initial.entities if item.kind == 'light')
    event = observed(first, entity)
    assert event.source_context == entity_source_context(first, entity)
    await first.close()
    options = {}
    registration = server.registry['config/entity_registry/list'][0]
    if change == 'instance':
        options['instance'] = 'SYNTHETIC-other-instance'
    elif change == 'endpoint':
        options['url'] = 'https://another-simulated.invalid'
    elif change == 'endpoint_port_zero':
        options['url'] = 'http://simulated.invalid:0'
    elif change == 'registry':
        registration['id'] = 'SYNTHETIC-reused-registry'
    elif change == 'device':
        registration['device_id'] = 'SYNTHETIC-replacement-device'
        server.registry['config/device_registry/list'].append({'id': registration['device_id'], 'area_id': 'SIMULATED-area'})
    elif change == 'unique':
        registration['unique_id'] = 'SYNTHETIC-replacement-unique'
    elif change == 'platform':
        registration['platform'] = 'SYNTHETIC-replacement-platform'
    elif change == 'entity_area':
        registration['area_id'] = 'SYNTHETIC-other-area'
    elif change == 'device_area':
        server.registry['config/device_registry/list'][0]['area_id'] = 'SYNTHETIC-other-area'
    server.registry['config/area_registry/list'].append({'area_id': 'SYNTHETIC-other-area', 'name': 'SYNTHETIC other room'})
    second = ingress(server, **options)
    try:
        current = await second.snapshot()
        target = next(item for item in current.entities if item.kind == 'light')
        context = entity_source_context(second, target)
        assert context and context != event.source_context
        assert all(request.method == 'GET' for request in server.requests)
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_same_endpoint_normalizes_and_display_state_changes_do_not_change_source():
    server = Server()
    first = ingress(server)
    entity = next(item for item in (await first.snapshot()).entities if item.kind == 'light')
    digest = observed(first, entity).source_context
    await first.close()
    server.rows['light.simulated']['state'] = 'on'
    server.rows['light.simulated']['attributes']['friendly_name'] = 'SYNTHETIC renamed light'
    server.registry['config/area_registry/list'][0]['name'] = 'SYNTHETIC renamed room'
    second = ingress(server, url='http://SIMULATED.invalid:80/')
    try:
        renamed = next(item for item in (await second.snapshot()).entities if item.kind == 'light')
        assert first._boot != second._boot
        assert entity_source_context(second, renamed) == digest
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_missing_registry_ambiguous_binding_or_stale_projection_has_no_digest():
    server = Server()
    ha = ingress(server)
    try:
        entity = next(item for item in (await ha.snapshot()).entities if item.kind == 'light')
        assert entity_source_context(ha, entity)
        assert entity_source_context(ha, entity.model_copy(update={'physical_source_id': 'SYNTHETIC-replaced'})) is None
        assert entity_source_context(ha, entity.model_copy(update={'physical_source_id': '\ud800'})) is None
        assert entity_source_context(ha, entity.model_copy(update={'bindings': [*entity.bindings, Binding(source='ha', external_id='light.other')]})) is None
        assert entity_source_context(ha, entity.model_copy(update={'room': 'SYNTHETIC wrong room'})) is None
        assert entity_source_context(SimpleNamespace(url=ha.url, instance_id=ha.instance_id), entity) is None
        ha._registry_ok = False
        assert observed(ha, entity).source_context is None
        server.reject_auth = True
        unavailable = await ha.snapshot()
        assert ha._registry_ok is False
        assert all(entity_source_context(ha, item) is None for item in unavailable.entities)
    finally:
        await ha.close()


@pytest.mark.asyncio
async def test_registry_unique_id_fallback_is_stable_but_provisional_identity_is_not():
    server = Server()
    for registry in server.registry['config/entity_registry/list']:
        registry.pop('id')
        registry['unique_id'] = 'SYNTHETIC-unique-' + registry['entity_id']
        registry['platform'] = 'SYNTHETIC-platform'
    first = ingress(server)
    entity = next(item for item in (await first.snapshot()).entities if item.kind == 'light')
    digest = observed(first, entity).source_context
    assert digest
    await first.close()
    second = ingress(server)
    try:
        entity = next(item for item in (await second.snapshot()).entities if item.kind == 'light')
        assert entity_source_context(second, entity) == digest
        for registry in server.registry['config/entity_registry/list']:
            registry.pop('unique_id')
        provisional = await second.snapshot()
        entity = next(item for item in provisional.entities if item.kind == 'light')
        assert ':provisional:' in entity.id
        assert observed(second, entity).source_context is None
    finally:
        await second.close()


def test_legacy_event_has_no_provenance_and_malformed_context_is_rejected():
    event = Event(source='ha', kind='state-transition', correlation_id='SYNTHETIC-event',
                  wall_time=datetime.now(UTC), monotonic_time=1, boot_id='SYNTHETIC-old-boot', payload={})
    legacy = event.model_dump(mode='json')
    legacy.pop('source_context')
    assert Event.model_validate(legacy).source_context is None
    for value in ('not-a-digest', 'A' * 64, 'a' * 63, 'a' * 65, 7):
        with pytest.raises(ValidationError):
            Event.model_validate({**legacy, 'source_context': value})


@pytest.mark.asyncio
async def test_simulator_producer_provenance_is_explicit_stable_and_fixture_bound(tmp_path):
    first, second = Simulator(), Simulator()
    event = first.contact_event('open')
    entity = next(item for item in second.entities if item.id == event.payload['entity_id'])
    assert first.boot_id != second.boot_id
    assert event.simulated and event.source_context == entity_source_context(second, entity)
    assert all(item.source_context is None for item in first.events if item.kind == 'state-snapshot')
    snapshot = await first.snapshot()
    for item in snapshot.events:
        if item.kind == 'quality-change':
            assert item.source_context
    fixture = json.loads(FIXTURE.read_text())
    copied = tmp_path / 'same-synthetic-fixture.json'
    copied.write_text(json.dumps(fixture))
    same_fixture = Simulator(copied)
    matching = next(item for item in same_fixture.entities if item.id == entity.id)
    assert entity_source_context(same_fixture, matching) == event.source_context
    target = next(item for item in fixture['entities'] if item['id'] == entity.id)
    target['physical_source_id'] = 'SYNTHETIC-replacement-contact'
    path = tmp_path / 'synthetic-fixture.json'
    path.write_text(json.dumps(fixture))
    changed = Simulator(path)
    replacement = next(item for item in changed.entities if item.id == entity.id)
    assert entity_source_context(changed, replacement) != event.source_context
    assert entity_source_context(changed, replacement.model_copy(update={'simulated': False})) is None


@pytest.mark.asyncio
async def test_action_producer_records_initial_binding_and_stage_never_relabels_history(tmp_path):
    from tests.test_homeplane_actions import close, plan, rig

    core, _, _, posts, _, _, _ = rig(tmp_path)
    try:
        trace = await plan(core)
        initial = trace.events[0].source_context
        assert initial and not posts
        entity = (await core.snapshot()).entities[0]
        assert initial == entity_source_context(core.adapter, entity)
        core.adapter._devices['physical-1']['area_id'] = 'SYNTHETIC-other-area'
        core.adapter._areas['SYNTHETIC-other-area'] = {'area_id': 'SYNTHETIC-other-area', 'name': 'SYNTHETIC other room'}
        relocated = entity.model_copy(update={'room': 'SYNTHETIC other room'})
        assert initial != entity_source_context(core.adapter, relocated)
        updated = core.actions._stage(trace, 'failed', 'unavailable_readback')
        assert all(item.source_context == initial for item in updated.events)
        legacy = updated.model_copy(update={'events': [item.model_copy(update={'source_context': None}) for item in updated.events]})
        unchanged = core.actions._stage(legacy, 'failed', 'unavailable_readback')
        assert all(item.source_context is None for item in unchanged.events)
        assert not posts and not unchanged.real_write_attempted
    finally:
        await close(core)
