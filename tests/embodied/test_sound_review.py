"""Original camera admission -> sound reviewer -> actual private event store.

All PCM, model results, camera observations and process pipes are synthetic.
No capture worker is imported, native classifier run, or speech/device started.
"""
import asyncio
import base64
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeplane.embodied.camera_audio import AudioIngress, PCMFramer
from homeplane.embodied.camera_provenance import camera_source_context
from homeplane.embodied.safety import SafetyPolicy, SafetyRuntime, SafetyStore
from homeplane.embodied.sound_model import AppleSoundProcess, catalog, sound_result
from homeplane.embodied.sound_review import SoundReviewer

CAMERA = dict(id='cam_1111111111', device={'did':'synthetic-only'})
CONTEXT = camera_source_context(CAMERA)


class Engine:
    def __init__(self, result=None):
        self.analyze = AsyncMock(return_value=result or {'kind':'crying','confidence':.96})
        self.close = AsyncMock()
    def readiness(self):
        return dict(available=True,reason=None,labels_verified=True,supported_types=['crying'])


async def settle():
    for _ in range(4):await asyncio.sleep(0)


class Journey:
    def __init__(self, tmp_path, monkeypatch, *, enabled=True, sound=True):
        from homeplane.embodied import safety as module
        self.clock = 0.
        monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:self.clock,time=lambda:1000+self.clock))
        monkeypatch.setattr('homeplane.embodied.paths.PRIVATE',tmp_path)
        self.private = tmp_path
        self.store = SafetyStore(tmp_path/'events.sqlite')
        self.store.configure(SafetyPolicy(enabled=enabled,sound_review=sound))
        self.engine = Engine()
        self.runtime = SafetyRuntime(self.store,sound_engine=self.engine,speaker=SimpleNamespace(say=AsyncMock()))
        self.runtime.sound.clock = lambda:self.clock
        self.runtime.sound.wall = lambda:1000+self.clock
        self.framer = PCMFramer(CAMERA,'a'*32)
        self.gate = AudioIngress(CAMERA,'a'*32)

    async def tick(self, index, *, wrong_source=False, stable=True):
        self.clock = index*.2
        await self.runtime.observe(CAMERA['id'],'visual-session',[],self.clock,self.clock,
            stable=stable,jpeg=b'SYNTHETIC-FRAME',source_context=CONTEXT,source_name='示例镜头')
        frame=dict(type='frame',received_at=1000+self.clock,capture_session='a'*32,camera_source_context=CONTEXT)
        assert self.gate.observe_frame(frame,wall=1000+self.clock,mono=self.clock)
        packet=self.framer.push(bytes(6400),received=1000+self.clock,decoded=1000+self.clock,
                                wall=1000+self.clock,mono=self.clock)[0]
        if wrong_source:packet['camera_source_context']='b'*64
        self.runtime.observe_audio(CAMERA['id'],self.gate.accept(packet,wall=1000+self.clock,mono=self.clock))
        await settle()


@pytest.mark.asyncio
async def test_actual_qualified_audio_creates_one_reviewable_event_without_audio_or_speech(tmp_path,monkeypatch):
    j=Journey(tmp_path,monkeypatch)
    try:
        for i in range(10):await j.tick(i)
        assert j.engine.analyze.await_count==1 and j.store.events()==[]
        for i in range(10,22):await j.tick(i)
        events=j.store.events();assert len(events)==1
        e=events[0]
        assert e['kind']=='sound_review' and e['sound_kind']=='crying' and e['status']=='pending_review'
        assert e['person_id'] is e['person_name'] is e['track'] is None
        assert e['camera_source_context']==CONTEXT and e['camera_name']=='示例镜头' and e['frames']
        assert e['sound_evidence']==dict(confidence=.96,corroborating_windows=2,audio_seconds=4,age_basis='local_sdk_receive')
        assert e['raw_audio_stored'] is e['directional_audio'] is False
        assert 'pcm' not in json.dumps(e) and e['announcement']['status']=='not_requested'
        j.runtime.speaker.say.assert_not_awaited()
        for i in range(22,60):await j.tick(i)
        assert len(j.store.events())==1,'Same episode cannot flood the event history'
        assert all(len(call.args[0])==64000 for call in j.engine.analyze.call_args_list)
        assert len(j.runtime.sound.windows[CAMERA['id']].packets)==10
    finally:await j.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['quiet', 'gap', 'busy', 'model_error', 'retired'])
