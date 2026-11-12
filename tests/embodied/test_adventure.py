"""Real local rules driven by synthetic joints; no camera or model startup."""

import copy
import json
import math
import subprocess

import pytest

from homeplane.embodied.hiit import HIITCircuit
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.practice import RecognitionPractice
from tests.embodied.test_hiit import new_action_pose
from tests.embodied.test_motion_games import LOCK, human

ACTIONS = ["horizontal", "overhead", "squat", "side_tap", "high_knee", "jack", "balance",
           "side_bend", "lunge", "reach_up", "arm_open", "cross_knee", "side_raise", "overhead_clap"]
NEUTRAL = {"arm_open": "arms_closed", "overhead_clap": "arms_high_open"}


def actor(action="neutral", side="left"):
    if action in {"balance", "cross_knee"}:
        return new_action_pose(action, side)
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
    elif action in {"side_tap", "tap_missing_ankle", "two_feet_out"}:
        feet = ("left", "right") if action == "two_feet_out" else ("right" if side == "left" else "left",)
        for foot in feet:
            dx = .08 if foot == "right" else -.08
            j[foot+"Knee"]["x"] += dx
            j[foot+"Ankle"]["x"] += 2 * dx
        if action == "tap_missing_ankle":
            p["motion_anchor"] = {"method": "extrapolated", "floor": [.5, .5]}
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
    elif action == "side_raise":
        sign = -1 if side == "left" else 1
        j[side+"Knee"].update(x=.5+sign*.167, y=.71)
        j[side+"Ankle"].update(x=.5+sign*.24, y=.86)
    elif action in {"arms_high_open", "overhead_clap"}:
        gap = .19 if action == "arms_high_open" else .025
        for side, sign in (("left", -1), ("right", 1)):
            j[side+"Wrist"].update(x=.5+sign*gap, y=.12)
            j[side+"Elbow"].update(x=.5+sign*.13, y=.25)
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
def test_fourteen_station_journey_completes_from_joints_and_replay_starts_clean(difficulty):
    journey = Journey()
    g = journey.game
    g.reset('quest', difficulty=difficulty)
    initial_state = journey.feed(count=30)
    initial = initial_state["quest"]
    packets = [copy.deepcopy(initial_state)]
    assert initial["targets"] == ACTIONS
    assert initial["total_levels"] == len(ACTIONS)
    assert len({stage["teaching"] for stage in initial["stages"]}) == len(ACTIONS)
    history = []
    for level, action in enumerate(ACTIONS):
        state = journey.feed(count=76)
        packets.append(copy.deepcopy(state))
        assert g.level == level and state["quest"]["phase"] == "work"
        stage = state["quest"]["stage"]
        if action == "side_tap":
            assert stage["step_pattern"] == {
                "easy": ["left", "right"],
                "standard": ["left", "right", "right", "left"],
                "challenge": ["left", "right", "right", "left", "left", "right"],
            }[difficulty]
        if stage["hold"]:
            frames = math.ceil(stage['hold'] / .05) + 4
            packets.append(copy.deepcopy(journey.feed(action, frames // 2)))
            journey.feed(action, frames - frames // 2)
        else:
            neutral = NEUTRAL.get(action, "neutral")
            journey.feed(neutral)
            sides = stage.get("step_pattern") or ["left" if rep % 2 == 0 else "right" for rep in range(stage["reps"])]
            for side in sides:
                journey.feed(action, 26 if action == "balance" else 16, side=side)
                packets.append(copy.deepcopy(g.public))
                journey.feed(neutral)
                packets.append(copy.deepcopy(g.public))
        assert g.level == level + 1, (action, g.public["quest"])
        history.append(action)
        assert [item["action"] for item in g.public["quest"]["history"]] == history
        assert g.public["quest"]["progress"] == round((level + 1) / len(ACTIONS), 4)
        assert g.complete is (level == len(ACTIONS) - 1)
        packets.append(copy.deepcopy(g.public))
    done = copy.deepcopy(g.public)
    assert done["quest"]["total_reps"] == {'easy':26, 'standard':49, 'challenge':75}[difficulty]
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


@pytest.mark.parametrize('route,expected', [
    ('forest', ACTIONS[:3]), ('stream', ACTIONS[3:7]), ('camp', ACTIONS[7:]),
])
def test_each_chapter_ends_at_its_last_station_and_replays_from_its_first(route, expected):
    journey = Journey()
    game = journey.game
    game.reset('off')
    game.select('quest', quest_route=route, difficulty='standard')
    assert game.public['quest']['targets'] == expected
    game.reset('quest')
    for level, action in enumerate(expected):
        state = journey.feed(count=76)
        station = state['quest']['stage']
        assert state['quest_route'] == route and station['scene'] == route
        assert station['action'] == action
        if station['hold']:
            journey.feed(action, math.ceil(station['hold'] / .05) + 4)
        else:
            neutral = NEUTRAL.get(action, 'neutral')
            journey.feed(neutral)
            sides = station.get('step_pattern') or ['left' if rep % 2 == 0 else 'right' for rep in range(station['reps'])]
            for side in sides:
                journey.feed(action, 26 if action == 'balance' else 16, side=side)
                journey.feed(neutral)
        assert game.level == level + 1
    completed = game.public
    assert completed['status'] == 'complete' and completed['quest']['progress'] == 1
    assert [item['action'] for item in completed['quest']['history']] == expected
    game.reset('quest')
    assert game.public['quest_route'] == route
    assert game.public['quest']['stage']['action'] == expected[0]
    assert game.public['quest']['history'] == []


@pytest.mark.parametrize('difficulty', ['easy', 'standard', 'challenge'])
def test_full_training_difficulty_finishes_all_twelve_actions_from_joints(difficulty):
    journey = Journey()
    game = journey.game
    game.reset('hiit', difficulty=difficulty)
    journey.feed(count=30)
    plan = game.circuit.plan
    for level, (action, _, count, _) in enumerate(plan):
        journey.feed(count=130)
        assert game.circuit.level == level
        neutral = NEUTRAL.get(action, 'neutral')
        journey.feed(neutral)
        for rep in range(count):
            journey.feed(action, 26 if action == 'balance' else 16, side='left' if rep % 2 == 0 else 'right')
            journey.feed(neutral)
        assert game.circuit.level == level + 1, (action, game.public['hiit'])
    assert game.complete and game.public['status'] == 'complete'
    assert game.public['hiit']['total_reps'] == {'easy':26, 'standard':49, 'challenge':75}[difficulty]
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
    j.feed("side_tap")
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
    j.feed("side_tap")
    j.feed()
    assert j.game.public["quest"]["reps"] == 1
    j.feed("side_tap")
    j.feed()
    assert j.game.public["quest"]["reps"] == 1
    j.feed("side_tap", side="right")
    j.feed()
    assert j.game.public["quest"]["reps"] == 2


def test_creek_stones_require_one_foot_in_the_cued_direction_then_return():
    j = Journey()
    j.game.reset("off")
    j.game.select("quest", quest_route="stream")
    j.game.reset("quest")
    q = j.feed(count=40)["quest"]
    assert q["step"] == {"side": "left", "phase": "step"}
    for pose in ("side_step", "two_feet_out", "side_raise", "tap_missing_ankle"):
        j.feed(pose, side="right")
        assert j.feed()["quest"]["reps"] == 0
    q = j.feed("side_tap", side="right")["quest"]
    assert q["step"] == {"side": "left", "phase": "center"}
    j.feed("side_tap", side="left")
    assert j.feed()["quest"]["reps"] == 0
    q = j.feed("side_tap", side="left")["quest"]
    assert q["step"] == {"side": "left", "phase": "return"} and q["reps"] == 0
    assert j.feed("tap_missing_ankle", 1)["quest"]["step"]["phase"] == "frame"
    assert j.feed()["quest"]["reps"] == 0
    for count, (side, next_side) in enumerate((("left", "right"), ("right", "right"), ("right", "left")), 1):
        q = j.feed("side_tap", side=side)["quest"]
        assert q["step"] == {"side": side, "phase": "return"}
        assert q["reps"] == count - 1
        q = j.feed()["quest"]
        assert q["step"] == {"side": next_side, "phase": "step"} and q["reps"] == count
    j.feed("side_tap", side="left")
    q = j.feed()["quest"]
    assert q["stage"]["action"] == "high_knee" and q["history"] == [{"action": "side_tap", "reps": 4, "hold": 0}]


@pytest.mark.parametrize("side", ["left", "right"])
def test_side_tap_rehearsal_recognizes_one_completed_step(side):
    now = 0.0
    practice = RecognitionPractice(clock=lambda: now)
    token = practice.begin("exercise.side_tap", ["synthetic"])["token"]
    sequence = (("neutral", 32), ("side_step", 16), ("neutral", 16), ("side_tap", 16), ("neutral", 16))
    for index, (pose, frames) in enumerate(sequence):
        for _ in range(frames):
            practice.observe("synthetic", "test-session", [actor(pose, side)], [], LOCK, now, now)
            now += .05
        assert (practice.status(token)["phase"] == "success") is (index == len(sequence) - 1)
    assert practice.status(token)["event"]["gesture"] == "exercise.side_tap"


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


def test_side_raise_needs_a_held_lateral_lift_and_alternating_returns():
    j = Journey()
    j.game.level = ACTIONS.index("side_raise")
    j.feed(count=30)
    j.feed("side_raise", 3)
    assert j.feed()["quest"]["reps"] == 0
    j.feed("balance", 26)
    assert j.feed()["quest"]["reps"] == 0
    j.feed("side_step", 26)
    assert j.feed()["quest"]["reps"] == 0
    assert j.feed("side_raise")["quest"]["reps"] == 0
    assert j.feed()["quest"]["reps"] == 1
    j.feed("side_raise")
    assert j.feed()["quest"]["reps"] == 1
    j.feed("side_raise", side="right")
    assert j.feed()["quest"]["reps"] == 2


def test_overhead_clap_does_not_count_both_hands_up_or_a_direct_clap_from_below():
    j = Journey()
    j.game.level = ACTIONS.index("overhead_clap")
    j.feed(count=30)
    j.feed("overhead_clap")
    assert j.feed("arms_high_open")["quest"]["reps"] == 0
    j.feed("overhead")
    j.feed("arms_closed")
    assert j.feed("arms_high_open")["quest"]["reps"] == 0
    assert j.feed("overhead_clap")["quest"]["reps"] == 0
    assert j.feed("arms_high_open")["quest"]["reps"] == 1
    assert j.feed("arms_high_open")["quest"]["reps"] == 1


def test_actual_adventure_scene_lifecycle():
    result = subprocess.run(["node", "--test", "tests/quest_scene.test.cjs"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_library_controls_wait_for_current_readback():
    result = subprocess.run(["node", "--test", "tests/games_library_state.test.cjs"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
