"""Actual rule differences and round lifetime; no camera/worker/model imports."""

import copy

import pytest

from homeplane.embodied.motion_games import MotionGames


def test_ski_difficulty_changes_travel_and_visible_gate_tolerance():
    values = []
    for difficulty in ("easy", "standard", "challenge"):
        game = MotionGames()
        game.reset("ski", difficulty=difficulty)
        game.ski({"arm_effort": 1.0, "lateral": 0.0}, 1.0, 1.0)
        public = game.publish([{"lateral": 0.0}], "playing", "")
        assert public["difficulty"] == difficulty
        gate = public["ski"]["gates"][0]
        assert gate["width"] == public["ski"]["guide"]["next_gate"]["width"]
        values.append((game.distance, gate["width"]))
    assert values[0][0] < values[1][0] < values[2][0]
    assert values[0][1] > values[1][1] > values[2][1]


def test_body_route_difficulty_changes_actual_goals_and_matching_instructions():
    goals = []
    for difficulty in ("easy", "standard", "challenge"):
        game = MotionGames()
        game.reset("quest", difficulty=difficulty)
        stages = game.public["quest"]["stages"]
        assert len(stages) == 14
        assert all(f"{s['hold']:g} 秒" in s["instruction"] for s in stages if s["hold"])
        assert all(f"{s['reps']} 次" in s["instruction"] for s in stages if s["reps"])
        for _ in range(100):
            if game.level:
                break
            game.quest({"pose_quality": {"horizontal": 1.0}}, 0.1)
        assert game.level == 1
        assert game.public["quest"]["required_hold"] == stages[0]["hold"]
        assert game.quest_history[0]["hold"] == stages[0]["hold"]
        goals.append((stages[0]["hold"], sum(s["reps"] for s in stages)))
    assert goals[0][0] < goals[1][0] < goals[2][0]
    assert goals[0][1] < goals[1][1] < goals[2][1]


def test_training_uses_real_difficulty_plan_and_preserves_alternating_pairs():
    totals = []
    for difficulty in ("easy", "standard", "challenge"):
        game = MotionGames()
        game.reset("hiit", difficulty=difficulty)
        assert len(game.circuit.plan) == 12
        assert game.public["hiit"]["required_reps"] == game.circuit.plan[0][2]
        assert all(
            item[2] % 2 == 0
            for item in game.circuit.plan
            if item[0] in {"lunge", "high_knee", "side_step", "side_bend", "reach_up", "balance", "cross_knee", "side_raise"}
        )
        totals.append(sum(item[2] for item in game.circuit.plan))
    assert totals[0] < totals[1] < totals[2]


def test_tennis_actual_return_window_matches_its_difficulty_guide():
    speeds = []
    for difficulty in ("easy", "standard", "challenge"):
        game = MotionGames()
        game.reset("tennis", difficulty=difficulty)
        game.round_pause = 0
        players = [dict(lateral=0, last_swing=10, swing_direction=1, observation="fresh")] * 2
        game.ball.update(x=0.5, target_x=0.5, z=0.11, toward=0)
        guide = game.publish(players, "playing", "")["tennis"]["guide"]
        speeds.append(game.ball["speed"])
        game.tennis(players, 0.001, 10)
        assert (game.rally == 1) == (difficulty == "easy")
        assert (guide["seconds"] == 0) == (difficulty == "easy")
    assert speeds[0] < speeds[1] < speeds[2]


def test_replay_and_source_suspend_preserve_chosen_difficulty_and_reset_only_earned_work():
    game = MotionGames()
    game.select("ski", difficulty="challenge")
    round_id = game.round_id
    game.reset("ski")
    game.distance = 12
    game.gates_passed = 2
    game.suspend_source()
    assert game.public["difficulty"] == "challenge" and game.distance == 12 and game.gates_passed == 2
    game.reset("ski")
    assert game.public["difficulty"] == "challenge" and game.round_id > round_id
    assert game.distance == 0 and game.gates_passed == 0
    game.select("quest")
    assert game.public["difficulty"] == "challenge"


@pytest.mark.parametrize("difficulty", ["expert", "", 0, True, [], {}])
def test_invalid_difficulty_cannot_mutate_existing_round(difficulty):
    game = MotionGames()
    game.reset("ski")
    before = copy.deepcopy(game.public)
    with pytest.raises(ValueError):
        game.reset("tennis", difficulty=difficulty)
    assert game.public == before and game.mode == "ski"
