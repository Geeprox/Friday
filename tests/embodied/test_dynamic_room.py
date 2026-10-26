import unittest, copy
import test_room_engine
import cv2, numpy as np


class DynamicTests(unittest.TestCase):
    def test_game_anchor_survives_sofa_overlap_and_moderate_ankle_confidence(self):
        r, p, h = test_room_engine.GeometryTests().fixture()
        for side, x, y in [("left", 0.2, 0.75), ("right", 0.3, 0.9)]:
            p["joints"][side + "Ankle"] = {"x": x, "y": y, "confidence": 0.45}
        p["joints"]["root"] = {"x": 0.3, "y": 0.5, "confidence": 0.9}
        furniture = [
            {
                "class": "soft_furniture",
                "name": "soft",
                "confidence": 0.8,
                "box": {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.7},
                "partial": False,
            }
        ]
        r.update(b"", [p], [], 0, objects=furniture)
        self.assertIsNotNone(p["motion_anchor"])
        self.assertEqual(p["motion_anchor"]["method"], "two_visible_ankles")

    def test_game_anchor_does_not_require_an_in_bounds_approximate_map(self):
        r, p, h = test_room_engine.GeometryTests().fixture()
        r.floor[0, 2] = 0.9
        for side, x in [("left", 0.2), ("right", 0.3)]:
            p["joints"][side + "Ankle"] = {"x": x, "y": 0.8, "confidence": 0.9}
        r.update(b"", [p], [], 0)
        self.assertIsNotNone(p["motion_anchor"])
        self.assertIsNone(p["position"])
        p["joints"].pop("leftAnkle")
        r.update(b"", [p], [], 0.1)
        self.assertIsNone(p["motion_anchor"])

    def test_stable_player_slot_and_reacquisition(self):
        r, p, h = test_room_engine.GeometryTests().fixture()
        r.config["dynamic_seats"] = True
        for t in (0, 0.15, 0.3, 0.45, 0.6):
            d = r.update(b"", [p], [], t)
        self.assertEqual(len(d["seats"]), 1)
        seat = d["seats"][0]["id"]
        p["id"] = 99
        d = r.update(b"", [p], [], 0.75)
        self.assertEqual(p["seat"], seat)

    def test_seated_occlusion_is_only_an_estimate(self):
        r, p, h = test_room_engine.GeometryTests().fixture()
        p["joints"]["root"] = {"x": 0.3, "y": 0.5, "confidence": 0.9}
        furniture = [
            {
                "class": "soft_furniture",
                "name": "soft",
                "confidence": 0.8,
                "box": {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.7},
                "partial": False,
            }
        ]
        d = r.update(b"", [p], [], 0, objects=furniture)
        self.assertEqual(p["position"]["method"], "sofa_support_estimate")
        self.assertEqual(p["position"]["quality"], "estimated")
        self.assertEqual(d["observed_furniture"][0]["source"], "detector_with_living_room_prior")

    def test_real_background_shift_suppresses_then_recovers_projection(self):
        r, p, h = test_room_engine.GeometryTests().fixture()
        rng = np.random.default_rng(11)
        image = rng.integers(0, 255, (360, 640), dtype=np.uint8)
        image = cv2.GaussianBlur(image, (3, 3), 0)
        _, jpg = cv2.imencode(".jpg", image)
        from homeplane.embodied.room_engine import Room

        r = Room(r.config, jpg.tobytes())
        r.register(jpg.tobytes(), [], 10)
        self.assertEqual(r.reg_info["status"], "locked")
        moved = cv2.warpAffine(image, np.float32([[1, 0, 12], [0, 1, 0]]), (640, 360))
        _, shift = cv2.imencode(".jpg", moved)
        r.register(shift.tobytes(), [], 10.4)
        self.assertEqual(r.reg_info["status"], "moving")
        self.assertIsNone(r.registration)
        r.register(shift.tobytes(), [], 10.8)
        self.assertEqual(r.reg_info["status"], "settling")
        r.register(shift.tobytes(), [], 11.3)
        self.assertEqual(r.reg_info["status"], "locked")
        self.assertIsNotNone(r.registration)


if __name__ == "__main__":
    unittest.main()
