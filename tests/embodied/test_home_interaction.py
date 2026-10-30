import copy, unittest, asyncio
from unittest.mock import AsyncMock, patch
import numpy as np
import pytest
from types import SimpleNamespace
from homeplane.embodied.smart_home import WindowGesture, Attention, Curtain, CURTAIN_DID
from homeplane.embodied.intent import address, parse_plan, utterance_focus
from homeplane.embodied.stable_tracks import StableTracks
from test_motion_games import human


class IntentTests(unittest.TestCase):
    def test_attention_or_explicit_wake_word_only_controls_admission(self):
        for text in ["帮我打开客厅窗帘", "别关窗帘", "帮我查一下今天股市怎么样"]:
            self.assertFalse(address(text, False)["admitted"])
            self.assertTrue(address(text, True)["admitted"])
            self.assertTrue(address("Friday，" + text, False)["admitted"])
        self.assertFalse(address("我刚说Friday打开窗帘", False)["admitted"])
        self.assertFalse(address("Friday，", False)["admitted"])
        self.assertTrue(address("Friday帮我打开窗帘", False)["admitted"])
        self.assertFalse(address("Fridayish 打开窗帘", False)["admitted"])

    def test_negation_is_admitted_and_left_for_codex(self):
        self.assertEqual(address("Friday，别关窗帘")["body"], "别关窗帘")

    def test_brief_turn_toward_camera_can_qualify_but_glance_or_person_swap_cannot(self):
        self.assertEqual(utterance_focus([(None, 0.2), (7, 0.8)]), (7, True))
        self.assertEqual(utterance_focus([(None, 0.7), (7, 0.3)]), (None, False))
        self.assertEqual(utterance_focus([(7, 0.9), (8, 0.1)]), (None, False))
        self.assertEqual(utterance_focus([(7, 0.2)]), (None, False))

    def test_real_codex_noop_empty_reply_is_acknowledged_without_action(self):
        p = parse_plan('{"is_command":true,"route":"no_op","device":null,"action":null,"reply":""}')
        self.assertIsNone(p["action"])
        self.assertEqual(p["route"], "no_op")
        self.assertTrue(p["reply"])

    def test_unexpected_device_action_is_rejected(self):
        import json

        valid = {
            "is_command": True,
            "route": "curtain",
            "device": "selected_cover",
            "action": "open",
            "reply": "正在打开客厅梦幻帘",
        }
        self.assertEqual(parse_plan(json.dumps(valid))["action"], "open")
        for change in [{"device": "door"}, {"action": "toggle"}, {"is_command": False}, {"route": "no_op"}]:
            with self.assertRaises(ValueError):
                parse_plan(json.dumps(dict(valid, **change)))


class WindowTests(unittest.TestCase):
    def pose(self, spread=1.0):
        p = human()
        p["joints"]["leftWrist"].update(x=0.5 - 0.1 * spread, y=0.32)
        p["joints"]["rightWrist"].update(x=0.5 + 0.1 * spread, y=0.32)
        return p

    def test_horizontal_paired_open_then_close_triggers_once_each(self):
        w = WindowGesture()
        events = []
        for i in range(20):
            if e := w.update(self.pose(0.6), i * 0.05):
                events.append(e)
        for i in range(20, 110):
            if e := w.update(self.pose(3), i * 0.05):
                events.append(e)
        for i in range(110, 130):
            if e := w.update(self.pose(0.6), i * 0.05):
                events.append(e)
        self.assertEqual([e["action"] for e in events], ["open", "close"])

    def test_fast_open_and_close_without_long_endpoint_holds(self):
        w = WindowGesture()
        for t in [0, 0.06, 0.12, 0.20, 0.26]:
            w.update(self.pose(0.6), t)
        self.assertIsNone(w.update(self.pose(3), 0.4))
        self.assertEqual(w.update(self.pose(3), 0.47)["action"], "open")
        for t in [0.6, 0.72, 0.84]:
            self.assertIsNone(w.update(self.pose(2), t))
        self.assertIsNone(w.update(self.pose(0.6), 0.9))
        self.assertIsNone(w.update(self.pose(0.6), 0.98))
        self.assertEqual(w.update(self.pose(0.6), 1.06)["action"], "close")

    def test_static_open_pose_and_raised_arms_do_not_trigger(self):
        w = WindowGesture()
        for i in range(35):
            self.assertIsNone(w.update(self.pose(3), i * 0.05))
        for i in range(35, 65):
            p = self.pose(0.6)
            p["joints"]["leftWrist"]["y"] = 0.05
            p["joints"]["rightWrist"]["y"] = 0.05
            self.assertIsNone(w.update(p, i * 0.05))

    def test_gap_or_owner_switch_cannot_finish_old_gesture(self):
        w = WindowGesture()
        for i in range(20):
            w.update(self.pose(0.6), i * 0.05)
        self.assertIsNone(w.update(self.pose(3), 3))
        for i in range(20):
            p = self.pose(0.6)
            p["id"] = 2
            self.assertIsNone(w.update(p, 3.1 + i * 0.05))


