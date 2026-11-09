"""Persisted direction choices and production call paths, without camera I/O."""

import ast
import asyncio
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from homeplane.embodied.camera_intent import CameraIntentStore
from homeplane.embodied import previews


CAMERA = {'id': 'cam_1111111111', 'region': 'cn', 'device': {'did': 'synthetic-device'}}


def store_at(path, camera=None):
    return CameraIntentStore(path, camera or CAMERA)


def actual_worker_functions(names, scope):
    path = Path('homeplane/embodied/worker.py')
    nodes = [n for n in ast.parse(path.read_text()).body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)


def test_missing_preference_is_read_only_and_uses_the_callers_default(tmp_path):
    path = tmp_path / 'camera'
    store = store_at(path)
    assert store.load().mode == 'auto'
    assert store.load(default='hold').mode == 'hold'
    assert not path.exists()


def test_explicit_choices_survive_new_instances_and_survey_never_replays(tmp_path):
    path = tmp_path / 'camera'
    revisions = []
    for mode in ('hold', 'auto', 'pets', 'survey'):
        saved = store_at(path).save(mode)
        revisions.append(saved.revision)
        assert saved.mode == mode and saved.status == 'saved'
        loaded = store_at(path).load()
        assert loaded.mode == ('hold' if mode == 'survey' else mode)
        assert loaded.revision == saved.revision and loaded.status == 'saved'
    assert len(set(revisions)) == 4
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((path / 'director-intent.json').stat().st_mode) == 0o600
    assert CAMERA['device']['did'] not in (path / 'director-intent.json').read_text()


@pytest.mark.parametrize('changes', [
    {'id': 'cam_2222222222'}, {'region': 'us'}, {'device': {'did': 'another-device'}},
])
def test_persisted_choice_is_bound_to_exact_registered_camera(tmp_path, changes):
    path = tmp_path / 'camera'
    store_at(path).save('auto')
    loaded = store_at(path, dict(CAMERA, **changes)).load()
    assert loaded.mode == 'hold' and loaded.status == 'unavailable'


@pytest.mark.parametrize('damage', [
    'invalid_json', 'duplicate_key', 'unknown_mode', 'boolean_version',
    'extra_field', 'bad_revision', 'invalid_utf8', 'oversized', 'shared_permissions',
])
def test_unreadable_or_untrusted_preference_holds_instead_of_resuming(tmp_path, damage):
    path = tmp_path / 'camera'
    store = store_at(path)
    store.save('auto')
    record = path / 'director-intent.json'
    value = json.loads(record.read_text())
    if damage == 'invalid_json':
        record.write_text('{')
    elif damage == 'duplicate_key':
        record.write_text(record.read_text()[:-1] + ',"mode":"auto"}')
    elif damage == 'invalid_utf8':
        record.write_bytes(b'\xff')
    elif damage == 'oversized':
        record.write_text(' ' * 4097)
    elif damage == 'shared_permissions':
        record.chmod(0o644)
    else:
        key, replacement = {
            'unknown_mode': ('mode', 'survey'), 'boolean_version': ('version', True),
            'extra_field': ('extra', 1), 'bad_revision': ('revision', 'old'),
        }[damage]
        value[key] = replacement
        record.write_text(json.dumps(value))
    assert store.load().mode == 'hold' and store.load().status == 'unavailable'
    if damage not in {'oversized', 'shared_permissions'}:
        store.save('hold')  # An explicit choice can repair our bounded private record.
        assert store.load().status == 'saved'
    else:
        with pytest.raises(ValueError):
            store.save('auto')


@pytest.mark.parametrize('kind', ['symlink', 'hardlink'])
def test_linked_record_is_never_read_or_replaced(tmp_path, kind):
    path = tmp_path / 'camera'
    store = store_at(path)
    store.save('auto')
    record, original = path / 'director-intent.json', tmp_path / 'original'
    record.rename(original)
    if kind == 'symlink':
        record.symlink_to(original)
    else:
        os.link(original, record)
    before = original.read_bytes()
    assert store.load().mode == 'hold'
    with pytest.raises((OSError, ValueError)):
        store.save('auto')
    assert original.read_bytes() == before


def test_shared_or_symlinked_directory_does_not_create_preference(tmp_path):
    original = tmp_path / 'original'
    original.mkdir(mode=0o700)
    linked = tmp_path / 'linked'
    linked.symlink_to(original, target_is_directory=True)
    for path in (linked, original):
        if path == original:
            original.chmod(0o755)
        assert store_at(path).load().mode == 'hold'
        with pytest.raises((OSError, ValueError)):
            store_at(path).save('auto')
    assert not list(original.iterdir())


