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


def patrol_view(at, *, x=0.0, y=0.0, basis='synthetic-room'):
    return (basis, ((1, 0, x), (0, 1, y), (0, 0, 1)), at)


def sample(at, *, visible=True, people=(), session='first', x=0.0):
    return dict(status='live', _local_received=at, scene_visible=visible,
                people=list(people), session_id=session, _patrol_view=patrol_view(at, x=x))


def person(track=1, x=.75, identity=None, *, y=.2, h=.6):
    return dict(id=track, box=dict(x=x, y=y, w=min(.15, 1 - x), h=h),
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


@pytest.mark.parametrize(('y', 'height', 'direction'), [(.79, .18, 'down'), (.03, .60, 'up')])
def test_follow_tilts_only_after_two_fresh_observations_of_the_same_target(y, height, direction):
    director = CameraDirector()
    clear(director)
    target = person(x=.43, y=y, h=height, identity='synthetic-target')
    first = sample(1, people=[target])
    assert director.choose(first, 1) is None
    assert director.choose(first, 1.1) is None
    second = sample(1.2, people=[target])
    assert director.choose(second, 1.2) == direction
    assert director.decision_current(second, 1.2, direction)
    centered = sample(1.3, people=[person(x=.43, identity='synthetic-target')])
    assert not director.decision_current(centered, 1.3, direction)
    replacement = sample(1.3, people=[dict(target, id=2)])
    assert not director.decision_current(replacement, 1.3, direction)
    assert not director.decision_current(second, 2.8, direction)
    director.sent(1.2)
    assert director.choose(second, 3.3) is None
    assert director.choose(sample(3.5, people=[target]), 3.5) is None
    assert director.choose(sample(3.6, people=[target]), 3.6) is None
    assert director.choose(sample(3.7, people=[target]), 3.7) == direction


def test_tilt_favors_the_nearest_edge_and_ignores_an_oversized_or_invalid_box():
    director = CameraDirector()
    clear(director)
    closest_to_right = person(x=.84, y=.78, h=.19)
    assert director.choose(sample(1, people=[closest_to_right]), 1) == 'right'
    closest_to_bottom = person(x=.75, y=.78, h=.20)
    assert director.choose(sample(1.2, people=[closest_to_bottom]), 1.2) is None
    assert director.choose(sample(1.4, people=[closest_to_bottom]), 1.4) == 'down'
    tall = person(x=.43, y=.03, h=.94)
    for at in (1.6, 1.8):
        assert director.choose(sample(at, people=[tall]), at) is None
    for at in (2, 2.2):
        assert director.choose(sample(at, people=[person(y=float('nan'))]), at) is None


def test_interrupted_or_replaced_target_cannot_inherit_a_tilt_observation():
    director = CameraDirector()
    clear(director)
    low = person(1, x=.43, y=.78, h=.20)
    assert director.choose(sample(1, people=[low]), 1) is None
    replaced = dict(low, id=2)
    assert director.choose(sample(1.2, people=[replaced]), 1.2) is None
    assert director.choose(sample(1.4, people=[replaced]), 1.4, review_pending=True) is None
    assert director.choose(sample(1.6, people=[replaced]), 1.6) is None
    assert director.choose(sample(1.8, people=[replaced]), 1.8) == 'down'
    switched = sample(2, people=[replaced], session='replacement')
    assert not director.decision_current(switched, 2, 'down')
    assert director.choose(switched, 2) is None
    assert director.choose(dict(switched, _local_received=2.2), 2.2) is None
    assert director.choose(dict(switched, _local_received=2.4), 2.4) == 'down'


def test_finished_game_cannot_supply_the_first_observation_for_a_new_tilt():
    from homeplane.embodied.motion_games import MotionGames

    director = CameraDirector()
    game = MotionGames()
    clear(director)
    low = person(x=.43, y=.78, h=.20)
    assert director.choose(dict(sample(1, people=[low]), game=game.public), 1) is None
    game.reset('ski')
    game.reset('off')
    assert director.choose(dict(sample(1.2, people=[low]), game=game.public), 1.2) is None
    assert director.choose(dict(sample(1.4, people=[low]), game=game.public), 1.4) == 'down'


@pytest.mark.parametrize('restored', [False, True])
def test_obscured_view_reverses_a_confirmed_tilt_once_and_avoids_that_angle(restored):
    director = CameraDirector()
    clear(director)
    low = person(x=.43, y=.78, h=.20, identity='synthetic-target')
    assert director.choose(sample(1, people=[low]), 1) is None
    assert director.choose(sample(1.2, people=[low]), 1.2) == 'down'
    director.sent(1.2)
    for at in (3.5, 4):
        assert director.choose(sample(at, visible=False), at) is None
    obscured = sample(4.6, visible=False)
    assert director.choose(obscured, 4.6) == 'up'
    assert director.decision_current(obscured, 4.6, 'up')
    director.sent(4.6)
    if restored:
        for at in (6.7, 7.2, 7.8):
            assert director.choose(sample(at, people=[low]), at) is None
        assert director.public()['view_usable'] and director.position == (0, 0)
        assert (0, -1) in director.blocked_positions
    else:
        for at in (6.7, 7.2):
            assert director.choose(sample(at, visible=False), at) is None
        assert director.choose(sample(7.8, visible=False), 7.8) in ('left', 'right')


@pytest.mark.parametrize('interruption', ['manual', 'game_cycle'])
def test_manual_and_game_changes_retire_recovery_for_the_previous_tilt(interruption):
    from homeplane.embodied.motion_games import MotionGames

    director = CameraDirector()
    game = MotionGames()
    clear(director)
    low = person(x=.43, y=.78, h=.20)
    assert director.choose(dict(sample(1, people=[low]), game=game.public), 1) is None
    assert director.choose(dict(sample(1.2, people=[low]), game=game.public), 1.2) == 'down'
    director.sent(1.2, 'down', automatic=interruption != 'manual')
    if interruption == 'game_cycle':
        game.reset('ski')
        game.reset('off')
    for at in (3.5, 4):
        assert director.choose(dict(sample(at, visible=False), game=game.public), at) is None
    assert director.choose(dict(sample(4.6, visible=False), game=game.public), 4.6) in ('left', 'right')


@pytest.mark.asyncio
@pytest.mark.parametrize('target_recenters', [False, True])
async def test_secondary_camera_rechecks_the_live_tilt_before_writing_to_its_worker(target_recenters):
    import secrets

    from tests.embodied.test_review_motion import source_function

    director = CameraDirector()
    commands, waiters = [], {}
    low = person(x=.43, y=.78, h=.20)
    centered = person(x=.43)
    latest = dict(now=0., captured_mono=0., session='secondary', people=[low],
                  tracking_current=True, scene_visible=True)
    item = dict(source_active=True, privacy_closed=False, control_epoch=1, mode='auto', mode_revision='current')

    async def camera_is_open(_):
        if target_recenters:
            now = latest['now'] + .04
            latest.update(people=[centered], now=now, captured_mono=now)
        return True

    async def advance(delay):
        latest['now'] += delay
        if latest['now'] > .8:
            raise asyncio.CancelledError
        latest['captured_mono'] = latest['now']

    def write(raw):
        command = json.loads(raw)
        commands.append(command)
        waiters[command['request_id']].set_result({'native_code': 0})

    scope = dict(director=director, latest=latest, item=item, selected={'id': 'synthetic-camera'},
        motion_lock=asyncio.Lock(), waiters=waiters, camera_is_open=camera_is_open, recognizer=None,
        process=SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=write, drain=AsyncMock())),
        self=SimpleNamespace(focus=Mock(return_value=None), occupied_elsewhere=Mock(return_value=False),
                             review_pending=Mock(return_value=False), runtime_log=SimpleNamespace(record=Mock()), on_clear=None),
        time=SimpleNamespace(monotonic=lambda: latest['now']), json=json, secrets=secrets,
        asyncio=SimpleNamespace(sleep=advance, wait_for=asyncio.wait_for, get_running_loop=asyncio.get_running_loop,
                                CancelledError=asyncio.CancelledError))
    path = 'homeplane/embodied/previews.py'
    for name in ('current_sample', 'send_motion', 'direct'):
        source_function(path, name, scope)
    with pytest.raises(asyncio.CancelledError):
        await scope['direct']()
    assert not waiters
    if target_recenters:
        assert not commands and 'last_motor_sent' not in item
    else:
        command, = commands
        assert command['action'] == 'motor_step' and command['direction'] == 'down'
        assert command['expires_mono'] == pytest.approx(2.1)
        assert item['last_motor_sent'] == pytest.approx(.6)


