"""Persistent wake word -> quality/admission -> original local command routing.

All text/audio, HA and model ports here are synthetic; no capture or inference.
"""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from homeplane.embodied.arl_intent import IntentRuntime
from homeplane.embodied.home_service import HomeService
from homeplane.embodied.household import basic_command
from homeplane.embodied.intent import address,transcript_quality
from homeplane.embodied.local_intent import household_plan
from homeplane.embodied.voice_preferences import VoiceEdit,VoicePreferencesStore


def test_saved_preference_is_private_persistent_and_revision_checked(tmp_path):
    owner=VoicePreferencesStore(tmp_path)
    assert owner.read().wake_word=='Friday' and not owner.path.exists()
    saved=owner.save(VoiceEdit(expected_revision=0,wake_word='小福'))
    assert VoicePreferencesStore(tmp_path).read()==saved and saved.revision==1
    assert owner.path.stat().st_mode&0o777==0o600
    with pytest.raises(ValueError,match='changed'):
        owner.save(VoiceEdit(expected_revision=0,wake_word='Jarvis'))
    assert owner.read()==saved
    assert not list(tmp_path.glob('.voice-preferences-*'))


@pytest.mark.parametrize('word',['x','a'*25,'小福.*','小福\n','小福/abc','小福\x00'])
def test_invalid_wake_word_never_becomes_a_pattern(word):
    with pytest.raises(ValidationError):VoiceEdit(expected_revision=0,wake_word=word)


@pytest.mark.parametrize('word,text',[('小福','小福，打开客厅灯'),('Jarvis','ＪＡＲＶＩＳ，打开客厅灯'),('小爱同学','小爱同学打开客厅灯')])
def test_custom_cue_routes_without_gaze_but_does_not_accept_old_or_quoted_cue(word,text):
    assert address(text,False,wake_word=word)['admitted']
    assert basic_command(text,False,wake_word=word)['device']=='客厅灯'
    assert not address('Friday，打开客厅灯',False,wake_word=word)['admitted']
    assert not address('他说'+text,False,wake_word=word)['admitted']
    assert transcript_quality(word,wake_word=word)==(True,'accepted')
    assert address(word,False,wake_word=word)['reason']=='waiting_instruction'
    assert transcript_quality(text,{'avg_logprob':-2},wake_word=word)==(False,'low_asr_confidence')
    assert household_plan(word+'别关窗帘',False,wake_word=word)['route']=='no_op'
    assert basic_command(word+'打开客厅灯然后关空调',False,wake_word=word) is None


@pytest.mark.parametrize('origin,port',[('http://127.0.0.1:8740',8740),('http://127.0.0.1:18743/',18743),('http://127.0.0.1',80)])
def test_model_bridge_uses_configured_console_port_only(tmp_path,origin,port):
    calls=[];(tmp_path/'control-key').write_text('synthetic-worker-key-'*3)
    def transport(request):
        calls.append(request)
        return httpx.Response(200,json={'simulated':False,'native_acknowledgements_verified':True,
            'application_admission':{'scenario_id':'voice_intent','package_id':'low'},'text':'SYNTHETIC'})
    owner=IntentRuntime(client=httpx.Client(transport=httpx.MockTransport(transport)),config=lambda:{'main_origin':origin},private=tmp_path)
    try:
        assert owner.complete('SYNTHETIC',request_id='synthetic-1',voice_revision=3)['text']=='SYNTHETIC'
        assert len(calls)==1 and (calls[0].url.port or 80)==port
        assert calls[0].url.path=='/api/embodied/model'
        assert json.loads(calls[0].content)['voice_revision']==3
    finally:owner.close()


@pytest.mark.parametrize('origin',['http://127.0.0.1:0','http://127.0.0.1:65536','https://127.0.0.1:18743','http://localhost:18743','http://127.0.0.1:18743/other','http://user@127.0.0.1:18743','http://127.0.0.1:18743?x=1'])
def test_model_bridge_rejects_unqualified_owner_without_requesting_it(tmp_path,origin):
    def transport(request):raise AssertionError('No request allowed')
    owner=IntentRuntime(client=httpx.Client(transport=httpx.MockTransport(transport)),config=lambda:{'main_origin':origin},private=tmp_path)
    try:
        with pytest.raises(ValueError):owner.complete('SYNTHETIC',request_id='synthetic-1')
    finally:owner.close()


def service(owner):
    h=HomeService(camera_id='cam_1111111111',voice_preferences=owner.read)
    h.selected=h.armed=True;h.last_video=time.monotonic()
    h.command=AsyncMock();h.speaker.say=AsyncMock()
    return h


