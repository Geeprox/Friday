"""Original admitted voice -> HTTP owner -> scoped source read -> mocked output.

All input is synthetic. No camera, microphone, model, speaker or household I/O.
"""
import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from homeplane.arl import current_model_task
from homeplane.embodied.camera_provenance import camera_source_context
from homeplane.embodied.home_service import HomeService
from homeplane.embodied.voice_preferences import VoiceEdit, VoicePreferencesStore
from homeplane.query_contracts import ReadScope
from homeplane.runtime_log import RuntimeLog
from tests.test_homeplane_home_questions import QuestionRuntime, application, make_adapter

CAMERA = 'cam_1111111111'
KEY = 'synthetic-voice-question-worker-key-'*2


@asynccontextmanager
async def setup(tmp_path, monkeypatch, *, runtime=None, scenario=None, adapter=None):
    import homeplane.embodied.api as api
    import homeplane.embodied.bridge as bridge
    import homeplane.embodied.paths as paths
    import homeplane.embodied.home_service as home
    private = tmp_path/'voice'; private.mkdir()
    selected = dict(id=CAMERA,room='客厅',name='合成摄像头',model='SYNTHETIC',region='cn',
                    device={'did':'SYNTHETIC-NOT-A-HOUSEHOLD-CAMERA'})
    config = private/'config.json'
    config.write_text(json.dumps(dict(version=1,main_origin='http://127.0.0.1:18743',
        default_camera=CAMERA,cameras=[selected])))
    config.chmod(0o600); (private/'control-key').write_text(KEY)
    monkeypatch.setattr(api,'PRIVATE',private);monkeypatch.setattr(paths,'PRIVATE',private)
    monkeypatch.setattr(bridge,'PRIVATE',private)
    monkeypatch.setattr(home,'VOICE_INPUT_PAUSED',private/'voice-input-paused')
    preferences = VoicePreferencesStore(private)
    adapter = adapter or make_adapter(tmp_path,scenario=scenario)
    async with application(tmp_path,runtime=runtime,adapter=adapter) as (app,client):
        app.state.embodied.process = SimpleNamespace(returncode=None)
        app.state.embodied.capture_enabled = app.state.embodied.interaction_enabled = True
        app.state.infrastructure.policy = app.state.infrastructure.policy.model_copy(update={'enabled':True})
        client_type = httpx.AsyncClient
        monkeypatch.setattr(bridge,'httpx',SimpleNamespace(AsyncClient=lambda **kwargs:
            client_type(transport=httpx.ASGITransport(app=app),**kwargs)))
        home_service = HomeService(camera_id=CAMERA,voice_preferences=preferences.read)
        home_service.selected = home_service.armed = True
        home_service.last_video = time.monotonic()
        home_service.runtime_log = RuntimeLog(tmp_path/'logs')
        home_service.command = AsyncMock()
        home_service.speaker.say = AsyncMock()
        effect = AsyncMock(side_effect=AssertionError('Question must not reach an actuator'))
        monkeypatch.setattr(bridge,'command',effect)
        try:
            yield SimpleNamespace(app=app,client=client,home=home_service,private=private,
                preferences=preferences,selected=selected,effect=effect)
        finally:
            app.state.embodied.process = None


def request(selected, *, text='Friday，温度多少？', attending=False, **changes):
    return dict(request_id='a'*64,camera=CAMERA,camera_source_context=camera_source_context(selected),
        text=text,attending=attending,captured_mono=time.monotonic(),voice_revision=0,**changes)


async def post(owner, body, *, headers=None):
    return await owner.client.post('/api/embodied/question',json=body,
        headers={'X-Friday-Worker':KEY,**(headers or {})})


def transcript(text, revision=0):
    return dict(text=text,voice_revision=revision,address={'admitted':True,'wake_word':True},laya={})


