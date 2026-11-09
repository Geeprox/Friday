"""Actual joint rules and rehearsal owner; no capture, model or device startup."""

import copy
import subprocess

import pytest

from homeplane.embodied.hiit import PLAN, HIITCircuit
from homeplane.embodied.motion_games import BodyMotion
from homeplane.embodied.practice import RecognitionPractice
from homeplane.embodied.quest import STAGES
from tests.embodied.test_motion_games import LOCK, human


def upper_actor(action='neutral', side='left'):
    person = human(arms='horizontal' if action == 'arm_open' else 'down')
    joints = person['joints']
    if action == 'reach_up':
        joints[side+'Wrist']['y'] = .1
        joints[side+'Elbow']['y'] = .2
    elif action == 'arms_closed':
        for name, x in [('left', .47), ('right', .53)]:
            joints[name+'Wrist'].update(x=x, y=.40)
    return person


class Observations:
    def __init__(self):
        self.now = 0.0
        self.motion = BodyMotion()

    def feed(self, person, count=16):
        for _ in range(count):
            result = self.motion.update(copy.deepcopy(person), self.now)
            self.now += .05
        return result


def test_routes_include_reachable_upper_body_stations_and_distinct_teaching():
    for action in ('reach_up', 'arm_open'):
        assert any(row[0] == action for row in PLAN)
        stage = next(stage for stage in STAGES if stage.action == action)
        assert stage.reps >= 4
        assert stage.teaching == 'exercise.'+action


def test_original_teaching_source_motion_and_fallback_contract():
    result = subprocess.run(['node', '--test', 'tests/upper_body_teaching.test.cjs'],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr


def test_reach_requires_one_raised_arm_and_standing_body():
    stream = Observations()
    stream.feed(upper_actor())
    for side in ('left', 'right'):
        assert stream.feed(upper_actor('reach_up', side))['exercise_signals']['reach_up'] == side
    assert stream.feed(human(arms='overhead'))['exercise_signals']['reach_up'] is None
    person = upper_actor('reach_up')
    person['joints']['rightWrist']['confidence'] = .1
    assert stream.feed(person)['exercise_signals']['reach_up'] is None
    person = upper_actor('reach_up')
    for side in ('left', 'right'):
        person['joints'][side+'Shoulder']['x'] += .15
    assert stream.feed(person)['exercise_signals']['reach_up'] is None


def test_chest_open_requires_clear_arm_band_and_actual_closed_return():
    stream = Observations()
    stream.feed(upper_actor())
    assert stream.feed(upper_actor('arm_open'))['exercise_signals']['arm_open'] == 'open'
    assert stream.feed(upper_actor('arms_closed'))['exercise_signals']['arm_open'] == 'closed'
    assert stream.feed(upper_actor())['exercise_signals']['arm_open'] is None
    person = upper_actor('arms_closed')
    person['joints']['leftWrist']['confidence'] = .1
    assert stream.feed(person)['exercise_signals']['arm_open'] is None
    person = upper_actor('arms_closed')
    person['joints'].pop('rightAnkle')
    result = stream.feed(person)
    assert not result['exercise_signals']['visible']
    assert result['exercise_signals'].get('arm_open') is None


@pytest.mark.parametrize('action,neutral', [('reach_up', 'neutral'), ('arm_open', 'arms_closed')])
def test_complete_return_counts_once_and_does_not_bridge_lost_observation(action, neutral):
    stream = Observations()
    circuit = HIITCircuit(plan=((action, action, 4, '完成动作后回到起点'),), require_neutral=True)

    def feed(pose, count=16, side='left'):
        for _ in range(count):
            p = stream.feed(upper_actor(pose, side), 1)
            circuit.update(p, .05)

    feed(action)
    feed(neutral)
    assert circuit.total_reps == 0
    feed(action)
    assert circuit.total_reps == 0
    feed(neutral)
    assert circuit.total_reps == 1
    feed(neutral, 40)
    assert circuit.total_reps == 1
    if action == 'reach_up':
        feed(action)
        feed(neutral)
        assert circuit.total_reps == 1
    feed(action, side='right')
    for _ in range(12):
        circuit.update({'squat_calibrated': True, 'exercise_signals': {'visible': False}}, .05)
    feed(neutral)
    assert circuit.total_reps == 1
    feed(action, side='right')
    feed(neutral)
    assert circuit.total_reps == 2


@pytest.mark.parametrize('action,neutral', [('reach_up', 'neutral'), ('arm_open', 'arms_closed')])
@pytest.mark.parametrize('interrupt', [None, 'session', 'person', 'gap', 'camera_motion'])
def test_independent_practice_uses_actual_cycle_and_retires_unfinished_source(action, neutral, interrupt):
    now = 0.0
    practice = RecognitionPractice(clock=lambda: now)
    lease = practice.begin('exercise.'+action, ['cam_synthetic'])
    session, track = 'source-1', 1

    def feed(pose, count=16, registration=LOCK):
        nonlocal now
        for _ in range(count):
            person = upper_actor(pose)
            person['id'] = track
            practice.observe('cam_synthetic', session, [person], [], registration, now, now)
            now += .05

    feed(neutral, 25)
    feed(action)
    assert practice.status(lease['token'])['phase'] != 'success'
    if interrupt == 'session':
        session = 'source-2'
    elif interrupt == 'person':
        track = 2
    elif interrupt == 'gap':
        now += .6
    elif interrupt == 'camera_motion':
        feed(neutral, registration={'status': 'moving'})
    feed(neutral, 25)
    if interrupt:
        assert practice.status(lease['token'])['phase'] != 'success'
        feed(action)
        feed(neutral)
    result = practice.status(lease['token'])
    assert result['phase'] == 'success'
    assert result['event']['gesture'] == 'exercise.'+action
    assert not {'joints', 'image', 'identity'} & result.keys()
