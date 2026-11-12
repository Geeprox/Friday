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


def sample(at, *, visible=True, people=(), session='first', x=0.0, y=0.0, basis='synthetic-room'):
    return dict(status='live', _local_received=at, scene_visible=visible,
                people=list(people), session_id=session, _patrol_view=patrol_view(at, x=x, y=y, basis=basis))


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
        motion_lock=asyncio.Lock(), room_lock=asyncio.Lock(), waiters=waiters,
        camera_is_open=camera_is_open, recognizer=None,
        process=SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=write, drain=AsyncMock())),
        self=SimpleNamespace(focus=Mock(return_value=None), occupied_elsewhere=Mock(return_value=False),
                             review_pending=Mock(return_value=False), fall_pending=Mock(return_value=False),
                                 sound_cue=Mock(return_value=None),
                             runtime_log=SimpleNamespace(record=Mock()), on_motion=None),
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


def test_fall_has_a_short_independent_window_and_respects_camera_ownership():
    director = CameraDirector()
    director.apply_mode('auto', 1)
    clear(director)
    assert director.choose(sample(1), 1, review_pending=True) is None
    assert director.choose(sample(31), 31, review_pending=True, fall_pending=True) is None
    assert director.phase == 'review_held'
    assert director.choose(sample(38.9), 38.9, fall_pending=True) is None
    assert director.choose(sample(39), 39, review_pending=True, fall_pending=True) == 'right'
    assert director.decision_current(sample(39.1), 39.1, 'right', fall_pending=True)
    game = dict(sample(40), game={'mode': 'ski'})
    assert director.choose(game, 40, fall_pending=True) is None and director.phase == 'game_held'
    director.apply_mode('hold', 2)
    assert director.choose(sample(41), 41, fall_pending=True) is None and director.phase == 'held'


def test_two_level_survey_is_finite_and_repeated_intent_does_not_restart_it():
    d=CameraDirector();d.apply_mode('survey',7)
    attempts=[]
    x = y = 0.0
    for at in range(300):
        if direction:=d.choose(sample(at,x=x,y=y),at):
            attempts.append(direction);d.sent(at)
            x += .05 * ((direction == 'right') - (direction == 'left'))
            y += .05 * ((direction == 'down') - (direction == 'up'))
    assert attempts == ['right', 'right', 'down', 'left', 'left', 'left', 'left', 'up', 'right', 'right']
    assert d.phase=='survey_finished' and d.public()['survey_result'] == 'complete'
    assert d.public()['survey_confirmed_moves'] == len(attempts)
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

def patrol_sample(at, **changes):
    return dict(sample(at, people=[person(1, .42)], **changes), tracking_current=True)


def test_scheduled_survey_uses_full_route_and_rearms_after_completion():
    director = CameraDirector()
    director.apply_mode('patrol', 'synthetic-schedule')
    due = director.SCHEDULE_INTERVAL
    assert director.choose(patrol_sample(0), 0) is None
    assert director.choose(patrol_sample(due-1), due-1) is None
    assert not director.public()['scheduled_survey_active']
    attempted = []
    x = y = 0.0
    for at in range(due, due+director.SCHEDULE_SWEEP_LIMIT):
        director.apply_mode('patrol', 'synthetic-schedule')
        view = patrol_sample(at, x=x, y=y)
        if direction := director.choose(view, at, occupied_elsewhere=True):
            assert director.decision_current(view, at, direction, occupied_elsewhere=True)
            attempted.append(direction)
            director.sent(at)
            x += .05 * ((direction == 'right') - (direction == 'left'))
            y += .05 * ((direction == 'down') - (direction == 'up'))
        if director.public()['survey_result'] == 'complete' and not director.public()['scheduled_survey_active']:
            break
    assert tuple(attempted) == director.SURVEY_ROUTE
    assert director.public()['survey_confirmed_moves'] == len(attempted)
    assert not director.public()['physical_motion_verified']
    due = at+director.SCHEDULE_INTERVAL
    assert director.choose(patrol_sample(due-1), due-1) is None
    assert not director.public()['scheduled_survey_active']
    assert director.choose(patrol_sample(due), due) is None
    assert director.public()['scheduled_survey_active']
    assert director.public()['survey_confirmed_moves'] == 0


def test_scheduled_survey_waits_for_clear_input_and_does_not_catch_up_on_a_new_source():
    director = CameraDirector()
    director.apply_mode('patrol', 'synthetic-schedule')
    due = director.SCHEDULE_INTERVAL
    director.choose(patrol_sample(0), 0)
    assert director.choose(patrol_sample(due, session='replacement'), due) is None
    assert not director.public()['scheduled_survey_active']
    due *= 2
    assert director.choose(patrol_sample(due-2, session='replacement'), due) is None
    assert director.choose(patrol_sample(due, session='replacement'), due, review_pending=True) is None
    assert not director.public()['scheduled_survey_active']
    view = patrol_sample(due+1, session='replacement')
    assert director.choose(dict(view, game={'mode': 'ski'}), due+1) is None
    assert not director.public()['scheduled_survey_active']
    assert director.choose(patrol_sample(due+2, session='replacement'), due+2) is None
    assert director.public()['scheduled_survey_active']