def test_lock_contention_leaves_previous_choice_intact(tmp_path):
    path = tmp_path / 'camera'
    store = store_at(path)
    old = store.save('hold')
    with (path / 'director-intent.lock').open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            store.save('auto')
        assert store.load().revision == old.revision
    assert store.save('auto').mode == 'auto'
    assert not list(path.glob('*.tmp'))


def test_atomic_replace_failure_preserves_old_choice_and_releases_lock(tmp_path, monkeypatch):
    path = tmp_path / 'camera'
    store = store_at(path)
    old = store.save('hold')
    def failed_replace(*_, **__):
        raise OSError('synthetic disk failure')
    with monkeypatch.context() as change:
        change.setattr(os, 'replace', failed_replace)
        with pytest.raises(OSError):
            store.save('auto')
    assert store.load().revision == old.revision
    assert not list(path.glob('*.tmp'))
    assert store.save('auto').mode == 'auto'


@pytest.mark.asyncio
async def test_secondary_new_session_restores_choice_before_any_motor_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(previews, 'DATA', tmp_path)
    monkeypatch.setattr(previews, 'camera', lambda _: CAMERA)
    path = tmp_path / 'camera'
    async def inert_monitor(*_):
        pass
    monkeypatch.setattr(previews.PreviewPool, '_monitor', inert_monitor)
    factory = lambda camera: store_at(path, camera)
    pool = previews.PreviewPool(recognize=True, intent_factory=factory)
    await pool.start(CAMERA['id'])
    await asyncio.sleep(0)
    item = pool.sessions[CAMERA['id']]
    assert item['mode'] == 'auto'
    async def send(direction, *, expected_epoch):
        assert store_at(path).load().mode == 'hold'
        assert item['mode'] == 'hold' and expected_epoch == item['control_epoch']
        return {'native_code': 0}
    item.update(control=AsyncMock(side_effect=send), control_epoch=4, manual_tasks=set())
    await pool.control(CAMERA['id'], direction='left')
    assert item['control_epoch'] == 5 and not item['manual_tasks']
    for requested, restored in (('hold', 'hold'), ('auto', 'auto'), ('pets', 'pets'), ('survey', 'hold')):
        await pool.control(CAMERA['id'], mode=requested)
        replacement = previews.PreviewPool(recognize=True, intent_factory=factory)
        await replacement.start(CAMERA['id'])
        await asyncio.sleep(0)
        assert replacement.sessions[CAMERA['id']]['mode'] == restored
    read_only = previews.PreviewPool(recognize=False, intent_factory=factory)
    store_at(path).save('auto')
    await read_only.start(CAMERA['id'])
    await asyncio.sleep(0)
    assert read_only.sessions[CAMERA['id']]['mode'] == 'hold'


@pytest.mark.asyncio
async def test_secondary_save_failure_holds_locally_and_does_not_dispatch(monkeypatch):
    monkeypatch.setattr(previews, 'camera', lambda _: CAMERA)
    pool = previews.PreviewPool.__new__(previews.PreviewPool)
    pool.intent_factory = lambda _: SimpleNamespace(save=lambda _: (_ for _ in ()).throw(OSError()))
    send = AsyncMock()
    item = dict(control=send, mode='auto', mode_revision='old', control_epoch=4, manual_tasks=set(),
                camera_control={'mode': 'auto'}, received_at=time.time(), state='live', frames=1,
                shutter_changed=False)
    pool.sessions = {CAMERA['id']: item}
    pool.recognize = True
    pool.on_audio = None
    with pytest.raises(RuntimeError, match='camera_intent_unavailable'):
        await pool.control(CAMERA['id'], direction='left')
    assert item['mode'] == 'hold' and item['control_epoch'] == 5
    assert item['mode_revision'] != 'old' and item['intent_status'] == 'unavailable'
    send.assert_not_awaited()
    assert pool.status(CAMERA['id'])['camera_control']['mode'] == 'hold'
    assert item['camera_control']['mode'] == 'auto', 'Projection must not mutate prior snapshots'


def primary_scope(tmp_path):
    scope = dict(Request=object, HTTPException=HTTPException, secrets=secrets, time=time,
                 SELECTED=CAMERA, camera_intents=store_at(tmp_path / 'camera'),
                 camera_planner=SimpleNamespace(mode='auto', intent_revision='old', intent_status='default',
                                               visited=set(), last_person=0, last_move=0),
                 motor_step=AsyncMock(return_value={'native_code': 0}),
                 previews=SimpleNamespace(control=AsyncMock()))
    actual_worker_functions({'set_primary_camera_mode', 'camera_control', 'manual_motor', 'preview_control'}, scope)
    return scope


def request(data, *, origin=None, host='127.0.0.1'):
    headers = {'host': '127.0.0.1:18743'}
    if origin:
        headers['origin'] = origin
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers,
                           json=AsyncMock(return_value=data))