async def test_two_infant_windows_require_a_continuous_third_before_sustained_review(tmp_path, monkeypatch, ending):
    j = Journey(tmp_path, monkeypatch)
    infant = sound_result(reply(), catalog(['baby_crying', 'screaming']))
    third = infant
    if ending == 'quiet':
        third = None
    elif ending == 'model_error':
        third = RuntimeError('synthetic classifier fault')
    j.engine.analyze.side_effect = [infant, infant, third]
    try:
        for index in range(20):
            await j.tick(index)
        assert j.engine.analyze.await_count == 2 and not j.store.events()
        assert j.runtime.review_pending(CAMERA['id'], 'visual-session')
        if ending == 'gap':
            j.runtime.sound.retry_after = 6.8
        elif ending == 'busy':
            j.runtime.sound.retry_after = 20
        elif ending == 'retired':
            await j.tick(20, stable=False)
            assert not j.runtime.review_pending(CAMERA['id'], 'visual-session')
        start = 21 if ending == 'retired' else 20
        end = 44 if ending in {'busy', 'model_error'} else 36
        for index in range(start, end):
            await j.tick(index)
            await j.runtime.sound.maintain()
        await j.runtime.drain()
        assert j.engine.analyze.await_count == (2 if ending == 'busy' else 3)
        events = j.store.events()
        if ending == 'retired':
            assert not events
        else:
            assert len(events) == 1
            event = events[0]
            assert event['sound_kind'] == 'crying' and event['captured_at'] == pytest.approx(1003.8)
            assert event['sound_evidence']['corroborating_windows'] == 2
            assert 'pattern' not in event['sound_evidence']
    finally:
        await j.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('committed', [False, True])
async def test_qualified_sound_retries_same_incident_without_another_sound(tmp_path, monkeypatch, committed):
    j = Journey(tmp_path, monkeypatch)
    create = j.store.create
    attempts = []

    def interrupted_write(event, frames, **options):
        attempts.append((options.get('event_id'), event['captured_at']))
        if len(attempts) == 1:
            if committed:
                saved = create(event, frames, **options)
                j.store.update(saved['id'], review='false_alarm')
            raise sqlite3.OperationalError('synthetic interrupted write')
        return create(event, frames, **options)

    monkeypatch.setattr(j.store, 'create', interrupted_write)
    try:
        for index in range(22):
            await j.tick(index)
        for index in range(22, 29):
            j.clock = index * .2
            await j.runtime.observe(CAMERA['id'], 'visual-session', [], j.clock, j.clock,
                                    stable=True, jpeg=b'SYNTHETIC-FRAME', source_context=CONTEXT)
        await asyncio.wait_for(j.runtime.drain(), 1)
        events = j.store.events()
        assert len(attempts) == 2 and attempts[0] == attempts[1], '必须保留已判定的事件和采集时间'
        assert len(events) == 1 and (events[0]['id'], events[0]['captured_at']) == attempts[0]
        assert events[0]['review'] == ('false_alarm' if committed else 'unreviewed')
        assert events[0]['frames'] and j.engine.analyze.await_count == 2
        assert j.runtime.sound.error is None
        j.runtime.speaker.say.assert_not_awaited()
    finally:
        await j.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('pause_input', [False, True])
