"""Original capture receiver failure paths with inert pipes and no worker import."""
import asyncio
import ast
import base64
import copy
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeplane.embodied.party_camera import PartyCamera

from tests.embodied.test_camera_audio import actual, CAMERA
from homeplane.embodied.camera_wall import CameraWall
from homeplane.embodied.motion_games import MotionGames


def primary_scope(reads, *, obsolete=False):
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait({'type': 'frame', 'received_at': 999})
    closed = AsyncMock()
    scope = dict(
        asyncio=asyncio, json=json, time=SimpleNamespace(time=lambda: 1000, monotonic=lambda: 10),
        capture=SimpleNamespace(stdout=SimpleNamespace(readline=AsyncMock(side_effect=reads))),
        my_generation=1, generation=2 if obsolete else 1, SELECTED=CAMERA,
        queue=queue, latest_item={'type': 'frame', 'received_at': 999}, last_video_lease=10,
        state={'status': 'live'}, captured_times=deque(), motor_waiters={}, wall_epoch=7,
        primary_capture_closed=closed, primary_wall_epoch=7,
    )
    def offer(q, packet):
        if q.full():
            q.get_nowait()
        q.put_nowait(packet)
    scope['offer_frame'] = offer
    return scope, queue, closed


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [
    b'{private-malformed-packet', b'[]\n', OSError('private-pipe-failure'),
    ValueError('private-line-limit'), b'',
])
async def test_primary_receiver_ends_with_one_fault_signal_instead_of_waiting_85_seconds(fault):
    scope, queue, closed = primary_scope([fault])
    receive = actual('homeplane/embodied/worker.py', 'receive', scope)
    await receive()
    assert queue.qsize() == 1
    packet = queue.get_nowait()
    assert packet['type'] == 'error'
    assert packet['reason'] == ('source_closed' if fault == b'' else 'capture_stream_unavailable')
    closed.assert_awaited_once_with(1, 7, packet['reason'], packet.get('error_class'))
    assert scope['latest_item'] is None
    assert 'private-' not in json.dumps(packet)


@pytest.mark.asyncio
async def test_obsolete_receiver_cannot_signal_or_clear_a_new_source_even_for_bad_json():
    scope, queue, closed = primary_scope([b'{private-old-packet'], obsolete=True)
    await actual('homeplane/embodied/worker.py', 'receive', scope)()
    closed.assert_not_called()
    assert queue.get_nowait()['type'] == 'frame'
    assert scope['latest_item']['type'] == 'frame'


