"""Original source lifecycle plus pure game rules; no SDK, models or devices."""
import ast
import asyncio
import base64
import collections
import copy
import secrets
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from homeplane.embodied.camera_wall import CameraWall
from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_motion_games import human, LOCK


WORKER=Path('homeplane/embodied/worker.py')


def source_preamble(scope):
    source=next(n for n in ast.parse(WORKER.read_text()).body
                if isinstance(n,ast.AsyncFunctionDef)and n.name=='run_source')
    node=copy.deepcopy(source);node.decorator_list=[];node.name='preamble'
    node.body=node.body[:next(i for i,n in enumerate(node.body)if isinstance(n,ast.Try))]
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(WORKER),'exec'),scope)
    return scope['preamble']


def source_scope(game,room=None):
    camera=dict(id='cam_1111111111',name='Synthetic camera',room='Synthetic room')
    noop=SimpleNamespace(reset=Mock())
    return dict(wall=CameraWall([camera]),SELECTED=camera,identities=SimpleNamespace(session='old'),
        SESSION='synthetic',secrets=secrets,state={},games=game,
        home=SimpleNamespace(armed=True,selected=True,epoch=1,vad=[],pending_audio=None,gesture=noop),
        fleet=SimpleNamespace(clear=Mock()),Tracks=Mock(side_effect=lambda:object()),
        PetTracks=Mock(side_effect=lambda:SimpleNamespace(public=lambda:{})),
        processed_times=collections.deque(),captured_times=collections.deque(),room=room)


def progress(game):
    # Completed work only. Partial pose holds and movement cycles are not earned reps.
    return copy.deepcopy(dict(mode=game.mode,round=game.round_id,elapsed=game.elapsed,active=game.active,
        level=game.level,reps=game.reps,distance=game.distance,passed=game.passed,collisions=game.collisions,
        obstacles=game.obstacles,score=game.score,ball=game.ball,rally=game.rally,best=game.best_rally,
        hiit={k:v for k,v in game.circuit.public().items()if k!='feedback'}))


def partial_game(mode):
    game=MotionGames();game.reset(mode)
    if mode=='hiit':
        for _ in range(2):
            for depth in [.8,0.0]:
                game.circuit.update({'squat_calibrated':True,
                    'exercise_signals':{'visible':True,'squat':depth}},.12)
        game.publish([],'playing','synthetic rule fixture')
    else:
        for i in range(500):
            people=[human(1,arms='horizontal'if mode=='quest'else'down')]
            if mode=='ski':
                for side,offset in [('left',0),('right',1)]:
                    people[0]['joints'][side+'Wrist']['y']=.3+(.15 if(i//8+offset)%2 else-.15)
            elif mode=='tennis':people=[human(1,.3),human(2,.7)]
            game.update(people,LOCK,i*.05)
            if(mode=='quest'and game.level>0 or mode=='ski'and game.distance>2
                    or mode=='tennis'and sum(game.score)>0):break
    return game


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['quest','ski','tennis','hiit'])
async def test_original_source_initialization_preserves_an_explicit_new_round(mode):
    game=MotionGames();game.reset(mode);before=progress(game)
    await source_preamble(source_scope(game))(1)
    assert progress(game)==before
    assert game.mode==mode


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['quest','ski','tennis','hiit'])
async def test_original_source_recreation_keeps_completed_game_work(mode):
    game=partial_game(mode);before=progress(game)
    await source_preamble(source_scope(game))(1)
    assert progress(game)==before


@pytest.mark.asyncio
async def test_new_source_room_isolated_from_an_old_registration_thread(tmp_path):
    loop=asyncio.get_running_loop();entered=asyncio.Event();release=threading.Event()
    class OldRoom:
        def __init__(self):self.registration={'value':'old'}
        def retire(self):pass
        def register(self):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):raise TimeoutError('synthetic thread was not released')
            self.registration['value']='late-old-result'
    old=OldRoom();work=asyncio.create_task(asyncio.to_thread(old.register))
    try:
        await asyncio.wait_for(entered.wait(),1)
        scope=source_scope(MotionGames(),old)
        await source_preamble(scope)(1)
        tree=next(n for n in ast.parse(WORKER.read_text()).body
                  if isinstance(n,ast.AsyncFunctionDef)and n.name=='run_source')
        node=next(n for n in ast.walk(tree)if isinstance(n,ast.If)
                  and ast.unparse(n.test)=='room is None')
        wrapper=ast.parse('async def create_room():\n global room\n for _ in range(1):\n  pass').body[0]
        wrapper.body[-1].body=[copy.deepcopy(node)]
        made=[]
        def room(folder,*,scope):
            result=SimpleNamespace(registration={'value':'new'})
            made.append(result);return result
        scope.update(calibration_path=tmp_path/'calibration.json',reference_path=tmp_path/'reference.bin',
            DEFAULT={'mode':'unconfigured','seats':[],'furniture':[]},copy=copy,
            json=__import__('json'),picture=b'SYNTHETIC',RoomSession=room,
            asyncio=asyncio,CAMERA_STORAGE=tmp_path,source_scope=object(),load_room=lambda folder,picture,*,factory:factory({},picture),
            primary_frame_current=lambda *_:True,my_generation=1,generation=1,
            frame_motion_generation=0,item={'received_at':0},
            configuration=lambda:{'default_camera':scope['SELECTED']['id']})
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),
                     '<original first-frame room construction>','exec'),scope)
        await scope['create_room']()
        release.set();await work
        assert len(made)==1 and scope['room']is made[0]
        assert scope['room'].registration=={'value':'new'}
        assert old.registration=={'value':'late-old-result'}
    finally:
        release.set();await asyncio.gather(work,return_exceptions=True)