async def test_corroborated_sound_does_not_hold_classifier_while_the_store_waits(tmp_path, monkeypatch, pause_input):
    j = Journey(tmp_path, monkeypatch)
    create = j.store.create
    entered, release = threading.Event(), threading.Event()
    other = dict(id='cam_2222222222', device={'did': 'synthetic-secondary'})
    capture = 'b' * 32
    context = camera_source_context(other)
    framer, gate = PCMFramer(other, capture), AudioIngress(other, capture)

    def held_write(*args, **options):
        entered.set()
        assert release.wait(4)
        return create(*args, **options)

    async def secondary_input():
        assert j.runtime.offer(other['id'], 'secondary-visual', [], j.clock, j.clock,
                               stable=True, jpeg=b'SYNTHETIC-SECONDARY', source_context=context)
        if j.runtime.observation_task:
            await j.runtime.observation_task
        stamp = 1000 + j.clock
        assert gate.observe_frame(dict(type='frame', received_at=stamp, capture_session=capture,
                                       camera_source_context=context), wall=stamp, mono=j.clock)
        packet, = framer.push(bytes([1]) * 6400, received=stamp, decoded=stamp, wall=stamp, mono=j.clock)
        j.runtime.observe_audio(other['id'], gate.accept(packet, wall=stamp, mono=j.clock))
        await settle()

    monkeypatch.setattr(j.store, 'create', held_write)
    try:
        for index in range(20):
            await j.tick(index)
            if index >= 15:
                await secondary_input()
        assert await asyncio.to_thread(entered.wait, 1)
        for index in range(20, 25):
            j.clock = index * .2
            assert j.runtime.offer(CAMERA['id'], 'visual-session', [], j.clock, j.clock,
                                   stable=True, jpeg=b'SYNTHETIC-FRAME', source_context=CONTEXT)
            await secondary_input()
        assert j.engine.analyze.await_count == 3 and j.engine.analyze.await_args.args[0][0] == 1
        assert j.runtime.sound.task is None and j.runtime.recording_status()['in_flight'] == 1
        assert j.runtime.review_pending(CAMERA['id'], 'visual-session')
        if pause_input:
            from fastapi import HTTPException, Request

            from tests.embodied.test_review_motion import source_function

            control = source_function('homeplane/embodied/worker.py', 'voice_control', dict(
                Request=Request, HTTPException=HTTPException, PRIVATE=tmp_path, safety=j.runtime,
                home=SimpleNamespace(armed=False, close=AsyncMock()), secondary_homes={}))
            request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'), json=AsyncMock(return_value={'input':False}))
            assert (await control(request))['voice_input'] is False
            request.json.return_value = {'input':True}
            assert (await control(request))['voice_input'] is True
            assert not j.runtime.review_pending(CAMERA['id'], 'visual-session')
        release.set()
        await asyncio.wait_for(j.runtime.drain(), 1)
        assert len(j.store.events()) == (0 if pause_input else 1)
    finally:
        release.set()
        await j.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('condition',['disabled','sound_off','audio_paused','moving','wrong_source'])
async def test_ineligible_camera_audio_never_reaches_classifier(tmp_path,monkeypatch,condition):
    j=Journey(tmp_path,monkeypatch,enabled=condition!='disabled',sound=condition!='sound_off')
    if condition=='audio_paused':(tmp_path/'voice-input-paused').touch()
    try:
        for i in range(22):await j.tick(i,wrong_source=condition=='wrong_source',stable=condition!='moving')
        j.engine.analyze.assert_not_awaited();assert j.store.events()==[]
        assert not j.runtime.sound.windows
    finally:await j.runtime.close()


@pytest.mark.asyncio
async def test_replayed_packets_break_corroboration_and_current_input_recovers(tmp_path,monkeypatch):
    j=Journey(tmp_path,monkeypatch)
    try:
        for i in range(10):await j.tick(i)
        assert j.engine.analyze.await_count==1
        bad=j.gate.reject('sequence_replayed');j.runtime.observe_audio(CAMERA['id'],bad)
        assert not j.runtime.sound.windows
        for i in range(10,22):await j.tick(i)
        assert not j.store.events()
        for i in range(22,34):await j.tick(i)
        assert len(j.store.events())==1
    finally:await j.runtime.close()