@pytest.mark.asyncio
async def test_receiver_cancellation_stays_cancellation_without_a_failure_signal():
    scope, queue, closed = primary_scope([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await actual('homeplane/embodied/worker.py', 'receive', scope)()
    closed.assert_not_called()
    assert queue.get_nowait()['type'] == 'frame'


@pytest.mark.asyncio
async def test_primary_receiver_delivers_motor_reply_before_terminal_fault():
    reply={'type':'motor_result','request_id':3,'native_code':0}
    scope, queue, closed = primary_scope([json.dumps(reply).encode(), b'{private-next-packet'])
    future=asyncio.get_running_loop().create_future()
    scope['motor_waiters'][3]=future
    await actual('homeplane/embodied/worker.py','receive',scope)()
    assert future.result()==reply
    assert queue.get_nowait()['type']=='error'
    closed.assert_called_once()


def closed_scope():
    from homeplane.embodied.calibration_frames import CalibrationFrames
    camera=dict(CAMERA,name='Synthetic camera',room='Synthetic room')
    wall=CameraWall([camera],clock=lambda:1000)
    epoch=wall.begin(camera['id'])
    wall.frames[camera['id']]=(b'synthetic private frame',1000)
    wall.observations[camera['id']]={'people':[{'track':'old'}]}
    pending=asyncio.get_running_loop().create_future()
    acknowledged=asyncio.get_running_loop().create_future()
    acknowledged.set_result({'native_code':0})
    scope=dict(
        generation=1,primary_wall_epoch=epoch,primary_retired_scope=None,SELECTED=camera,motion_generation=4,
        calibration_frames=CalibrationFrames(clock=lambda:10,wall=lambda:1000),SESSION='synthetic',
        latest={'private':'frame'},last_frame=10,last_jpeg=b'private image',
        last_preview=b'private preview',last_preview_at=1000,
        state=dict(status='live',frame='private pixels',people=[{'id':1}],hands=[{}],
                   animals=[{}],animals_current=True,room={'private':'position'}),
        home=SimpleNamespace(armed=True,selected=True,epoch=2,vad=[b'private audio'],pending_audio=object()),
        secondary_homes={},safety=SimpleNamespace(clear=Mock()),fleet=SimpleNamespace(clear=Mock()),
        pet_tracks=SimpleNamespace(clear=Mock()),wall=wall,identities=SimpleNamespace(discard=Mock()),
        motor_waiters={3:pending,4:acknowledged},notify=AsyncMock(),games=MotionGames(),
    )
    scope['clear_camera']=actual('homeplane/embodied/worker.py','clear_camera',scope)
    close=actual('homeplane/embodied/worker.py','primary_capture_closed',scope)
    return scope,close,epoch,pending,acknowledged


@pytest.mark.asyncio
@pytest.mark.parametrize('privacy_closed',[False,True])
async def test_source_loss_invalidates_inputs_and_pending_motion_before_notifying(privacy_closed):
    scope,close,epoch,pending,acknowledged=closed_scope()
    if privacy_closed:
        scope['state'].update(status='shutter_closed',reason='privacy_shutter_closed')
    def notified():
        assert scope['last_frame']==0 and not scope['home'].armed
        assert pending.done() and scope['wall'].frame(CAMERA['id']) is None
    scope['notify'].side_effect=notified
    await close(1,epoch,'capture_stream_unavailable','JSONDecodeError')
    with pytest.raises(RuntimeError,match='^capture_stream_unavailable$'):
        await pending
    assert acknowledged.result()=={'native_code':0}
    assert scope['motion_generation']==5
    assert scope['latest'] is None and scope['last_jpeg']==scope['last_preview']==b''
    assert scope['last_preview_at']==0
    assert scope['state']['people']==scope['state']['hands']==scope['state']['animals']==[]
    assert scope['state']['frame'] is None and scope['state']['room'] is None
    assert not scope['home'].selected and scope['home'].epoch==3
    assert not scope['home'].vad and scope['home'].pending_audio is None
    scope['safety'].clear.assert_called_once_with(CAMERA['id'])
    scope['fleet'].clear.assert_called_once_with(CAMERA['id'])
    scope['identities'].discard.assert_called_once()
    scope['notify'].assert_awaited_once()
    assert scope['state']['status']==('shutter_closed'if privacy_closed else'unavailable')
    if privacy_closed:assert scope['state']['reason']=='privacy_shutter_closed'


@pytest.mark.asyncio
@pytest.mark.parametrize('retired',['generation','wall_epoch'])
async def test_retired_source_cannot_clear_live_replacement_or_fail_its_motor(retired):
    scope,close,epoch,pending,_=closed_scope()
    await close(0 if retired=='generation'else 1,epoch-1 if retired=='wall_epoch'else epoch,
                'capture_stream_unavailable')
    assert scope['state']['status']=='live' and scope['motion_generation']==4
    assert scope['last_frame']==10 and not pending.done()
    assert scope['wall'].frame(CAMERA['id']) is not None
    scope['notify'].assert_not_awaited()
    scope['safety'].clear.assert_not_called()
    pending.cancel()


def consume_terminal(scope):
    """Use the original run_source queue/error branch, before any model I/O."""
    tree=ast.parse(Path('homeplane/embodied/worker.py').read_text())
    loop=next(n for n in ast.walk(tree)if isinstance(n,ast.While)and any(
        isinstance(s,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='stage'for t in s.targets)
        and isinstance(s.value,ast.Constant)and s.value.value=='waiting_frame'for s in n.body))
    loop=copy.deepcopy(loop)
    end=next(i for i,s in enumerate(loop.body)if isinstance(s,ast.If)
             and ast.unparse(s.test)=="item['type'] != 'frame'")
    loop.body=loop.body[:end+1]
    wrapper=ast.parse('async def consume():\n pass').body[0]
    wrapper.body=[loop]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),
                 '<original run_source terminal branch>','exec'),scope)
    return scope['consume']


