import copy, unittest, math, statistics
from homeplane.embodied.motion_games import BodyMotion, MotionGames
from homeplane.embodied.ptz_control import LocalFollower


def human(track=1, x=0.5, seat=1, arms="down"):
    j = {}
    for side, xx in [("left", 0.4), ("right", 0.6)]:
        for name, y in [
            ("Shoulder", 0.3),
            ("Hip", 0.55),
            ("Knee", 0.72),
            ("Ankle", 0.9),
            ("Elbow", 0.45),
            ("Wrist", 0.6),
        ]:
            j[side + name] = {"x": xx, "y": y, "confidence": 0.9}
    if arms == "horizontal":
        j["leftWrist"].update(x=0.1, y=0.3)
        j["rightWrist"].update(x=0.9, y=0.3)
    if arms == "overhead":
        j["leftWrist"]["y"] = 0.1
        j["rightWrist"]["y"] = 0.1
    return {
        "id": track,
        "seat": seat,
        "joints": j,
        "motion_anchor": {"method": "two_visible_ankles", "floor": [x, 0.5]},
        "box": {"x": 0.4, "w": 0.2},
    }


LOCK = {"status": "locked"}


class MotionTests(unittest.TestCase):
    def test_stationary_arms_never_drive(self):
        g = MotionGames()
        g.reset("ski")
        for i in range(120):
            v = g.update([human()], LOCK, i * 0.05)
        self.assertEqual(v["status"], "playing")
        self.assertEqual(v["ski"]["distance"], 0)
        self.assertEqual(v["players"][0]["pumps"], 0)
        self.assertEqual(v["players"][0]["swings"], 0)

    def test_body_translation_without_relative_wrist_motion_is_not_swing(self):
        b = BodyMotion()
        for i in range(60):
            p = human(x=0.5 + i * 0.001)
            for q in p["joints"].values():
                q["x"] += i * 0.002
            v = b.update(p, i * 0.05)
        self.assertEqual(v["swings"], 0)
        self.assertLess(v["lateral"], -0.1)

    def test_seat_change_keeps_same_player_and_origin(self):
        g = MotionGames()
        g.reset("ski")
        for i in range(30):
            v = g.update([human(seat=1 if i < 20 else 8, x=0.5 if i < 20 else 0.56)], LOCK, i * 0.05)
        self.assertEqual(v["status"], "playing")
        self.assertEqual(v["players"][0]["id"], 1)
        self.assertLess(v["players"][0]["lateral"], -0.4)

    def test_ptz_pause_and_single_reacquisition_keep_origin(self):
        g = MotionGames()
        g.reset("ski")
        for i in range(22):
            g.update([human()], LOCK, i * 0.05)
        self.assertEqual(g.update([human()], {"status": "moving"}, 1.2)["status"], "paused")
        for i in range(30):
            v = g.update([human(track=1, x=0.56)], LOCK, 1.3 + i * 0.05)
        self.assertEqual(v["status"], "playing")
        self.assertLess(v["players"][0]["lateral"], -0.4)
        self.assertEqual(v["players"][0]["swings"], 0)

    def test_lost_player_is_not_replaced_by_sole_bystander(self):
        g = MotionGames()
        g.reset("ski")
        for i in range(25):
            g.update([human(1)], LOCK, i * 0.05)
        for i in range(25, 75):
            v = g.update([human(4)], LOCK, i * 0.05)
        self.assertEqual(g.slots, [1])
        self.assertEqual(v["status"], "waiting")
        for i in range(75, 125):
            v = g.update([human(4)], LOCK, i * 0.05, [{"person_id": 4, "gesture": "ok"}])
        self.assertEqual(g.slots, [4])
        self.assertEqual(v["status"], "playing")

    def test_alternating_arms_drive_and_stopping_slows(self):
        g = MotionGames()
        g.reset("ski")
        for i in range(160):
            p = human()
            phase = 1 if (i // 10) % 2 else -1
            p["joints"]["leftWrist"]["y"] = 0.3 + phase * 0.15
            p["joints"]["rightWrist"]["y"] = 0.3 - phase * 0.15
            v = g.update([p], LOCK, i * 0.05)
        self.assertGreater(v["players"][0]["pumps"], 5)
        self.assertGreater(v["ski"]["distance"], 1)
        speed = v["ski"]["speed"]
        for i in range(160, 240):
            v = g.update([human()], LOCK, i * 0.05)
        self.assertLess(v["ski"]["speed"], speed * 0.1)

    def test_pose_hold_tolerates_brief_gap_without_free_progress(self):
        g = MotionGames()
        g.reset("quest")
        for i in range(40):
            g.update([human(arms="horizontal")], LOCK, i * 0.05)
        self.assertEqual(g.level, 0)
        hold = g.hold
        g.update([human()], LOCK, 2.0)
        g.update([human()], LOCK, 2.05)
        self.assertAlmostEqual(g.hold, hold)
        for i in range(12):
            g.update([human(arms="horizontal")], LOCK, 2.1 + i * 0.05)
        self.assertEqual(g.level, 0)
        for i in range(35):
            g.update([human(arms="horizontal")], LOCK, 2.7 + i * 0.05)
        self.assertEqual(g.level, 1)

    def test_tennis_needs_two_and_does_not_advance_when_missing(self):
        g = MotionGames()
        g.reset("tennis")
        for i in range(30):
            v = g.update([human()], LOCK, i * 0.05)
        self.assertEqual(v["status"], "waiting")
        self.assertEqual(g.elapsed, 0)
        for i in range(30):
            v = g.update([human(1, 0.3), human(2, 0.7)], LOCK, 2 + i * 0.05)
        self.assertEqual(v["status"], "playing")
        z = g.ball["z"]
        v = g.update([human()], LOCK, 4)
        self.assertEqual(v["status"], "waiting")
        self.assertEqual(g.ball["z"], z)

    def test_tennis_requires_near_ball_position_and_recent_swing(self):
        players = [{"lateral": 0, "last_swing": -10, "swing_direction": 1} for _ in range(2)]
        for x, z, swing, expect in [
            (0.5, 0.06, -10, 0),
            (0.5, 0.5, 1, 0),
            (0.95, 0.06, 1, 0),
            (0.5, 0.06, 1, 1),
        ]:
            g = MotionGames()
            g.ball.update(x=x, z=z, toward=0)
            players[0]["last_swing"] = swing
            g.tennis(players, 0.01, 1)
            self.assertEqual(g.rally, expect)

    def test_hidden_ankles_pause(self):
        g = MotionGames()
        g.reset("ski")
        for i in range(30):
            g.update([human()], LOCK, i * 0.05)
        p = human()
        p["motion_anchor"] = None
        v = g.update([p], LOCK, 2)
        self.assertEqual(v["status"], "paused")


class FollowTests(unittest.TestCase):
    def state(self, x=0.8):
        return {
            "status": "live",
            "_local_received": 10,
            "people": [{"box": {"x": x, "w": 0.1}}],
            "room": {"registration": LOCK},
        }

    def test_edge_dwell_and_impulse_spacing(self):
        f = LocalFollower()
        s = self.state()
        self.assertIsNone(f.choose(s, 10))
        self.assertEqual(f.choose(s, 10.6), "right")
        f.sent(10.6)
        s["_local_received"] = 12
        self.assertIsNone(f.choose(s, 12))
        self.assertEqual(f.choose(s, 12.7), "right")

    def test_stale_unregistered_and_wide_multiplayer_do_not_move(self):
        f = LocalFollower()
        s = self.state()
        self.assertIsNone(f.choose(s, 12))
        s["_local_received"] = 12
        s["room"]["registration"] = {"status": "unregistered"}
        self.assertIsNone(f.choose(s, 12))
        s["room"]["registration"] = LOCK
        s["people"].append({"box": {"x": 0.05, "w": 0.1}})
        self.assertIsNone(f.choose(s, 13))


class GestureControlTests(unittest.TestCase):
    def feed(self, g, label, start, duration=1.25, owner=1, reg=None):
        for i in range(round(duration / 0.05) + 1):
            v = g.update(
                [human()],
                reg or LOCK,
                start + i * 0.05,
                [{"person_id": owner, "gesture": label}] if label else [],
            )
        return v

    def test_ok_starts_selected_game_without_restarting_held_gesture(self):
        g = MotionGames()
        g.select("ski")
        self.feed(g, "ok", 0, 0.7)
        self.assertEqual(g.mode, "off")
        self.feed(g, "ok", 0.75)
        self.assertEqual(g.mode, "ski")
        self.feed(g, "ok", 2, 4)
        self.assertGreater(g.elapsed, 3)

    def test_yeah_cycles_once_until_released(self):
        g = MotionGames()
        self.feed(g, "yeah", 0, 4)
        self.assertEqual(g.selected_mode, "ski")
        self.assertEqual(g.mode, "off")
        self.feed(g, None, 4.1, 0.7)
        self.feed(g, "yeah", 4.9, 1.2)
        self.assertEqual(g.selected_mode, "tennis")

    def test_unassociated_or_moving_camera_cannot_start(self):
        g = MotionGames()
        self.feed(g, "ok", 0, 2, owner=None)
        self.assertEqual(g.mode, "off")
        self.feed(g, "ok", 3, 2, reg={"status": "moving"})
        self.assertEqual(g.mode, "off")

    def test_short_or_conflicting_gesture_never_switches(self):
        g = MotionGames()
        self.feed(g, "yeah", 0, 0.6)
        self.feed(g, None, 0.65, 0.6)
        self.feed(g, "yeah", 1.3, 0.6)
        self.assertEqual(g.selected_mode, "quest")
        for i in range(30):
            g.update(
                [human()],
                LOCK,
                2 + i * 0.05,
                [{"person_id": 1, "gesture": "yeah"}, {"person_id": 1, "gesture": "ok"}],
            )
        self.assertEqual(g.selected_mode, "quest")
        self.assertEqual(g.mode, "off")


class ContinuousInputTests(unittest.TestCase):
    def test_brief_foot_occlusion_follows_observed_torso_then_expires(self):
        b = BodyMotion()
        for i in range(25):
            p = human()
            p["motion_anchor"]["reference"] = [0.5, 0.9]
            p["motion_torso"] = [0.5, 0.55]
            b.update(p, i * 0.05)
        p = human()
        p["motion_anchor"] = None
        p["motion_torso"] = [0.54, 0.55]
        v = b.update(p, 1.4)
        self.assertTrue(v["located"])
        self.assertEqual(v["foot_observation"], "torso_follow")
        self.assertLess(v["lateral"], -0.1)
        v = b.update(p, 2.3)
        self.assertFalse(v["located"])

    def test_deep_squat_with_foreshortened_torso_counts_after_standing(self):
        g = MotionGames()
        g.reset("quest")
        g.level = 2
        t = 0.0

        def feed(p, n):
            nonlocal t
            for _ in range(n):
                v = g.update([p], LOCK, t)
                t += 0.05
            return v

        feed(human(), 24)
        p = human()
        for side, d in [("left", -1), ("right", 1)]:
            p["joints"][side + "Hip"]["y"] = 0.72
            p["joints"][side + "Shoulder"]["y"] = 0.60
            p["joints"][side + "Knee"].update(x=0.5 + d * 0.22, y=0.78)
        v = feed(p, 8)
        self.assertGreaterEqual(v["players"][0]["squat_depth"], 0.65)
        feed(human(), 8)
        self.assertEqual(g.reps, 1)

    def test_steering_uses_background_axis_even_if_floor_axis_is_rotated(self):
        b = BodyMotion()
        for i in range(20):
            p = human()
            p["motion_anchor"]["reference"] = [0.5, 0.8]
            b.update(p, i * 0.05)
        for i in range(20, 35):
            p = human(x=0.1)
            p["motion_anchor"]["reference"] = [0.56, 0.8]
            v = b.update(p, i * 0.05)
        self.assertLess(v["lateral"], -0.45)

    def exercise(self, hz, paired=True, frames=100, drop=False):
        b = BodyMotion()
        values = []
        for i in range(frames):
            t = i * 0.05
            p = human()
            y = 0.49 + 0.12 * math.sin(t * hz * 2 * math.pi)
            p["joints"]["leftWrist"]["y"] = y
            p["joints"]["rightWrist"]["y"] = y if paired else 0.98 - y
            if drop and i % 13 == 0:
                p["joints"].pop("leftWrist")
            v = b.update(p, t)
            if i > 20:
                values.append(v["arm_effort"])
        return statistics.mean(values), v

    def test_both_same_direction_and_alternating_arms_generate_power(self):
        same, _ = self.exercise(0.9)
        alternating, _ = self.exercise(0.9, False)
        self.assertGreater(same, 0.12)
        self.assertAlmostEqual(same, alternating, delta=0.08)

    def test_faster_swing_generates_more_continuous_power(self):
        slow, _ = self.exercise(0.4)
        fast, _ = self.exercise(1.5)
        self.assertGreater(fast, slow * 1.5)

    def test_fast_return_does_not_cancel_travel_and_brief_dropout_does_not_stop(self):
        power, _ = self.exercise(1.8, drop=True)
        self.assertGreater(power, 0.3)
        b = BodyMotion()
        for i, y in enumerate([0.6, 0.3, 0.6]):
            p = human()
            p["joints"]["leftWrist"]["y"] = y
            p["joints"]["rightWrist"]["y"] = y
            v = b.update(p, i * 0.05)
        self.assertGreater(v["arm_effort"], 0.5)

    def test_mirrored_motion_maps_both_directions_consistently(self):
        for x, sign in [(0.56, -1), (0.44, 1)]:
            b = BodyMotion()
            for i in range(20):
                b.update(human(), i * 0.05)
            for i in range(20, 30):
                v = b.update(human(x=x), i * 0.05)
            self.assertGreater(v["lateral"] * sign, 0.45)

    def test_short_ankle_gap_holds_input_and_long_gap_pauses(self):
        g = MotionGames()
        g.reset("ski")
        for i in range(30):
            g.update([human()], LOCK, i * 0.05)
        p = human()
        p["motion_anchor"] = None
        self.assertEqual(g.update([p], LOCK, 1.5)["status"], "playing")
        self.assertEqual(g.update([p], LOCK, 1.8)["status"], "paused")

    def test_ski_speed_tracks_strength(self):
        results = []
        for effort in [0.15, 0.75]:
            g = MotionGames()
            for i in range(25):
                g.ski({"arm_effort": effort, "lateral": 0}, 0.04, i * 0.04)
            results.append(g.speed)
        self.assertGreater(results[1], results[0] * 3)

    def test_squat_needs_lowering_bend_and_full_return(self):
        g = MotionGames()
        g.reset("quest")
        g.level = 2
        t = 0.0

        def feed(p, n):
            nonlocal t
            for _ in range(n):
                g.update([p], LOCK, t)
                t += 0.05

        feed(human(), 24)
        p = human()
        for side, d in [("left", -1), ("right", 1)]:
            p["joints"][side + "Hip"]["y"] = 0.62
            p["joints"][side + "Shoulder"]["y"] = 0.37
            p["joints"][side + "Knee"].update(x=0.5 + d * 0.17, y=0.75)
        feed(p, 8)
        self.assertEqual(g.reps, 0)
        feed(human(), 8)
        self.assertEqual(g.reps, 1)
        feed(p, 30)
        self.assertEqual(g.reps, 1)
        feed(human(), 8)
        self.assertEqual(g.reps, 2)

    def test_game_does_not_pan_when_whole_player_fits(self):
        f = LocalFollower()
        s = {
            "status": "live",
            "_local_received": 10,
            "people": [{"box": {"x": 0.78, "w": 0.12}}],
            "room": {"registration": LOCK},
            "game": {"status": "playing"},
        }
        self.assertIsNone(f.choose(s, 10))
        self.assertIsNone(f.choose(s, 10.8))
        s["people"][0]["box"]["x"] = 0.94
        s["_local_received"] = 11
        self.assertIsNone(f.choose(s, 11))
        self.assertEqual(f.choose(s, 11.6), "right")


if __name__ == "__main__":
    unittest.main()
