import copy, json, os, tempfile, unittest
from pathlib import Path
from homeplane.embodied.ptz_control import CameraLease, SearchPlanner


class FakeCamera:
    def __init__(self):
        self.values = {
            "camera_power": True,
            "cruise": True,
            "human_tracking": True,
            "pet_tracking": True,
            "human_close_up": False,
            "active_preset": 1,
            "presets": [{"idx": 1, "name": "5YWl5Y+j"}, {"idx": 2, "name": "5rKZ5Y+R"}],
        }
        self.writes = []
        self.fail = None

    async def read(self):
        return copy.deepcopy(self.values)

    async def write(self, k, v):
        self.writes.append((k, v))
        if self.fail == (k, v):
            raise RuntimeError("simulated_control_failure")
        self.values[k] = v


class LeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "lease.json"
        self.io = FakeCamera()
        self.lease = CameraLease(self.io, self.path)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_takeover_and_restore_exact_flags(self):
        original = {
            k: self.io.values[k] for k in ("cruise", "human_tracking", "pet_tracking", "human_close_up")
        }
        await self.lease.acquire()
        self.assertTrue(all(self.io.values[k] is False for k in original))
        await self.lease.move(2)
        self.assertEqual(self.io.values["active_preset"], 2)
        self.assertTrue(await self.lease.release())
        self.assertEqual({k: self.io.values[k] for k in original}, original)
        self.assertEqual(self.io.values["active_preset"], 1)
        self.assertEqual(json.loads(self.path.read_text())["status"], "restored")

    async def test_restore_failure_is_durable_and_retryable(self):
        await self.lease.acquire()
        self.io.fail = ("human_tracking", True)
        self.assertFalse(await self.lease.release())
        self.assertEqual(json.loads(self.path.read_text())["status"], "restore_pending")
        self.assertTrue(self.io.values["cruise"])
        self.assertTrue(self.io.values["pet_tracking"])
        self.io.fail = None
        self.assertTrue(await self.lease.release())

    async def test_unknown_presets_never_sent(self):
        await self.lease.acquire()
        count = len(self.io.writes)
        with self.assertRaises(ValueError):
            await self.lease.move(63)
        self.assertEqual(len(self.io.writes), count)

    async def test_live_other_owner_is_not_overwritten(self):
        self.path.write_text(json.dumps({"status": "owned", "pid": os.getppid()}))
        with self.assertRaises(RuntimeError):
            await self.lease.acquire()
        self.assertEqual(self.io.writes, [])

    async def test_prior_manual_baseline_migrates_once(self):
        self.io.values.update(cruise=False, human_tracking=False, pet_tracking=False)
        lease = CameraLease(
            self.io, self.path, {"cruise": True, "human_tracking": True, "pet_tracking": True}
        )
        await lease.acquire()
        await lease.release()
        self.assertTrue(self.io.values["pet_tracking"])
        self.io.values.update(cruise=False, human_tracking=False, pet_tracking=False)
        next_lease = CameraLease(self.io, self.path, {"cruise": True})
        await next_lease.acquire()
        await next_lease.release()
        self.assertFalse(self.io.values["cruise"])


class PreviewRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_preview_is_temporarily_opened_and_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            camera = FakeCamera()
            camera.values["camera_power"] = False
            lease = CameraLease(camera, Path(temp) / "lease.json")
            await lease.acquire()
            self.assertTrue(camera.values["camera_power"])
            await lease.release()
            self.assertFalse(camera.values["camera_power"])

    async def test_manual_close_during_session_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            camera = FakeCamera()
            lease = CameraLease(camera, Path(temp) / "lease.json")
            await lease.acquire()
            camera.values["camera_power"] = False
            self.assertTrue(await lease.release())
            self.assertFalse(camera.values["camera_power"])
            self.assertTrue(lease.snapshot["manual_preview_close_preserved"])


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.p = SearchPlanner([{"idx": 1, "name": "入口"}, {"idx": 2, "name": "沙发"}], 10, 1)
        self.s = {
            "status": "live",
            "people": [],
            "hands": [],
            "room": {"registration": {"status": "locked"}},
            "_local_received": 30,
        }

    def test_empty_view_advances_only_after_dwell(self):
        self.assertIsNone(self.p.choose(self.s, 20))
        self.assertEqual(self.p.choose(self.s, 30), 2)

    def test_stale_video_never_moves(self):
        self.assertIsNone(self.p.choose(self.s, 40))

    def test_player_or_hand_stops_search(self):
        self.s["people"] = [{"box": {"x": 0.2, "w": 0.2}}]
        self.assertIsNone(self.p.choose(self.s, 30))
        self.s["people"][0]["box"]["x"] = 0.01
        self.s["hands"] = [{}]
        self.assertIsNone(self.p.choose(self.s, 31))
        self.assertEqual(self.p.phase, "observing")

    def test_turning_waits_for_stability(self):
        self.s["room"]["registration"]["status"] = "moving"
        self.assertIsNone(self.p.choose(self.s, 30))

    def test_hand_without_body_keeps_the_view(self):
        self.s["hands"] = [{"joints": {str(i): {} for i in range(21)}}]
        self.assertIsNone(self.p.choose(self.s, 30))
        self.assertEqual(self.p.reason, "手部可见，保持识别")

    def test_following_uses_only_a_better_observed_view(self):
        self.s["people"] = [{"box": {"x": 0.01, "w": 0.1}}]
        self.p.views[2] = {"center": [0.1, 0.5]}
        ref = {"center": [0.5, 0.5], "target": [0.1, 0.5]}
        self.assertIsNone(self.p.choose(self.s, 27, ref))
        self.assertEqual(self.p.choose(self.s, 30, ref), 2)


if __name__ == "__main__":
    unittest.main()
