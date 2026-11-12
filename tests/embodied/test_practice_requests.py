"""使用原 worker 路由和合成传输验证体验取消，不启动镜头、模型或声音。"""

import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request

from homeplane.embodied.api import PracticeStart, PracticeToken
from homeplane.embodied.practice import RecognitionPractice
from homeplane.embodied.service import EmbodiedService


def worker_app(runtime):
    source=Path(__file__).resolve().parents[2]/"homeplane/embodied/worker.py"
    tree=ast.parse(source.read_text())
    app=FastAPI()
    scope=dict(Request=Request,HTTPException=HTTPException,practice=runtime,
        games=SimpleNamespace(mode="home"),view=lambda:{"camera_sessions":[{"state":"live"}]},
        configuration=lambda:{"cameras":[{"id":"SIMULATED-camera"}]})
    for name,method in [("practice_capabilities","GET"),("practice_start","POST"),("practice_status","POST"),("practice_stop","POST")]:
        function=next((n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name),None)
        if function is None:continue
        function.decorator_list=[]
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),str(source),"exec"),scope)
        app.add_api_route('/api/practice/'+name.removeprefix('practice_'),scope[name],methods=[method])
    return app


@pytest_asyncio.fixture
async def flow():
    runtime=RecognitionPractice()
    service=EmbodiedService();service.process=SimpleNamespace(returncode=None)
    client=httpx.AsyncClient(transport=httpx.ASGITransport(app=worker_app(runtime)),base_url="http://SIMULATED")
    calls=[]
    async def request(path,body=None,**kwargs):
        calls.append((path,body))
        response=await client.request('GET' if body is None else 'POST',path,json=body)
        response.raise_for_status();return response.json()
    service.request=request
    yield SimpleNamespace(runtime=runtime,service=service,send=request,calls=calls)
    owner=getattr(service,'_practice_requests',None)
    if owner is not None:await owner.retire()
    await client.aclose();await service.client.aclose()


def start(nonce='a'):
    return dict(gesture='hand.ok',request_id=nonce*64,issued_at_ms=int(time.time()*1000))


def console_app(service):
    source=Path(__file__).resolve().parents[2]/'homeplane/embodied/api.py'
    tree=ast.parse(source.read_text());app=FastAPI()
    scope=dict(service=service,PracticeStart=PracticeStart,PracticeToken=PracticeToken,HTTPException=HTTPException)
    for operation in ('start','status','stop'):
        name='practice_'+operation
        function=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
        function.decorator_list=[]
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),str(source),'exec'),scope)
        app.add_api_route('/api/embodied/practice/'+operation,scope[name],methods=['POST'])
    return app


@pytest.mark.asyncio
async def test_lost_start_reply_can_be_cancelled_without_the_missing_token(flow):
    async def lost(path,body=None,**kwargs):
        result=await flow.send(path,body,**kwargs)
        if path.endswith('/start'):raise httpx.ReadTimeout('SIMULATED lost reply')
        return result
    flow.service.request=lost
    with pytest.raises(ValueError):await flow.service.practice('start',start())
    assert flow.runtime.suppresses('SIMULATED-camera') and flow.service.practice_deadline>time.monotonic()
    result=await flow.service.practice('stop',{'request_id':'a'*64})
    assert result['phase']=='ended'
    assert not flow.runtime.suppresses('SIMULATED-camera') and flow.service.practice_deadline==0


@pytest.mark.asyncio
async def test_cancel_before_start_records_the_intent_and_never_creates_a_lesson(flow):
    await flow.service.practice('stop',{'request_id':'b'*64})
    result=await flow.service.practice('start',start('b'))
    assert result['phase']=='ended'
    assert not flow.runtime.active() and not any(path.endswith('/start') for path,_ in flow.calls)


