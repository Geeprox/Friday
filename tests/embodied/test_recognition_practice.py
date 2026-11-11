"""Lessons consume synthetic observations; these tests never load perception or media."""
import pytest

from homeplane.embodied.practice import RecognitionPractice
from tests.embodied.test_hiit import new_action_pose
from tests.embodied.test_motion_games import LOCK, human


@pytest.fixture
def lesson():
    clock = [0.0]
    return RecognitionPractice(clock=lambda: clock[0]), clock


def observe(lesson, clock, at, *, camera='a', person=1, label='ok', captured=None, moving=False):
    clock[0] = at
    lesson.observe(camera, 'session', [{'id': person, 'joints': {}}],
                   [{'person_id': person, 'gesture': label}],
                   {'status': 'moving' if moving else 'locked'}, at, at if captured is None else captured)


def test_fresh_hold_completes_once_and_fences_every_camera(lesson):
    runtime, clock = lesson
    token = runtime.begin('hand.ok', ['a', 'b'])['token']
    assert runtime.suppresses('a') and runtime.suppresses('b')
    for at in (0, .2, .4, .6, .8):
        observe(runtime, clock, at)
    result = runtime.status(token)
    assert result['phase'] == 'success'
    assert result['event']['camera'] == 'a'
    event = result['event']
    observe(runtime, clock, 1.0, camera='b')
    assert runtime.status(token)['event'] == event
    assert set(event) == {'id', 'gesture', 'camera'}
    assert runtime.suppresses('a'), 'Success must not resume household controls while still practising'
    runtime.stop(token)
    assert not runtime.suppresses('a')


def test_replayed_frames_cannot_complete_a_hold(lesson):
    runtime, clock = lesson
    token = runtime.begin('hand.ok', ['a'])['token']
    for at in (0, .2, .4, .6, .8):
        observe(runtime, clock, at, captured=0)
    assert runtime.status(token)['phase'] != 'success'
    clock[0] = 2
    assert runtime.status(token)['phase'] == 'waiting'


def test_new_person_or_moving_camera_breaks_hold(lesson):
    runtime, clock = lesson
    token = runtime.begin('hand.ok', ['a'])['token']
    observe(runtime, clock, 0)
    observe(runtime, clock, .3)
    observe(runtime, clock, .6, person=2)
    observe(runtime, clock, .9, person=2)
    assert runtime.status(token)['phase'] != 'success'
    observe(runtime, clock, 1, person=2, moving=True)
    observe(runtime, clock, 1.2, person=2)
    assert runtime.status(token)['progress'] == 0


def test_empty_other_view_does_not_erase_person_feedback(lesson):
    runtime, clock = lesson
    token = runtime.begin('hand.ok', ['a', 'b'])['token']
    observe(runtime, clock, 0)
    observe(runtime, clock, .2)
    runtime.observe('b', 'session-b', [], [], {'status': 'locked'}, .2, .2)
    assert runtime.status(token)['camera'] == 'a'
    assert runtime.status(token)['phase'] == 'recognizing'


def test_foreign_token_cannot_read_or_end_lesson_and_lease_expires(lesson):
    runtime, clock = lesson
    token = runtime.begin('hand.ok', ['a'])['token']
    assert runtime.status('0' * 32)['phase'] == 'ended'
    runtime.stop('0' * 32)
    assert runtime.suppresses('a')
    with pytest.raises(ValueError):
        runtime.begin('hand.ok', ['b'])
    clock[0] = 90
    assert runtime.status(token)['phase'] == 'ended'
    assert not runtime.suppresses('a')
    runtime.begin('hand.ok', ['b'])


def test_unknown_input_does_not_create_a_lease(lesson):
    runtime, _ = lesson
    with pytest.raises(ValueError):
        runtime.begin('gesture.made_up', ['a'])
    assert not runtime.active()


@pytest.mark.parametrize('action', ['balance', 'cross_knee'])
def test_new_exercise_practice_reports_success_after_return_to_neutral(lesson, action):
    runtime, clock = lesson
    gesture = f'exercise.{action}'
    token = runtime.begin(gesture, ['a', 'b'])['token']

    def feed(person, frames):
        for _ in range(frames):
            clock[0] += .05
            runtime.observe('b', 'session', [person], [], LOCK, clock[0], clock[0])
        return runtime.status(token)

    feed(human(), 22)
    if action == 'cross_knee':
        feed(new_action_pose(action), 6)
        occluded = new_action_pose(action)
        occluded['joints'].pop('rightWrist')
        feed(occluded, 1)
        assert feed(human(), 6)['phase'] != 'success'
    assert feed(new_action_pose(action), 27)['phase'] != 'success'
    if action == 'cross_knee':
        release = new_action_pose(action)
        release['joints']['rightWrist'] = human()['joints']['rightWrist']
        assert feed(release, 12)['phase'] != 'success'
    result = feed(human(), 6)
    assert result['phase'] == 'success'
    assert result['event']['camera'] == 'b' and result['event']['gesture'] == gesture
    assert runtime.suppresses('b')


@pytest.mark.asyncio
async def test_start_does_not_start_a_worker_and_stop_is_token_bound():
    from homeplane.embodied.service import EmbodiedService
    from types import SimpleNamespace
    service = EmbodiedService()
    try:
        with pytest.raises(ValueError, match='not_running'):
            await service.practice('start', {'gesture': 'hand.ok'})
        calls = []
        service.process = SimpleNamespace(returncode=None)

        async def request(path, body):
            calls.append((path, body))
            return {'phase': 'waiting', 'token': 'a' * 32} if path.endswith('/start') else {'phase': 'ended'}

        service.request = request
        await service.practice('start', {'gesture': 'hand.ok'})
        assert service.practice_deadline > 0
        await service.practice('stop', {'token': 'b' * 32})
        assert service.practice_deadline > 0
        await service.practice('stop', {'token': 'a' * 32})
        assert service.practice_deadline == 0
        assert [c[0] for c in calls] == ['/api/practice/start', '/api/practice/stop', '/api/practice/stop']
    finally:
        await service.client.aclose()