@pytest.mark.asyncio
@pytest.mark.parametrize('via_preview', [False, True])
async def test_primary_public_and_legacy_manual_paths_save_hold_before_send(tmp_path, via_preview):
    scope = primary_scope(tmp_path)
    async def send(direction):
        assert direction == 'up'
        assert scope['camera_intents'].load().mode == 'hold'
        assert scope['camera_planner'].mode == 'hold'
        return {'native_code': 0}
    scope['motor_step'].side_effect = send
    req = request({'direction': 'up'})
    result = (await scope['preview_control'](CAMERA['id'], req) if via_preview
              else await scope['manual_motor'](req))
    assert result['native_code'] == 0
    scope['motor_step'].assert_awaited_once()


@pytest.mark.asyncio
async def test_primary_mode_resume_and_survey_restart_preference(tmp_path):
    scope = primary_scope(tmp_path)
    for mode in ('hold', 'auto', 'pets', 'survey'):
        assert await scope['camera_control'](request({'mode': mode})) == {'mode': mode}
        assert scope['camera_planner'].mode == mode
        assert scope['camera_intents'].load().mode == ('hold' if mode == 'survey' else mode)
    scope['motor_step'].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['manual_motor', 'camera_control', 'preview_control'])
async def test_primary_save_failure_returns_conflict_and_never_sends(tmp_path, route):
    scope = primary_scope(tmp_path)
    scope['camera_intents'] = SimpleNamespace(save=lambda _: (_ for _ in ()).throw(OSError()))
    req = request({'mode': 'auto'} if route == 'camera_control' else {'direction': 'left'})
    with pytest.raises(HTTPException) as error:
        if route == 'preview_control':
            await scope[route](CAMERA['id'], req)
        else:
            await scope[route](req)
    assert error.value.status_code == 409 and scope['camera_planner'].mode == 'hold'
    scope['motor_step'].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['manual_motor', 'camera_control', 'preview_control'])
@pytest.mark.parametrize('bad', ['body', 'origin', 'host', 'value'])
async def test_rejected_requests_do_not_change_or_persist_intent(tmp_path, route, bad):
    scope = primary_scope(tmp_path)
    data = {'mode': 'auto'} if route == 'camera_control' else {'direction': 'left'}
    if bad == 'body':
        data = []
    if bad == 'value':
        data = {'mode': 'invalid'} if route == 'camera_control' else {'direction': 'invalid'}
    req = request(data, origin='http://foreign.invalid' if bad == 'origin' else None,
                  host='192.0.2.1' if bad == 'host' else '127.0.0.1')
    with pytest.raises(HTTPException) as error:
        if route == 'preview_control':
            await scope[route](CAMERA['id'], req)
        else:
            await scope[route](req)
    assert error.value.status_code in {400, 403}
    assert scope['camera_planner'].mode == 'auto' and scope['camera_planner'].intent_revision == 'old'
    assert not scope['camera_intents'].directory.exists()
    scope['motor_step'].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revision', 'planner'])
async def test_pending_primary_auto_decision_is_cancelled_after_intent_changes(change):
    sent = []
    noop = lambda *_, **__: None
    planner = SimpleNamespace(mode='auto', intent_revision='original')
    scope = dict(asyncio=asyncio, time=time, secrets=secrets, json=json,
                 capture=SimpleNamespace(stdin=SimpleNamespace(write=sent.append, drain=AsyncMock())),
                 camera_lease=SimpleNamespace(snapshot={'status': 'owned'}), camera_planner=planner,
                 state={'status': 'live'}, SELECTED=CAMERA, camera_is_open=AsyncMock(return_value=True),
                 close_primary_view=noop, motor_sequence=0, motion_generation=0, motor_waiters={},
                 identities=SimpleNamespace(discard=noop), fleet=SimpleNamespace(clear=noop),
                 home=SimpleNamespace(selected=True, epoch=0, vad=[], pending_audio=None,
                                      runtime_log=SimpleNamespace(record=noop)),
                 room=SimpleNamespace(expect_motion=noop))
    class RoomLock:
        async def __aenter__(self):
            await asyncio.sleep(0)
            if change == 'revision':
                planner.intent_revision = 'new'
            else:
                scope['camera_planner'] = SimpleNamespace(mode='auto', intent_revision='original')
        async def __aexit__(self, *_):
            pass
    scope['room_lock'] = RoomLock()
    actual_worker_functions({'motor_step'}, scope)
    with pytest.raises(RuntimeError, match='camera_intent_changed'):
        await scope['motor_step']('left', time.monotonic(), expected_intent='original')
    assert not sent and not scope['motor_waiters']
