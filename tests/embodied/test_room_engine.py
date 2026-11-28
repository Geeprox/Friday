import copy, unittest
import cv2, numpy as np
from homeplane.embodied.room_engine import Room, DEFAULT, validate


class GeometryTests(unittest.TestCase):
    def fixture(self):
        c = copy.deepcopy(DEFAULT)
        c["mode"] = "relative_calibrated"
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
                "ringMCP": q(0.19, 0.54),
                "ringPIP": q(0.22, 0.54),
                "ringTip": q(0.20, 0.52),
                "littlePIP": q(0.21, 0.54),
                "littleTip": q(0.20, 0.52),
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


def test_same_body_wrist_with_flickering_hand_side():
    room, person, hand = GeometryTests().fixture()
    person["joints"]["leftWrist"] = dict(x=.62, y=.52, confidence=.9)
    for i, side in enumerate(["left", "right", "unknown"]):
        hand["side"] = side
        room.update(b"", [person], [hand], i * .12)
    assert hand["gesture"] == "pointing"


def test_linux_fingertips_recover_a_missing_body_wrist_only_for_unique_owner():
    from tests.embodied.test_common_hand_poses import hand as shape
    from homeplane.embodied.portable_vision import body_rows
    from tests.embodied.test_portable_vision import pose
    room, person, h = GeometryTests().fixture()
    h["joints"] = shape("horns")
    points = pose()
    points[16].visibility = 0
    for n, name in ((18,"littleTip"), (20,"indexTip"), (22,"thumbTip")):
        points[n].x, points[n].y = (h["joints"][name][axis] for axis in ("x","y"))
    _, poses = body_rows([points])
    person["joints"] = poses[0]["joints"]
    for i in range(3):
        h["sample_id"] = i
        room.update(b"", [person], [h], .12*i)
    assert (h["person_id"], h["gesture"]) == (1, "horns")
    other = dict(copy.deepcopy(person), id=2)
    room.update(b"", [person, other], [h], .4)
    assert h["person_id"] is None
    for finger in ("IndexTip", "LittleTip", "ThumbTip"):
        person["joints"]["left"+finger] = person["joints"]["right"+finger]
    room.update(b"", [person], [h], .45)
    assert h["person_id"] is None
    for side in ["left", "right"]:
        for finger in ["IndexTip","LittleTip"]:
            person["joints"].pop(side+finger, None)
    room.update(b"", [person], [h], .5)
    assert h["person_id"] is None


def test_rock_lesson_keeps_same_hand_when_other_wrist_leaves_frame():
    from homeplane.embodied.practice import RecognitionPractice
    from tests.embodied.test_common_hand_poses import hand as shape
    room, person, hand = GeometryTests().fixture()
    hand["joints"] = shape("horns")
    person["joints"]["rightWrist"] = hand["joints"]["wrist"]
    clock = [0.]
    practice = RecognitionPractice(clock=lambda: clock[0])
    token = practice.begin("hand.horns", ["test"])["token"]
    phases = []
    for i in range(6):
        clock[0] = i * .2
        if i % 2 == 0:
            person["joints"]["leftWrist"] = dict(x=.2, y=.2, confidence=.95)
        else:
            person["joints"].pop("leftWrist")
        hand["sample_id"] = i
        room.update(b"", [person], [hand], clock[0])
        practice.observe("test", "local", [person], [hand], room.reg_info, clock[0], clock[0])
        phases.append(practice.status(token)["phase"])
    assert phases == ["seen", "seen", "recognizing", "recognizing", "recognizing", "success"]


def test_partial_arm_matches_only_current_single_hand_and_supplies_no_body_pose():
    from homeplane.embodied.stable_tracks import StableTracks
    from tests.embodied.test_common_hand_poses import hand as shape
    room, _, h = GeometryTests().fixture()
    h["joints"] = shape("horns")
    wrist = h["joints"]["wrist"]
    arm = {"rightShoulder":dict(x=.4, y=.45, confidence=.95),
           "rightElbow":dict(x=.45, y=.70, confidence=.95), "rightWrist":wrist}
    box = dict(x=.15, y=.2, w=.75, h=.75, confidence=.95)
    frame = dict(boxes=[box], poses=[dict(joints=arm)])
    tracker = StableTracks()
    tracker.update(frame, 0.)
    person, = tracker.update(frame, .15)
    assert person["joints"] == {} and person["actions"] == ["pose_unknown"]
    for i in range(3):
        h["sample_id"] = i
        room.update(b"", [person], [h], i*.2)
    assert h["person_id"] == person["id"] and h["gesture"] == "horns"
    room.update(b"", [person], [h, copy.deepcopy(h)], .6)
    assert h["person_id"] is None
    h["joints"]["wrist"] = dict(wrist, x=.7)
    room.update(b"", [person], [h], .8)
    assert h["person_id"] is None
    h["joints"]["wrist"] = wrist
    for i, poses in enumerate(([dict(joints={"rightWrist":wrist})], [dict(joints=arm)] * 2,
                               [dict(joints=dict(arm, rightElbow=dict(arm["rightElbow"], confidence=.45)))])):
        person, = tracker.update(dict(boxes=[box], poses=poses), 1 + i*.1)
        room.update(b"", [person], [h], 1 + i*.1)
        assert h["person_id"] is None and not person.get("arm_wrists")