def test_wake_focus_and_empty_view_patrol_do_not_guess_between_people():
    d=CameraDirector();clear(d)
    people=[person(1,.1),person(2,.75)]
    assert d.choose(sample(1,people=people),1) is None
    assert d.choose(sample(2,people=people),2,focus=dict(track=2,at=2,session_id='first'))=='right'
    d.sent(2)
    assert d.choose(sample(20),20,occupied_elsewhere=True) is None
    assert d.choose(sample(21),21)=='right'
    d.sent(21)
    assert d.choose(sample(24,x=.05),24) is None
    assert d.choose(sample(25,x=.05),25) is None and d.scan_steps==1
    assert d.choose(sample(35,visible=False,x=.05),35) is None
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
    x = 0.0
    for at in range(300):
        if direction:=d.choose(sample(at,x=x),at):
            attempts.append(direction);d.sent(at)
            x += .05 if direction == 'right' else -.05
    assert len(attempts)==12 and d.phase=='survey_finished'
    assert d.public()['survey_confirmed_moves'] == 12
    assert d.scan_direction == 'left'
    assert d.public()['physical_motion_verified'] is False
    d.apply_mode('survey',7)
    assert d.choose(sample(301,x=x),301) is None
    d.apply_mode('survey',8)
    assert d.choose(sample(302,x=x),302) is None
    assert d.choose(sample(303,x=x),303) in {'left','right'}
    assert d.survey_attempts==1
    assert d.survey_confirmed_moves==0
    d.apply_mode('hold',9)
    assert d.choose(sample(320,visible=False),320) is None
    assert d.phase=='held'


