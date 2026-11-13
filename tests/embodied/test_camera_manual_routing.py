import asyncio
import ast
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from homeplane.embodied.api import create_router
from homeplane.embodied.previews import PreviewPool


@pytest.mark.asyncio
async def test_secondary_manual_request_reuses_owned_camera_route():
    request=AsyncMock(return_value={'native_code':0})
    async def camera_control(camera_id, *, direction=None, mode=None):
        return await request('/api/previews/'+camera_id+'/control',{'direction':direction} if direction else {'mode':mode})
    service=SimpleNamespace(camera_control=camera_control,selected='cam_1111111111',process=SimpleNamespace(returncode=None),
        cameras=lambda:[{'id':'cam_1111111111'},{'id':'cam_2222222222'}],request=request)
    app=FastAPI();app.include_router(create_router(service,SimpleNamespace(),asyncio.Lock()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        result=await client.post('/api/embodied/cameras/cam_2222222222/control',json={'direction':'up'})
        assert result.status_code==200
        request.assert_awaited_once_with('/api/previews/cam_2222222222/control',{'direction':'up'})
        assert (await client.post('/api/embodied/cameras/cam_3333333333/control',json={'direction':'up'})).status_code==409
        assert request.await_count==1


@pytest.mark.asyncio
async def test_manual_control_invalidates_pending_auto_decision_and_holds(tmp_path, monkeypatch):
    from homeplane.embodied import previews
    from homeplane.embodied.camera_intent import CameraIntentStore
    selected={'id':'cam_2222222222','device':{'did':'synthetic-secondary'}}
    monkeypatch.setattr(previews,'camera',lambda _:selected)
    pool=PreviewPool.__new__(PreviewPool)
    pool.intent_factory=lambda _:CameraIntentStore(tmp_path/'camera',selected)
    send=AsyncMock(return_value={'native_code':0})
    entry={'control':send,'mode':'auto','control_epoch':4,'manual_tasks':set()}
    pool.sessions={'secondary':entry}
    result=await pool.control('secondary',direction='up')
    assert result['native_code']==0 and entry['mode']=='hold'
    send.assert_awaited_once_with('up',expected_epoch=5)
    assert not entry['manual_tasks']
    await pool.control('secondary',mode='auto')
    assert entry['mode']=='auto' and entry['control_epoch']==6
    with pytest.raises(ValueError):await pool.control('secondary',direction='up',mode='auto')
    assert entry['control_epoch']==6


@pytest.mark.parametrize(('state_name', 'age', 'reported'), [
    ('shutter_closed', 100, 'shutter_closed'),
    ('live', .3, 'live'),
    ('live', 2, 'stale'),
    ('live', -.2, 'stale'),
    ('live', None, 'stale'),
])
def test_primary_recognition_health_requires_a_current_frame(tmp_path, state_name, age, reported):
    from homeplane.embodied.motion_games import MotionGames
    source=Path('homeplane/embodied/worker.py').read_text()
    function=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='view')
    state={'status':state_name,'received_at':time.time()-(100 if state_name=='shutter_closed' else .3),
           'processed_frames':2,'pipeline_ms':251,
           '_patrol_view':('private-reference', ((1,0,0),(0,1,0),(0,0,1)), 1),
           'camera_control':{'view_usable':True,'target_locked':True}}
    from homeplane.embodied.party_camera import PartyCamera
    context=dict(state=state,games=MotionGames(),party_camera=PartyCamera(),recorder=SimpleNamespace(public=lambda:{}),
        safety=SimpleNamespace(recording_status=lambda:{'state':'off'}),
        home=SimpleNamespace(public=lambda:{},voice_health=lambda:{}),secondary_homes={},fleet=SimpleNamespace(public=lambda _:{}),time=time,
        PRIVATE=tmp_path,last_frame=time.monotonic()-age if age is not None else 0,
        SELECTED={'id':'main'},camera_planner=None,
        configuration=lambda:{'cameras':[{'id':'main'}]})
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<actual view>','exec'),context)
    result=context['view']()
    assert '_patrol_view' not in result
    primary,=result['camera_sessions']
    assert primary['state']==reported
    assert primary['recognition']==('running' if reported=='live' else 'waiting')
    assert primary['latency_ms']==(251 if reported=='live' else None)
    if state_name=='shutter_closed':
        assert result['status']=='shutter_closed'
        assert not result['camera_control']['view_usable'] and not result['camera_control']['target_locked']
    assert state['camera_control']['view_usable'], 'Public status must not mutate the director snapshot'


@pytest.mark.asyncio
async def test_status_api_keeps_secondary_health_and_retires_it_with_its_owner(tmp_path, monkeypatch):
    from homeplane.embodied import service

    primary='cam_1111111111'
    secondary='cam_2222222222'
    monkeypatch.setattr(service,'PRIVATE',tmp_path)
    monkeypatch.setattr(service,'configuration',lambda:{'cameras':[{'id':primary},{'id':secondary}]})
    owner=service.EmbodiedService()
    owner.configured=lambda:True
    owner.selected=primary
    owner.process=SimpleNamespace(returncode=None)
    pool=PreviewPool(recognize=True)
    pool.sessions[secondary]=dict(state='live',received_at=time.time()-1.1,frames=7,
                                  shutter_changed=False,latency_ms=1090)
    worker=AsyncMock(return_value=dict(status='shutter_closed',camera_sessions=[
        dict(camera=primary,state='shutter_closed',recognition='waiting'),pool.status(secondary)],
        pipeline_ms=120))
    owner.request=worker
    app=FastAPI()
    app.include_router(create_router(owner,SimpleNamespace(),asyncio.Lock()))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.get('/api/embodied/status')
            assert response.status_code==200
            assert response.json()['camera_sessions'][1]['latency_ms']==1090
            worker.assert_awaited_once_with('/api/status')
            worker.side_effect=ValueError('embodied_operation_unavailable')
            unavailable=(await client.get('/api/embodied/status')).json()
            assert unavailable['state']=='unavailable' and unavailable['camera_sessions']==[]

            async def retire_owner(_):
                owner.process=None
                return worker.return_value

            worker.side_effect=retire_owner
            retired=(await client.get('/api/embodied/status')).json()
            assert not retired['owned'] and retired['camera_sessions']==[] and retired['latency_ms'] is None
    finally:
        await owner.client.aclose()
