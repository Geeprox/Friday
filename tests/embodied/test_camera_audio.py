"""Real capture/worker entry functions with in-memory PCM; no model, mic or SDK."""

import ast
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from homeplane.embodied.camera_audio import AudioIngress, PCMFramer, deliver_audio, voice_window_deadlines
from homeplane.embodied.camera_provenance import camera_source_context


CAMERA=dict(id='cam_1111111111',device={'did':'synthetic-camera'})


def actual(path, name, scope):
    nodes=[n for n in ast.walk(ast.parse(Path(path).read_text()))
           if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name]
    assert len(nodes)==1
    node=nodes[0];node.decorator_list=[]
    node.body=[ast.copy_location(ast.Global(names=n.names),n) if isinstance(n,ast.Nonlocal) else n
               for n in node.body]
    exec(compile(ast.Module(body=[node],type_ignores=[]),path,'exec'),scope)
    return scope[name]


@pytest.mark.asyncio
async def test_capture_does_not_relabel_audio_left_before_a_gap_as_current():
    output=[];clock=[10.0]
    scope=dict(audio_observed=True,audio_until=20,sound_until=0,
               time=SimpleNamespace(monotonic=lambda:clock[0],time=lambda:1000+clock[0]),
               base64=base64,emit=output.append)
    scope['audio_framer']=PCMFramer(CAMERA,'a'*32)
    decode=actual('homeplane/embodied/capture_worker.py','decoded_audio',scope)
    await decode('synthetic-camera',np.ones(2000,dtype=np.int16),0,0,1010000,1010000)
    clock[0]=14
    await decode('synthetic-camera',np.ones(1200,dtype=np.int16),1,0,1014000,1014000)
    assert not output,'A partial old buffer must be discarded at the gap, not given the newer timestamp'


@pytest.mark.asyncio
async def test_video_observation_survives_pipe_pressure_and_capture_cannot_reopen_expired_audio(tmp_path, monkeypatch):
    from homeplane.embodied import home_service as module

    clock = [10.0]
    monotonic = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(module, 'time', monotonic)
    paused = tmp_path / 'paused'
    monkeypatch.setattr(module, 'VOICE_INPUT_PAUSED', paused)
    service = voice_service(module)
    service.ensure_voice = Mock()
    service.attention = SimpleNamespace(update=Mock(), candidate=lambda *_: None)
    buffered = [32]
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), is_closing=lambda: False,
                             transport=SimpleNamespace(get_write_buffer_size=lambda: buffered[0]))
    process = SimpleNamespace(stdin=writer)

    async def observe(captured):
        await service.observe([dict(id=1)], [], {'status': 'locked'}, clock[0], process, True,
                              session_id='camera-session', captured_mono=captured)

    for tick in (10.0, 10.5, 11.0):
        clock[0] = tick
        await observe(tick)
    writer.write.assert_not_called()
    assert service.last_video == 11 and service.visible_tracks == {1}

    buffered[0] = 0
    clock[0] = 11.5
    await observe(11.1)
    clock[0] = 12.0
    writer.write.side_effect = BrokenPipeError
    await observe(12.0)
    writer.write.side_effect = None
    clock[0] = 12.5
    await observe(12.5)
    paused.touch()
    clock[0] = 13.0
    await observe(13.0)
    assert writer.write.call_count == 4 and service.last_video == 13.0
    writer.drain.assert_not_awaited()
    expired, _, fresh, muted = [call.args[0] for call in writer.write.call_args_list]
    assert json.loads(expired)['expires_mono'] == pytest.approx(12.6)

    arrivals = iter([(12.7, expired), (12.85, fresh), (13.0, muted)])
    deadlines = []

    async def readline():
        deadlines.append((scope['audio_until'], scope['attention_until']))
        row = next(arrivals, None)
        if row is None:
            return b''
        clock[0], packet = row
        return packet

    framer = PCMFramer(CAMERA, 'a' * 32)
    assert framer.push(bytes(3200), received=1012.5, decoded=1012.5, wall=1012.5, mono=12.5) == []
    epoch = framer.epoch
    scope = dict(reader=SimpleNamespace(readline=readline), json=json, audio_until=12.6, sound_until=0, attention_until=12.6,
                 time=monotonic, audio_framer=framer, voice_window_deadlines=voice_window_deadlines, emit=Mock())
    await actual('homeplane/embodied/capture_worker.py', 'controls', scope)()
    assert deadlines[1] == (0.0, 0.0)
    assert deadlines[2] == pytest.approx((13.8, 13.8))
    assert deadlines[3] == pytest.approx((0.0, 14.3))
    assert not framer.pending and framer.epoch > epoch
    scope['emit'].assert_not_called()


