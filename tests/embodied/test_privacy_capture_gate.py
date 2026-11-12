"""Closed shutters do not spawn capture/model processes; failed reads stay unknown."""
import ast
import asyncio
import collections
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeplane.embodied import paths, previews
from homeplane.embodied.camera_wall import CameraWall
from homeplane.embodied.pet_tracks import PetTracks
from homeplane.people.live import close_identity


@pytest.mark.asyncio
@pytest.mark.parametrize('reply,expected', [
    ([{'code': 0, 'value': False, 'did':'SYNTHETIC', 'siid':7, 'piid':9}], False),
    ([{'code': 0, 'value': True, 'did':'SYNTHETIC', 'siid':7, 'piid':9}], True),
    ([{'code': 0, 'value': True, 'did':'OTHER', 'siid':7, 'piid':9}], None),
    ([{'code': 0, 'value': True, 'did':'SYNTHETIC', 'siid':7, 'piid':8}], None),
    ([{'code': 0, 'value': True}], None),
    ([{'code': 0, 'value': True, 'did':'SYNTHETIC', 'siid':7, 'piid':9},
      {'code': -1, 'value': False, 'did':'SYNTHETIC', 'siid':7, 'piid':9}], None),
    ([{'code': -1}], None),
    ([], None),
])
async def test_privacy_readback_never_turns_transport_failure_into_closed(tmp_path, monkeypatch, reply, expected):
    (tmp_path/'miot-session.json').write_text(json.dumps({'access_token':'SYNTHETIC','expires_ts':time.time()+3600}))
    (tmp_path/'miot-session.json').chmod(0o600)
    monkeypatch.setattr(paths, 'PRIVATE', tmp_path)
    client = SimpleNamespace(get_props_async=AsyncMock(return_value=reply), deinit_async=AsyncMock())
    selected = {'device': {'did': 'SYNTHETIC'}, 'spec': {'services': [
        {'iid': 7, 'type': 'urn:miot-spec-v2:service:camera-control:00007803',
         'properties': [{'iid': 9, 'type': 'urn:miot-spec-v2:property:on:00000006', 'access': ['read']}]}]}}
    async def read():
        return await previews.camera_is_open(selected, client_factory=lambda **_: client,
                                             parameter_factory=SimpleNamespace)
    if expected is None:
        with pytest.raises(RuntimeError, match='camera_state_unavailable'):
            await read()
    else:
        assert await read() is expected
    client.deinit_async.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_open',['0','1'])