def test_patrol_motor_ack_does_not_count_an_unchanged_view():
    director = CameraDirector()
    director.apply_mode('survey', 1)
    clear(director)
    assert director.choose(sample(16), 16) == 'right'
    director.sent(16.1)
    for at in (18.3, 19.4, 20.5, 22):
        assert director.choose(sample(at), at) is None
    assert director.scan_steps == 0 and director.position == (0, 0)
    assert director.public()['survey_attempts'] == 1
    assert director.public()['survey_confirmed_moves'] == 0


@pytest.mark.parametrize('change', ['repeated', 'cached_fit', 'opposite', 'vertical', 'new_basis', 'stale'])
def test_unrelated_or_unstable_samples_do_not_confirm_patrol(change):
    director = CameraDirector()
    director.apply_mode('survey', 1)
    clear(director)
    assert director.choose(sample(16), 16) == 'right'
    director.sent(16.1)
    first = sample(18.4, x=.05)
    assert director.choose(first, 18.4) is None
    second, now = sample(19.1, x=.05), 19.1
    if change == 'repeated':
        second = first
    elif change == 'cached_fit':
        second['_patrol_view'] = patrol_view(18.6, x=.05)
    elif change == 'opposite':
        second['_patrol_view'] = patrol_view(now, x=-.05)
    elif change == 'vertical':
        second['_patrol_view'] = patrol_view(now, x=.05, y=.05)
    elif change == 'new_basis':
        second['_patrol_view'] = patrol_view(now, x=.05, basis='restarted')
    elif change == 'stale':
        now = 21
    assert director.choose(second, now) is None
    assert director.public()['patrol_view_state'] == 'checking'
    assert director.choose(sample(25, x=.05), 25) is None
    assert director.public()['patrol_view_state'] == 'unconfirmed'
    assert director.survey_confirmed_moves == 0


@pytest.mark.parametrize('change', ['camera_session', 'game_window', 'mode'])
def test_patrol_confirmation_expires_with_its_original_scope(change):
    director = CameraDirector()
    director.apply_mode('survey', 1)
    clear(director)
    assert director.choose(sample(16), 16) == 'right'
    director.sent(16.1)
    assert director.choose(sample(18.4, x=.05), 18.4) is None
    newer = sample(19, x=.05)
    if change == 'camera_session':
        newer['session_id'] = 'second'
    elif change == 'game_window':
        newer['game'] = dict(control_context='new-game', control_revision=2)
    else:
        director.apply_mode('hold', 2)
    assert director.choose(newer, 19) is None
    assert director.patrol_confirmation is None
    assert director.survey_confirmed_moves == 0 and director.position == (0, 0)


def test_patrol_does_not_keep_pulsing_when_acks_arrive_without_movement():
    director = CameraDirector()
    clear(director)
    for attempt, at in enumerate((16, 29, 42), 1):
        assert director.choose(sample(at), at) == 'right'
        director.sent(at)
        assert director.choose(sample(at+8), at+8) is None
        assert director.failed_moves == attempt
        assert director.choose(sample(at+10), at+10) is None
        assert director.choose(sample(at+11), at+11) is None
    assert director.choose(sample(109), 109) is None
    assert director.phase == 'motor_unavailable'
    assert director.choose(sample(110), 110) is None
    assert director.choose(sample(111), 111) == 'right'
    director.sent(111)
    assert director.choose(sample(113.5, x=.05), 113.5) is None
    assert director.choose(sample(114, x=.05), 114) is None
    assert director.failed_moves == 0 and director.position == (1, 0)


def test_unknown_dispatch_forgets_relative_angle_and_preserves_confirmed_count():
    director = CameraDirector()
    director.apply_mode('survey', 1)
    clear(director)
    assert director.choose(sample(16), 16) == 'right'
    director.sent(16)
    assert director.choose(sample(18.5, x=.05), 18.5) is None
    assert director.choose(sample(19, x=.05), 19) is None
    assert director.position == (1, 0)
    assert director.choose(sample(28, x=.05), 28) == 'right'
    director.failed(28.1)
    assert director.position == (0, 0) and director.survey_confirmed_moves == 1
    assert director.survey_attempts == 2 and director.public()['patrol_view_state'] == 'unconfirmed'


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
    from homeplane.embodied.party_camera import PartyCamera
    scope={'state':{'status':'live'},'last_frame':20,'games':SimpleNamespace(public=current),
           'party_camera':PartyCamera(),'SELECTED':{'id':'cam_1111111111'},'now':20}
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