@pytest.mark.asyncio
async def test_audio_capture_preserves_sound_window_when_voice_is_revoked():
    clock = [10.0]
    frames = []
    framer = PCMFramer(CAMERA, 'a' * 32)
    assert framer.push(bytes(3200), received=1010, decoded=1010, wall=1010, mono=10) == []
    initial_epoch = framer.epoch
    commands = iter([
        (10.1, dict(action='sound_window', enabled=True, attention=True, expires_mono=11.2)),
        (10.2, dict(action='voice_window', enabled=False, attention=False, expires_mono=11.2)),
        (10.3, dict(action='sound_window', enabled=False, expires_mono=11.2)),
    ])
    time = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0])
    scope = dict(time=time, json=json, audio_framer=framer, audio_until=10.8, attention_until=10.8,
                 sound_until=0, voice_window_deadlines=voice_window_deadlines, emit=frames.append, audio_observed=True)
    decode = actual('homeplane/embodied/capture_worker.py', 'decoded_audio', scope)

    async def readline():
        item = next(commands, None)
        if item is None:
            assert framer.epoch > initial_epoch and not framer.pending
            await decode('synthetic', np.ones(3200, dtype=np.int16), 0, 0, 1010300, 1010300)
            return b''
        clock[0], command = item
        if command['action'] == 'voice_window':
            assert scope['attention_until'] == 10.8
        if clock[0] == 10.3:
            assert framer.epoch == initial_epoch
            assert scope['audio_until'] == scope['attention_until'] == 0
            await decode('synthetic', np.ones(1600, dtype=np.int16), 0, 0, 1010300, 1010300)
        return json.dumps(command).encode()

    scope['reader'] = SimpleNamespace(readline=readline)
    await actual('homeplane/embodied/capture_worker.py', 'controls', scope)()
    assert len(frames) == 1 and frames[0]['audio_epoch'] == initial_epoch


@pytest.mark.parametrize('deadline', [None, True, float('nan'), 10, 11.31])
def test_capture_rejects_invalid_or_stale_voice_window_deadlines(deadline):
    command = dict(enabled=True, attention=True, expires_mono=deadline)
    assert voice_window_deadlines(command, 10) == (0.0, 0.0)


@pytest.mark.asyncio
async def test_secondary_old_audio_cannot_reach_voice_recognition():
    service=SimpleNamespace(audio_allowed=True,audio=AsyncMock(),reset_audio=Mock(),state={})
    scope=dict(secondary_homes={CAMERA['id']:service},fleet=SimpleNamespace(voice_current=lambda *_:True),
               time=__import__('time'),deliver_audio=deliver_audio,safety=SimpleNamespace(observe_audio=Mock()))
    callback=actual('homeplane/embodied/worker.py','secondary_audio',scope)
    await callback(CAMERA['id'],dict(type='audio',sample_rate=16000,received_at=1,
                   pcm=base64.b64encode(bytes(6400)).decode()))
    service.audio.assert_not_awaited()


def audio(at=0, *, framer=None):
    framer=framer or PCMFramer(CAMERA,'a'*32)
    return framer.push((np.ones(3200,dtype='<i2')*1024).tobytes(),received=1000+at,
                       decoded=1000+at,wall=1000+at,mono=at)[0]


def video(gate, at):
    return gate.observe_frame(dict(type='frame',capture_session='a'*32,
        camera_source_context=camera_source_context(CAMERA),received_at=1000+at),wall=1000+at,mono=at)