@pytest.mark.asyncio
@pytest.mark.parametrize('privacy_closed',[False,True])
async def test_original_queue_consumer_publishes_fault_and_preserves_a_closed_shutter(privacy_closed):
    scope,queue,closed=primary_scope([b'{private-corrupt-json'])
    await actual('homeplane/embodied/worker.py','receive',scope)()
    scope.update(camera_task=None,games=SimpleNamespace(mode='ski'),notify=AsyncMock())
    if privacy_closed:scope['state'].update(status='shutter_closed',reason='privacy_shutter_closed')
    await asyncio.wait_for(consume_terminal(scope)(),.2)
    assert queue.empty()
    if privacy_closed:
        assert scope['state']['status']=='shutter_closed'
        scope['notify'].assert_not_awaited()
    else:
        assert scope['state']['status']=='unavailable'
        assert scope['state']['reason']=='capture_stream_unavailable'
        assert scope['state']['error_class']=='JSONDecodeError'
        assert scope['state']['game']['status']=='paused'
        scope['notify'].assert_awaited_once()


@pytest.mark.parametrize('source_status',['unavailable','stopped','shutter_closed'])
def test_live_status_view_cannot_publish_old_game_players_while_source_is_down(tmp_path,source_status):
    home=SimpleNamespace(public=lambda:{})
    game=MotionGames();game.reset('ski');game.publish([{'id':1}],'playing','')
    scope=dict(
        state=dict(status=source_status,processed_frames=5,people=[],hands=[],room=None),
        games=game,party_camera=PartyCamera(),
        home=home,recorder=SimpleNamespace(public=lambda:{}),secondary_homes={},
        safety=SimpleNamespace(recording_status=lambda:{'state':'off'}),
        fleet=SimpleNamespace(public=lambda _:{}),time=SimpleNamespace(monotonic=lambda:10,time=lambda:1000),
        PRIVATE=tmp_path,camera_planner=None,last_frame=0,SELECTED=CAMERA,
        configuration=lambda:{'cameras':[CAMERA]},previews=None,
    )
    result=actual('homeplane/embodied/worker.py','view',scope)()
    assert result['game']['status']=='paused' and result['game']['players']==[]
    assert result['game']['control_context']==game.control_context
    assert game.public['players']==[{'id':1}]
    assert scope['games'].public['status']=='playing','Presentation must not rewrite game progress'


@pytest.mark.asyncio
@pytest.mark.parametrize('automatic',[False,True])
async def test_capture_loss_while_waiting_for_room_lock_cannot_send_a_manual_or_automatic_move(automatic):
    from tests.embodied.test_motor_recovery import primary_scope as motor_scope
    scope,sent=motor_scope()
    scope.update(generation=1,primary_wall_epoch=1,primary_retired_scope=None,pet_tracks=SimpleNamespace(clear=Mock()),
                 wall=SimpleNamespace(clear=Mock()),safety=SimpleNamespace(clear=Mock()),
                 secondary_homes={},notify=AsyncMock(),games=MotionGames())
    scope['clear_camera']=actual('homeplane/embodied/worker.py','clear_camera',scope)
    close=actual('homeplane/embodied/worker.py','primary_capture_closed',scope)
    class RoomWait:
        async def __aenter__(self):await close(1,1,'capture_stream_unavailable')
        async def __aexit__(self,*_):pass
    scope['room_lock']=RoomWait()
    with pytest.raises(RuntimeError,match='^capture_source_changed$'):
        await scope['motor_step']('right',scope['last_frame']if automatic else None,
                                  expected_intent='original')
    assert not sent and not scope['motor_waiters']