@pytest.mark.asyncio
async def test_saved_wake_word_reaches_original_fast_home_path_without_model(tmp_path,monkeypatch):
    owner=VoicePreferencesStore(tmp_path);owner.save(VoiceEdit(expected_revision=0,wake_word='小福'))
    h=service(owner)
    h.request=AsyncMock(return_value={'text':'小福，打开客厅灯','voice_revision':1,'address':{'admitted':True,'wake_word':True},'laya':{}})
    command=AsyncMock(return_value={'status':'reported_match','acknowledged':True,'reached_target':True})
    monkeypatch.setattr('homeplane.embodied.home_service.bridge.command',command)
    await h.transcribe(b'SYNTHETIC',None,time.monotonic(),False)
    assert [call.args[0] for call in h.request.call_args_list]==['transcribe']
    assert command.call_args.args[:2]==('客厅灯','on')
    assert command.call_args.kwargs['camera_id']=='cam_1111111111'
    assert h.state['voice_command']['state']=='readback'


@pytest.mark.asyncio
async def test_changed_preference_drops_old_asr_and_late_model_without_effect(tmp_path,monkeypatch):
    owner=VoicePreferencesStore(tmp_path);owner.save(VoiceEdit(expected_revision=0,wake_word='小福'))
    h=service(owner);command=AsyncMock();monkeypatch.setattr('homeplane.embodied.home_service.bridge.command',command)
    async def request(op,**kwargs):
        if op=='transcribe':
            return {'text':'小福，请看看家里再帮我处理一下','voice_revision':1,'address':{'admitted':True,'wake_word':True},'laya':{'choice':'指令或任务请求'}}
        owner.save(VoiceEdit(expected_revision=1,wake_word='小管家'))
        return {'type':'task_result','simulated':False,'provider':'synthetic-model','plan':{
            'is_command':True,'route':'home','device':'客厅灯','action':'on','reply':'正在尝试。'}}
    h.request=AsyncMock(side_effect=request)
    await h.transcribe(b'SYNTHETIC',None,time.monotonic(),False)
    assert [call.args[0] for call in h.request.call_args_list]==['transcribe','delegate']
    command.assert_not_awaited();h.command.assert_not_awaited();h.speaker.say.assert_not_awaited()
    h.request=AsyncMock(return_value={'text':'小福，打开客厅灯','voice_revision':1,'address':{'admitted':True,'wake_word':True}})
    await h.transcribe(b'SYNTHETIC',None,time.monotonic(),False)
    command.assert_not_awaited();assert h.state['reason']=='唤醒词已更新，请重新说一次'


@pytest.mark.asyncio
async def test_optional_classifier_outage_does_not_block_an_addressed_complex_request(tmp_path,monkeypatch):
    owner=VoicePreferencesStore(tmp_path);owner.save(VoiceEdit(expected_revision=0,wake_word='小福'))
    h=service(owner);command=AsyncMock();monkeypatch.setattr('homeplane.embodied.home_service.bridge.command',command)
    h.request=AsyncMock(side_effect=[{'text':'小福，为什么天空是蓝色的','voice_revision':1,
        'address':{'admitted':True,'wake_word':True},'laya':{'status':'unavailable'}},
        {'type':'task_result','simulated':False,'provider':'synthetic-codex','plan':{
            'is_command':True,'route':'reply','device':None,'action':None,'reply':'合成知识回答。'}}])
    await h.transcribe(b'SYNTHETIC question',None,time.monotonic(),False)
    assert [call.args[0] for call in h.request.call_args_list]==['transcribe','delegate']
    assert h.request.call_args.kwargs['voice_revision']==1
    command.assert_not_awaited();h.command.assert_not_awaited()
    h.speaker.say.assert_awaited_once()
    assert h.speaker.say.await_args.args == ('合成知识回答。',)
    assert callable(h.speaker.say.await_args.kwargs['current'])
    assert h.speaker.say.await_args.kwargs['current']()
    h.epoch += 1
    assert not h.speaker.say.await_args.kwargs['current']()