def test_framer_exports_oldest_receipt_time_and_bounded_monotonic_sequence():
    f=PCMFramer(CAMERA,'a'*32)
    assert f.push(bytes(3200),received=1000,decoded=1000,wall=1000,mono=0)==[]
    p=f.push(bytes(3200),received=1000.1,decoded=1000.1,wall=1000.1,mono=.1)[0]
    assert p['received_at']==1000 and p['decoded_at']==1000.1
    assert p['audio_sequence']==1 and p['sample_start']==0
    assert p['capture_session']=='a'*32 and p['camera_source_context']==camera_source_context(CAMERA)
    assert p['channels']==1 and p['sample_rate']==16000 and p['sample_format']=='s16le'
    more=f.push(bytes(32000),received=1000.2,decoded=1000.2,wall=1000.2,mono=.2)
    assert len(more)==5 and [p['audio_sequence'] for p in more]==list(range(2,7))
    assert len(f.pending)<6400


@pytest.mark.parametrize('bad',[dict(received=999),dict(decoded=1001),dict(received=float('nan')),
    dict(mono=-1),dict(raw=b'odd'),dict(raw=bytes(32002))])
def test_invalid_capture_input_discards_partial_audio(bad):
    f=PCMFramer(CAMERA,'a'*32)
    assert f.push(bytes(3000),received=1000,decoded=1000,wall=1000,mono=0)==[]
    args=dict(raw=bytes(1000),received=1000,decoded=1000,wall=1000,mono=0);args.update(bad)
    assert f.push(**args)==[] and not f.pending and not f.parts


@pytest.mark.parametrize('bad',[
    dict(capture_session='b'*32),dict(camera_source_context='b'*64),dict(sample_rate=48000),
    dict(sample_rate=True),dict(channels=2),dict(channels=True),dict(sample_format='f32'),
    dict(received_at=998),dict(decoded_at=1001),dict(received_at=float('nan')),
    dict(audio_sequence=True),dict(audio_epoch=-1),dict(sample_start=1),dict(audio_sequence=0),
    dict(pcm='!!!'),dict(pcm=base64.b64encode(bytes(6402)).decode()),dict(pcm='a'*10000),
])
def test_bad_packets_cannot_supply_audio_or_raw_data_in_diagnostics(bad):
    g=AudioIngress(CAMERA,'a'*32);assert video(g,0)
    p=audio();p.update(bad);result=g.accept(p,wall=1000,mono=0)
    assert result.packet is None and result.reset and g.status()['dropped_packets']==1
    assert 'pcm' not in json.dumps(result.diagnostics) and 'capture_session' not in result.diagnostics


def test_packet_replay_sample_overlap_source_epoch_and_video_freshness_are_independent():
    g=AudioIngress(CAMERA,'a'*32);f=PCMFramer(CAMERA,'a'*32);assert video(g,0)
    p=audio(framer=f);first=g.accept(p,wall=1000,mono=0);assert first.packet and first.reset
    assert g.accept(p,wall=1000.1,mono=.1).packet is None
    p=audio(.2,framer=f);p['sample_start']=0
    assert g.accept(p,wall=1000.2,mono=.2).packet is None
    p=audio(.4,framer=f);assert video(g,.4)
    resumed=g.accept(p,wall=1000.4,mono=.4);assert resumed.packet and resumed.reset
    assert g.accept(audio(2.1,framer=f),wall=1002.1,mono=2.1).packet is None
    assert video(g,2.1)
    p=audio(2.3,framer=f);assert g.accept(p,wall=1002.3,mono=2.3).packet
    p=audio(2.5,framer=f);p['audio_epoch']=0
    assert g.accept(p,wall=1002.5,mono=2.5).packet is None


