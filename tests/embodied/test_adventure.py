"""Real local rules driven by synthetic joints; no camera or model startup."""

import copy
import json
import math
import subprocess

import pytest

from homeplane.embodied.hiit import HIITCircuit
from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_motion_games import LOCK, human

ACTIONS = ["horizontal", "overhead", "squat", "side_step", "high_knee", "jack", "side_bend", "lunge", "reach_up", "arm_open"]


def actor(action="neutral", side="left"):
    p = human(arms=action if action in {"horizontal", "overhead"} else "down")
    j = p["joints"]
    if action == "squat":
        for name, sign in (("left", -1), ("right", 1)):
            j[name + "Hip"]["y"] = .62
            j[name + "Shoulder"]["y"] = .37
            j[name + "Knee"].update(x=.5 + sign * .17, y=.75)
    elif action == "side_step":
        dx = .08 if side == "left" else -.08
        p["motion_anchor"]["floor"][0] += dx
        for point in j.values():
            point["x"] += dx
    elif action == "high_knee":
        j[side + "Knee"]["y"] = .55
    elif action == "jack":
        for name, sign in (("left", -1), ("right", 1)):
            j[name + "Wrist"]["y"] = .1
            j[name + "Ankle"]["x"] = .5 + sign * .3
    elif action == "side_bend":
        for name in ("left", "right"):
            for part in ("Shoulder", "Elbow", "Wrist"):
                j[name + part]["x"] += -.09 if side == "left" else .09
    elif action == "lunge":
        other = "right" if side == "left" else "left"
        sign = -1 if side == "left" else 1
        j[side + "Knee"]["x"] = .5 + sign * .35
        j[side + "Ankle"]["x"] = .5 + sign * .34
        j[other + "Knee"]["x"] = .5 - sign * .23
        j[other + "Ankle"]["x"] = .5 - sign * .36
    elif action == "reach_up":
        j[side + "Wrist"]["y"] = .1
        j[side + "Elbow"]["y"] = .2
    elif action == "arm_open":
        j["leftWrist"].update(x=.1, y=.3)
        j["rightWrist"].update(x=.9, y=.3)
    elif action == "arms_closed":
        j["leftWrist"].update(x=.47, y=.4)
        j["rightWrist"].update(x=.53, y=.4)
    return p


class Journey:
    def __init__(self):
        self.now = 0.0
        self.game = MotionGames()
        self.game.clock = lambda: self.now
        self.game.reset("quest")

    def feed(self, pose="neutral", count=16, *, side="left", registration=LOCK, people=True):
        for _ in range(count):
            self.state = self.game.update([actor(pose, side)] if people else [], registration, self.now)
            self.now += .05
        return self.state


