import unittest

import pytest

from homeplane.embodied.hiit import PLAN, HIITCircuit, signals
from homeplane.embodied.motion_games import BodyMotion, MotionGames
from tests.embodied.test_motion_games import LOCK, human


class CircuitTests(unittest.TestCase):
    def feed(self, c, value, n=4, visible=True):
        key = PLAN[min(c.level, len(PLAN)-1)][0]
        for _ in range(n):
            c.update({"squat_calibrated": True, "exercise_signals": {"visible": visible, key: value}}, 0.05)

    def test_squat_counts_return_once_and_requires_visible_legs(self):
        c = HIITCircuit()
        self.feed(c, 1.0, 20)
        self.assertEqual(c.reps, 0)
        self.feed(c, 0.0, 4, False)
        self.assertEqual(c.reps, 0)
        self.feed(c, 0.0)
        self.assertEqual(c.reps, 1)
        self.feed(c, 0.0, 20)
        self.assertEqual(c.reps, 1)

    def test_long_occlusion_cannot_complete_old_half_rep(self):
        c = HIITCircuit()
        self.feed(c, 1.0)
        self.feed(c, None, 10, False)
        self.feed(c, 0.0)
        self.assertEqual(c.reps, 0)

    def test_rest_does_not_count_pose_and_next_stage_starts_clean(self):
        c = HIITCircuit()
        for _ in range(3):
            self.feed(c, 1.0)
            self.feed(c, 0.0)
        self.assertEqual(c.level, 1)
        self.assertEqual(c.phase, "rest")
        self.feed(c, "open", 50)
        self.assertEqual(c.reps, 0)
        self.assertEqual(c.phase, "rest")
        self.feed(c, "closed", 55)
        self.assertEqual(c.phase, "work")
        self.assertEqual(c.reps, 0)
        self.feed(c, "open")
        self.feed(c, "closed")
        self.assertEqual(c.reps, 1)

    def test_alternating_knees_need_return_and_other_side(self):
        c = HIITCircuit()
        c.level = 2
        self.feed(c, "left")
        self.feed(c, "neutral")
        self.assertEqual(c.reps, 1)
        self.feed(c, "left")
        self.feed(c, "neutral")
        self.assertEqual(c.reps, 1)
        self.feed(c, "right")
        self.feed(c, "neutral")
        self.assertEqual(c.reps, 2)

    def test_completed_twelve_stage_circuit_has_bounded_history(self):
        c = HIITCircuit()
        for level, (key, _, target, _) in enumerate(PLAN):
            self.assertEqual(c.level, level)
            if c.phase == "rest":
                self.feed(c, None, 101)
            for i in range(target):
                value = (
                    1.0
                    if key == "squat"
                    else "open"
                    if key in {"jack", "arm_open"}
                    else "active"
                    if key == "overhead_clap"
                    else ("left" if i % 2 == 0 else "right")
                )
                neutral = 0.0 if key == "squat" else "closed" if key in {"jack", "arm_open"} else "neutral"
                self.feed(c, value, 22 if key == 'balance' else 6)
                self.feed(c, neutral)
        self.assertTrue(c.complete)
        self.assertEqual(c.total_reps, 49)
        self.assertEqual(len(c.history), 12)

    def test_new_side_moves_need_other_side_and_cannot_bridge_occlusion(self):
        for level in (4, 5):
            c=HIITCircuit();c.level=level
            self.feed(c,'left');self.feed(c,'neutral')
            self.assertEqual(c.reps,1)
            self.feed(c,'left');self.feed(c,'neutral')
            self.assertEqual(c.reps,1)
            self.feed(c,'right');self.feed(c,None,10,False);self.feed(c,'neutral')
            self.assertEqual(c.reps,1)
            self.feed(c,'right');self.feed(c,'neutral')
            self.assertEqual(c.reps,2)

    def test_hiit_is_selectable_and_yeah_cycles_only_between_games(self):
        g = MotionGames()
        g.select("hiit")
        g.reset("hiit")
        for i in range(30):
            v = g.update([human()], LOCK, i * 0.05)
        self.assertEqual(v["mode"], "hiit")
        self.assertEqual(v["status"], "playing")
        for i in range(30, 55):
            g.update([human()], LOCK, i * 0.05, [{"person_id": 1, "gesture": "yeah"}])
        self.assertEqual(g.selected_mode, "quest")
        for i in range(55, 100):
            g.update([human()], LOCK, i * 0.05)
        for i in range(100, 125):
            g.update([human()], LOCK, i * 0.05, [{"person_id": 1, "gesture": "yeah"}])
        self.assertEqual(g.selected_mode, "ski")

    def test_single_player_binds_ok_owner_when_bystander_is_visible(self):
        g = MotionGames()
        g.select("hiit")
        for i in range(50):
            v = g.update([human(1, 0.3), human(2, 0.7)], LOCK, i * 0.05, [{"person_id": 2, "gesture": "ok"}])
        self.assertEqual(v["mode"], "hiit")
        self.assertEqual(g.slots, [2])
        self.assertEqual(v["status"], "playing")
        self.assertEqual(v["players"][0]["id"], 2)