@pytest.mark.asyncio
async def test_old_request_cancel_cannot_end_a_new_lesson(flow):
    first=await flow.service.practice('start',start())
    await flow.service.practice('stop',{'request_id':'a'*64})
    second=await flow.service.practice('start',start('b'))
    await flow.service.practice('stop',{'request_id':'a'*64})
    assert first['token']!=second['token']
    assert flow.runtime.suppresses('SIMULATED-camera') and flow.service.practice_token==second['token']
    await flow.service.practice('stop',{'request_id':'c'*64,'token':second['token']})
    assert flow.runtime.lease['token']==second['token'] and flow.service.practice_token==second['token']


@pytest.mark.asyncio
async def test_console_lost_reply_cancel_releases_the_actual_game_admission_gate(flow, monkeypatch):
    async def lost(path,body=None,**kwargs):
        result=await flow.send(path,body,**kwargs)
        if path.endswith('/start'):raise httpx.ReadTimeout('SIMULATED lost reply')
        return result
    monkeypatch.setattr(flow.service,'request',lost)
    monkeypatch.setattr(flow.service,'configured',lambda:False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=console_app(flow.service)),base_url='http://SIMULATED') as client:
        response=await client.post('/api/embodied/practice/start',json=start())
        assert response.status_code==409 and flow.runtime.active()
        with pytest.raises(ValueError,match='practice_active'):
            await flow.service.start(app_id='ski',expected_intent='a'*64)
        response=await client.post('/api/embodied/practice/stop',json={'request_id':'a'*64})
        assert response.status_code==200 and response.json()['phase']=='ended'
        assert not flow.runtime.active()
        with pytest.raises(ValueError,match='embodied_not_configured'):
            await flow.service.start(app_id='ski',expected_intent='a'*64)


@pytest.mark.asyncio
async def test_cancel_during_capability_read_never_sends_start_or_clears_new_owner(flow):
    entered,release=asyncio.Event(),asyncio.Event()
    async def delayed(path,body=None,**kwargs):
        if path.endswith('/capabilities') and not entered.is_set():
            entered.set();await release.wait()
        return await flow.send(path,body,**kwargs)
    flow.service.request=delayed
    task=asyncio.create_task(flow.service.practice('start',start()))
    await entered.wait()
    await flow.service.practice('stop',{'request_id':'a'*64})
    second=await flow.service.practice('start',start('b'))
    release.set();assert (await task)['phase']=='ended'
    assert flow.service.practice_token==second['token']
    assert [body['request_id'] for path,body in flow.calls if path.endswith('/start')]==['b'*64]


@pytest.mark.asyncio
async def test_worker_cancel_tombstone_defeats_a_late_delivered_start(flow):
    entered,release=asyncio.Event(),asyncio.Event()
    async def delayed(path,body=None,**kwargs):
        if path.endswith('/start'):entered.set();await release.wait()
        return await flow.send(path,body,**kwargs)
    flow.service.request=delayed
    task=asyncio.create_task(flow.service.practice('start',start()))
    await entered.wait();await flow.service.practice('stop',{'request_id':'a'*64})
    release.set();assert (await task)['phase']=='ended'
    assert flow.runtime.active() is None and flow.service.practice_deadline==0


@pytest.mark.asyncio
async def test_late_reply_and_repeated_start_do_not_resurrect_or_extend_a_session(flow):
    entered,release=asyncio.Event(),asyncio.Event()
    async def delayed(path,body=None,**kwargs):
        result=await flow.send(path,body,**kwargs)
        if path.endswith('/start') and body['request_id']=='a'*64:
            entered.set();await release.wait()
        return result
    flow.service.request=delayed
    first=asyncio.create_task(flow.service.practice('start',start()))
    await entered.wait()
    duplicate=asyncio.create_task(flow.service.practice('start',start()))
    await flow.service.practice('stop',{'request_id':'a'*64})
    second=await flow.service.practice('start',start('b'))
    deadline=flow.service.practice_deadline
    release.set()
    assert (await first)['phase']==(await duplicate)['phase']=='ended'
    repeat=await flow.service.practice('start',start('b'))
    assert repeat['token']==second['token'] and flow.service.practice_deadline==deadline
    assert len([1 for path,_ in flow.calls if path.endswith('/start')])==2
    assert flow.runtime.lease['token']==second['token']


