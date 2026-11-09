"""Offline policy tests; do not claim physical movement or speaker identity."""
import asyncio
import ast
import base64
import json
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import cv2
import numpy as np
import pytest

from homeplane.embodied.camera_director import CameraDirector, visible_scene
from homeplane.embodied.intent import address, transcript_quality
from homeplane.embodied.home_service import HomeService
from homeplane.embodied.ptz_control import CameraLease, FLAGS


def sample(at, *, visible=True, people=(), session='first'):
    return dict(status='live', _local_received=at, scene_visible=visible,
                people=list(people), session_id=session)


def person(track=1, x=.75, identity=None):
    return dict(id=track, box=dict(x=x, y=.2, w=.15, h=.6),
                identity=dict(state='matched', person_id=identity) if identity else {})


def clear(d):
    d.choose(sample(0), 0)
    d.choose(sample(.1), .1)


def test_sharp_timestamp_on_cover_is_not_an_open_view():
    picture=np.full((360,640),120,np.uint8)
    cv2.putText(picture,'2026-10-25 14:30:00',(8,25),cv2.FONT_HERSHEY_SIMPLEX,.8,255,2)
    assert not visible_scene(cv2.imencode('.jpg',picture)[1].tobytes())
    rng=np.random.default_rng(8)
    scene=rng.integers(0,256,(360,640),np.uint8)
    assert visible_scene(cv2.imencode('.jpg',scene)[1].tobytes())


def test_repeated_frame_never_confirms_open_or_repeats_motion():
    d=CameraDirector()
    assert d.choose(sample(0,visible=False),0) is None
    assert d.choose(sample(.5,visible=False),.5) is None
    assert d.choose(sample(1,visible=False),1)=='up'
    d.sent(1)
    assert d.choose(sample(1,visible=False),3) is None
    assert d.choose(sample(4),4) is None
    assert d.choose(sample(4),4.5) is None
    assert not d.clear
    assert d.choose(sample(4.6),4.6) is None
    assert d.clear


def test_cover_recovery_and_failed_motor_are_bounded():
    d=CameraDirector()
    moves=0
    for at in range(0,250,1):
        if d.choose(sample(at,visible=False),at):
            moves+=1;d.sent(at)
    assert moves==20 and d.phase=='view_obscured'
    d=CameraDirector()
    for at in range(20):
        if d.choose(sample(at,visible=False),at):d.failed(at)
    assert d.choose(sample(22,visible=False),22) is None
    assert d.phase=='motor_unavailable'


def test_following_uses_fresh_visible_body_and_keeps_registered_target():
    d=CameraDirector();clear(d)
    assert d.choose(sample(1,people=[person(identity='one')]),1)=='right'
    d.sent(1)
    assert d.choose(sample(3.2,people=[person(1000001,identity='one')]),3.2) is None
    assert d.choose(sample(3.5,people=[person(1000001,identity='one')]),3.5)=='right'
    assert d.choose(sample(4,people=[person(2,identity='other')]),4) is None
    assert d.phase=='target_ambiguous'
    assert d.choose(sample(4,people=[person()]),6) is None


def test_wake_focus_and_empty_view_patrol_do_not_guess_between_people():
    d=CameraDirector();clear(d)
    people=[person(1,.1),person(2,.75)]
    assert d.choose(sample(1,people=people),1) is None
    assert d.choose(sample(2,people=people),2,focus=dict(track=2,at=2,session_id='first'))=='right'
    d.sent(2)
    assert d.choose(sample(20),20,occupied_elsewhere=True) is None
    assert d.choose(sample(21),21)=='right'
    d.sent(21)
    assert d.choose(sample(25),25) is None
    assert d.choose(sample(35,visible=False),35) is None
    assert d.phase=='view_obscured'


def test_game_keeps_visible_players_in_a_stable_control_frame():
    d=CameraDirector();clear(d)
    one=person(1,.75)
    playing=sample(1,people=[one]);playing['game']={'status':'playing'}
    assert d.choose(playing,1) is None
    assert d.target is None and d.phase=='game_held'
    group=sample(2,people=[person(1,.08),person(2,.72)]);group['game']={'status':'calibrating'}
    assert d.choose(group,2) is None
    edge=sample(3,people=[person(1,.9)]);edge['game']={'status':'playing'}
    assert d.choose(edge,3) is None  # The game owns its frame even when a player is clipped.
    assert d.phase=='game_held'


