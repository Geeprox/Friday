import asyncio, base64, time, unittest, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import numpy as np
from homeplane.embodied.home_service import HomeService
from homeplane.embodied.intent import transcript_quality
from homeplane.embodied.voice_activity import VoiceActivity
from homeplane.embodied.voice_worker import normalize_audio
from homeplane.embodied.speech import Speaker


class QuietModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_home_keeps_curtain_service_without_starting_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "paused"
            marker.touch()
            h = HomeService()
            h.poll_curtain = AsyncMock()
            with patch("homeplane.embodied.home_service.VOICE_INPUT_PAUSED", marker):
                await h.enable()
            self.assertTrue(h.enabled and h.armed)
            self.assertIsNone(h.worker)
            self.assertEqual(h.state["voice"], "paused")
            await h.close()

    async def test_paused_reply_never_sends_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "paused"
            marker.touch()
            speaker = Speaker()
            speaker.ha.request = Mock()
            with patch("homeplane.embodied.speech.VOICE_PAUSED", marker):
                r = await speaker.say("测试文本")
            self.assertEqual(r["status"], "paused")
            speaker.ha.request.assert_not_called()


class TranscriptTests(unittest.TestCase):
    def test_japanese_silence_and_poor_decodes_do_not_reach_intent(self):
        for text, quality, reason in [
            ("", {}, "no_speech"),
            ("おわり", {}, "unsupported_script"),
            ("好的、こんにちは", {}, "unsupported_script"),
            ("thank you", {}, "no_chinese_task"),
            ("打开窗帘", {"avg_logprob": -1.4}, "low_asr_confidence"),
            ("打开窗帘", {"no_speech_prob": 0.91}, "likely_no_speech"),
        ]:
            self.assertEqual(transcript_quality(text, quality), (False, reason))
        self.assertEqual(
            transcript_quality("Friday，打开客厅窗帘。", {"avg_logprob": -0.3, "no_speech_prob": 0.01}),
            (True, "accepted"),
        )

    def test_quiet_audio_gain_is_bounded_and_never_clips_out_of_range(self):
        samples = np.array([0.0005, -0.0005, 0.8, -0.8], dtype=np.float32)
        normalized, rms, gain = normalize_audio(samples)
        self.assertLessEqual(gain, 24)
        self.assertLessEqual(float(np.max(np.abs(normalized))), 0.980001)
        normalized, rms, gain = normalize_audio(np.full(3200, 0.0005, dtype=np.float32))
        self.assertEqual(gain, 24)
        self.assertAlmostEqual(float(normalized[0]), 0.012, 5)


class AudioIngressTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        h = HomeService()
        h.selected = h.armed = True
        h.state["voice"] = "ready"
        h.last_video = time.monotonic()
        h.voice_owner = 7
        h.activity = SimpleNamespace(measure=lambda raw: (True, 1.0, 0.02))
        h.transcribe = AsyncMock()
        return h

    def packet(self):
        return {"pcm": base64.b64encode(np.full(3200, 650, dtype="<i2").tobytes()).decode()}

    async def end(self, h):
        h.activity.measure = lambda raw: (False, 0.0, 0.001)
        for _ in range(4):
            await h.audio(self.packet())

    async def test_receives_whole_utterance_while_asr_busy_and_bounds_pending(self):
        h = self.service()
        h.voice_busy = True
        h.state["voice"] = "transcribing"
        for _ in range(6):
            await h.audio(self.packet())
        await self.end(h)
        self.assertIsNotNone(h.pending_audio)
        pcm, owner, _, attending, epoch = h.pending_audio
        self.assertEqual(len(pcm), 10 * 6400)
        self.assertEqual(owner, 7)
        self.assertTrue(attending)
        h.transcribe.assert_not_awaited()
        previous = pcm
        h.activity.measure = lambda raw: (True, 1.0, 0.02)
        for _ in range(4):
            await h.audio(self.packet())
        await self.end(h)
        self.assertNotEqual(h.pending_audio[0], previous)
        self.assertLessEqual(len(h.pending_audio[0]), 256000)

    async def test_hiss_does_not_start_and_brief_noise_does_not_transcribe(self):
        import importlib.util
        from homeplane.embodied.paths import MODELS

        if not importlib.util.find_spec('onnxruntime') or not (MODELS / 'silero_vad.onnx').is_file():
            self.skipTest('Optional VAD runtime and operator-installed model required')
        h = self.service()
        h.activity = VoiceActivity()
        rng = np.random.default_rng(7)
        for _ in range(12):
            await h.audio(
                {
                    "pcm": base64.b64encode(
                        (rng.normal(0, 0.003, 3200) * 32767).astype("<i2").tobytes()
                    ).decode()
                }
            )
        self.assertFalse(h.speaking)
        h.transcribe.assert_not_awaited()
        h.activity.measure = lambda raw: (True, 0.3, 0.02)
        for _ in range(4):
            await h.audio(self.packet())
        await self.end(h)
        await asyncio.sleep(0)
        h.transcribe.assert_not_awaited()

    async def test_rejected_decode_never_dispatches_or_replies(self):
        h = self.service()
        h.transcribe = HomeService.transcribe.__get__(h)
        h.request = AsyncMock(
            return_value={
                "text": "",
                "address": {"admitted": False, "reason": "unsupported_script"},
                "asr_quality": {"filter": "unsupported_script"},
            }
        )
        h.command = AsyncMock()
        h.speaker.say = AsyncMock()
        await h.transcribe(b"1234", 7, time.monotonic(), True)
        h.command.assert_not_awaited()
        h.speaker.say.assert_not_awaited()
        self.assertIsNone(h.state["last_transcript"])
        self.assertEqual(h.state["asr_quality"]["filter"], "unsupported_script")

    async def test_unaddressed_background_text_does_not_replace_last_command(self):
        h = self.service()
        h.transcribe = HomeService.transcribe.__get__(h)
        h.state["last_transcript"] = "已确认的指令"
        h.request = AsyncMock(
            return_value={
                "text": "背景中的中文片段",
                "address": {"admitted": False, "reason": "need_wake_word_without_attention"},
            }
        )
        await h.transcribe(b"1234", None, time.monotonic(), False)
        self.assertEqual(h.state["last_transcript"], "已确认的指令")

    async def test_queued_utterance_is_consumed_after_current_decode(self):
        h = self.service()
        h.transcribe = HomeService.transcribe.__get__(h)
        h.worker = SimpleNamespace(returncode=None)
        h.request = AsyncMock(
            return_value={"text": "", "address": {"admitted": False, "reason": "no_speech"}}
        )
        h.pending_audio = (b"next", 7, time.monotonic(), True, h.epoch)
        await h.transcribe(b"first", 7, time.monotonic(), True)
        await asyncio.gather(*list(h.jobs))
        self.assertEqual(h.request.await_count, 2)
        self.assertIsNone(h.pending_audio)


if __name__ == "__main__":
    unittest.main()