@pytest.mark.asyncio
async def test_original_supervisor_retries_after_fault_and_a_replacement_receiver_admits_new_frames():
    owner,close,epoch,pending,acknowledged=closed_scope()
    attempts=[];delays=[];recovered=[]
    supervisor=dict(generation=1,state=owner['state'])
    async def run_source(my_generation):
        attempts.append(my_generation)
        scope,queue,_=primary_scope([b'{private-read-fault'])
        scope.update(owner)
        scope.update(my_generation=my_generation,wall_epoch=epoch,primary_wall_epoch=epoch,
                     camera_task=None)
        scope['clear_camera']=actual('homeplane/embodied/worker.py','clear_camera',scope)
        scope['primary_capture_closed']=actual('homeplane/embodied/worker.py','primary_capture_closed',scope)
        if len(attempts)==1:
            await actual('homeplane/embodied/worker.py','receive',scope)()
            await consume_terminal(scope)()
            assert scope['state']['status']=='unavailable' and queue.empty()
            with pytest.raises(RuntimeError,match='^capture_stream_unavailable$'):await pending
            assert acknowledged.result()=={'native_code':0}
            return
        queue.get_nowait()  # Empty the upstream fixture's initial old frame.
        frame=dict(type='frame',received_at=1000,capture_session='b'*32,jpeg='SYNTHETIC')
        packets=iter([json.dumps(dict(type='motor_result',request_id=3,native_code=0)).encode(),
                      json.dumps(frame).encode()])
        stopped=asyncio.Event()
        async def read():
            try:return next(packets)
            except StopIteration:await stopped.wait()
        scope['capture'].stdout.readline=read
        scope['audio_ingress']=SimpleNamespace(observe_frame=Mock())
        scope['state']['status']='connecting'
        scope['wall_epoch']=scope['wall'].begin(CAMERA['id'])
        scope['primary_wall_epoch']=scope['wall_epoch']
        receiver=asyncio.create_task(actual('homeplane/embodied/worker.py','receive',scope)())
        try:
            recovered.append(await asyncio.wait_for(queue.get(),.2))
            assert not receiver.done()
            assert isinstance(pending.exception(),RuntimeError),'An old ACK cannot replace the retired outcome'
            supervisor['generation']=2
            scope['generation']=2
        finally:
            receiver.cancel()
            await asyncio.gather(receiver,return_exceptions=True)
    async def sleep(delay):delays.append(delay)
    supervisor.update(run_source=run_source,asyncio=SimpleNamespace(sleep=sleep))
    await actual('homeplane/embodied/worker.py','supervised_source',supervisor)(1)
    assert attempts==[1,1] and delays==[2]
    assert recovered==[dict(type='frame',received_at=1000,capture_session='b'*32,jpeg='SYNTHETIC',
                            _motion_generation=4)]


def frame_continuation(scope):
    """Original post-Vision pipeline with inert recognition/layout ports."""
    tree=ast.parse(Path('homeplane/embodied/worker.py').read_text())
    source=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef)and n.name=='run_source')
    loop=next(n for n in ast.walk(source)if isinstance(n,ast.While)and any(
        isinstance(s,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='stage'for t in s.targets)
        and isinstance(s.value,ast.Constant)and s.value.value=='waiting_frame'for s in n.body))
    start=next(i for i,n in enumerate(loop.body)if isinstance(n,ast.Assign)
               and any(isinstance(t,ast.Name)and t.id=='hands'for t in n.targets))
    wrapper=ast.parse('async def continue_frame():\n for _sample in range(1):\n  pass').body[0]
    wrapper.args.args=[ast.arg(arg=name)for name in ('phase_at','current_scene','room_ok')]
    wrapper.args.defaults=[ast.Name(id=name,ctx=ast.Load())for name in ('phase_at','current_scene','room_ok')]
    wrapper.body[0].body=copy.deepcopy(loop.body[start:])
    wrapper.body=[copy.deepcopy(n)for n in source.body if isinstance(n,ast.Global)]+wrapper.body
    if any(isinstance(n,ast.FunctionDef)and n.name=='primary_frame_current'for n in tree.body):
        actual('homeplane/embodied/worker.py','primary_frame_current',scope)
    actual('homeplane/embodied/worker.py','scene_frame_current',scope)
    actual('homeplane/embodied/worker.py','calibration_source',scope)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),
                 '<original primary frame continuation>','exec'),scope)
    return scope['continue_frame']