async def after_standalone_wake(home, text):
    home.camera_session = 'SYNTHETIC-view'
    source = home.audio_source = ('a' * 32, 'b' * 64)
    now = home.last_video = time.monotonic()
    home.request = AsyncMock(side_effect=[
        dict(type='wake', voice_revision=0, latency_ms=1),
        dict(transcript(text), address={'admitted': True, 'reason': 'wake_followup'}),
    ])
    await home.transcribe(b'SYNTHETIC wake', None, now, source=source, ended=now)
    await home.transcribe(b'SYNTHETIC followup', None, time.monotonic(), source=source, started=now)
    assert home.request.await_args.kwargs['after_wake'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('followup', [False, True])
async def test_original_voice_bridge_reads_each_source_and_speaks_without_a_model_or_effect(
        tmp_path, monkeypatch, followup):
    runtime = QuestionRuntime()
    async with setup(tmp_path,monkeypatch,runtime=runtime) as o:
        if followup:
            await after_standalone_wake(o.home, '温度多少？')
        else:
            o.home.request = AsyncMock(return_value=transcript('Friday，温度多少？'))
            await o.home.transcribe(b'SYNTHETIC', None, time.monotonic(), False)
        assert [c.args[0] for c in o.home.request.call_args_list] == ['transcribe'] * (2 if followup else 1)
        command = o.home.state['voice_command']
        assert command['state']=='answered',o.home.state.get('last_result',o.home.state)
        assert command['route']=='query' and command['receipt']['read_only'] is True
        spoken = o.home.speaker.say.call_args.args[0]
        assert spoken.startswith('示例数据。') and '24.5度' in spoken and '26.5度' in spoken
        assert '25.5' not in spoken
        report = o.home.state['last_result']['report']
        scope = report['domain']['query']['permission_scope']
        assert scope['subject']=='embodied_voice' and scope['boundary']=='qualified_ambient'
        assert scope['requested_rooms']==['客厅']
        assert report['context_token'] is None and not report['choices']
        assert 'refresh_ref' not in report['domain']
        assert o.app.state.home_flow.sessions=={} and o.app.state.home_flow.questions.operations=={}
        assert not runtime.calls
        o.home.command.assert_not_awaited();o.effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_wake_without_gaze_reaches_existing_high_query_interpreter(tmp_path,monkeypatch):
    runtime = QuestionRuntime()
    async with setup(tmp_path,monkeypatch,runtime=runtime) as o:
        o.preferences.save(VoiceEdit(expected_revision=0,wake_word='小福'))
        o.home.request=AsyncMock(return_value=transcript('小福，客厅的温度能查一下吗？',1))
        await o.home.transcribe(b'SYNTHETIC model question',None,time.monotonic(),False)
        assert o.home.state['voice_command']['state']=='answered',o.home.state.get('last_result',o.home.state)
        assert len(runtime.calls)==1 and runtime.calls[0][0]=='home_planning'
        model_input=json.loads(runtime.calls[0][1].split('\n',1)[1])
        assert set(model_input)=={'question','catalogue'} and '24.5' not in runtime.calls[0][1]
        assert not o.home.state['last_result']['device_authority']
        o.effect.assert_not_awaited();o.home.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_gaze_admitted_question_and_stale_sensor_remain_per_source(tmp_path,monkeypatch):
    async with setup(tmp_path,monkeypatch,scenario='stale-temperature') as o:
        result = await post(o,request(o.selected,text='温度多少？',attending=True))
        assert result.status_code==200,result.text
        answer=result.json()
        assert '24.5' not in answer['reply'] and '26.5' in answer['reply']
        assert '暂时没有可用读数' in answer['reply']
        values={i['entity_id']:i['value'] for i in answer['report']['domain']['query']['items']}
        assert values['sensor.living.temperature'] is None and values['climate.living']==26.5


@pytest.mark.asyncio
@pytest.mark.parametrize('text,kind',[('Friday，家里有哪些设备？','devices'),('Friday，玄关灯开着吗？','state')])
async def test_device_inventory_and_explicit_other_room_keep_original_read_kind(tmp_path,monkeypatch,text,kind):
    async with setup(tmp_path,monkeypatch) as o:
        result=await post(o,request(o.selected,text=text))
        assert result.status_code==200,result.text
        answer=result.json();query=answer['report']['domain']['query']
        assert query['kind']==kind and query['items'] and query['execution_authority'] is False
        assert not query['next_cursor']
        if kind=='devices':
            assert '玄关灯' in answer['reply'] and '在线' not in answer['reply']
        else:
            assert query['permission_scope']['requested_rooms']==[]
            assert query['permission_scope']['requested_entities']==['light.hall']
            assert {item['entity_id'] for item in query['items']}=={'light.hall'}
            assert {item['room'] for item in query['items']}=={'玄关'}
            assert '玄关灯' in answer['reply']
        o.effect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[
    {'text':'温度多少？'}, {'camera':'cam_2222222222'}, {'camera_source_context':'b'*64},
    {'voice_revision':1}, {'captured_mono':0.0}, {'captured_mono':float('inf')},
    {'text':'Friday，打开客厅灯'}, {'text':'他说 Friday，客厅温度多少？'},
    {'scope':{'all_home':True}}, {'attending':'true'},
    {'text':'Friday，客厅温度多少？\x00'},
])
async def test_unqualified_request_never_reaches_a_query_or_model(tmp_path,monkeypatch,change):
    runtime=QuestionRuntime()
    async with setup(tmp_path,monkeypatch,runtime=runtime) as o:
        read=AsyncMock(side_effect=AssertionError('No read admitted'))
        monkeypatch.setattr(o.app.state.queries,'read',read)
        body=request(o.selected);body.update(change)
        if change.get('captured_mono')==float('inf'):
            body['captured_mono']='Infinity'
        result=await post(o,body)
        assert result.status_code in {409,422},result.text
        assert not runtime.calls;read.assert_not_awaited();o.effect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('gate',['key','origin','paused','policy','capture','interaction','process'])