class SignalTests(unittest.TestCase):
    def test_side_bend_needs_straight_visible_legs_and_torso_then_return(self):
        p=human();j=p['joints']
        for side in ('left','right'):
            j[side+'Shoulder']['x']-=.09
        self.assertEqual(signals(j,{'overhead':0},0,.35,180)['side_bend'],'left')
        for side in ('left','right'):
            j[side+'Shoulder']['x']+=.09
        self.assertEqual(signals(j,{'overhead':0},0,.35,180)['side_bend'],'neutral')
        j['leftKnee']['x']-=.16
        self.assertIsNone(signals(j,{'overhead':0},0,.35,180)['side_bend'])
        j.pop('rightAnkle')
        self.assertFalse(signals(j,{'overhead':0},0,.35,180)['visible'])

    def test_side_step_requires_calibrated_fresh_feet_not_torso_follow(self):
        reader=BodyMotion()
        for i in range(20):result=reader.update(human(),i*.05)
        self.assertEqual(result['exercise_signals']['side_step'],'neutral')
        for i in range(20,34):result=reader.update(human(x=.58),i*.05)
        self.assertEqual(result['exercise_signals']['side_step'],'left')
        p=human(x=.58);p.pop('motion_anchor',None)
        result=reader.update(p,1.7)
        self.assertIsNone(result['exercise_signals']['side_step'])

    def test_jack_needs_both_feet_open_and_both_arms_raised(self):
        p = human(arms="overhead")
        p["joints"]["leftAnkle"]["x"] = 0.2
        p["joints"]["rightAnkle"]["x"] = 0.8
        s = signals(p["joints"], {"overhead": 1}, 0.0, 0.35, 180)
        self.assertEqual(s["jack"], "open")
        self.assertIsNone(signals(p["joints"], {"overhead": 0}, 0.0, 0.35, 180)["jack"])

    def test_knee_lift_distinguishes_sides_and_missing_leg_is_unknown(self):
        p = human()
        p["joints"]["leftKnee"]["y"] = 0.55
        s = signals(p["joints"], {"overhead": 0}, 0.0, 0.35, 180)
        self.assertEqual(s["high_knee"], "left")
        p["joints"].pop("rightAnkle")
        self.assertFalse(signals(p["joints"], {"overhead": 0}, 0.0, 0.35, 180)["visible"])


def new_action_pose(action, side='left', *, wrong_hand=False):
    person = human()
    joints = person['joints']
    opposite = 'right' if side == 'left' else 'left'
    if action == 'balance':
        joints[side+'Knee']['y'] = .67
        joints[side+'Ankle']['y'] = .82
    else:
        joints[side+'Knee']['y'] = .57
        joints[side+'Ankle']['y'] = .75
        wrist = side if wrong_hand else opposite
        joints[wrist+'Wrist'].update(x=joints[side+'Knee']['x'], y=.58)
    return person


@pytest.mark.parametrize('mode', ['quest', 'hiit'])
@pytest.mark.parametrize('action', ['balance', 'cross_knee'])
def test_new_actions_use_live_joints_dwell_alternation_and_neutral_return(mode, action):
    game = MotionGames()
    game.reset(mode, difficulty='easy')
    if mode == 'quest':
        game.level = next(i for i, row in enumerate(game.quest_stages) if row.action == action)
    else:
        game.circuit.level = next(i for i, row in enumerate(game.circuit.plan) if row[0] == action)
    start_level = game.level if mode == 'quest' else game.circuit.level
    tick = 0

    def feed(person, frames):
        nonlocal tick
        for _ in range(frames):
            tick += 1
            state = game.update([person], LOCK, tick*.05)
        return state[mode]

    feed(human(), 28)
    if action == 'balance':
        feed(new_action_pose(action), 9)
    else:
        feed(new_action_pose(action, wrong_hand=True), 9)
    assert feed(human(), 5)['reps'] == 0

    active_frames = 25 if action == 'balance' else 6
    feed(new_action_pose(action), active_frames)
    assert feed(human(), 5)['reps'] == 1
    feed(new_action_pose(action), active_frames)
    assert feed(human(), 5)['reps'] == 1
    other = new_action_pose(action, 'right')
    feed(other, active_frames)
    tick += 1
    assert game.update([], LOCK, tick*.05)['status'] == 'waiting'
    assert feed(human(), 5)['reps'] == 1
    feed(other, active_frames)
    game.set_paused(True)
    game.set_paused(False)
    assert feed(human(), 28)['reps'] == 1
    assert feed(other, active_frames)['level'] == start_level
    result = feed(human(), 5)
    assert result['level'] == start_level + 1 and result['history'][-1]['action'] == action