@pytest.mark.asyncio
async def test_policy_revoked_between_result_check_and_sql_commit_cannot_persist(tmp_path,monkeypatch):
    j=Journey(tmp_path,monkeypatch);create=j.store.create;attempted=[]
    def revoke_before_commit(event, frames=(), **kwargs):
        attempted.append(True)
        policy=j.store.policy();j.store.configure(policy.model_copy(update={'sound_review':False}))
        return create(event,frames,**kwargs)
    monkeypatch.setattr(j.store,'create',revoke_before_commit)
    try:
        for i in range(22):await j.tick(i)
        assert attempted and not j.store.events()
    finally:await j.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke',['policy','camera','input_pause'])
async def test_late_native_result_cannot_survive_revocation(tmp_path,monkeypatch,revoke):
    j=Journey(tmp_path,monkeypatch);started=asyncio.Event();release=asyncio.Event()
    async def delayed(_):
        started.set()
        try:await release.wait()
        except asyncio.CancelledError:pass  # Model transport returning late cannot grant a new lease.
        return {'kind':'crying','confidence':.99}
    j.engine.analyze.side_effect=delayed
    try:
        for i in range(10):await j.tick(i)
        await started.wait()
        if revoke=='policy':
            p=j.store.policy();j.store.configure(p.model_copy(update={'sound_review':False}))
            await j.runtime.sync_policy()
        elif revoke=='camera':j.runtime.clear(CAMERA['id'])
        else:(tmp_path/'voice-input-paused').touch()
        release.set();await settle()
        assert not j.store.events()
    finally:release.set();await j.runtime.close()


@pytest.mark.asyncio
async def test_classifier_fault_is_private_backed_off_and_does_not_block_camera_observations(tmp_path,monkeypatch):
    j=Journey(tmp_path,monkeypatch);j.engine.analyze.side_effect=RuntimeError('SECRET upstream path')
    try:
        for i in range(22):await j.tick(i)
        assert j.engine.analyze.await_count==1 and j.runtime.sound.error=='sound_review_deferred'
        assert 'SECRET' not in json.dumps(j.runtime.status())
        assert j.runtime.last_seen[CAMERA['id']]==j.clock and not j.store.events()
        j.engine.analyze.side_effect=None
        for i in range(85,108):await j.tick(i)
        assert j.engine.analyze.await_count>=3 and len(j.store.events())==1
    finally:await j.runtime.close()


def reply(scores=None, **updates):
    window=dict(offset=0,duration=2,scores=scores or {'baby_crying':.95,'screaming':.02},competing_confidence=.01)
    window.update(updates)
    return dict(windows=[window])


def test_host_vocabulary_is_authoritative_and_competing_sounds_are_not_alerts():
    supported=catalog(['baby_crying','screaming','music'])
    assert supported=={'crying':('baby_crying',),'scream':('screaming',)}
    assert catalog(['music','different_cry_label'])=={}
    assert sound_result(reply(),supported)=={'kind':'crying','confidence':.95,'infant_cry':True}
    assert sound_result(reply(competing_confidence=.9),supported) is None
    assert sound_result(reply(scores={'baby_crying':.84,'screaming':.02}),supported) is None


def test_infant_label_must_be_distinct_from_other_cry_labels_on_every_native_result():
    supported = catalog(['baby_crying', 'crying'])
    infant = reply(scores={'baby_crying': .96, 'crying': .02})
    uncertain = reply(scores={'baby_crying': .96, 'crying': .90})
    combined = dict(windows=[*infant['windows'], *uncertain['windows']])
    assert sound_result(infant, supported)['infant_cry'] is True
    assert sound_result(combined, supported) == {'kind': 'crying', 'confidence': .96}
    assert sound_result(reply(scores={'crying': .92}), catalog(['crying'])) == {'kind': 'crying', 'confidence': .92}


