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
async def test_manual_control_invalidates_pending_auto_decision_and_holds():
    pool=PreviewPool.__new__(PreviewPool)
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


def test_closed_primary_view_is_not_overwritten_by_last_frames_age(tmp_path):
    source=Path('homeplane/embodied/worker.py').read_text()
    function=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='view')
    state={'status':'shutter_closed','received_at':time.time()-100,'processed_frames':2,
           'camera_control':{'view_usable':True,'target_locked':True}}
    context=dict(state=state,games=SimpleNamespace(public={}),recorder=SimpleNamespace(public=lambda:{}),
        home=SimpleNamespace(public=lambda:{}),secondary_homes={},fleet=SimpleNamespace(public=lambda _:{}),time=time,
        PRIVATE=tmp_path,last_frame=1,SELECTED={'id':'main'},configuration=lambda:{'cameras':[{'id':'main'}]})
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<actual view>','exec'),context)
    result=context['view']()
    assert result['status']=='shutter_closed' and result['camera_sessions'][0]['state']=='shutter_closed'
    assert not result['camera_control']['view_usable'] and not result['camera_control']['target_locked']
    assert state['camera_control']['view_usable'], 'Public status must not mutate the director snapshot'
