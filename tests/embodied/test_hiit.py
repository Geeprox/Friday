import copy, unittest
from homeplane.embodied.hiit import HIITCircuit, signals, PLAN
from homeplane.embodied.motion_games import MotionGames, BodyMotion
from test_motion_games import human, LOCK


class CircuitTests(unittest.TestCase):
    def feed(self, c, value, n=4, visible=True):
        key = PLAN[min(c.level, 3)][0]
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

    def test_completed_four_stage_circuit_has_bounded_history(self):
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
                    if key == "jack"
                    else ("left" if i % 2 == 0 else "right")
                )
                neutral = 0.0 if key == "squat" else "closed" if key == "jack" else "neutral"
                self.feed(c, value)
                self.feed(c, neutral)
        self.assertTrue(c.complete)
        self.assertEqual(c.total_reps, 17)
        self.assertEqual(len(c.history), 4)

    def test_hiit_is_selectable_and_yeah_reaches_home_then_pose(self):
        g = MotionGames()
        g.select("hiit")
        g.reset("hiit")
        for i in range(30):
            v = g.update([human()], LOCK, i * 0.05)
        self.assertEqual(v["mode"], "hiit")
        self.assertEqual(v["status"], "playing")
        for i in range(30, 55):
            g.update([human()], LOCK, i * 0.05, [{"person_id": 1, "gesture": "yeah"}])
        self.assertEqual(g.selected_mode, "home")
        for i in range(55, 100):
            g.update([human()], LOCK, i * 0.05)
        for i in range(100, 125):
            g.update([human()], LOCK, i * 0.05, [{"person_id": 1, "gesture": "yeah"}])
        self.assertEqual(g.selected_mode, "quest")

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
