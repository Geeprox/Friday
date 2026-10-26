import tempfile, unittest
from pathlib import Path
from homeplane.embodied.page_recording import PageRecording


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.r = PageRecording(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_only_allowlisted_format_and_no_unarmed_start(self):
        with self.assertRaises(ValueError):
            self.r.start("../file")
        self.r.arm(False)
        with self.assertRaises(ValueError):
            self.r.start("video/webm")

    def test_chunk_owner_limit_and_finalize(self):
        s = self.r.start("video/webm")
        token = s["id"]
        with self.assertRaises(ValueError):
            self.r.append("other", b"x")
        with self.assertRaises(ValueError):
            self.r.append(token, b"x" * (8 * 1024 * 1024 + 1))
        self.r.append(token, b"unit test only")
        self.r.arm(False)
        self.assertEqual(self.r.state["status"], "stopping")
        v = self.r.finish(token)
        self.assertEqual(v["status"], "saved")
        self.assertFalse(v["audio"])
        self.assertFalse(v["armed"])
        self.assertEqual(self.r.path.read_bytes(), b"unit test only")

    def test_second_owner_cannot_restart_active_recording(self):
        s = self.r.start("video/mp4")
        with self.assertRaises(ValueError):
            self.r.start("video/webm")
        self.assertEqual(self.r.state["id"], s["id"])

    def test_no_empty_recording_claims_saved(self):
        s = self.r.start("video/webm")
        with self.assertRaises(ValueError):
            self.r.finish(s["id"])

    def test_saved_recording_survives_restart_without_rearming(self):
        s = self.r.start("video/webm")
        self.r.append(s["id"], b"test only")
        self.r.finish(s["id"])
        restored = PageRecording(self.temp.name)
        self.assertEqual(restored.state["status"], "saved")
        self.assertFalse(restored.state["armed"])
        self.assertEqual(restored.state["id"], s["id"])

    def test_new_recording_clears_previous_completion_times(self):
        s = self.r.start("video/webm")
        self.r.append(s["id"], b"test only")
        self.r.finish(s["id"])
        self.r.arm(True)
        current = self.r.start("video/webm")
        self.assertEqual(current["status"], "recording")
        self.assertNotIn("finished_at", current)
        self.assertNotIn("duration_seconds", current)