def primary_frame_scope(tmp_path, fault_stage=None):
    from homeplane.embodied.motion_games import MotionGames
    from homeplane.embodied.pet_tracks import PetTracks
    scope,close,epoch,pending,_=closed_scope()
    scope['motor_waiters']={};pending.cancel()
    published=[]
    async def notify():published.append(scope['state']['status'])
    scope.update(time=SimpleNamespace(time=lambda:1000,monotonic=lambda:10),my_generation=1,wall_epoch=epoch,
        frame_motion_generation=4,now=10,PetTracks=PetTracks,base64=base64,
        item=dict(received_at=1000,preview=base64.b64encode(b'private-old-preview').decode(),
                  preview_at=1000,jpeg=base64.b64encode(b'SYNTHETIC').decode(),width=640,height=360),
        detection={'boxes':[],'hands':[]},picture=b'private-old-jpeg',phase_at=10,timings={},
        camera_planner=None,pet_owner=None,pet_revision=None,current_scene={'objects':[]},frame_scene={},room_ok=True,
        captured_times=deque(),processed_times=deque(),rate=lambda _:0,PRIVATE=tmp_path,
        interaction_enabled=True,room_lock=asyncio.Lock(),capture=object(),notify=notify,
        registry=SimpleNamespace(observe=Mock(return_value=[])),
        practice=SimpleNamespace(suppresses=lambda _:False,observe=Mock()),games=MotionGames(),party_camera=PartyCamera())
    scope['state']['processed_frames']=0
    scope['identities'].reason='ready';scope['identities'].session='synthetic-session'
    scope['games'].reset('ski')
    people=[dict(id=1,joints={},box={'x':.4,'y':.1,'w':.2,'h':.7})]
    layout={'registration':{'status':'locked'}}
    async def register(*_,**__):
        await lose('layout')
        return True
    async def update(*_,**__):
        await lose('visibility')
        return layout
    scope['room']=SimpleNamespace(register=register,update=update,config={'mode':'configured'},
                                  command_until=0,reg_info={'status':'locked'},registration={},
                                  scene_visible=True,draft=None,status=lambda:{'state':'ready'},retire=Mock())
    scope['tracks']=SimpleNamespace(update=Mock(return_value=people),input_valid=True,invalidate=Mock())
    scope['pet_tracks'].update=Mock(return_value=[])
    scope['pet_tracks'].current=False
    scope['pet_tracks'].public=lambda:{}
    async def lose(stage):
        if fault_stage==stage:await close(1,epoch,'capture_stream_unavailable','OSError')
    scope['asyncio']=asyncio
    from homeplane.embodied.room_protocol import unavailable_layout
    scope['unavailable_layout']=unavailable_layout
    async def analyze(*_,**__):await lose('identity');return []
    scope['analyze']=analyze
    async def enable():await lose('enable')
    async def observe(*_,**__):await lose('home')
    async def dispatch(*_):await lose('dispatch')
    scope['safety'].offer=Mock()
    scope['safety'].sound_requested=lambda _:False
    scope['home'].enable=AsyncMock(side_effect=enable)
    scope['home'].observe=AsyncMock(side_effect=observe)
    scope['home'].spawn=Mock()
    scope['home'].attention=SimpleNamespace(candidate=lambda:None)
    scope['home'].speaking=False;scope['home'].last_audio=None
    scope['fleet'].update=Mock(return_value=[]);scope['fleet'].voice=Mock(return_value=False)
    scope['fleet'].cameras={}
    scope['dispatch_interactions']=AsyncMock(side_effect=dispatch)
    return scope, published


