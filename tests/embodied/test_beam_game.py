"""在合成摄像头帧和真实对局规则之间检查标定、瞄准及恢复。"""

import pytest

from homeplane.embodied.beam_game import BOARD_ASPECT, CALIBRATION_MARKS, BeamRound, read_l_pose
from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_game_library_intent import CAMERA, bridge, displayed  # noqa: F401


def pose(dx=0., dy=0., *, released=False, mirror=False):
    points = {
        'leftShoulder': (.42, .31), 'rightShoulder': (.58, .31),
        'leftHip': (.45, .65), 'rightHip': (.55, .65),
        'leftElbow': (.444+dx, .50+dy), 'leftWrist': (.443+dx, .26+dy),
        'rightElbow': (.598+dx, .475+dy), 'rightWrist': (.445+dx, .47+dy),
    }
    if released:
        points.update(leftElbow=(.42, .53), leftWrist=(.41, .67),
                      rightElbow=(.59, .52), rightWrist=(.59, .66))
    return {name: dict(x=1-x if mirror else x, y=y, confidence=.97)
            for name, (x, y) in points.items()}


def directed(target, **kwargs):
    return pose((target[0]-.5)*.052/.25, (target[1]-.5)*.071/.21, **kwargs)


class Frames:
    def __init__(self):
        self.time = 40.
        self.round = BeamRound(sample_targets=lambda pool, n: pool[:n])

    def emit(self, joints, count=1, *, age=.02, spacing=.17):
        for _ in range(count):
            self.time += spacing
            self.round.update(joints, now=self.time, captured_at=self.time-age, aspect=BOARD_ASPECT)
        return self.round.public(self.time)

    def calibrate(self, *, mirror=False):
        for mark in CALIBRATION_MARKS:
            self.emit(directed(mark, mirror=mirror), 4)
        assert self.round.public(self.time)['phase'] == 'release'
        self.emit(pose(released=True), 3)


def test_l_pose_supports_either_orientation_without_accepting_crossed_or_unseen_arms():
    assert read_l_pose(pose(), BOARD_ASPECT)[0] == 'l'
    assert read_l_pose(pose(mirror=True), BOARD_ASPECT)[0] == 'l'
    assert read_l_pose(pose(released=True), BOARD_ASPECT)[0] == 'released'
    crossed = pose()
    crossed['rightWrist'].update(x=.53, y=.36)
    assert read_l_pose(crossed, BOARD_ASPECT)[0] == 'forming'
    spread = pose()
    spread['rightElbow']['x'] = .75
    spread['rightWrist']['x'] = .58
    assert read_l_pose(spread, BOARD_ASPECT)[0] == 'forming'
    cropped = pose()
    cropped['leftWrist']['confidence'] = .31
    assert read_l_pose(cropped, BOARD_ASPECT)[0] == 'unseen'


def test_calibration_needs_distinct_positions_and_new_capture_times():
    frame = Frames()
    for _ in range(12):
        assert frame.emit(pose(), age=.7)['calibration']['step'] == 0
    for _ in range(20):
        frame.round.update(pose(), now=frame.time, captured_at=frame.time, aspect=BOARD_ASPECT)
    assert frame.round.public(frame.time)['calibration']['step'] == 0
    assert frame.emit(pose(), 20)['calibration']['step'] == 1
    assert frame.emit(directed(CALIBRATION_MARKS[1]), 4)['calibration']['step'] == 2
    assert frame.emit(directed(CALIBRATION_MARKS[1]), 9)['calibration']['step'] == 2