def test_requested_survey_is_finite_and_repeated_intent_does_not_restart_it():
    d=CameraDirector();d.apply_mode('survey',7)
    attempts=[]
    for at in range(300):
        if direction:=d.choose(sample(at),at):
            attempts.append(direction);d.sent(at)
    assert len(attempts)==12 and d.phase=='survey_finished'
    assert d.public()['physical_motion_verified'] is False
    d.apply_mode('survey',7)
    assert d.choose(sample(301),301) is None
    d.apply_mode('survey',8)
    assert d.choose(sample(302),302) is None
    assert d.choose(sample(303),303) in {'left','right'}
    assert d.survey_attempts==1
    d.apply_mode('hold',9)
    assert d.choose(sample(320,visible=False),320) is None
    assert d.phase=='held'


def test_survey_charges_unknown_dispatch_outcomes_before_any_retry():
    d=CameraDirector();d.apply_mode('survey',1)
    # Each new frame is a new decision; no acknowledgement is manufactured.
    choices=[d.choose(sample(at),at) for at in range(200)]
    assert sum(value is not None for value in choices)==12
    assert d.survey_attempts==12 and d.phase=='survey_finished'


def test_primary_controller_receives_the_actual_game_state():
    source=ast.parse(Path('homeplane/embodied/worker.py').read_text())
    search=next(n for n in source.body if isinstance(n,ast.AsyncFunctionDef)and n.name=='camera_search')
    assignment=next(n for n in ast.walk(search) if isinstance(n,ast.Assign)
                    and any(isinstance(t,ast.Name)and t.id=='sample' for t in n.targets))
    current={'mode':'tennis','status':'playing'}
    scope={'state':{'status':'live'},'last_frame':20,'games':SimpleNamespace(public=current)}
    exec(compile(ast.Module(body=[assignment],type_ignores=[]),'<actual camera input>','exec'),scope)
    assert scope['sample']['game'] is current


@pytest.mark.asyncio
async def test_background_preserves_privacy_and_restores_vendor_tracking(tmp_path):
    values=dict(camera_power=False,cruise=True,human_tracking=True,pet_tracking=True,
                human_close_up=False,active_preset=1,presets=[])
    writes=[]
    async def read():return dict(values)
    async def write(name,value):writes.append((name,value));values[name]=value
    io=SimpleNamespace(read=read,write=write)
    lease=CameraLease(io,tmp_path/'lease.json',open_privacy=False,restore_native=True)
    with pytest.raises(RuntimeError,match='privacy_shutter_closed'):await lease.acquire()
    assert not writes and not lease.path.exists()
    values['camera_power']=True
    await lease.acquire()
    assert all(values[k] is False for k in FLAGS)
    values['camera_power']=False
    writes.clear()
    assert await lease.release()
    assert values['cruise'] and values['human_tracking'] and values['pet_tracking']
    assert values['human_close_up'] is False
    assert not any(k=='active_preset' or (k=='camera_power' and v) for k,v in writes)


def test_bare_wake_word_retains_quality_gate_without_becoming_device_command():
    assert transcript_quality('Hey Friday!')==(True,'accepted')
    assert transcript_quality('Friday',dict(no_speech_prob=.95))==(False,'likely_no_speech')
    assert address('Friday')==dict(admitted=False,wake_word=True,reason='waiting_instruction')
    assert not address('Fridayish')['admitted']


@pytest.mark.asyncio
async def test_bare_wake_focus_requires_current_video_and_unique_visible_target():
    h=HomeService(voice_preferences=lambda:SimpleNamespace(wake_word='Friday',revision=0))
    h.selected=h.armed=True;h.visible_tracks={8};h.camera_session='first'
    h.request=AsyncMock(return_value=dict(text='Friday',voice_revision=0,address=address('Friday')))
    h.last_video=time.monotonic()
    await h.transcribe(b'',None,time.monotonic(),epoch=h.epoch)
    assert h.camera_focus['track']==8
    h.camera_focus=None;h.visible_tracks={8,9}
    await h.transcribe(b'',None,time.monotonic(),epoch=h.epoch)
    assert h.camera_focus is None
    h.visible_tracks={8};h.last_video-=10
    await h.transcribe(b'',None,time.monotonic(),epoch=h.epoch)
    assert h.camera_focus is None