def recover(game,clock,*,count=None,steps=22):
    count=count if count is not None else(2 if game.mode=='tennis'else 1)
    for _ in range(steps):
        clock[0]+=.05
        people=[human(501+i,.3+i*.4)for i in range(count)]
        result=game.update(people,LOCK,clock[0],
            [{'person_id':501,'gesture':'ok'}],captured_at=clock[0])
    return result


@pytest.mark.parametrize('mode',['quest','ski','tennis','hiit'])
def test_source_recovery_preserves_earned_work_until_explicit_ready_resume(mode):
    game=partial_game(mode);clock=[100.0];game.clock=lambda:clock[0];before=progress(game)
    old_control=(game.control_context,game.control_revision)
    game.suspend_source();game.begin_source()
    suspended_revision=game.control_revision
    assert not game.control_current(*old_control)
    assert game.source_resume_required and game.manually_paused
    with pytest.raises(ValueError,match='game_input_not_ready'):game.set_paused(False)
    assert game.control_revision==suspended_revision
    result=recover(game,clock)
    assert result['source_recovery']['ready'] and progress(game)==before
    assert game.control_revision==suspended_revision,'Observation frames do not advance user intent'
    assert game.slots==([502,501]if mode=='tennis'else[501])
    assert set(game.readers)==set(game.slots) and game.gesture_event is None
    game.set_paused(False)
    assert game.resume_seconds==3 and not game.source_resume_required
    while game.resume_seconds>0:
        clock[0]+=.05
        people=[human(501+i,.3+i*.4)for i in range(2 if mode=='tennis'else 1)]
        game.update(people,LOCK,clock[0])
        assert progress(game)==before,'Countdown may not advance a score, flight, route or exercise'
    assert game.status=='playing'
    assert all(reader.last_swing<0 for reader in game.readers.values())


@pytest.mark.parametrize('condition',['stale','feet_missing','extra_person','moving','new_track'])
def test_changed_or_stale_recovery_input_requires_new_confirmation(condition):
    clock=[100.0];game=partial_game('ski');game.clock=lambda:clock[0]
    game.suspend_source();game.begin_source();recover(game,clock)
    assert game.recovery_public()['ready']
    if condition=='stale':clock[0]+=2
    else:
        clock[0]+=.05;people=[human(501,.3)];registration=LOCK
        if condition=='feet_missing':people[0]['motion_anchor']=None
        if condition=='extra_person':people.append(human(502,.7))
        if condition=='moving':registration={'status':'moving'}
        if condition=='new_track':people=[human(800,.3)]
        game.update(people,registration,clock[0])
    revision=game.control_revision
    assert not game.recovery_public()['ready']
    with pytest.raises(ValueError,match='game_input_not_ready'):game.set_paused(False)
    assert game.control_revision==revision and game.manually_paused


def test_completed_result_survives_source_recreation_and_explicit_restart_starts_fresh():
    game=MotionGames();game.reset('ski')
    for i in range(2500):
        game.ski({'arm_effort':1,'lateral':0},.1,i*.1)
        if game.complete:break
    assert game.complete
    game.publish([],'complete','finished');before=progress(game)
    game.suspend_source();game.begin_source()
    assert game.status=='complete' and progress(game)==before and not game.source_resume_required
    game.reset('ski');game.begin_source()
    assert game.mode=='ski' and game.round_id==before['round']+1 and game.distance==0
    assert not game.source_resume_required and game.resume_seconds==0


def test_partial_cycle_is_discarded_but_completed_alternating_side_survives():
    game=MotionGames();game.reset('hiit');game.circuit.level=2
    for value in ['neutral','left','neutral','right']:
        game.circuit.update({'squat_calibrated':True,
            'exercise_signals':{'visible':True,'high_knee':value}},.12)
    assert game.circuit.total_reps==1 and game.circuit.pending=='right'
    game.suspend_source()
    assert game.circuit.total_reps==1 and game.circuit.last_side=='left' and game.circuit.pending is None


@pytest.mark.asyncio
async def test_actual_resume_endpoint_requires_fresh_recalibration_and_current_round():
    from tests.embodied.test_game_controls import game_handler,request
    clock=[100.0];game=partial_game('tennis');game.clock=lambda:clock[0]
    game.suspend_source();game.begin_source();control,started,enabled=game_handler(game)
    from fastapi import HTTPException
    with pytest.raises(HTTPException)as error:
        await control(request({'mode':'tennis','action':'resume'},game=game))
    assert error.value.status_code==409 and error.value.detail=='game_input_not_ready'
    recover(game,clock)
    result=await control(request({'mode':'tennis','action':'resume'},game=game))
    assert result['resume_seconds']==3 and result['manually_paused'] is False
    started.assert_not_awaited();enabled.assert_not_awaited()


def test_tennis_cannot_use_a_countdown_swing_on_the_first_resumed_frame():
    clock=[100.0];game=partial_game('tennis');game.clock=lambda:clock[0]
    game.suspend_source();game.begin_source();recover(game,clock)
    game.ball.update(x=.5,z=.05,target_x=.5,toward=0);game.round_pause=0
    before_score=list(game.score);game.set_paused(False);i=0
    while game.resume_seconds>0:
        clock[0]+=.05;i+=1
        people=[human(501,.3),human(502,.7)]
        for p in people:
            for side,offset in [('left',0),('right',1)]:
                p['joints'][side+'Wrist']['x']+=(.15 if(i//3+offset)%2 else-.15)
        game.update(people,LOCK,clock[0])
    clock[0]+=.05
    game.update([human(501,.3),human(502,.7)],LOCK,clock[0])
    assert game.rally==0 and game.score==before_score