@pytest.mark.asyncio
async def test_qualified_secondary_packet_reaches_original_voice_consumer_and_reports_gap():
    g=AudioIngress(CAMERA,'a'*32);f=PCMFramer(CAMERA,'a'*32);assert video(g,0)
    service=SimpleNamespace(audio_allowed=True,audio=AsyncMock(),reset_audio=Mock(),state={})
    scope=dict(secondary_homes={CAMERA['id']:service},fleet=SimpleNamespace(voice_current=lambda *_:True),
               time=__import__('time'),deliver_audio=deliver_audio,safety=SimpleNamespace(observe_audio=Mock()))
    callback=actual('homeplane/embodied/worker.py','secondary_audio',scope)
    p=audio(framer=f);await callback(CAMERA['id'],g.accept(p,wall=1000,mono=0))
    service.audio.assert_awaited_once_with(p)
    await callback(CAMERA['id'],g.accept(p,wall=1000.1,mono=.1))
    assert service.audio.await_count==1 and service.reset_audio.call_count==2
    assert service.state['audio_input']['audio_persisted'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('closed',[False,True])
async def test_secondary_receiver_preserves_source_admission_and_privacy_before_voice(closed):
    g=AudioIngress(CAMERA,'a'*32);framer=PCMFramer(CAMERA,'a'*32)
    frame=dict(type='frame',capture_session='a'*32,camera_source_context=camera_source_context(CAMERA),
               received_at=1000,jpeg='SYNTHETIC')
    packet=audio(framer=framer)
    service=SimpleNamespace(audio_allowed=True,audio=AsyncMock(),reset_audio=Mock(),state={})
    time=SimpleNamespace(monotonic=lambda:0,time=lambda:1000)
    callback=actual('homeplane/embodied/worker.py','secondary_audio',dict(
        secondary_homes={CAMERA['id']:service},fleet=SimpleNamespace(voice_current=lambda *_:True),
        time=time,deliver_audio=deliver_audio,safety=SimpleNamespace(observe_audio=Mock())))
    future=asyncio.get_running_loop().create_future();offered=[];queue=asyncio.Queue(maxsize=2)
    stream=[frame,dict(packet,received_at=998),packet,
            dict(type='motor_result',request_id=7,native_code=0)]
    scope=dict(process=SimpleNamespace(stdout=SimpleNamespace(readline=AsyncMock(
        side_effect=[json.dumps(p).encode() for p in stream]+[b'']))),
        self=SimpleNamespace(on_audio=callback,on_capture_frame=None,wall=None),selected=CAMERA,audio_ingress=g,
        item={'privacy_closed':closed},waiters={7:future},queue=queue,last_video_lease=0,
        wall_epoch=1,time=time,json=json,offer_frame=lambda _,p:offered.append(p),retire_source=Mock())
    await actual('homeplane/embodied/previews.py','receive',scope)()
    assert offered==[frame] and future.result()['native_code']==0
    if closed:
        service.audio.assert_not_awaited()
        assert service.state['audio_input']['reason']=='camera_paused'
    else:
        service.audio.assert_awaited_once_with(packet)
        assert service.state['audio_input']['dropped_packets']==1


@pytest.mark.asyncio
async def test_original_primary_receiver_gates_pcm_and_stops_an_obsolete_generation():
    g=AudioIngress(CAMERA,'a'*32);assert video(g,0)
    p=audio();old=dict(p,received_at=998)
    service=SimpleNamespace(audio_allowed=True,audio=AsyncMock(),reset_audio=Mock(),state={})
    for obsolete in (False,True):
        scope=dict(capture=SimpleNamespace(stdout=SimpleNamespace(readline=AsyncMock(side_effect=[json.dumps(old).encode(),b'']))),
            state={'status':'live'},audio_ingress=g,home=service,deliver_audio=deliver_audio,
            safety=SimpleNamespace(observe_audio=Mock()),
            generation=2 if obsolete else 1,my_generation=1,
            SELECTED=CAMERA,fleet=SimpleNamespace(voice_current=lambda *_:True),
            time=SimpleNamespace(time=lambda:1000,monotonic=lambda:0),json=json,
            offer_frame=lambda *_:None,queue=object(),wall_epoch=1,primary_wall_epoch=1,
            primary_capture_closed=AsyncMock())
        receiver=actual('homeplane/embodied/worker.py','receive',scope)
        await receiver()
        service.audio.assert_not_awaited()
    scope['generation']=1;scope['capture'].stdout.readline=AsyncMock(side_effect=[json.dumps(p).encode(),b''])
    receiver=actual('homeplane/embodied/worker.py','receive',scope);await receiver()
    service.audio.assert_awaited_once_with(p)


@pytest.mark.asyncio
@pytest.mark.parametrize('break_stream',[False,True])
async def test_real_voice_buffer_reaches_asr_only_with_a_complete_fresh_segment(tmp_path,monkeypatch,break_stream):
    from homeplane.embodied import home_service as module
    clock=[0.0];jobs=[]
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    monkeypatch.setattr(module,'VOICE_INPUT_PAUSED',tmp_path/'paused')
    class VAD:
        probability=.9
        def __init__(self):self.count=0
        def reset(self):pass
        def measure(self,_):
            self.count+=1;return self.count<=6,1.0 if self.count<=6 else 0.0,.03
    s=voice_service(module)
    s.state={'voice':'ready'};s.activity=VAD();s.last_audio=None;s.selected=s.audio_allowed=True;s.audio_retry_at=0
    s.reply_until=0;s.last_video=0;s.noise=.004;s.voice_owner=None;s.voice_busy=False;s.epoch=1
    s.pending_audio=None;s.transcribe=AsyncMock();s.spawn=lambda coro:jobs.append(asyncio.create_task(coro))
    s.reset_audio();g=AudioIngress(CAMERA,'a'*32);f=PCMFramer(CAMERA,'a'*32)
    for i in range(10):
        clock[0]=i*.2;s.last_video=clock[0];assert video(g,clock[0])
        p=audio(clock[0],framer=f)
        if break_stream and i==6:p['received_at']=900
        await deliver_audio(s,g.accept(p,wall=1000+clock[0],mono=clock[0]))
    await asyncio.gather(*jobs)
    assert s.transcribe.await_count==(0 if break_stream else 1)
    if not break_stream:assert len(s.transcribe.call_args.args[0])==64000


def voice_service(module):
    s=module.HomeService()
    s.enabled=s.armed=True
    # Isolate VAD/PCM admission from the ASR pipe. The production constructor
    # still supplies all state used by public(); pipe recovery is tested apart.
    s.voice_process=SimpleNamespace(status='ready',metadata={})
    s.state={'voice':'ready'};s.activity=None;s.last_audio=None;s.selected=s.audio_allowed=True
    s.audio_retry_at=0;s.audio_input_at=None;s.reply_until=0;s.last_video=0;s.noise=.004
    s.voice_owner=None;s.voice_busy=False;s.epoch=1;s.pending_audio=None
    s.transcribe=AsyncMock();s.spawn=lambda _:(_ for _ in ()).throw(AssertionError('no ASR expected'))
    s.reset_audio()
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['load','measure','reset'])
async def test_local_audio_model_failure_does_not_stop_primary_frames_or_motor_results(tmp_path,monkeypatch,fault):
    from collections import deque
    from homeplane.embodied import home_service as module
    clock=[0.0];time=SimpleNamespace(monotonic=lambda:clock[0],time=lambda:1000+clock[0])
    monkeypatch.setattr(module,'time',time);monkeypatch.setattr(module,'VOICE_INPUT_PAUSED',tmp_path/'paused')
    model=Mock(side_effect=RuntimeError('private-model-error-not-for-status'))
    monkeypatch.setattr(module,'VoiceActivity',model)
    s=voice_service(module);g=AudioIngress(CAMERA,'a'*32);assert video(g,0)
    framer=PCMFramer(CAMERA,'a'*32)
    if fault!='load':
        s.activity=SimpleNamespace(measure=Mock(side_effect=RuntimeError('private-measure-error')),
            reset=Mock(side_effect=RuntimeError('private-reset-error') if fault=='reset' else None))
        if fault=='reset':s.last_audio=-1
    frame=dict(type='frame',capture_session='a'*32,camera_source_context=camera_source_context(CAMERA),
               received_at=1000,jpeg='SYNTHETIC')
    future=asyncio.get_running_loop().create_future();offered=[]
    scope=dict(capture=SimpleNamespace(stdout=SimpleNamespace(readline=AsyncMock(side_effect=[
        json.dumps(audio(framer=framer)).encode(),json.dumps(frame).encode(),
        json.dumps(dict(type='motor_result',request_id=7,native_code=0)).encode(),b'']))),
        state={'status':'live'},audio_ingress=g,home=s,deliver_audio=deliver_audio,generation=1,my_generation=1,
        safety=SimpleNamespace(observe_audio=Mock(),capture_frame=Mock()),
        SELECTED=CAMERA,fleet=SimpleNamespace(voice_current=lambda *_:True),time=time,json=json,
        offer_frame=lambda _,item:offered.append(item),queue=object(),last_video_lease=0,
        captured_times=deque(),wall=SimpleNamespace(offer=Mock()),wall_epoch=1,motor_waiters={7:future},
        primary_wall_epoch=1,primary_capture_closed=AsyncMock(),asyncio=asyncio,motion_generation=4)
    await actual('homeplane/embodied/worker.py','receive',scope)()
    assert offered[0]==dict(frame,_motion_generation=4)
    assert future.result()=={'type':'motor_result','request_id':7,'native_code':0}
    initial_calls=1 if fault=='load' else 0
    assert s.state['audio_analysis']['status']=='unavailable' and model.call_count==initial_calls
    assert 'private-' not in json.dumps(s.state)
    clock[0]=1;s.last_video=1;assert video(g,1)
    await deliver_audio(s,g.accept(audio(1,framer=framer),wall=1001,mono=1))
    assert model.call_count==initial_calls and s.state['audio_analysis']['retry_ms']==4000
    class HealthyVAD:
        def measure(self,_):return False,0,.01
        def reset(self):pass
    model.side_effect=None;model.return_value=HealthyVAD()
    clock[0]=5.1;s.last_video=5.1;assert video(g,5.1)
    await deliver_audio(s,g.accept(audio(5.1,framer=framer),wall=1005.1,mono=5.1))
    assert model.call_count==initial_calls+1 and s.state['audio_analysis']['status']=='ready'