@pytest.mark.asyncio
@pytest.mark.parametrize('supported,expected', [(None, 'experimental'), (True, 'experimental'), (False, 'unavailable')])
async def test_safety_status_uses_the_worker_vocabulary_for_infant_cry(tmp_path, monkeypatch, supported, expected):
    import httpx
    from fastapi import APIRouter, FastAPI

    from homeplane.embodied import safety_api

    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    worker = SimpleNamespace(process=SimpleNamespace(returncode=None), request=AsyncMock(return_value=dict(
        sound_review=dict(available=True, labels_verified=supported is not None, infant_cry_supported=supported))))
    router = APIRouter()
    runtime = safety_api.install(router, worker)
    app = FastAPI()
    app.include_router(router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
            report = (await client.get('/api/embodied/safety')).json()
        crying = next(row for row in report['capabilities'] if row['id'] == 'crying')
        assert crying['status'] == expected and report['sound_review']['infant_cry_supported'] is supported
        worker.request.assert_awaited_once_with('/api/safety/status')
    finally:
        await runtime.close()


@pytest.mark.parametrize('bad',[dict(scores={'invented':.99}),dict(scores={'baby_crying':float('nan'),'screaming':.1}),
    dict(duration=.1),dict(duration=20),dict(offset=-1),dict(offset=1),dict(competing_confidence=True)])
def test_invalid_native_evidence_is_refused(bad):
    original=dict(offset=0,duration=2,scores={'baby_crying':.99,'screaming':.01},competing_confidence=.001)
    original.update(bad)
    with pytest.raises(ValueError,match='sound_protocol_invalid'):
        sound_result(dict(windows=[original]),catalog(['baby_crying','screaming']))


@pytest.mark.asyncio
async def test_two_camera_latest_windows_are_served_fairly_without_concurrent_models():
    from homeplane.embodied.camera_audio import AdmittedAudio
    now=[0.];policy=SafetyPolicy(enabled=True,sound_review=True)
    engine=Engine();delivered=[]
    r=SoundReviewer(policy=lambda:policy,current=lambda *_:True,deliver=lambda *args:delivered.append(args),
                    engine=engine,clock=lambda:now[0],wall=lambda:1000+now[0])
    try:
        for i in range(24):
            now[0]=i*.2
            for camera in ('a','b'):
                p=dict(capture_session='session',audio_epoch=0,audio_sequence=i+1,sample_start=i*3200,
                       pcm=base64.b64encode(bytes(6400)).decode())
                r.offer(camera,'visual',CONTEXT,0,AdmittedAudio(p,i==0,{},now[0]))
            await settle()
        assert {args[0] for args in delivered}=={'a','b'}
        assert len(r.windows)==2 and all(len(w.packets)==10 for w in r.windows.values())
    finally:await r.close()


@pytest.mark.asyncio
async def test_native_adapter_validates_protocol_and_releases_the_exact_process(tmp_path,monkeypatch):
    from homeplane.embodied import sound_model as module
    binary=tmp_path/'worker';binary.write_text('synthetic executable; never run');binary.chmod(0o700)
    monkeypatch.setattr(module.platform,'system',lambda:'Darwin')
    process=SimpleNamespace(returncode=None,stdin=SimpleNamespace(write=Mock(),drain=AsyncMock(),close=Mock()),
        stdout=SimpleNamespace(readline=AsyncMock(side_effect=[json.dumps(dict(type='ready',version=1,
            sample_rate=16000,known_classifications=['baby_crying'])).encode(),
            json.dumps(dict(type='result',version=1,id=1,windows=[dict(offset=0,duration=2,
                scores={'baby_crying':.95},competing_confidence=.01)])).encode()]),read=AsyncMock(return_value=b'')),
        terminate=Mock(),wait=AsyncMock(return_value=0),kill=Mock())
    spawn=AsyncMock(return_value=process);monkeypatch.setattr(module.asyncio,'create_subprocess_exec',spawn)
    model=AppleSoundProcess(binary,tmp_path,budget=lambda:SimpleNamespace(total_bytes=8*1024**3,free_percent=50,load=0,cores=4))
    assert model.readiness()['available'] and not model.readiness()['labels_verified'];spawn.assert_not_awaited()
    assert model.readiness()['infant_cry_supported'] is None
    assert await model.analyze(bytes(64000))=={'kind':'crying','confidence':.95,'infant_cry':True}
    assert model.readiness()['infant_cry_supported'] is True
    request=json.loads(process.stdin.write.call_args.args[0]);assert len(base64.b64decode(request['pcm']))==64000
    assert request['identifiers']==['baby_crying']
    await model.close();process.terminate.assert_called_once();assert model.lease is None
    assert sorted(p.name for p in tmp_path.iterdir())==['sound-analysis.lock','worker']


@pytest.mark.asyncio
async def test_native_adapter_does_not_start_when_host_budget_is_unknown(tmp_path,monkeypatch):
    from homeplane.embodied import sound_model as module
    spawn=AsyncMock();monkeypatch.setattr(module.asyncio,'create_subprocess_exec',spawn)
    model=AppleSoundProcess(tmp_path/'missing',tmp_path,budget=lambda:SimpleNamespace(total_bytes=8*1024**3,free_percent=None,load=0,cores=4))
    with pytest.raises(ValueError,match='sound_memory_pressure'):await model.analyze(bytes(64000))
    spawn.assert_not_awaited();assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['primary','secondary'])