@pytest.mark.parametrize('mirror', [False, True])
def test_capture_to_six_hits_requires_l_hold_on_target_and_visible_release(mirror):
    frame = Frames()
    frame.calibrate(mirror=mirror)
    assert frame.emit(pose(released=True))['aim'] is None
    target = frame.round.targets[0]
    charge = frame.emit(directed(target, mirror=mirror), 3)
    assert charge['aim'] == target and 0 < charge['charge'] < 1 and charge['hits'] == 0
    away = frame.emit(directed((.29, .5), mirror=mirror))
    assert away['charge'] == 0 and away['hits'] == 0
    assert frame.emit(directed(target, mirror=mirror), 3)['hits'] == 0
    for index, target in enumerate(frame.round.targets):
        hit = frame.emit(directed(target, mirror=mirror), 5)
        assert hit['hits'] == index+1
        assert hit['event']['kind'] == 'hit' and hit['event']['point'] == target
        assert hit['aim'] is None and hit['target'] is None
        if hit['phase'] == 'complete':
            break
        held = frame.emit(directed(target, mirror=mirror), 8)
        assert held['phase'] == 'release' and held['hits'] == index+1 and held['aim'] is None
        cropped = pose(released=True)
        cropped.pop('leftWrist')
        frame.emit(cropped, 7)
        assert frame.emit(directed(target, mirror=mirror))['phase'] == 'release'
        released = frame.emit(pose(released=True), 3)
        assert released['phase'] == 'ready' and released['aim'] is None
    assert hit['phase'] == 'complete' and hit['hits'] == hit['total']


def test_gaps_clear_charge_even_when_the_next_pose_matches_and_viewer_expires_aim():
    frame = Frames()
    frame.calibrate()
    assert frame.emit(pose(), 3)['charge'] > .5
    assert frame.round.public(frame.time+.64)['aim'] is None
    assert frame.emit(pose(), spacing=.7)['charge'] == 0
    assert frame.emit(pose(), 3)['hits'] == 0
    assert frame.emit({}, 1)['aim'] is None
    assert frame.emit(pose(), 4)['hits'] == 0
    assert frame.emit(pose())['hits'] == 1


def test_real_motion_round_requires_visible_display_and_remeasures_after_player_replacement():
    now = 60.
    game = MotionGames(clock=lambda: now, require_presentation=True)
    game.select('beam')
    with pytest.raises(ValueError, match='game_presentation_not_ready'):
        game.require_game_view('beam')
    game.reset('beam')
    number = 0

    def display(visible=True):
        nonlocal number
        g = game.viewer_state()
        report = g['presentation']
        game.observe_presentation(dict(expected_context=g['control_context'], expected_round=g['round_id'],
            expected_view=report['context'], mode='beam', view_id='b'*32,
            sequence=number, window=report['window'], visible=visible, ready=visible))
        number += 1

    def capture(joints, count=1, track=1, *, report=True, extra=False):
        nonlocal now
        for _ in range(count):
            now += .17
            if report:
                display()
            players = [dict(id=track, joints=joints)]
            if extra:
                players.append(dict(id=100, joints=joints))
            game.update(players, {'status': 'locked'}, now, captured_at=now-.02, aspect=BOARD_ASPECT)
        return game.viewer_state()

    assert capture(pose(), 4, report=False)['beam']['calibration']['step'] == 0
    for mark in CALIBRATION_MARKS:
        capture(directed(mark), 4)
    capture(pose(released=True), 3)
    assert capture(pose(), 3)['beam']['charge'] > .5
    assert capture(pose(), extra=True)['beam']['charge'] == 0
    assert capture(pose(), 5)['beam']['hits'] == 1
    assert capture(pose(released=True), 4, track=2)['source_recovery']['ready']
    assert game.beam.hits == 1 and game.beam.public(now)['calibration']['step'] == 0
    assert game.manually_paused
    game.set_paused(False)
    assert game.slots == [2] and not game.source_resume_required
    capture(pose(released=True), 30, track=2)
    assert capture(pose(), 3, track=2)['beam']['hits'] == 1
    display(visible=False)
    state = capture(pose(), 12, track=2, report=False)
    assert state['source_recovery']['required'] and state['beam']['aim'] is None and state['beam']['hits'] == 1


@pytest.mark.asyncio
async def test_library_opens_the_original_round_bound_stage_for_beam(bridge):
    catalog = (await bridge.client.get('/api/games')).json()
    assert any(app['id'] == app['mode'] == 'beam' and app['players'] == [1, 1] for app in catalog['apps'])
    result = await bridge.client.post('/api/embodied/start',
        json=dict(camera=CAMERA, app='beam', expected_intent=await displayed(bridge)))
    assert result.status_code == 200, result.text
    assert result.json()['game']['selected_mode'] == 'beam'
    assert (await bridge.client.get('/api/games/beam/open')).status_code == 200
