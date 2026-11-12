"""Same observed swing must survive a longer frame without a late-swing rescue."""

import copy

import pytest

from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_tennis_match import players


def match(receiver=0):
    game = MotionGames()
    game.reset("tennis", difficulty="challenge")
    game.round_pause = 0
    game.ball.update(
        x=0.5,
        target_x=0.5,
        z=0.09 if receiver == 0 else 0.91,
        toward=receiver,
        speed=game.difficulty.tennis_max_speed,
    )
    return game


def swing(receiver=0, stamp=10):
    observed = players()
    observed[receiver].update(last_swing=stamp, swing_direction=1)
    return observed


@pytest.mark.parametrize("receiver", [0, 1])
def test_return_leaves_from_the_actual_contact_depth(receiver):
    game = match(receiver)
    game.ball["z"] = 0.06 if receiver == 0 else 0.94
    contact_z = game.ball["z"]

    game.tennis(swing(receiver), 0, 10)

    assert game.rally == 1
    assert game.ball["z"] == pytest.approx(contact_z)
    assert game.ball["toward"] == 1 - receiver


@pytest.mark.parametrize("receiver", [0, 1])
@pytest.mark.parametrize("cadence", [0.02, 0.03, 0.04, 0.06, 0.12])
@pytest.mark.parametrize("curving", [False, True])
def test_one_observed_leading_swing_has_the_same_result_at_fast_and_slow_frames(receiver, cadence, curving):
    fast, slow = match(receiver), match(receiver)
    if curving:
        fast.ball.update(x=0.704, target_x=0.12)
        slow.ball.update(x=0.704, target_x=0.12)
    observed = swing(receiver)
    for i in range(12):
        fast.tennis(observed, 0.01, 10 + (i + 1) * 0.01)
    for i in range(round(0.12 / cadence)):
        slow.tennis(observed, cadence, 10 + (i + 1) * cadence)
    assert fast.rally == slow.rally == 1
    assert fast.score == slow.score == [0, 0]
    assert fast.tennis_used_swings == slow.tennis_used_swings
    assert fast.ball["toward"] == slow.ball["toward"] == 1 - receiver
    assert slow.ball["x"] == pytest.approx(fast.ball["x"])
    assert slow.ball["z"] == pytest.approx(fast.ball["z"])


@pytest.mark.parametrize("receiver", [0, 1])
def test_original_joint_recognition_supplies_the_leading_swing_to_the_original_game_update(receiver):
    from tests.embodied.test_motion_games import human, LOCK

    game = match(receiver)
    game.round_pause = 2.0
    for i in range(30):
        game.update([human(1, x=0.3), human(2, x=0.7)], LOCK, i * 0.05)
    assert game.public["status"] == "playing"
    game.round_pause = 0
    now = game.previous
    people = [human(1, x=0.3), human(2, x=0.7)]
    player = next(p for p in people if p["id"] == game.slots[receiver])
    player["joints"]["rightWrist"]["x"] = 0.9
    start = 0.09 + game.ball["speed"] * 0.02
    game.ball.update(x=0.5, target_x=0.5, z=start if receiver == 0 else 1 - start, toward=receiver)
    first = game.update(people, LOCK, now + 0.02)
    assert first["players"][receiver]["last_swing"] == pytest.approx(now + 0.02)
    assert first["tennis"]["rally"] == 0
    after = game.update(people, LOCK, now + 0.14)
    assert after["tennis"]["rally"] == 1 and after["tennis"]["score"] == [0, 0]


@pytest.mark.parametrize(
    "focus,lateral,observation",
    [("position", -1, "fresh"), ("timing", 0, "fresh"), (None, 0, "brief_joint_gap")],
)
def test_point_feedback_coaches_only_the_observed_attempt(focus, lateral, observation):
    game = match()
    observed = players(lateral=lateral, observation=observation)
    game.tennis(observed, 0.12, 10.12)
    event = game.publish(observed, "playing", "")["tennis"]["event"]
    assert event["kind"] == "point" and event.get("next_focus") == focus


@pytest.mark.parametrize("receiver", [0, 1])
def test_a_swing_observed_after_the_baseline_still_loses_the_point(receiver):
    game = match(receiver)
    game.tennis(swing(receiver, 10.12), 0.12, 10.12)
    assert game.rally == 0 and game.score[1 - receiver] == 1


@pytest.mark.parametrize("now", [1.0, 10.0, 1234567.89])
def test_a_current_swing_inside_the_strip_is_not_lost_to_clock_subtraction(now):
    game = match()
    game.ball["z"] = 0.06
    game.tennis(swing(stamp=now), 0.01, now)
    assert game.rally == 1 and game.score == [0, 0]


@pytest.mark.parametrize("receiver", [0, 1])
@pytest.mark.parametrize("fault", ["future", "cached", "wrong_position", "already_used"])
def test_a_longer_frame_never_invents_a_valid_swing(receiver, fault):
    game = match(receiver)
    observed = swing(receiver)
    if fault == "future":
        observed[receiver]["last_swing"] = 10.13
    elif fault == "cached":
        observed[receiver]["observation"] = "brief_joint_gap"
    elif fault == "wrong_position":
        observed[receiver]["lateral"] = -1
    else:
        game.tennis_used_swings[receiver] = 10
    before = copy.deepcopy(observed)
    game.tennis(observed, 0.12, 10.12)
    assert game.rally == 0 and game.score[1 - receiver] == 1
    assert observed == before