async def test_original_camera_pipe_delivers_sound_without_an_active_voice_owner(tmp_path,monkeypatch,source):
    from collections import deque
    from tests.embodied.test_camera_audio import actual,voice_service
    from homeplane.embodied import home_service as home_module
    from homeplane.embodied.camera_audio import deliver_audio
    j=Journey(tmp_path,monkeypatch);index=0;frame_pending=True
    monkeypatch.setattr(home_module,'VOICE_INPUT_PAUSED',tmp_path/'voice-input-paused')
    monkeypatch.setattr(home_module,'time',SimpleNamespace(monotonic=lambda:j.clock))
    home=voice_service(home_module);home.selected=home.audio_allowed=False
    async def read():
        nonlocal index,frame_pending
        await settle()
        if index>=24:return b''
        j.clock=index*.2
        if frame_pending:
            await j.runtime.observe(CAMERA['id'],'visual-session',[],j.clock,j.clock,stable=True,
                jpeg=b'SYNTHETIC-FRAME',source_context=CONTEXT)
            result=dict(type='frame',received_at=1000+j.clock,capture_session='a'*32,
                        camera_source_context=CONTEXT,jpeg='SYNTHETIC')
        else:
            result=j.framer.push(bytes(6400),received=1000+j.clock,decoded=1000+j.clock,
                wall=1000+j.clock,mono=j.clock)[0]
            index+=1
        frame_pending=not frame_pending
        return json.dumps(result).encode()
    time=SimpleNamespace(monotonic=lambda:j.clock,time=lambda:1000+j.clock)
    process=SimpleNamespace(stdout=SimpleNamespace(readline=read),
                            stdin=SimpleNamespace(write=Mock(),drain=AsyncMock()))
    try:
        if source=='primary':
            scope=dict(capture=process,state={'status':'live'},home=home,audio_ingress=j.gate,
                safety=j.runtime,deliver_audio=deliver_audio,SELECTED=CAMERA,generation=1,my_generation=1,
                fleet=SimpleNamespace(voice_current=lambda *_:False),time=time,json=json,queue=object(),
                wall=SimpleNamespace(offer=Mock(),video={CAMERA['id']:SimpleNamespace(subscribers=0)}),
                wall_epoch=1,primary_wall_epoch=1,primary_capture_closed=AsyncMock(),
                last_video_lease=0,captured_times=deque(),offer_frame=lambda *_:None,
                asyncio=asyncio,motion_generation=4)
            await actual('homeplane/embodied/worker.py','receive',scope)()
        else:
            # Background sound review does not need a HomeService/voice owner.
            callback=actual('homeplane/embodied/worker.py','secondary_audio',dict(
                safety=j.runtime,secondary_homes={},time=time,deliver_audio=deliver_audio))
            scope=dict(process=process,self=SimpleNamespace(on_audio=callback,wall=None),
                selected=CAMERA,audio_ingress=j.gate,item={'privacy_closed':False},waiters={},
                queue=asyncio.Queue(maxsize=1),last_video_lease=0,wall_epoch=1,time=time,json=json,
                offer_frame=lambda *_:None,retire_source=Mock())
            await actual('homeplane/embodied/previews.py','receive',scope)()
        await settle()
        assert j.engine.analyze.await_count>=2 and len(j.store.events())==1
        home.transcribe.assert_not_awaited()
    finally:await j.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('paused',[False,True])