async def test_closed_secondary_does_not_start_models_or_capture(monkeypatch,legacy_open):
    monkeypatch.setenv('FRIDAY_CAMERA_OPEN',legacy_open)
    monkeypatch.setattr(previews, 'camera_is_open', AsyncMock(return_value=False))
    spawn = AsyncMock(side_effect=AssertionError('closed camera must not spawn'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    pool = previews.PreviewPool(continuous=True, recognize=True)
    item = {'state':'connecting', 'recognizer':None, 'process':None, 'shutter_changed':False}
    await pool._run({'id':'cam_test'}, item)
    assert item['state'] == 'shutter_closed'
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['hold','survey'])
async def test_secondary_reconnect_preserves_intent_and_consumed_survey_window(monkeypatch,mode):
    monkeypatch.setattr(previews,'camera_is_open',AsyncMock(return_value=False))
    monkeypatch.setattr(previews,'RuntimeLog',lambda *_:SimpleNamespace(record=lambda *_,**__:None))
    spawn=AsyncMock(side_effect=AssertionError('closed camera must not spawn'))
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    pool=previews.PreviewPool(continuous=True,recognize=True)
    item={'state':'connecting','recognizer':None,'process':None,'shutter_changed':False,
          'mode':mode,'control_epoch':4,'mode_revision':3,'survey_attempts':7}
    await pool._run({'id':'cam_test'},item)
    first_epoch=item['control_epoch']
    await pool._run({'id':'cam_test'},item)
    assert item['mode']==mode and item['mode_revision']==3 and item['survey_attempts']==7
    assert item['control_epoch']>first_epoch>4
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_open',['0','1'])
@pytest.mark.parametrize('cleanup_fault',[False,True])
async def test_closed_primary_uses_real_source_function_without_capture(monkeypatch,legacy_open,cleanup_fault):
    monkeypatch.setenv('FRIDAY_CAMERA_OPEN',legacy_open)
    # Importing the worker starts its private identity store; compile its actual
    # production function instead so this test has no private data side effects.
    source = Path('homeplane/embodied/worker.py').read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name=='run_source')
    identities = SimpleNamespace(session=None, close=lambda:None)
    reset = SimpleNamespace(reset=lambda *_:None)
    home = SimpleNamespace(armed=True, selected=True, epoch=0, vad=[], pending_audio=None, gesture=reset)
    scope = dict(asyncio=asyncio, time=__import__('time'), secrets=__import__('secrets'),
        os=__import__('os'), generation=1, close_identity=close_identity,
        SELECTED={'id':'cam_test','takeover':False}, SESSION='synthetic', identities=identities,
        games=SimpleNamespace(begin_source=lambda:None,mode='off'), home=home,
        fleet=SimpleNamespace(clear=lambda _:None), Tracks=lambda:None, PetTracks=PetTracks,
        processed_times=collections.deque(), captured_times=collections.deque(),state={},
        wall=CameraWall([dict(id='cam_test',name='Synthetic',room='Synthetic')]),
        safety=SimpleNamespace(clear=lambda _:None),
        camera_is_open=AsyncMock(return_value=False), notify=AsyncMock(),reap=AsyncMock(),
        capture=None,vision=None,camera_lease=None,room=None,close_rooms=AsyncMock(),
        primary_capture_closed=AsyncMock(side_effect=RuntimeError('synthetic_notify_fault')if cleanup_fault else None))
    spawn=AsyncMock(side_effect=AssertionError('closed camera must not spawn'))
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<production run_source>','exec'),scope)
    if cleanup_fault:
        with pytest.raises(RuntimeError,match='synthetic_notify_fault'):await scope['run_source'](1)
    else:await scope['run_source'](1)
    assert scope['state']['status']=='shutter_closed' and not home.selected
    spawn.assert_not_awaited()
    assert scope['reap'].await_count==2


@pytest.mark.asyncio
@pytest.mark.parametrize('practice_active', [False, True])
async def test_secondary_gesture_dispatch_does_not_require_primary_camera_armed(monkeypatch, practice_active):
    from homeplane.embodied import bridge
    from homeplane.embodied.practice import RecognitionPractice
    practice = RecognitionPractice()
    if practice_active:
        practice.begin('hand.thumb_up', ['secondary'])
    command = AsyncMock(return_value={'status':'reported_match','acknowledged':True,'reached_target':True})
    monkeypatch.setattr(bridge,'command',command)
    source=Path('homeplane/embodied/worker.py').read_text()
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='dispatch_interactions')
    source_guard=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='interaction_source_current')
    process=SimpleNamespace(returncode=None)
    pool=previews.PreviewPool.__new__(previews.PreviewPool)
    pool.sessions={'secondary':dict(source_active=True,process=process,state='live',privacy_closed=False,
                                    recognizer=SimpleNamespace(session='synthetic-secondary'))}
    tasks=[]
    scope=dict(time=__import__('time'),practice=practice,interaction_enabled=True,SELECTED={'id':'primary'},
        home=SimpleNamespace(armed=False,spawn=lambda coro:tasks.append(asyncio.create_task(coro))),
        secondary_homes={'secondary':SimpleNamespace(armed=True)},
        wall=CameraWall([dict(id='secondary',name='Synthetic',room='Synthetic')]),
        fleet=SimpleNamespace(current=lambda *_:True,receipts=[]),previews=pool)
    exec(compile(ast.Module(body=[source_guard,node],type_ignores=[]),'<production dispatch>','exec'),scope)
    event=dict(camera='secondary',session='synthetic-secondary',action='on',event_id='synthetic',target='lamp',
               binding='a'*32,revision=1,gesture='hand.open_palm',scope='home',
               triggered_at=time.time(),observed_mono=time.monotonic())
    await scope['dispatch_interactions']([event]);await asyncio.gather(*tasks)
    if practice_active:
        command.assert_not_awaited()
        assert not scope['fleet'].receipts
    else:
        command.assert_awaited_once()