def test_mask_owner_requires_same_tracked_box_and_single_hand_in_stationary_view():
    room, person, hand = GeometryTests().fixture()
    person["joints"] = {}
    hand["mask_owner_box"] = dict(person["box"])
    room.update(b"", [person], [hand], 0)
    assert hand["person_id"] == person["id"]
    for people, hands, status in [([person, dict(person, id=2)], [hand], "locked"),
                                  ([person], [hand, copy.deepcopy(hand)], "locked"),
                                  ([person], [hand], "moving")]:
        room.reg_info = {"status":status}
        room.update(b"", people, hands, .2)
        assert hand["person_id"] is None
    room.reg_info = {"status":"locked"}
    hand["mask_owner_box"]["x"] += .05
    room.update(b"", [person], [hand], .4)
    assert hand["person_id"] is None


def test_sparse_wrist_with_opposite_model_side_stabilizes():
    room, person, hand = GeometryTests().fixture()
    hand["side"] = "left"
    for index in range(5):
        if index % 2:
            person["joints"].pop("leftWrist")
        else:
            person["joints"]["leftWrist"] = dict(x=.62, y=.52, confidence=.9)
        hand["sample_id"] = index
        room.update(b"", [person], [hand], index * .5)
    assert hand["gesture"] == "pointing"


def test_point_target_hold_survives_model_side_changes_and_cached_frames():
    room, person, hand = GeometryTests().fixture()
    person["joints"]["leftWrist"] = dict(x=.62, y=.52, confidence=.9)
    for index in range(43):
        sampled = index % 3 == 0
        hand["sample_id"] = index // 3
        hand["side"] = ("left", "right", "unknown")[hand["sample_id"] % 3]
        result = room.update(b"", [person], [hand], index * .05, hands_sampled=sampled)
        if not sampled:
            assert result["pointing"] == []
    assert result["pointing"][0]["confirmed"] and result["pointing"][0]["target"] == 2


def test_both_arms_without_torso_anchor_supply_wrists_only():
    from homeplane.embodied.stable_tracks import StableTracks
    tracker = StableTracks()
    box = dict(x=.1, y=.1, w=.8, h=.8, confidence=.95)
    arms = {side + part: dict(x=x, y=y, confidence=.95)
            for side, x in (("left", .3), ("right", .7))
            for part, y in (("Shoulder", .35), ("Elbow", .5), ("Wrist", .65))}
    frame = dict(boxes=[box], poses=[dict(joints=arms)])
    tracker.update(frame, 0)
    person, = tracker.update(frame, .15)
    assert person["arm_wrists"] == {k:v for k,v in arms.items() if k.endswith("Wrist")}
    assert person["joints"] == {} and person["actions"] == ["pose_unknown"]
    arms["neck"] = dict(x=.99, y=.99, confidence=.95)
    person, = tracker.update(frame, .30)
    assert not person.get("arm_wrists")


def test_mask_only_hand_lesson_accepts_only_fresh_samples():
    from homeplane.embodied.practice import RecognitionPractice
    from tests.embodied.test_common_hand_poses import hand as shape
    room, person, hand = GeometryTests().fixture()
    person["joints"] = {}
    hand["joints"] = shape("horns")
    now = 0.
    practice = RecognitionPractice(clock=lambda: now)
    token = practice.begin("hand.horns", ["test"])["token"]
    for index in range(24):
        now = index * .05
        sampled = index % 3 == 0
        hand["sample_id"] = index // 3
        if sampled:
            hand["mask_owner_box"] = dict(person["box"])
        else:
            hand.pop("mask_owner_box", None)
        previous = practice.status(token)
        room.update(b"", [person], [hand], now, hands_sampled=sampled)
        practice.observe("test", "local", [person], [hand], room.reg_info, now, now, hands_sampled=sampled)
        if not sampled:
            assert hand["person_id"] is None
            assert practice.status(token)["progress"] == previous["progress"]
    assert practice.status(token)["phase"] == "success"