@pytest.mark.asyncio
@pytest.mark.parametrize('fault_stage',[None,'layout','identity','visibility','enable','home','dispatch'])
async def test_source_loss_during_processing_cannot_resurrect_frames_or_continue_old_actions(tmp_path,fault_stage):
    scope, published = primary_frame_scope(tmp_path, fault_stage)
    await frame_continuation(scope)()
    if fault_stage is None:
        assert scope['state']['status']=='live' and scope['last_jpeg']==b'private-old-jpeg'
        assert published==['live']
    else:
        assert scope['state']['status']=='unavailable' and published==['unavailable']
        assert scope['last_jpeg']==scope['last_preview']==b'' and scope['last_frame']==0
        assert scope['state']['people']==[] and scope['state']['frame'] is None
        assert CAMERA['id'] not in scope['wall'].observations
        if fault_stage in {'layout','identity','visibility'}:scope['safety'].offer.assert_not_called()
        if fault_stage in {'layout','identity','visibility','enable'}:scope['practice'].observe.assert_not_called()
        if fault_stage!='dispatch':scope['dispatch_interactions'].assert_not_awaited()


@pytest.mark.asyncio
async def test_cleared_safety_source_cannot_return_through_a_waiting_policy_gate(tmp_path,monkeypatch):
    from homeplane.embodied import safety as module
    from homeplane.embodied.safety import SafetyPolicy,SafetyRuntime,SafetyStore
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:10,time=lambda:1000))
    store=SafetyStore(tmp_path/'events.sqlite');store.configure(SafetyPolicy(enabled=True))
    runtime=SafetyRuntime(store)
    try:
        await runtime.sync_policy()
        await runtime.policy_lock.acquire()
        old=asyncio.create_task(runtime.observe(CAMERA['id'],'old',[],10,10,stable=True,jpeg=b'SYNTHETIC'))
        await asyncio.sleep(0)
        assert not old.done()
        runtime.clear(CAMERA['id'])
        runtime.policy_lock.release()
        await old
        assert CAMERA['id'] not in runtime.frames and CAMERA['id'] not in runtime.last_seen
        assert not runtime.sound.windows and store.events()==[]
        await runtime.observe(CAMERA['id'],'replacement',[],10,10,stable=True,jpeg=b'SYNTHETIC')
        assert runtime.frames[CAMERA['id']]['session']=='replacement'
    finally:
        if runtime.policy_lock.locked():runtime.policy_lock.release()
        await runtime.close()


@pytest.mark.asyncio
async def test_a_queued_extension_uses_its_original_capture_context_even_after_recovery():
    scope,close,epoch,pending,_=closed_scope()
    pending.cancel();scope['motor_waiters']={}
    scope.update(time=SimpleNamespace(time=lambda:1000,monotonic=lambda:10),
        event=SimpleNamespace(track=1,observed_mono=10),my_generation=1,frame_motion_generation=4,
        item={'received_at':1000},games=MotionGames(),interaction_enabled=True,
        practice=SimpleNamespace(suppresses=lambda _:False))
    actual('homeplane/embodied/worker.py','primary_frame_current',scope)
    scope['games'].preferred_track=1
    tree=ast.parse(Path('homeplane/embodied/worker.py').read_text())
    call=next(n for n in ast.walk(tree)if isinstance(n,ast.Call)and isinstance(n.func,ast.Attribute)
              and isinstance(n.func.value,ast.Name)and n.func.value.id=='bindings'and n.func.attr=='dispatch')
    predicate=next(k.value for k in call.keywords if k.arg=='current')
    current=eval(compile(ast.Expression(body=predicate),'<original extension lease>','eval'),scope)
    assert current()
    await close(1,epoch,'capture_stream_unavailable')
    assert not current()
    scope['state']['status']='live'
    scope['primary_wall_epoch']=scope['wall'].begin(CAMERA['id'])
    assert not current(),'A fresh replacement frame cannot renew an old event lease'


@pytest.mark.asyncio
async def test_duplicate_source_retirement_preserves_the_first_cause_and_notifies_once():
    scope,close,epoch,pending,_=closed_scope()
    await close(1,epoch,'capture_stream_unavailable','JSONDecodeError')
    await close(1,epoch,'source_closed',stage='cleanup')
    with pytest.raises(RuntimeError):await pending
    assert scope['motion_generation']==5 and scope['state']['reason']=='capture_stream_unavailable'
    assert scope['state']['error_class']=='JSONDecodeError' and scope['state']['error_stage']=='capture'
    scope['notify'].assert_awaited_once()
