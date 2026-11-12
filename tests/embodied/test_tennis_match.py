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


def pose_players(swing_track=None):
    people = [human(1, x=.3), human(2, x=.7)]
    if swing_track is not None:
        wrist = people[swing_track - 1]['joints']['rightWrist']
        wrist['x'] += .22
        wrist['y'] -= .15
    return people


@pytest.mark.parametrize("receiver", [0, 1])
@pytest.mark.parametrize("missing", ["wrists", "feet"])
def test_match_waits_for_the_receiver_to_be_visible_then_uses_a_new_swing(receiver, missing):
    game = rally()
    for step in range(1, 70):
        game.update(pose_players(), LOCK, step * .05)
    receiver_track = game.slots[receiver]
    game.ball.update(x=.5, z=.07 if receiver == 0 else .93, target_x=.5, toward=receiver)
    ball = dict(game.ball)
    for step in range(70, 77):
        people = pose_players()
        player = next(p for p in people if p["id"] == receiver_track)
        if missing == "wrists":
            del player["joints"]["leftWrist"], player["joints"]["rightWrist"]
        else:
            player.pop("motion_anchor")
        state = game.update(people, LOCK, step * .05)
        assert state["status"] == "paused"
        assert state["tennis"]["ball"] == ball
        assert state["tennis"]["score"] == [0, 0]
        assert state["tennis"]["lineup"][receiver] == ("hands" if missing == "wrists" else "feet")

    resumed = game.update(pose_players(swing_track=receiver_track), LOCK, 77 * .05)
    assert resumed["status"] == "playing"
    assert resumed["tennis"]["ball"] == ball
    assert resumed["tennis"]["rally"] == 0
    returned = game.update(pose_players(), LOCK, 78 * .05)["tennis"]
    assert returned["rally"] == 1 and returned["score"] == [0, 0]


def test_first_ball_waits_for_the_designated_players_fresh_swing():
    game = MotionGames()
    game.reset('tennis')
    for step in range(160):
        state = game.update(pose_players(), LOCK, step * .05)
    waiting = state['tennis']
    assert waiting['phase'] == 'serve' and waiting['serve'] == {'player': 0, 'ready': True}
    assert waiting['score'] == [0, 0] and not waiting['guide']['active']

    wrong_player = game.update(pose_players(swing_track=1), LOCK, 8.)
    assert wrong_player['players'][1]['swings'] == 1
    assert wrong_player['tennis']['ball'] == waiting['ball'] and wrong_player['tennis']['event'] is None
    served = game.update(pose_players(swing_track=2), LOCK, 8.05)['tennis']
    assert served['phase'] == 'rally' and served['serve'] is None
    assert served['event']['kind'] == 'serve' and served['event']['player'] == 0
    assert served['flight'] == waiting['flight'] + 1 and served['ball']['toward'] == 1
    assert served['guide']['receiver'] == 1 and served['score'] == [0, 0]
    assert served['rally'] == 0


def test_service_switches_after_the_first_point_and_then_every_two():
    game = rally()
    for score, server in [([0, 1], 1), ([0, 2], 1), ([0, 3], 0), ([0, 4], 0), ([0, 5], 1)]:
        game.round_pause = 0
        game.ball.update(x=.5, z=.001, target_x=.5, toward=0)
        game.tennis(players(), .01, 10)
        point = game.publish(players(), 'playing', '')['tennis']
        assert point['score'] == score and point['phase'] == 'point'
        assert point['serve'] == {'player': server, 'ready': False} and not point['guide']['active']


def test_pause_requires_a_new_serve_after_standing_ready_again():
    game = MotionGames()
    game.reset('tennis')
    for step in range(80):
        game.update(pose_players(), LOCK, step * .05)
    game.set_paused(True)
    game.set_paused(False)
    # 第一个回来的挥拍发生在重新就位提示之前。
    old_swing = game.update(pose_players(swing_track=2), LOCK, 4.)
    assert old_swing['tennis']['event'] is None and old_swing['tennis']['ball']['toward'] is None
    for step in range(81, 110):
        ready = game.update(pose_players(), LOCK, step * .05)
    assert ready['tennis']['phase'] == 'serve' and ready['tennis']['serve']['ready']
    fresh = game.update(pose_players(swing_track=2), LOCK, 5.5)['tennis']
    assert fresh['event']['kind'] == 'serve' and fresh['ball']['toward'] == 1


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


def test_single_player_practice_goes_from_center_to_wide_to_faster_and_can_replay():
    game = MotionGames()
    lobby = game.select('tennis', tennis_variant='practice')
    assert lobby['tennis_variant'] == 'practice' and lobby['tennis']['practice']['results'] == []
    game.reset('tennis')
    state = game.public
    fed = {}
    for step in range(1600):
        tennis = state['tennis']
        guide = tennis['guide']
        attempt = len(tennis['practice']['results'])
        lane = guide['target_x'] if guide['active'] else .5
        if guide['active']:
            fed.setdefault(attempt, (lane, tennis['ball']['speed']))
        pose = human(x=.5 if attempt == 3 else .5 - (lane - .5) * .30)
        if guide['cue'] == 'swing' and attempt not in {3, 6}:
            pose['joints']['rightWrist']['x'] += .22
            pose['joints']['rightWrist']['y'] -= .15
        state = game.update([pose], LOCK, step * .05)
        if state['status'] == 'complete':
            break
    assert state['status'] == 'complete'
    assert state['tennis']['practice']['results'] == [True, True, True, False, True, True, False, True, True]
    assert state['tennis']['best_rally'] == 3 and state['tennis']['score'] == [0, 0]
    assert state['tennis']['winner'] is None and not state['tennis']['guide']['active']
    assert [fed[index][0] for index in (0, 1, 2)] == [.5, .5, .5]
    assert fed[3][0] < .3 and fed[4][0] > .7 and fed[3][1] < fed[6][1]

    game.reset('tennis')
    assert game.public['tennis_variant'] == 'practice'
    assert game.public['tennis']['practice']['results'] == []
    assert game.public['tennis']['best_rally'] == 0 and game.public['tennis']['event'] is None