@pytest.mark.asyncio
async def test_failed_or_mismatched_cancel_keeps_gate_until_confirmed_retry(flow):
    await flow.service.practice('start',start())
    async def unconfirmed(path,body=None,**kwargs):
        if path.endswith('/stop'):
            return {'phase':'ended','request_id':'b'*64,'scope':flow.runtime.scope}
        return await flow.send(path,body,**kwargs)
    flow.service.request=unconfirmed
    with pytest.raises(ValueError,match='stop_unconfirmed'):
        await flow.service.practice('stop',{'request_id':'a'*64})
    assert flow.runtime.active() and flow.service.practice_deadline>time.monotonic()
    flow.service.request=flow.send
    await flow.service.practice('stop',{'request_id':'a'*64})
    assert not flow.runtime.active() and flow.service.practice_deadline==0


@pytest.mark.asyncio
async def test_incompatible_worker_is_checked_before_start_and_does_not_leave_a_gate(flow):
    async def old_worker(path,body=None,**kwargs):
        assert path.endswith('/capabilities')
        return {'scope':flow.runtime.scope}
    flow.service.request=old_worker
    with pytest.raises(ValueError):await flow.service.practice('start',start())
    assert flow.service.practice_deadline==0 and flow.runtime.active() is None


@pytest.mark.asyncio
async def test_caller_cancellation_and_service_retirement_still_join_the_owned_tasks(flow):
    entered=asyncio.Event()
    async def delayed(path,body=None,**kwargs):
        if path.endswith('/start'):entered.set();await asyncio.Event().wait()
        return await flow.send(path,body,**kwargs)
    flow.service.request=delayed
    caller=asyncio.create_task(flow.service.practice('start',start()))
    await entered.wait();caller.cancel()
    with pytest.raises(asyncio.CancelledError):await caller
    owner=flow.service._practice_requests
    assert not owner.entries['a'*64]['start_task'].done()
    await owner.retire()
    assert all(task.done() for entry in owner.entries.values() for task in entry['tasks'])
    with pytest.raises(ValueError,match='closed'):await flow.service.practice('stop',{'request_id':'a'*64})
    flow.service.process.returncode=0;owner.worker_stopped()
    assert flow.service.practice_deadline==0


@pytest.mark.asyncio
async def test_stale_start_and_process_replacement_cannot_target_a_new_worker(flow):
    with pytest.raises(ValueError,match='expired'):
        await flow.service.practice('start',start()|{'issued_at_ms':int((time.time()-20)*1000)})
    assert not flow.calls
    await flow.service.practice('start',start())
    flow.service.process=SimpleNamespace(returncode=None)
    before=len(flow.calls)
    with pytest.raises(ValueError,match='stop_unconfirmed'):
        await flow.service.practice('stop',{'request_id':'a'*64})
    assert len(flow.calls)==before and flow.service.practice_deadline>time.monotonic()


def test_worker_scope_and_expired_forwarding_cannot_replay_after_retention():
    now=[100.0];runtime=RecognitionPractice(clock=lambda:now[0]);scope=runtime.scope
    runtime.stop_request('a'*64,scope)
    now[0]=221
    with pytest.raises(ValueError,match='expired'):
        runtime.begin_request('hand.ok',['SIMULATED'],'a'*64,scope,105,190)
    with pytest.raises(ValueError,match='source_changed'):
        runtime.begin_request('hand.ok',['SIMULATED'],'b'*64,'0'*32,225,300)
    assert runtime.active() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('data',[
    {'gesture':'hand.ok','request_id':'a'*64},
    {'gesture':'hand.ok','issued_at_ms':1},
    {'gesture':'hand.ok','request_id':'a'*64,'issued_at_ms':True},
    {'gesture':'hand.ok','request_id':'a'*64,'issued_at_ms':'1'},
])
async def test_console_invalid_request_never_reaches_service(flow,data):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=console_app(flow.service)),base_url='http://SIMULATED') as client:
        response=await client.post('/api/embodied/practice/start',json=data)
    assert response.status_code==422 and not flow.calls