async def test_worker_and_current_input_gates_are_required_before_original_read(tmp_path,monkeypatch,gate):
    async with setup(tmp_path,monkeypatch) as o:
        read=AsyncMock(side_effect=AssertionError('No read admitted'))
        monkeypatch.setattr(o.app.state.queries,'read',read)
        headers={}
        if gate=='key':headers['X-Friday-Worker']='wrong'
        elif gate=='origin':headers['Origin']='http://another.invalid'
        elif gate=='paused':(o.private/'voice-input-paused').touch()
        elif gate=='policy':o.app.state.infrastructure.policy=o.app.state.infrastructure.policy.model_copy(update={'enabled':False})
        elif gate=='capture':o.app.state.embodied.capture_enabled=False
        elif gate=='interaction':o.app.state.embodied.interaction_enabled=False
        elif gate=='process':o.app.state.embodied.process.returncode=0
        result=await post(o,request(o.selected),headers=headers)
        assert result.status_code in {403,409},result.text
        read.assert_not_awaited();o.effect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['wake','camera','worker','policy','adapter'])
async def test_late_model_result_cannot_survive_source_or_input_changes(tmp_path,monkeypatch,change):
    runtime=QuestionRuntime()
    async with setup(tmp_path,monkeypatch,runtime=runtime) as o:
        original=runtime.complete_scenario
        async def complete(*args,**kwargs):
            result=await original(*args,**kwargs)
            if change=='wake':o.preferences.save(VoiceEdit(expected_revision=0,wake_word='小福'))
            elif change=='camera':
                config=json.loads((o.private/'config.json').read_text());config['cameras'][0]['device']['did']='SYNTHETIC-REPLACEMENT'
                (o.private/'config.json').write_text(json.dumps(config))
            elif change=='worker':(o.private/'control-key').write_text('replacement-synthetic-worker-'*3)
            elif change=='policy':o.app.state.infrastructure.policy=o.app.state.infrastructure.policy.model_copy(update={'interaction':False})
            elif change=='adapter':o.app.state.core.adapter=make_adapter(tmp_path)
            assert current_model_task().current() is False
            return result
        monkeypatch.setattr(runtime,'complete_scenario',complete)
        read=AsyncMock(side_effect=AssertionError('Late result must not read'))
        monkeypatch.setattr(o.app.state.queries,'read',read)
        result=await post(o,request(o.selected,text='Friday，客厅的温度能查一下吗？'))
        assert result.status_code==409,result.text
        read.assert_not_awaited();o.effect.assert_not_awaited()
        assert o.app.state.home_flow.questions.operations=={}