def test_practice_preserves_the_in_flight_ball_when_the_players_hands_are_missing():
    game = MotionGames()
    game.select('tennis', tennis_variant='practice')
    game.reset('tennis')
    for step in range(70):
        state = game.update([human()], LOCK, step * .05)
    assert state['tennis']['guide']['active'] and state['tennis']['ball']['toward'] == 0
    no_wrists = human()
    no_wrists['joints'].pop('leftWrist')
    no_wrists['joints'].pop('rightWrist')
    for step in range(70, 200):
        held = game.update([no_wrists], LOCK, step * .05)
    assert held['status'] == 'paused' and held['tennis']['ball'] == state['tennis']['ball']
    assert held['tennis']['practice']['results'] == [] and not held['tennis']['guide']['active']
    held = game.suspend_source()
    assert held['source_recovery']['required_players'] == 1 and held['tennis']['ball'] == state['tennis']['ball']


def solo_pose(game, *, miss_once=False, change_direction=False):
    """给原动作识别器提供合成站位和挥拍，不写入球路或分数。"""
    tennis = game.public['tennis']
    guide, serve = tennis['guide'], tennis['serve']
    direction = 1 if tennis['opponent']['x'] < .5 else -1
    human_serving = serve is not None and serve['player'] == 0
    x = .5
    if guide['active']:
        x = guide['target_x'] + (.11*direction if change_direction else 0.)
    elif change_direction and human_serving and game.status == 'playing':
        x += .24*direction
    player = human(x=.5-(x-.5)*.30)
    returning = guide['cue'] == 'swing' and (not miss_once or tennis['score'][1] > 0)
    if (human_serving and serve['ready']) or returning:
        player['joints']['rightWrist']['x'] += .22*direction
        player['joints']['rightWrist']['y'] -= .15
    return player


@pytest.mark.parametrize('difficulty', ['easy', 'standard', 'challenge'])
def test_solo_can_play_and_finish_a_scored_match_from_one_pose_source(difficulty):
    game = MotionGames()
    lobby = game.select('tennis', tennis_variant='solo', difficulty=difficulty)
    assert lobby['tennis_variant'] == 'solo' and lobby['tennis']['practice'] is None
    game.reset('tennis')
    first_round = game.round_id
    observed_events, positions = {}, set()
    for frame in range(7200):
        pose = solo_pose(game, miss_once=True, change_direction=difficulty == 'challenge')
        result = game.update([pose], LOCK, frame*.05)
        tennis = result['tennis']
        assert result['source_recovery']['required_players'] == 1
        assert len(result['players']) <= 1
        positions.add(tennis['opponent']['x'])
        if tennis['event']:
            event = tennis['event']
            observed_events[event['sequence']] = (event['kind'], event['player'])
        if result['status'] == 'complete':
            break
    assert result['status'] == 'complete'
    assert tennis['winner'] == 0 and tennis['score'][0] == 7 and tennis['score'][1] >= 1
    assert tennis['score'][0]-tennis['score'][1] >= 2
    assert {('serve', 0), ('serve', 1), ('hit', 0), ('hit', 1), ('point', 0), ('point', 1)} <= set(observed_events.values())
    assert len(positions) > 10 and not tennis['guide']['active']

    game.reset('tennis')
    reset = game.public
    assert reset['round_id'] > first_round and reset['tennis_variant'] == 'solo' and reset['difficulty'] == difficulty
    assert reset['tennis']['score'] == [0, 0] and reset['tennis']['opponent']['x'] == .5
    assert reset['tennis']['winner'] is None and reset['tennis']['event'] is None


def test_solo_opponent_and_ball_wait_for_the_same_fresh_player():
    game = MotionGames()
    game.select('tennis', tennis_variant='solo')
    game.reset('tennis')
    for frame in range(160):
        played = game.update([solo_pose(game)], LOCK, frame*.05)
        tennis = played['tennis']
        if tennis['ball']['toward'] == 1 and tennis['opponent']['x'] != .5:
            break
    assert played['status'] == 'playing' and tennis['ball']['toward'] == 1
    assert tennis['opponent']['x'] != .5
    no_wrists = human()
    no_wrists['joints'].pop('leftWrist')
    no_wrists['joints'].pop('rightWrist')
    for t in range(8, 22):
        held = game.update([no_wrists], LOCK, float(t))
        assert held['status'] == 'paused'
        assert held['tennis']['ball'] == tennis['ball'] and held['tennis']['opponent'] == tennis['opponent']
        assert held['tennis']['score'] == tennis['score']
    interrupted = game.suspend_source()
    assert interrupted['source_recovery']['required_players'] == 1
    assert interrupted['tennis']['ball'] == tennis['ball']
