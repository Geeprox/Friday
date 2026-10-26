import copy, unittest
from homeplane.embodied.gestures import classify_hand, GestureStabilizer


class HandTests(unittest.TestCase):
    def hand(self):
        def q(x, y):
            return {"x": x, "y": y, "confidence": 0.95}

        j = {"wrist": q(0.5, 0.85), "thumbTip": q(0.3, 0.5)}
        for name, x, y in [
            ("index", 0.44, 0.60),
            ("middle", 0.51, 0.58),
            ("ring", 0.58, 0.61),
            ("little", 0.65, 0.66),
        ]:
            j[name + "MCP"] = q(x, y)
            j[name + "PIP"] = q(x, y - 0.18)
            j[name + "Tip"] = q(x, y - 0.43)
        return j

    def folded(self, j, name):
        j[name + "Tip"]["y"] = 0.77

    def test_open_hand(self):
        self.assertEqual(classify_hand(self.hand()), "open_palm")

    def test_yeah(self):
        j = self.hand()
        self.folded(j, "ring")
        self.folded(j, "little")
        self.assertEqual(classify_hand(j), "yeah")

    def test_ok(self):
        j = self.hand()
        j["indexTip"] = dict(j["thumbTip"])
        self.assertEqual(classify_hand(j), "ok")

    def test_pointing(self):
        j = self.hand()
        for n in ("middle", "ring", "little"):
            self.folded(j, n)
        self.assertEqual(classify_hand(j), "pointing")

    def test_missing_joints_do_not_claim_a_gesture(self):
        self.assertEqual(classify_hand({"wrist": self.hand()["wrist"]}), "hand_visible")

    def test_temporal_loss(self):
        s = GestureStabilizer()
        w = {"x": 0.4, "y": 0.5}
        self.assertEqual(s.update(1, "ok", w, 0), "hand_visible")
        s.update(1, "ok", w, 0.1)
        self.assertEqual(s.update(1, "ok", w, 0.2), "ok")
        self.assertEqual(s.update(1, "ok", w, 0.8), "hand_visible")

    def test_cached_sample_does_not_count_as_fresh_hand_observations(self):
        s = GestureStabilizer()
        w = {"x": 0.4, "y": 0.5}
        for i in range(10):
            self.assertEqual(s.update(1, "ok", w, i * 0.02, 10), "hand_visible")
        self.assertEqual(s.update(1, "ok", w, 0.2, 11), "hand_visible")
        self.assertEqual(s.update(1, "ok", w, 0.4, 12), "ok")


if __name__ == "__main__":
    unittest.main()