@pytest.mark.parametrize('interruption', ['wake', 'game', 'motor', 'deadline', 'source', 'hold'])
def test_scheduled_survey_interruptions_fence_a_pending_motor_step(interruption):
    from homeplane.embodied.camera_audio import WakeCameraCue

    director = CameraDirector()
    director.apply_mode('patrol', 'synthetic-schedule')
    due = director.SCHEDULE_INTERVAL
    director.choose(patrol_sample(0), 0)
    director.choose(patrol_sample(due), due)
    assert director.choose(patrol_sample(due+.5), due+.5) is None
    at = due+16
    direction = director.choose(patrol_sample(at), at)
    assert direction == 'right'
    view = patrol_sample(at+.1)
    cue = None
    if interruption == 'wake':
        source = ('synthetic-capture', 'synthetic-context')
        cue = WakeCameraCue(at, 'first', source, 1)
        view['_wake_source'] = source
    elif interruption == 'game':
        view['game'] = {'mode':'ski'}
    elif interruption == 'motor':
        director.failed(at)
    elif interruption == 'deadline':
        at = due+director.SCHEDULE_SWEEP_LIMIT
        view = patrol_sample(at+.1)
    elif interruption == 'source':
        view = patrol_sample(at+.1, session='replacement')
    else:
        director.apply_mode('hold', 'manual')
    assert not director.decision_current(view, at+.1, direction, sound_cue=cue)
    assert director.choose(view, at+.1, sound_cue=cue) is None
    report = director.public()
    assert not report.get('scheduled_survey_active')
    assert report['survey_result'] == (None if interruption == 'hold' else 'interrupted')
    assert report['survey_confirmed_moves'] == 0
    if interruption == 'wake':
        assert director.sound_search_cue is cue


def test_explicit_survey_uses_its_route_when_people_or_speech_focus_change():
    director = CameraDirector()
    director.apply_mode('survey', 'explicit')
    clear(director)
    assert director.public()['reason'] == '巡视中，观察当前视角'
    current = sample(16, people=[person(1, .08)])
    assert director.choose(current, 16, occupied_elsewhere=True,
                           focus=dict(track=1, at=16, session_id='first')) == 'right'
    updated = sample(16.3, people=[person(2, .08), person(3, .1)])
    assert director.decision_current(updated, 16.3, 'right', occupied_elsewhere=True,
                                     focus=dict(track=2, at=16.3, session_id='first'))
    assert not director.decision_current(updated, 16.3, 'right', fall_pending=True)
    assert not director.decision_current(updated, 16.3, 'right', review_pending=True)
    assert not director.decision_current(dict(updated, game={'mode': 'ski'}), 16.3, 'right')
    assert not director.decision_current(dict(updated, _patrol_view=patrol_view(16.3, x=.03)), 16.3, 'right')
    assert not director.decision_current(dict(updated, _patrol_view=None), 16.3, 'right')
    assert director.target is None and director.survey_attempts == 1


def start_low_pass(director):
    director.apply_mode('survey', 'synthetic-route')
    clear(director)
    for index, at in enumerate((16, 28)):
        assert director.choose(sample(at, x=.05*index), at) == 'right'
        director.sent(at)
        for offset in (2.5, 3):
            assert director.choose(sample(at+offset, x=.05*(index+1)), at+offset) is None
    assert director.choose(sample(40, x=.1), 40) == 'down'


def test_unconfirmed_downward_survey_does_not_continue_an_unknown_route():
    director = CameraDirector()
    start_low_pass(director)
    director.sent(40)
    for at in (42.5, 43, 46, 48, 55, 90):
        assert director.choose(sample(at, x=.15), at) is None
    assert director.public()['survey_result'] == 'interrupted'
    assert director.survey_confirmed_moves == 2 and director.survey_attempts == 3
    assert director.public()['patrol_view_state'] == 'unconfirmed'


@pytest.mark.parametrize('change', ['session', 'registration', 'game'])
def test_survey_cannot_continue_after_its_live_view_is_replaced(change):
    director = CameraDirector()
    start_low_pass(director)
    director.sent(40)
    for at in (42.5, 43):
        assert director.choose(sample(at, x=.1, y=.05), at) is None
    assert director.survey_confirmed_moves == 3
    if change == 'game':
        assert director.choose(dict(sample(52, x=.1, y=.05), game={'mode':'ski'}), 52) is None
    for at in (55, 85, 120):
        current = sample(at, x=.1, y=.05, session='new' if change == 'session' else 'first',
                         basis='new-reference' if change == 'registration' else 'synthetic-room')
        assert director.choose(current, at) is None
    assert director.public()['survey_result'] == 'interrupted' and director.survey_confirmed_moves == 3


def test_secondary_reconnect_reports_partial_survey_and_waits_for_new_intent():
    director = CameraDirector()
    director.apply_mode('survey', 'old')
    director.restore_survey_progress(4, 3)
    for at in (0, 16, 30):
        assert director.choose(sample(at), at) is None
    assert director.public()['survey_result'] == 'interrupted' and director.survey_confirmed_moves == 3
    director.apply_mode('survey', 'new')
    assert director.public()['survey_result'] is None
    assert director.survey_attempts == director.survey_confirmed_moves == 0


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
    assert director.public()['patrol_view_state'] == ('unconfirmed' if change == 'new_basis' else 'checking')
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