@pytest.mark.parametrize('difficulty', ['easy', 'standard', 'challenge'])
def test_ten_station_journey_completes_from_joints_and_replay_starts_clean(difficulty):
    journey = Journey()
    g = journey.game
    g.reset('quest', difficulty=difficulty)
    initial_state = journey.feed(count=30)
    initial = initial_state["quest"]
    packets = [copy.deepcopy(initial_state)]
    assert initial["targets"] == ACTIONS
    assert initial["total_levels"] == 10
    assert len({stage["teaching"] for stage in initial["stages"]}) == 10
    history = []
    for level, action in enumerate(ACTIONS):
        state = journey.feed(count=76)
        packets.append(copy.deepcopy(state))
        assert g.level == level and state["quest"]["phase"] == "work"
        stage = state["quest"]["stage"]
        if stage["hold"]:
            frames = math.ceil(stage['hold'] / .05) + 4
            packets.append(copy.deepcopy(journey.feed(action, frames // 2)))
            journey.feed(action, frames - frames // 2)
        else:
            if action == "arm_open":
                journey.feed("arms_closed")
            for rep in range(stage["reps"]):
                journey.feed(action, side="left" if rep % 2 == 0 else "right")
                packets.append(copy.deepcopy(g.public))
                journey.feed("arms_closed" if action == "arm_open" else "neutral")
                packets.append(copy.deepcopy(g.public))
        assert g.level == level + 1, (action, g.public["quest"])
        history.append(action)
        assert [item["action"] for item in g.public["quest"]["history"]] == history
        assert g.public["quest"]["progress"] == pytest.approx((level + 1) / 10)
        assert g.complete is (level == 9)
        packets.append(copy.deepcopy(g.public))
    done = copy.deepcopy(g.public)
    assert done["quest"]["total_reps"] == {'easy':18, 'standard':33, 'challenge':51}[difficulty]
    assert done["quest"]["phase"] == "complete"
    assert journey.feed("horizontal", 80)["quest"] == done["quest"]
    g.reset("quest")
    assert g.public['difficulty'] == difficulty
    assert g.public["quest"]["level"] == 0
    assert g.public["quest"]["history"] == []
    assert g.public["quest"]["progress"] == 0
    packets.append(copy.deepcopy(g.public))
    result = subprocess.run(["node", "tests/quest_scene.test.cjs", "--packets"],
                            input=json.dumps(packets), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["original_rule_packets"] == len(packets)
    assert receipt["completed"] and receipt["replayed"]


@pytest.mark.parametrize('difficulty', ['easy', 'standard', 'challenge'])
def test_full_training_difficulty_finishes_eight_original_joint_cycles(difficulty):
    journey = Journey()
    game = journey.game
    game.reset('hiit', difficulty=difficulty)
    journey.feed(count=30)
    plan = game.circuit.plan
    for level, (action, _, count, _) in enumerate(plan):
        journey.feed(count=130)
        assert game.circuit.level == level
        if action == 'arm_open':
            journey.feed('arms_closed')
        for rep in range(count):
            journey.feed(action, side='left' if rep % 2 == 0 else 'right')
            journey.feed('arms_closed' if action == 'arm_open' else 'neutral')
        assert game.circuit.level == level + 1, (action, game.public['hiit'])
    assert game.complete and game.public['status'] == 'complete'
    assert game.public['hiit']['total_reps'] == {'easy':18, 'standard':33, 'challenge':51}[difficulty]
    assert game.public['difficulty'] == difficulty
    game.reset('hiit')
    assert game.public['difficulty'] == difficulty and game.public['hiit']['total_reps'] == 0


def test_preparation_is_visible_and_neither_pose_nor_missing_source_skips_it():
    j = Journey()
    j.feed(count=30)
    state = j.feed("horizontal", 64)
    assert state["quest"]["phase"] == "transition"
    remaining = state["quest"]["transition_seconds"]
    for _ in range(3):
        state = j.feed("overhead", 24, registration={"status": "moving"})
    assert state["quest"]["transition_seconds"] == remaining
    j.feed(count=30)
    state = j.feed("overhead", 10)
    assert state["quest"]["hold"] == 0
    assert state["quest"]["level"] == 1


@pytest.mark.parametrize("interruption", ["manual", "registration", "missing", "source", "frame_gap"])
def test_new_rep_cannot_bridge_an_interruption(interruption):
    j = Journey()
    j.game.level = 3
    j.feed(count=30)
    j.feed("side_step")
    assert j.game.public["quest"]["reps"] == 0
    if interruption == "manual":
        j.game.set_paused(True)
        j.feed(count=30)
        j.game.set_paused(False)
    elif interruption == "registration":
        j.feed(count=30, registration={"status": "moving"})
    elif interruption == "missing":
        j.feed(count=30, people=False)
    elif interruption == "frame_gap":
        j.now += 4.0
    else:
        j.game.suspend_source()
        j.feed(count=50)
        assert j.game.public["source_recovery"]["ready"] is True
        j.game.set_paused(False)
        j.feed(count=80)
    j.feed(count=50)
    assert j.game.public["quest"]["reps"] == 0
    j.feed("side_step")
    j.feed()
    assert j.game.public["quest"]["reps"] == 1
    j.feed("side_step")
    j.feed()
    assert j.game.public["quest"]["reps"] == 1
    j.feed("side_step", side="right")
    j.feed()
    assert j.game.public["quest"]["reps"] == 2


def test_strict_cycle_requires_a_fresh_neutral_before_the_first_rep():
    c = HIITCircuit(plan=(("squat", "浅蹲", 1, "下蹲再站起"),), require_neutral=True)
    def feed(depth, count=10):
        for _ in range(count):
            c.update({"squat_calibrated": True, "exercise_signals": {"visible": True, "squat": depth}}, .05)
    feed(.9)
    feed(0)
    assert c.total_reps == 0
    feed(.9)
    feed(0)
    assert c.complete and c.total_reps == 1


def test_actual_stage_presenter_uses_the_same_route_and_teaching_keys():
    result = subprocess.run(["node", "--test", "tests/adventure_stage.test.cjs"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_actual_adventure_scene_lifecycle():
    result = subprocess.run(["node", "--test", "tests/quest_scene.test.cjs"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_library_controls_wait_for_current_readback():
    result = subprocess.run(["node", "--test", "tests/games_library_state.test.cjs"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
