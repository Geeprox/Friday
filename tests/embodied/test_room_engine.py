import copy, unittest
import cv2, numpy as np
from homeplane.embodied.room_engine import Room, DEFAULT, validate


class GeometryTests(unittest.TestCase):
    def fixture(self):
        c = copy.deepcopy(DEFAULT)
        c["dynamic_seats"] = False
        c["anchors"] = [
            {"image": p, "map": p} for p in [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]]
        ]
        c["seats"] = [
            {"id": 1, "image": [0.2, 0.5], "map": [0.2, 0.5]},
            {"id": 2, "image": [0.8, 0.5], "map": [0.8, 0.5]},
            {"id": 3, "image": [0.8, 0.8], "map": [0.8, 0.8]},
        ]
        _, jpg = cv2.imencode(".jpg", np.zeros((360, 640), np.uint8))
        r = Room(c, jpg.tobytes())
        r.registration = np.eye(3)
        r.register = lambda *args: None
        r.reg_info = {"status": "locked"}

        def q(x, y):
            return {"x": x, "y": y, "confidence": 0.9}

        p = {
            "id": 1,
            "box": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.5},
            "joints": {"rightWrist": q(0.2, 0.5), "root": q(0.2, 0.5)},
        }
        h = {
            "side": "right",
            "confidence": 0.9,
            "joints": {
                "wrist": q(0.2, 0.5),
                "indexMCP": q(0.25, 0.5),
                "indexPIP": q(0.28, 0.5),
                "indexTip": q(0.34, 0.5),
                "middleMCP": q(0.20, 0.53),
                "littleMCP": q(0.18, 0.52),
                "middlePIP": q(0.22, 0.52),
                "middleTip": q(0.21, 0.51),
            },
        }
        return r, p, h

    def test_hold_and_loss(self):
        r, p, h = self.fixture()
        for t in np.arange(0, 1.66, 0.15):
            result = r.update(b"", [p], [h], float(t))
            if t < 0.29:
                continue
            self.assertEqual(result["pointing"][0]["target"], 2)
            if t <= 1.2:
                self.assertFalse(result["pointing"][0]["confirmed"])
            if t >= 1.5:
                self.assertTrue(result["pointing"][0]["confirmed"])
        r.update(b"", [p], [], 1.8)
        self.assertEqual(r.update(b"", [p], [h], 1.9)["pointing"][0]["progress"], 0)

    def test_ambiguous_targets_never_confirm(self):
        r, p, h = self.fixture()
        r.config["seats"][2]["image"] = [0.7, 0.52]
        for t in np.arange(0, 2, 0.1):
            result = r.update(b"", [p], [h], float(t))
            if t < 0.21:
                continue
            self.assertTrue(result["pointing"][0]["ambiguous"])
            self.assertFalse(result["pointing"][0]["confirmed"])

    def test_open_hand_is_not_pointing(self):
        r, p, h = self.fixture()
        h["joints"]["middleTip"]["x"] = 0.36
        self.assertEqual(r.update(b"", [p], [h], 0)["pointing"], [])

    def test_no_feet_no_location(self):
        r, p, h = self.fixture()
        r.update(b"", [p], [h], 0)
        self.assertIsNone(p["position"])

    def test_registration_loss_clears_location_and_target(self):
        r, p, h = self.fixture()
        r.registration = None
        result = r.update(b"", [p], [h], 0)
        self.assertIsNone(p["position"])
        self.assertEqual(result["pointing"], [])

    def test_degenerate_calibration_rejected(self):
        r, p, h = self.fixture()
        c = copy.deepcopy(r.config)
        for i, a in enumerate(c["anchors"]):
            a["image"] = [0.1 + i * 0.1, 0.5]
        with self.assertRaises(ValueError):
            validate(c)


if __name__ == "__main__":
    unittest.main()