@pytest.mark.asyncio
async def test_cancel_fences_noncooperative_model_before_join_and_does_not_hold_app_lock(tmp_path,monkeypatch):
    runtime=QuestionRuntime();entered=asyncio.Event();cancelled=asyncio.Event();release=asyncio.Event()
    async with setup(tmp_path,monkeypatch,runtime=runtime) as o:
        original=runtime.complete_scenario
        async def complete(*args,**kwargs):
            result=await original(*args,**kwargs)
            task=current_model_task();entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                assert task.current() is False
                cancelled.set();await release.wait()
            return result
        monkeypatch.setattr(runtime,'complete_scenario',complete)
        query=asyncio.create_task(post(o,request(o.selected,text='Friday，客厅的温度能查一下吗？')))
        try:
            await asyncio.wait_for(entered.wait(),2)
            # Console bootstrap uses its original owner while this model is held.
            response=await asyncio.wait_for(o.client.post('/api/home-flow/bootstrap',json={}),1)
            assert response.status_code==200
            query.cancel();await asyncio.wait_for(cancelled.wait(),2)
            assert not query.done()
        finally:
            release.set();await asyncio.gather(query,return_exceptions=True)
        assert o.app.state.home_flow.questions.operations=={}
        o.effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_drops_late_reply_after_local_selection_change(tmp_path,monkeypatch):
    import homeplane.embodied.bridge as bridge
    original=bridge.question
    async with setup(tmp_path,monkeypatch) as o:
        async def question(*args,**kwargs):
            result=await original(*args,**kwargs)
            o.home.epoch+=1
            return result
        monkeypatch.setattr(bridge,'question',question)
        o.home.request=AsyncMock(return_value=transcript('Friday，温度多少？'))
        await o.home.transcribe(b'SYNTHETIC late question',None,time.monotonic(),False)
        assert o.home.state['voice_command']['state']=='expired'
        o.home.speaker.say.assert_not_awaited();o.effect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['before','after'])
async def test_gaze_only_question_drops_when_its_visual_track_disappears(tmp_path,monkeypatch,phase):
    import homeplane.embodied.bridge as bridge
    original=bridge.question
    async with setup(tmp_path,monkeypatch) as o:
        o.home.attention.latest={7:True} if phase=='after' else {}
        called=[]
        async def question(*args,**kwargs):
            called.append(True)
            answer=await original(*args,**kwargs)
            o.home.attention.latest={}
            return answer
        monkeypatch.setattr(bridge,'question',question)
        o.home.request=AsyncMock(return_value=transcript('温度多少？'))
        await o.home.transcribe(b'SYNTHETIC gaze owner',7,time.monotonic(),True)
        assert o.home.state['voice_command']['state']=='expired'
        assert len(called)==(1 if phase=='after' else 0)
        o.home.speaker.say.assert_not_awaited();o.effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_wake_keeps_read_admission_when_optional_gaze_track_disappears(tmp_path,monkeypatch):
    async with setup(tmp_path,monkeypatch) as o:
        o.home.attention.latest={}
        o.home.request=AsyncMock(return_value=transcript('Friday，温度多少？'))
        await o.home.transcribe(b'SYNTHETIC wake alternative',7,time.monotonic(),True)
        assert o.home.state['voice_command']['state']=='answered'
        o.home.speaker.say.assert_awaited_once();o.effect.assert_not_awaited()