class AttentionTests(unittest.TestCase):
    def face(self):
        return {
            "box": {"x": 0.3, "y": 0.05, "w": 0.2, "h": 0.2},
            "confidence": 0.95,
            "facing_candidate": True,
            "eyes_clear": True,
            "gaze_candidate": True,
            "sample_age_ms": 0,
            "face_pixels": 140,
        }

    def test_unique_stable_head_facing_candidate_and_ambiguity(self):
        a = Attention()
        p = human()
        p["joints"]["nose"] = {"x": 0.4, "y": 0.15, "confidence": 0.9}
        a.update([p], [self.face()], 0)
        self.assertIsNone(a.candidate())
        a.update([p], [self.face()], 0.5)
        self.assertEqual(a.candidate(), 1)
        other = copy.deepcopy(p)
        other["id"] = 2
        a.update([p, other], [self.face()], 0.6)
        a.update([p, other], [self.face()], 1.2)
        self.assertIsNone(a.candidate())
        f = self.face()
        f["sample_age_ms"] = 500
        a.update([p], [f], 1.3)
        self.assertIsNone(a.candidate())

    def test_head_pose_alone_or_eyes_away_never_qualify_for_wakeless_command(self):
        a = Attention()
        p = human()
        p["joints"]["nose"] = {"x": 0.4, "y": 0.15, "confidence": 0.9}
        for change in ({"eyes_clear": False}, {"gaze_candidate": False}):
            f = dict(self.face(), **change)
            a.update([p], [f], 0)
            a.update([p], [f], 1)
            self.assertIsNone(a.candidate())
            self.assertFalse(a.public[0]["qualified"])
            self.assertTrue(a.public[0]["head_facing"])


class TrackTests(unittest.TestCase):
    def detections(self, x=0.2):
        return {"boxes": [{"x": x, "y": 0.2, "w": 0.15, "h": 0.6, "confidence": 0.9}], "poses": []}

    def test_id_survives_temporary_occlusion_and_camera_pan(self):
        t = StableTracks()
        t.update(self.detections(), 0)
        p = t.update(self.detections(), 0.05)
        track = p[0]["id"]
        self.assertGreater(track, 0)
        t.update({"boxes": []}, 0.5)
        self.assertEqual(t.update(self.detections(), 2)[0]["id"], track)
        self.assertEqual(t.update(self.detections(0.5), 2.1, camera_moving=True), [])
        h = np.array([[1, 0, -0.3], [0, 1, 0], [0, 0, 1]], dtype=float)
        self.assertEqual(t.update(self.detections(0.5), 2.5, transform=h), [])
        p = t.update(self.detections(0.5), 2.55, transform=h)
        self.assertNotEqual(p[0]["id"], track)

    def test_uncalibrated_camera_tracks_in_image_space_without_reusing_ids(self):
        t = StableTracks()
        t.update(self.detections(), 0, transform=None)
        image = t.update(self.detections(), .05, transform=None)[0]
        self.assertEqual(image["tracking"]["space"], "image")
        self.assertEqual(t.update(self.detections(), .1), [])
        mapped = t.update(self.detections(), .15)[0]
        self.assertNotEqual(mapped["id"], image["id"])

    def test_unconfirmed_track_is_never_zero_or_negative(self):
        self.assertEqual(StableTracks().update(self.detections(), 0), [])


class FakeAPI:
    def __init__(self):
        self.pos = 50
        self.writes = []

    async def get_props_async(self, params):
        return [
            dict(did=CURTAIN_DID, siid=2, piid=p.piid, value=0 if p.piid == 1 else self.pos, code=0)
            for p in params
        ]

    async def set_prop_async(self, p):
        self.writes.append((p.did, p.siid, p.piid, p.value))
        self.pos = 100 if p.value == 2 else 0
        return {"code": 0}


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    def curtain(self):
        c = Curtain()
        c.api = FakeAPI()
        c.get_parameter = c.set_parameter = SimpleNamespace
        c.binding = {'refs': {'current-position': (2, 6), 'target-position': (2, 7),
                             'fault': (2, 1), 'motor-control': (2, 2)},
                     'commands': {'open': 2, 'close': 1, 'stop': 0}}
        return c

    async def test_exact_device_allowlist_readback_and_event_deduplication(self):
        c = self.curtain()
        with patch("homeplane.embodied.smart_home.asyncio.sleep", new=AsyncMock()):
            r = await c.act("open", "test-one")
            await c.monitor
        self.assertTrue(r["acknowledged"])
        self.assertTrue(r["position_changed"])
        self.assertTrue(r["reached_target"])
        self.assertEqual(c.api.writes, [(CURTAIN_DID, 2, 2, 2)])
        self.assertEqual((await c.act("open", "test-one"))["status"], "duplicate")
        with self.assertRaises(ValueError):
            await c.act("other_device", "test-two")

    async def test_next_opposite_command_does_not_wait_for_position_readback(self):
        c = self.curtain()
        a = await c.act("open", "fast-open")
        b = await c.act("close", "fast-close")
        self.assertTrue(a["acknowledged"])
        self.assertTrue(b["acknowledged"])
        self.assertEqual([x[-1] for x in c.api.writes], [2, 1])
        c.monitor.cancel()
        await asyncio.gather(c.monitor, return_exceptions=True)

    async def test_already_open_does_not_write_again(self):
        c = self.curtain()
        c.api.pos = 100
        r = await c.act("open", "test")
        self.assertEqual(r["status"], "already_at_target")
        self.assertFalse(c.api.writes)


if __name__ == "__main__":
    unittest.main()

@pytest.mark.asyncio
async def test_unreliable_angle_feedback_never_suppresses_command_or_claims_target():
    c=DeviceTests().curtain();c.api.pos=100;c.binding['feedback_quality']='unreliable'
    result=await c.act('open','explicit-angle-command')
    assert c.api.writes and result['acknowledged']
    assert not result['reached_target'] and result['status']=='angle_unconfirmed'
    assert c.monitor is None