@pytest.mark.asyncio
async def test_preferences_http_does_not_enable_media_and_keeps_stale_write(tmp_path,monkeypatch):
    import homeplane.embodied.api as api
    from tests.test_homeplane_home_flow import application
    monkeypatch.setattr(api,'PRIVATE',tmp_path/'voice')
    async with application(tmp_path) as (app,client):
        before=app.state.embodied.capture_enabled
        current=(await client.get('/api/embodied/voice-preferences')).json()
        saved=await client.post('/api/embodied/voice-preferences',json={'expected_revision':current['revision'],'wake_word':'小福'})
        assert saved.status_code==200 and saved.json()['wake_word']=='小福'
        stale=await client.post('/api/embodied/voice-preferences',json={'expected_revision':0,'wake_word':'Jarvis'})
        assert stale.status_code==409
        assert (await client.get('/api/embodied/voice-preferences')).json()['wake_word']=='小福'
        assert app.state.embodied.capture_enabled==before


@pytest.mark.asyncio
async def test_custom_wake_flows_through_worker_bridge_original_api_and_ha_readback(tmp_path,monkeypatch):
    import homeplane.embodied.api as api
    import homeplane.embodied.bridge as bridge
    import homeplane.embodied.paths as paths
    from tests.test_homeplane_core_rules import fixture
    from tests.test_homeplane_home_flow import application
    core,rows,calls,_,grant_path,_,owner,_,runtime=await fixture(tmp_path)
    next(row for row in rows if row['entity_id'].startswith('light.'))['attributes']['friendly_name']='客厅主灯'
    adapter=core.adapter
    await runtime.close();owner.close();core.actions.close();core.store.close()
    private=tmp_path/'voice';private.mkdir()
    key='synthetic-worker-key-'*3;(private/'control-key').write_text(key)
    config=private/'config.json'
    config.write_text(json.dumps({'version':1,'main_origin':'http://testserver','default_camera':'cam_1111111111',
        'cameras':[{'id':'cam_1111111111','name':'合成镜头','room':'SIMULATED room'}]}));config.chmod(0o600)
    monkeypatch.setattr(api,'PRIVATE',private);monkeypatch.setattr(paths,'PRIVATE',private);monkeypatch.setattr(bridge,'PRIVATE',private)
    prefs=VoicePreferencesStore(private);prefs.save(VoiceEdit(expected_revision=0,wake_word='小福'))
    client_type=httpx.AsyncClient
    async with application(tmp_path,adapter=adapter,authority_path=grant_path) as (app,_):
        monkeypatch.setattr(bridge,'httpx',SimpleNamespace(AsyncClient=lambda **kwargs:client_type(transport=httpx.ASGITransport(app=app))))
        h=service(prefs)
        h.request=AsyncMock(return_value={'text':'小福，打开客厅主灯','voice_revision':1,
            'address':{'admitted':True,'wake_word':True},'laya':{}})
        await h.transcribe(b'SYNTHETIC wake command',None,time.monotonic(),False)
        assert [call.args[0] for call in h.request.call_args_list]==['transcribe']
        assert len(calls)==1 and calls[0].url.path=='/api/services/light/turn_on'
        assert h.state['voice_command']['state']=='readback'
        assert h.state['voice_command']['receipt']['reached_target'] is True
        h.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_port_rejects_old_revision_and_drops_result_after_preference_change(tmp_path,monkeypatch):
    import homeplane.embodied.api as api
    from homeplane.api import create_app
    from homeplane.arl import current_model_task
    private=tmp_path/'voice';private.mkdir();key='synthetic-model-key-'*3;(private/'control-key').write_text(key)
    monkeypatch.setattr(api,'PRIVATE',private)
    prefs=VoicePreferencesStore(private);calls=[]
    class Model:
        async def complete_scenario(self,scenario,text,**kwargs):
            calls.append(kwargs['request_id'])
            if kwargs['request_id']=='late-result':
                prefs.save(VoiceEdit(expected_revision=0,wake_word='小福'))
                assert current_model_task().current() is False
            return {'synthetic_only':True}
    app=create_app(db_path=tmp_path.resolve()/'model.sqlite',settings_path=tmp_path/'settings.json',arl_runtime=Model(),background_enabled=False)
    app.state.embodied.capture_enabled=True
    app.state.infrastructure.policy=app.state.infrastructure.policy.model_copy(update={'enabled':True})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',headers={'Origin':'http://testserver','X-Friday-Worker':key}) as client:
            stale=await client.post('/api/embodied/model',json={'request_id':'wrong-revision','input':'SYNTHETIC','voice_revision':1})
            assert stale.status_code==409 and not calls
            late=await client.post('/api/embodied/model',json={'request_id':'late-result','input':'SYNTHETIC','voice_revision':0})
            assert late.status_code==409
            fresh=await client.post('/api/embodied/model',json={'request_id':'fresh-result','input':'SYNTHETIC','voice_revision':1})
            assert fresh.status_code==200 and calls==['late-result','fresh-result']