def test_ambient_scope_cannot_borrow_console_identity_or_history():
    for change in [{'subject':'console'},{'history':True},{'family_media_sources':frozenset({'nas'})}]:
        values=dict(subject='embodied_voice',revision='synthetic',expires_at=datetime.now(UTC)+timedelta(seconds=5),
                    boundary='qualified_ambient',permitted=True)
        values.update(change)
        with pytest.raises(ValueError,match='ambient_read_scope'):ReadScope(**values)


@pytest.mark.asyncio
async def test_voice_read_runs_through_original_ha_adapter_without_a_service_write(tmp_path,monkeypatch):
    from tests.test_homeplane_core_rules import fixture
    core,_,calls,_,_,_,rules,_,runtime=await fixture(tmp_path)
    adapter=core.adapter
    await runtime.close();rules.close();core.actions.close();core.store.close()
    async with setup(tmp_path,monkeypatch,adapter=adapter) as o:
        snapshot=await o.app.state.core.snapshot()
        room=next(e.room for e in snapshot.entities if e.room)
        o.home.request=AsyncMock(return_value=transcript(f'Friday，{room}设备状态怎么样？'))
        await o.home.transcribe(b'SYNTHETIC HA voice question',None,time.monotonic(),False)
        assert o.home.state['voice_command']['state']=='answered',o.home.state
        query=o.home.state['last_result']['report']['domain']['query']
        assert query['items'] and query['simulated'] is False
        assert query['permission_scope']['subject']=='embodied_voice'
        assert query['execution_authority'] is False and calls==[]
        o.home.speaker.say.assert_awaited_once();o.effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_model_cannot_supply_a_reading_or_fall_back_to_a_model_answer(tmp_path,monkeypatch):
    runtime=QuestionRuntime({'kind':'state','rooms':['客厅'],'capabilities':['temperature'],'value':999})
    async with setup(tmp_path,monkeypatch,runtime=runtime) as o:
        read=AsyncMock(side_effect=AssertionError('Invalid intent must not read'))
        monkeypatch.setattr(o.app.state.queries,'read',read)
        o.home.request=AsyncMock(return_value=transcript('Friday，客厅的温度能查一下吗？'))
        await o.home.transcribe(b'SYNTHETIC invalid intent',None,time.monotonic(),False)
        assert o.home.state['voice_command']['state']=='clarify'
        assert [c.args[0] for c in o.home.request.call_args_list]==['transcribe']
        assert '999' not in o.home.speaker.say.call_args.args[0]
        read.assert_not_awaited();o.effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_generic_function_is_not_reclassified_by_its_name(tmp_path,monkeypatch):
    from homeplane.contracts import Capability
    fixture=copy.deepcopy(make_adapter(tmp_path).fixture)
    light=next(e for e in fixture['entities'] if e['id']=='light.hall')
    light['capabilities'].append(Capability(name='cover.state',access='write',support='supported').model_dump(mode='json'))
    adapter=make_adapter(tmp_path,fixture=fixture)
    async with setup(tmp_path,monkeypatch,adapter=adapter) as o:
        result=await post(o,request(o.selected,text='Friday，玄关灯开着吗？'))
        assert result.status_code==200,result.text
        answer=result.json()
        assert answer['report']['phase']=='unavailable'
        assert answer['report']['code']=='question_selection_unavailable'
        assert answer['report']['domain'] is None
        o.effect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('origin',['https://127.0.0.1:18743','http://example.invalid:18743',
                                   'http://127.0.0.1:18743/another','http://127.0.0.1:0'])
async def test_question_bridge_rejects_non_owner_origins_before_http(tmp_path,monkeypatch,origin):
    import homeplane.embodied.bridge as bridge
    async with setup(tmp_path,monkeypatch) as o:
        path=o.private/'config.json';config=json.loads(path.read_text());config['main_origin']=origin
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError,match='question_owner_origin_invalid'):
            await bridge.question('Friday，温度多少？',camera_id=CAMERA,request_id='b'*64,
                captured_mono=time.monotonic(),voice_revision=0,attending=False)
        assert o.app.state.home_flow.questions.operations=={}