async def test_original_capture_lease_can_review_sound_with_voice_off_but_respects_input_pause(tmp_path,monkeypatch,paused):
    from homeplane.embodied import home_service as module
    from tests.embodied.test_camera_audio import voice_service
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:0))
    marker=tmp_path/'paused';monkeypatch.setattr(module,'VOICE_INPUT_PAUSED',marker)
    if paused:marker.touch()
    h=voice_service(module);h.selected=h.enabled=h.armed=False;h.voice_provider=None;h.gate_sent=-1
    h.attention=SimpleNamespace(update=Mock(),candidate=lambda *_:None);h.gesture=SimpleNamespace(reset=Mock())
    process=SimpleNamespace(stdin=SimpleNamespace(write=Mock(),drain=AsyncMock(),
        is_closing=lambda:False,transport=SimpleNamespace(get_write_buffer_size=lambda:0)))
    await h.observe([],[],{'status':'locked'},0,process,False,sound_review=True)
    packet=json.loads(process.stdin.write.call_args.args[0])
    assert packet['action']=='voice_window' and packet['enabled']==(not paused)
    assert packet['expires_mono']==pytest.approx(1.3)
    process.stdin.drain.assert_not_awaited()
    assert h.selected is False


@pytest.mark.parametrize('success',[True,False])
def test_explicit_sound_build_retains_only_runtime_result_and_bounded_receipt(tmp_path,monkeypatch,success):
    import runpy
    from pathlib import Path
    namespace=runpy.run_path('scripts/embodied/runtime.py')
    build=namespace['build_sound'];scope=build.__globals__
    source=tmp_path/'project/native/EmbodiedVision/SoundWorker.swift';source.parent.mkdir(parents=True)
    source.write_text('// SYNTHETIC input; compiler is not executed')
    scope['ROOT']=tmp_path/'project';scope['DATA']=tmp_path/'operator-runtime'
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    def compile_only(command,**kwargs):
        candidate=Path(command[-1]);(candidate.parent/'modules').mkdir();(candidate.parent/'modules/cache').write_text('synthetic')
        if success:candidate.write_bytes(b'SYNTHETIC compiled bytes; never executable')
        assert Path(kwargs['env']['TMPDIR'])==candidate.parent and kwargs['timeout']==180
        return SimpleNamespace(returncode=0 if success else 1,stdout='',stderr='')
    monkeypatch.setattr(scope['subprocess'],'run',compile_only)
    if success:
        result=build();assert result['status']=='built'
        assert (scope['DATA']/'bin/friday-sound').read_bytes().startswith(b'SYNTHETIC')
    else:
        with pytest.raises(RuntimeError,match='sound_build_failed'):build()
        assert not scope['DATA'].exists()
    receipts=list((tmp_path/'Library/Caches/CodexBuilds.noindex').rglob('manifest.json'))
    assert len(receipts)==1
    run=receipts[0].parent
    assert sorted(p.name for p in run.iterdir())==['compile.log','manifest.json']
    assert json.loads(receipts[0].read_text())['temporary_outputs_removed'] is True
