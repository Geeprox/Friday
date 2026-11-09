"""Original rule event -> shipped sound planner; native audio is always replaced."""
import copy
import json
from pathlib import Path
import subprocess

from homeplane.embodied.motion_games import MotionGames


def node(*args, data=None):
    result = subprocess.run(['node', *args], input=json.dumps(data) if data is not None else None,
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_sound_control_native_lifecycle_and_full_shipped_bootstrap_are_inert():
    text = node('--test', 'tests/game_sound.test.cjs')
    assert 'fail 0' in text
    node('tests/embodied_games_boot.cjs')


def test_original_ski_and_joint_driven_tennis_events_reach_sound_without_scoring_changes():
    from tests.embodied.test_motion_games import LOCK, human
    game = MotionGames(); game.reset('ski')
    before = copy.deepcopy(game.publish([], 'playing', ''))
    game.distance, game.speed = 7.99, 2.4
    game.ski({'arm_effort': 1, 'lateral': 0}, .05, 1)
    after = copy.deepcopy(game.publish([], 'playing', ''))
    assert after['ski']['gates_passed'] == 1
    traces = [dict(name='original gate crossing', packets=[before, after, after], expected=['gate'])]
    game = MotionGames(); game.reset('tennis', difficulty='challenge')
    game.ball['speed'] = game.difficulty.tennis_max_speed
    game.round_pause = 2
    for i in range(30):
        game.update([human(1, x=.3), human(2, x=.7)], LOCK, i * .05)
    game.round_pause = 0
    now = game.previous
    people = [human(1, x=.3), human(2, x=.7)]
    next(p for p in people if p['id'] == game.slots[0])['joints']['rightWrist']['x'] = .9
    game.ball.update(x=.5, target_x=.5, z=.09 + game.ball['speed'] * .02, toward=0)
    before = copy.deepcopy(game.update(people, LOCK, now + .02))
    after = copy.deepcopy(game.update(people, LOCK, now + .14))
    assert before['tennis']['rally'] == 0 and after['tennis']['rally'] == 1
    traces.append(dict(name='original observed wrist to return', packets=[before, after, after], expected=['racket']))
    result = json.loads(node('tests/game_sound_packets.cjs', data=traces))
    assert [row['cues'] for row in result] == [['gate'], ['racket']]
    for trace, count in zip(traces, [2, 2]):
        stage = json.loads(node('tests/game_sound_stage.cjs', data={**trace, 'expected_notes': count}))
        assert stage['actual_delegated_click'] and stage['closed'] == 1


def test_original_clothed_tutorial_games_keep_repetition_and_station_events_distinct():
    from tests.embodied.test_adventure import Journey
    j = Journey(); before = copy.deepcopy(j.feed(count=30)); level = copy.deepcopy(j.feed('horizontal', 70))
    assert level['quest']['level'] == 1
    traces = [dict(name='original shoulder hold station', packets=[before, level], expected=['stage'])]
    j = Journey(); j.game.reset('hiit')
    j.feed(count=30); before = copy.deepcopy(j.game.public)
    j.feed('squat', 16); after = copy.deepcopy(j.feed('neutral', 16))
    assert after['hiit']['total_reps'] == before['hiit']['total_reps'] + 1
    traces.append(dict(name='original squat cycle', packets=[before, after, after], expected=['rep']))
    assert len(json.loads(node('tests/game_sound_packets.cjs', data=traces))) == 2
    for trace, count in zip(traces, [2, 1]):
        assert json.loads(node('tests/game_sound_stage.cjs', data={**trace, 'expected_notes': count}))['closed'] == 1


def test_sound_is_an_exact_public_asset_and_canvas_recording_stays_silent():
    from homeplane.embodied.game_assets import game_asset_path
    assert game_asset_path('game-sound.js') == Path('homeplane/embodied/static/game-sound.js').resolve()
    assert game_asset_path('../game-sound.js') is None
    text = Path('homeplane/embodied/static/games.html').read_text()
    assert text.index('/assets/game-sound.js') < text.index('/assets/games.js')
    text = Path('homeplane/embodied/static/games.js').read_text()
    record = text[text.index('async function beginPageRecording()'):text.index('function updateRecording(')]
    assert 'captureStream(15)' in record and 'AudioContext' not in record and 'createMediaStreamDestination' not in record