@pytest.mark.asyncio
async def test_audio_model_cancellation_still_cancels_the_receiver(tmp_path,monkeypatch):
    from homeplane.embodied import home_service as module
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:0))
    monkeypatch.setattr(module,'VOICE_INPUT_PAUSED',tmp_path/'paused')
    s=voice_service(module)
    s.activity=SimpleNamespace(measure=Mock(side_effect=asyncio.CancelledError),reset=Mock())
    with pytest.raises(asyncio.CancelledError):await s.audio(audio())


def test_audio_reset_fault_discards_the_broken_model_and_keeps_completed_requests(monkeypatch):
    from homeplane.embodied import home_service as module
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:10))
    s=voice_service(module);s.vad=[b'partial'];s.speaking=True
    s.pending_audio=object();completed=s.pending_audio
    s.activity=SimpleNamespace(reset=Mock(side_effect=RuntimeError('private')))
    s.reset_audio()
    assert s.activity is None and not s.vad and not s.speaking and s.audio_retry_at==15
    assert s.pending_audio is completed and s.state['audio_analysis']['status']=='unavailable'


def test_public_audio_input_expires_without_mutating_the_admitted_snapshot(monkeypatch):
    from homeplane.embodied import home_service as module
    clock=[0.0];monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    s=voice_service(module);s.enabled=s.armed=True;s.owner=None;s.curtain=SimpleNamespace(state={})
    s.attention=SimpleNamespace(public=[]);s.gesture=SimpleNamespace(public={});s.speaker=SimpleNamespace(state={})
    s.runtime_log=SimpleNamespace(export=lambda:[]);s.audio_input_at=0
    s.state['audio_input']={'status':'ready','reason':None,'age_basis':'local_sdk_receive'}
    assert s.public()['audio_input']['status']=='ready'
    clock[0]=2
    assert s.public()['audio_input']['status']=='waiting'
    assert s.public()['audio_input']['reason']=='input_stale'
    assert s.state['audio_input']['status']=='ready' and s.public()['voice']=='ready'
