"""Pure match contracts: readable guidance follows the actual collision rules."""
import math

import pytest

from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_motion_games import LOCK, human


def players(*, swing=-100, lateral=0, observation='fresh'):
    return [dict(lateral=lateral, last_swing=swing, swing_direction=1, observation=observation),
            dict(lateral=0, last_swing=-100, swing_direction=-1, observation='fresh')]


def rally(difficulty='standard'):
    game = MotionGames()
    game.reset('tennis', difficulty=difficulty)
    game.round_pause = 0  # Boundary tests begin after the explicit preparation.
    return game


@pytest.mark.parametrize('stamp', [10.1, float('nan'), float('inf'), True])
def test_future_or_invalid_swing_cannot_return_a_ball(stamp):
    game = rally()
    game.ball.update(x=.5, z=.06, toward=0)
    game.tennis(players(swing=stamp), .01, 10)
    assert game.rally == 0


def test_brief_joint_hold_keeps_avatar_but_never_turns_cached_swing_into_a_hit():
    game = rally()
    game.ball.update(x=.5, z=.06, toward=0)
    game.tennis(players(swing=10, observation='brief_joint_gap'), .01, 10)
    assert game.rally == 0


def test_swing_after_ball_crosses_the_baseline_cannot_save_the_point():
    game = rally()
    game.ball.update(x=.5, z=.001, toward=0)
    game.tennis(players(swing=10), .01, 10)
    assert game.rally == 0 and game.score == [0, 1]
    state = game.publish(players(), 'playing', '')['tennis']
    assert state['phase'] == 'point' and state['event']['kind'] == 'point'
    assert state['event']['player'] == 1


def test_ready_timer_and_ball_freeze_when_paused_or_players_are_missing():
    game = MotionGames()
    game.reset('tennis')
    state = game.publish(players(), 'playing', '')['tennis']
    assert state['phase'] == 'ready' and state['ready_seconds'] == 2
    ball = dict(game.ball)
    game.set_paused(True)
    game.update([], dict(status='locked'), 50)
    assert game.ball == ball and game.round_pause == 2
    game.set_paused(False)
    game.update([], dict(status='locked'), 100)
    assert game.ball == ball and game.round_pause == 2


@pytest.mark.parametrize('receiver', [0, 1])
@pytest.mark.parametrize('difficulty', ['easy', 'standard', 'challenge'])
def test_visible_intercept_is_the_actual_analytic_ball_arrival(receiver, difficulty):
    game = rally(difficulty)
    game.ball.update(x=.23, target_x=.8, z=.8 if receiver == 0 else .2, toward=receiver, speed=.42)
    guide = game.publish(players(), 'playing', '')['tennis']['guide']
    assert guide['receiver'] == receiver and guide['active']
    expected = guide['target_x']
    remaining = guide['seconds']
    now = 10
    while remaining > 1e-10:
        step = min(.02, remaining)
        game.tennis(players(), step, now)
        now += step
        remaining -= step
    assert game.ball['x'] == pytest.approx(expected, abs=1e-6)
    window = {'easy':.14, 'standard':.1, 'challenge':.08}[difficulty]
    assert guide['window'] == pytest.approx(window)
    assert game.ball['z'] == pytest.approx(window if receiver == 0 else 1-window, abs=1e-6)


def test_guide_prompts_movement_then_swing_at_the_real_hitting_window():
    game = rally()
    game.ball.update(x=.7, target_x=.7, z=.6, toward=0)
    left = players(lateral=-1)
    guide = game.publish(left, 'playing', '')['tennis']['guide']
    assert guide['cue'] == 'right' and not guide['in_position']
    aligned = players(lateral=.5)
    assert game.publish(aligned, 'playing', '')['tennis']['guide']['cue'] == 'wait'
    game.ball['z'] = .18
    assert game.publish(aligned, 'playing', '')['tennis']['guide']['cue'] == 'swing'
    game.ball['z'] = .07
    assert game.publish(aligned, 'playing', '')['tennis']['guide']['cue'] == 'swing'
    assert not game.publish(aligned, 'paused', '')['tennis']['guide']['active']


def test_source_cue_arrives_before_contact_and_recent_swing_is_accepted_on_arrival():
    game = rally()
    game.ball.update(x=.5, target_x=.5, z=.18, toward=0)
    p = players(swing=10)
    guide = game.publish(p, 'playing', '')['tennis']['guide']
    assert guide['cue'] == 'swing' and guide['seconds'] > 0
    for step in range(6):
        game.tennis(p, .05, 10+step*.05)
        if game.rally: break
    assert game.rally == 1 and game.ball['toward'] == 1


def test_hit_event_is_source_confirmed_and_replay_discards_old_feedback():
    game = rally()
    round_id = game.public['round_id']
    game.ball.update(x=.5, z=.06, toward=0)
    game.tennis(players(swing=10), .01, 10)
    state = game.publish(players(), 'playing', '')['tennis']
    assert state['event']['kind'] == 'hit' and state['event']['player'] == 0
    assert state['event']['quality'] == 'perfect'
    sequence = state['event']['sequence']
    game.tennis(players(swing=10), .01, 10.01)
    assert game.publish(players(), 'playing', '')['tennis']['event']['sequence'] == sequence
    game.reset('tennis')
    assert game.public['round_id'] != round_id
    assert game.public['tennis']['event'] is None and game.score == [0, 0]


def test_guide_has_no_fake_target_for_unknown_players_or_finished_match():
    game = rally()
    assert not game.publish([], 'waiting', '')['tennis']['guide']['active']
    game.tennis_winner = 0
    state = game.publish(players(), 'complete', '')['tennis']
    assert state['phase'] == 'complete' and not state['guide']['active']
    assert all(math.isfinite(value) for key, value in state['guide'].items() if type(value) is float)


def test_lineup_keeps_mirrored_player_numbers_through_dropout_and_replay():
    game = MotionGames()
    game.reset('tennis')
    right, left = human(1, x=.3), human(2, x=.7)
    state = game.update([right, left], LOCK, 10)
    assert game.slots == [2, 1]
    assert state['tennis']['lineup'] == ['calibrating', 'calibrating']
    for step in range(1, 15):
        state = game.update([left, right], LOCK, 10 + step * .1)
    assert state['status'] == 'playing'
    spectator = human(3, x=.7)
    state = game.update([right, spectator], LOCK, 11.5)
    assert state['status'] == 'waiting'
    assert state['tennis']['lineup'] == ['missing', 'ready']
    assert not state['tennis']['guide']['active']
    state = game.update([right, left], LOCK, 11.6)
    assert state['status'] == 'playing'
    assert [player['id'] for player in state['players']] == [2, 1]
    assert game.update([], dict(status='moving'), 11.7)['tennis']['lineup'] == []
    assert game.set_paused(True)['tennis']['lineup'] == []
    assert game.set_paused(False)['tennis']['lineup'] == []
    game.reset('tennis')
    assert game.public['tennis']['lineup'] == []


def test_lineup_requests_visible_feet_for_only_the_affected_player():
    game = MotionGames()
    game.reset('tennis')
    right, left = human(1, x=.3), human(2, x=.7)
    for step in range(15):
        game.update([left, right], LOCK, 10 + step * .1)
    right.pop('motion_anchor')
    for step in range(15, 20):
        state = game.update([right, left], LOCK, 10 + step * .1)
    assert state['status'] == 'paused'
    assert state['tennis']['lineup'] == ['ready', 'feet']
