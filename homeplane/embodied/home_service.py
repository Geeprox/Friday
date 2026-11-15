"""Nonblocking home interactions over fresh tracks, attention and private PCM."""

import asyncio
import base64
import hashlib
import json
import time
from collections import deque
from contextlib import nullcontext
from datetime import UTC, datetime

import numpy as np

from homeplane.embodied import bridge
from homeplane.embodied.camera_audio import MAX_AGE as CAMERA_INPUT_MAX_AGE
from homeplane.embodied.camera_audio import (
    AdmittedAudio, WakeCameraCue, WakeCameraPause, offer_voice_window, prepare_voice_packet,
)
from homeplane.embodied.household import basic_command
from homeplane.embodied.intent import address, parse_plan, utterance_focus
from homeplane.embodied.local_intent import household_plan
from homeplane.embodied.paths import DATA, PRIVATE, PYTHON
from homeplane.embodied.smart_home import Attention, Curtain, WindowGesture
from homeplane.embodied.speech import Speaker
from homeplane.embodied.voice_activity import VoiceActivity
from homeplane.embodied.voice_preferences import read_preferences
from homeplane.embodied.voice_process import VoiceProcess
from homeplane.embodied.voice_routing import RETRY_OTHER_MIC, Utterance
from homeplane.embodied.vision_benchmark import LatencyHistogram, WakeMotionCounts
from homeplane.runtime_log import RuntimeLog, observe
from homeplane.home_questions import is_home_question
from homeplane.task_composition import is_task_request

VOICE_PYTHON = str(PYTHON)
VOICE_PAUSED = PRIVATE / "voice-paused"
VOICE_INPUT_PAUSED = PRIVATE / 'voice-input-paused'
WAKE_CAMERA_SECONDS = 16.0
WAKE_VOICED_SECONDS = .128
COMMAND_VOICED_SECONDS = .45
VOICE_PACKET_BACKLOG = 2
CURRENT_ATTENTION = object()
ASR_REFUSALS = {
    'no_speech': 'voice_asr_empty',
    'unsupported_script': 'voice_asr_language_mismatch',
    'no_chinese_task': 'voice_asr_language_mismatch',
    'low_asr_confidence': 'voice_asr_low_confidence',
    'likely_no_speech': 'voice_asr_likely_no_speech',
    'invalid_text': 'voice_asr_rejected',
    'wake_word_required': 'voice_asr_wake_word_missing',
}


def voice_command_outcome(receipt):
    """Readback and acknowledgement are different evidence; neither proves physical action."""
    if receipt.get("status") in {"reported_match", "already_at_target", "observed_at_target"} and receipt.get("reached_target") is True:
        return "readback"
    if receipt.get("status") == "partial":
        return "partial"
    if receipt.get("status") == "unknown_outcome":
        return "unknown_outcome"
    if receipt.get("acknowledged") is True:
        return "accepted"
    return "failed"


class HomeService:
    def __init__(self, camera_id=None, voice_provider=None, *, voice_preferences=None, voice_routing=None,
                 input_allowed=None, voice_decisions=None):
        self.camera_id = camera_id
        self.voice_provider = voice_provider
        self.voice_routing = voice_routing
        self.voice_decisions = voice_decisions if voice_provider is None else voice_provider.voice_decisions
        self.input_allowed = input_allowed or (lambda: True)
        self.voice_preferences=voice_preferences or read_preferences
        self.audio_allowed = True
        self.runtime_log = RuntimeLog(DATA / "logs")
        self.enabled = False
        self.armed = False
        self.curtain = Curtain()
        self.gesture = WindowGesture()
        self.attention = Attention()
        self.voice_process = None if voice_provider else VoiceProcess(
            (VOICE_PYTHON, '-B', '-m', 'homeplane.embodied.voice_worker'))
        self.poller = None
        self.jobs = set()
        self.selected = False
        self.owner = None
        self.voice_owner = None
        self.vad = []
        self.speaking = False
        self.silence = 0.0
        self.duration = 0.0
        self.voiced_duration = self.wake_duration = 0.0
        self.voice_events = {}
        self.voice_segments = self.asr_attempts = self.asr_results = 0
        self.voice_timings = {stage: LatencyHistogram()
                              for stage in ('asr_request', 'voice_segment_to_wake_confirm')}
        self.wake_motion_counts = WakeMotionCounts()
        self.noise = 0.004
        self.last_audio = None
        self.audio_input_at = None
        self.audio_backlog = deque(maxlen=VOICE_PACKET_BACKLOG)
        self.audio_task = None
        self.audio_revision = 0
        self.activity_gate = voice_provider.activity_gate if voice_provider else asyncio.Lock()
        self.audio_retry_at = 0.0
        self.voice_busy = False
        self.last_video = -100.0
        self.camera_focus = None
        self.wake_camera = None
        self.camera_session = None
        self.visible_tracks = set()
        self.activity = None  # Optional model loads only after admitted live audio.
        self.speaker = Speaker()
        self.epoch = 0
        self.utterance_attention = []
        self.utterance_started = self.utterance_ended = self.utterance_level = 0.0
        self.utterance_source = None
        self.reply_until = 0.0
        self.pending_audio = None
        self.state = {
            "enabled": False,
            "voice": "not_started",
            "reason": "后台服务准备中",
            "attention_method": "head_and_pupil_geometry_experimental",
            "precise_eye_gaze": False,
            "last_event": None,
            "last_transcript": None,
            "last_result": None,
            "voice_command": None,
            "laya": "command_gate_with_local_or_codex_intent",
            "arl_scope": "codex_plan_local_allowlisted_actuator",
            "audio_persisted": False,
            "wake_policy": "attention_optional_wake_otherwise_required",
        }

    def public(self):
        self.refresh_voice()
        audio_input = self.state.get('audio_input')
        if audio_input:
            age = time.monotonic()-self.audio_input_at if self.audio_input_at is not None else None
            audio_input = dict(audio_input,age_ms=round(age*1000) if age is not None and age >= 0 else None)
            if audio_input.get('status') == 'ready' and (age is None or not 0 <= age <= 1.5):
                audio_input.update(status='waiting',reason='input_stale')
        return {
            **self.state,
            'audio_input': audio_input,
            'voice_ingress': dict(self.voice_events),
            "enabled": self.enabled and self.armed,
            "owner_track": self.owner,
            "curtain": dict(self.curtain.state),
            "attention": list(self.attention.public),
            "window_gesture": dict(self.gesture.public),
            "speaker": dict(self.speaker.state),
            "diagnostics": self.runtime_log.export(),
        }

    def vision_benchmark_voice(self):
        return dict(voice_packets=self.state.get('audio_packets', 0), voice_segments=self.voice_segments,
                    asr_attempts=self.asr_attempts, asr_results=self.asr_results,
                    wake_confirmed=self.voice_events.get('wake_confirmed', 0))

    def voice_status(self):
        if not self.enabled:
            return 'stopped' if self.state['voice'] == 'stopped' else 'not_started'
        if VOICE_INPUT_PAUSED.exists() and self.enabled:
            return 'paused'
        owner = self.voice_provider or self
        phase = owner.voice_process.status
        return 'transcribing' if self.voice_busy and phase in {'ready', 'transcribing'} else phase

    def ensure_voice(self):
        # The shared ASR owner is independent of its primary camera's admission.
        # A live secondary can recover ASR without arming an inactive primary.
        owner = self.voice_provider or self
        if VOICE_INPUT_PAUSED.exists():
            owner.voice_process.stop()
        else:
            owner.voice_process.ensure_started()

    def refresh_voice(self):
        phase = self.voice_status()
        previous = self.state['voice']
        if phase != previous:
            reason = {'ready': '等待语音指令', 'loading_local_models': '本地语音模型启动中',
                      'unavailable': '语音暂不可用', 'paused': '语音指令已暂停，手势控制仍可用'}
            self.state.update(voice=phase)
            if phase in reason and (phase != 'ready' or previous in {
                    'not_started', 'stopped', 'paused', 'loading_local_models', 'unavailable'}):
                self.state['reason'] = reason[phase]
        owner = self.voice_provider or self
        self.state.update(asr_model=owner.voice_process.metadata.get('asr_model'),
                          intelligence=owner.voice_process.metadata.get('intelligence'))

    async def enable(self):
        self.armed = True
        if not self.enabled and self.voice_provider is None:
            self.poller = asyncio.create_task(self.poll_curtain())
        self.enabled = True
        self.state['enabled'] = True
        self.ensure_voice()
        # Muting Friday's replies must not silently turn off gaze/voice input.
        if VOICE_INPUT_PAUSED.exists():
            self.discard_audio()
            self.pending_audio = None
            self.refresh_voice()
            return
        self.refresh_voice()

    async def poll_curtain(self):
        try:
            await self.curtain.connect()
            while True:
                await asyncio.sleep(12)
                if not self.curtain.busy:
                    await self.curtain.read()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.curtain.state.update(status="unavailable", error_class=type(e).__name__)

    async def request(self, op, **kwargs):
        if self.voice_provider is not None:
            return await self.voice_provider.request(op, **kwargs)
        return await self.voice_process.request(op, **kwargs)

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.jobs.add(t)
        t.add_done_callback(self.jobs.discard)
        return t

    @observe("embodied.command")
    async def command(self, action, event_id, source):
        receipt = await bridge.command("窗帘", action, event_id, camera_id=self.camera_id)
        self.state["last_event"] = {"source": source, "action": action, "receipt": receipt, "at": time.time()}
        return receipt

    async def observe(
        self, people, faces, registration, now, capture, selected, preferred=None, gestures_enabled=False,
        session_id=None, captured_mono=None,
    ):
        session = session_id if type(session_id) is str and 0 < len(session_id) <= 128 else None
        selected = bool(selected and self.enabled and self.armed and session is not None and self.input_allowed())
        continuous = 0 <= time.monotonic() - self.last_video < CAMERA_INPUT_MAX_AGE
        source_changed = session != self.camera_session or not continuous
        if selected != self.selected or source_changed:
            self.epoch += 1
            self.discard_audio("voice_frame_or_source_changed")
            self.pending_audio = None
            self.camera_focus = None
            if self.voice_routing is not None:
                self.voice_routing.discard(self.camera_id)
        if source_changed:
            # 恢复后的首帧不能延续断流前的注视时长。
            self.attention.update([], [], now)
        self.camera_session = session
        self.selected = selected
        self.last_video = now if captured_mono is None else captured_mono
        if selected:
            self.ensure_voice()
        self.refresh_voice()
        good = registration.get("status") not in {"moving", "settling", "lost", "stale"}
        self.attention.update(people if good else [], faces if good else [], now)
        ids = {p["id"] for p in people}
        self.visible_tracks = ids if good else set()
        owner = preferred if preferred in ids else self.attention.candidate() if preferred is None else None
        if owner is None and preferred is None and len(people) == 1:
            owner = people[0]["id"]
        self.owner = owner
        self.voice_owner = self.attention.candidate(preferred) if selected else None
        if not selected or not good:
            self.gesture.reset()
        elif gestures_enabled:
            event = self.gesture.update(next((p for p in people if p["id"] == owner), None), now)
            if event:
                self.spawn(self.command(event["action"], event["id"], "window_gesture"))
        if capture and capture.stdin:
            # 用每张有效识别帧续期。较迟送达的上一帧可能只剩很短的收音窗口。
            gate = bool(
                not VOICE_INPUT_PAUSED.exists() and
                selected and self.audio_allowed and self.voice_status() in ("ready", "transcribing")
                and now > self.reply_until and (
                    self.voice_routing is None or self.voice_routing.listening(now))
            )
            offer_voice_window(capture.stdin, now=time.monotonic(), captured=self.last_video, enabled=gate,
                attention=bool(people and registration.get('status') not in {'moving', 'settling'}))

    def note_voice(self, code, *, refused=False, correlation=None):
        count = self.voice_events.get(code, 0) + 1
        self.voice_events[code] = count
        # 拒绝只记录首次与后续累计节点，防止连续环境声淹没实测记录。
        if not refused or count & (count - 1) == 0:
            self.runtime_log.record("embodied.voice_ingress", "result",
                                    result="refused" if refused else "ok",
                                    code=code, correlation=correlation)

    def reset_audio(self, reason=None):
        """丢弃未完成语音，已提交请求使用自己的来源凭据。"""
        self.audio_revision += 1
        if reason and self.speaking:
            self.note_voice(reason, refused=True)
        self.vad = []
        self.speaking = False
        self.silence = self.duration = self.voiced_duration = self.wake_duration = 0.0
        self.utterance_owner = None
        self.utterance_attention = []
        self.utterance_started = self.utterance_ended = self.utterance_level = 0.0
        self.utterance_source = None
        self.state['voice_activity'] = False
        try:
            getattr(self.activity, 'reset', lambda: None)()
        except Exception:
            self.activity = None
            self.audio_retry_at = time.monotonic()+5
            self.state['audio_analysis'] = dict(status='unavailable',code='local_audio_model_unavailable')

    def discard_audio(self, reason=None):
        self.audio_backlog.clear()
        self.last_audio = self.audio_input_at = None
        self.reset_audio(reason)

    def offer_audio(self, admission):
        """接收循环只提交待处理音频，声音复核由原来源独立接收。"""
        if not isinstance(admission, AdmittedAudio) or admission.packet is None:
            self.audio_backlog.clear()
            prepare_voice_packet(self, admission)
            return
        if len(self.audio_backlog) == VOICE_PACKET_BACKLOG:
            self.discard_audio()
            self.note_voice('voice_audio_backlog', refused=True)
        self.audio_backlog.append((self.epoch, self.voice_owner, admission))
        if self.audio_task is None or self.audio_task.done():
            self.audio_task = asyncio.create_task(self._drain_audio(), name='friday-voice-packets')

    async def _drain_audio(self):
        try:
            while self.audio_backlog:
                epoch, owner, admission = self.audio_backlog.popleft()
                if epoch != self.epoch:
                    continue
                received = admission.received_mono
                if received is None or not 0 <= time.monotonic() - received <= CAMERA_INPUT_MAX_AGE:
                    self.reset_audio()
                    self.note_voice('voice_audio_stale', refused=True)
                    continue
                packet = prepare_voice_packet(self, admission)
                await self.audio(packet, observed_owner=owner)
        finally:
            self.audio_task = None

    async def _measure_audio(self, raw, current):
        await self.activity_gate.acquire()
        def finished(job):
            self.activity_gate.release()
            if not job.cancelled():
                job.exception()

        try:
            if not current():
                self.activity_gate.release()
                return None
            # 推理线程独占循环状态。换源或停用撤销结果，避免并发修改。
            previous, self.activity = self.activity, None

            def measure():
                model = previous if previous is not None else VoiceActivity()
                return model, model.measure(raw)

            job = asyncio.get_running_loop().run_in_executor(None, measure)
        except BaseException:
            self.activity_gate.release()
            raise
        job.add_done_callback(finished)
        # 取消调用方不能让新镜头抢占仍在运行的本机模型会话。
        return await asyncio.shield(job)

    def listening(self, now):
        return (self.selected and self.audio_allowed and self.input_allowed() and not VOICE_INPUT_PAUSED.exists()
                and self.voice_status() in ("ready", "transcribing") and now >= self.reply_until
                and (self.voice_routing is None or self.voice_routing.listening(now))
                and 0 <= now - self.last_video < CAMERA_INPUT_MAX_AGE)

    def finish_gap(self, packet):
        # 只复核同一采集中断前已经收到的语音。分片前后不拼接，
        # 失去新鲜画面、权限或来源时仍然丢弃。
        now = time.monotonic()
        if (self.speaking and self.utterance_source is not None
                and self.utterance_source == (packet.get("capture_session"), packet.get("camera_source_context"))
                and 0 <= now - self.utterance_ended <= CAMERA_INPUT_MAX_AGE
                and self.listening(now)):
            self.finish_audio(now, interrupted=True)

    async def audio(self, item, *, observed_owner=CURRENT_ATTENTION):
        now = time.monotonic()
        if observed_owner is CURRENT_ATTENTION:
            observed_owner = self.voice_owner
        if self.audio_input_at is None and self.last_audio is not None and now - self.last_audio > 0.6:
            self.reset_audio("voice_audio_discontinuity")
        self.last_audio = now
        self.state["audio_packets"] = self.state.get("audio_packets", 0) + 1
        if not self.listening(now):
            self.reset_audio("voice_input_gate_closed")
            return
        raw = base64.b64decode(item["pcm"])
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        duration = len(samples) / 16000.0
        rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
        self.state["audio_rms"] = round(rms, 4)
        if now < self.audio_retry_at:
            self.state['audio_analysis'] = dict(status='unavailable',code='local_audio_model_unavailable',
                                              retry_ms=round((self.audio_retry_at-now)*1000))
            return
        epoch, revision, received = self.epoch, self.audio_revision, self.audio_input_at

        def current():
            stamp = time.monotonic()
            return (self.epoch == epoch and self.audio_revision == revision and self.listening(stamp)
                    and (received is None or 0 <= stamp - received <= CAMERA_INPUT_MAX_AGE))

        model_failed = False
        try:
            measured = await self._measure_audio(raw, current)
        except Exception:
            measured, model_failed = None, True
        if not current():
            if self.epoch == epoch and self.audio_revision == revision:
                if not self.listening(time.monotonic()):
                    self.reset_audio('voice_input_gate_closed')
                else:
                    self.reset_audio()
                    self.note_voice('voice_audio_stale', refused=True)
            return
        if model_failed:
            self.reset_audio()
            self.audio_retry_at = time.monotonic() + 5
            self.state['audio_analysis'] = dict(status='unavailable',code='local_audio_model_unavailable',retry_ms=5000)
            return
        if measured is None:
            self.reset_audio()
            return
        self.activity, activity = measured
        owner = observed_owner if observed_owner == self.voice_owner else None
        speech, ratio, rms = activity.speech, activity.ratio, activity.rms
        self.audio_retry_at = 0.0
        self.state['audio_analysis'] = dict(status='ready',retry_ms=0)
        probability = getattr(self.activity, "probability", 0)
        self.state["speech_ratio"] = round(ratio, 2)
        self.state["speech_probability"] = round(probability, 3)
        self.state["vad_input_gain"] = round(getattr(self.activity, "gain", 1), 1)
        self.state["vad_model"] = "silero_onnx_cpu"
        # Strong neural speech evidence can admit a quiet voice; background noise
        # still needs to pass the independent model and minimum voiced duration.
        speech = speech and rms > (0.00025 if probability >= 0.65 else max(0.001, self.noise * 1.3))
        active = speech or activity.wake_seconds > 0
        self.state["voice_activity"] = active
        sample_at = now if received is None else received
        voiced = duration * ratio
        level = rms * max(voiced if speech else 0.0, activity.wake_seconds)
        if not self.speaking:
            if not active:
                self.noise = 0.98 * self.noise + 0.02 * min(rms, 0.018)
            self.vad = (self.vad + [raw])[-5:]
            if not active:
                return
            self.speaking = True
            self.duration = sum(len(p) for p in self.vad) / 32000.0
            self.silence = 0.0
            self.utterance_owner = owner
            self.utterance_attention = [(owner, duration)] if speech else []
            self.voiced_duration = voiced if speech else 0.0
            self.wake_duration = activity.wake_seconds
            self.utterance_started = sample_at
            self.utterance_ended = min(now, sample_at + duration)
            self.utterance_level = level
            self.utterance_source = (item.get('capture_session'), item.get('camera_source_context'))
            self.state["reason"] = "正在聆听（音频仅在内存）"
            return
        self.vad.append(raw)
        self.duration += duration
        self.silence = 0.0 if active else self.silence + duration
        self.wake_duration += activity.wake_seconds
        if active:
            self.utterance_ended = min(now, sample_at + duration)
            self.utterance_level += level
        if speech:
            self.utterance_attention.append((owner, duration))
            self.voiced_duration += voiced
        if self.duration >= 8 or self.silence >= 0.65:
            self.finish_audio(sample_at)

    def finish_audio(self, now, *, interrupted=False):
        pcm = b"".join(self.vad)
        self.vad = []
        self.speaking = False
        self.duration = 0.0
        voice_seconds = max(self.voiced_duration, self.wake_duration)
        minimum_bytes = 6400 if interrupted else 16000
        if len(pcm) < minimum_bytes or voice_seconds < WAKE_VOICED_SECONDS:
            self.note_voice("voice_audio_discontinuity" if interrupted else "voice_segment_short", refused=True)
            if not self.voice_busy:
                self.state["reason"] = "等待清晰的语音指令"
            return
        self.voice_segments += 1
        owner, attending = utterance_focus(self.utterance_attention)
        source = self.utterance_source
        if source is not None and not all(isinstance(value, str) and value for value in source):
            source = None
        segment = (pcm, owner, self.utterance_ended if interrupted else now, attending, self.epoch, source)
        wake_only = interrupted or self.voiced_duration < COMMAND_VOICED_SECONDS
        if interrupted:
            self.note_voice("voice_wake_after_audio_gap")
        if self.voice_routing is not None:
            if source is None:
                self.note_voice("voice_source_missing", refused=True)
            elif not self.voice_routing.offer(self, Utterance(
                    *segment, self.utterance_started, self.utterance_ended,
                    self.utterance_level / voice_seconds, wake_only=wake_only)):
                self.note_voice("voice_route_rejected", refused=True)
        elif self.voice_busy:
            self.pending_audio = (*segment, wake_only)
        else:
            self.voice_busy = True
            self.spawn(self.transcribe(*segment, wake_only))

    def current(self, captured, epoch):
        now = time.monotonic()
        return bool(
            self.selected
            and self.armed
            and self.input_allowed()
            and epoch == self.epoch
            and 0 <= now - captured < 45
            and 0 <= now - self.last_video < CAMERA_INPUT_MAX_AGE
        )

    def _voice_guard(self, text, captured, epoch, attending, policy, owner=None, *, cue_only=False):
        # Wake and gaze are alternative cues. Both still require the original
        # input source, policy and camera generation to remain current.
        addressed = address(text, attending, wake_word=policy.wake_word)
        explicit_wake = addressed.get('wake_word') is True
        admitted = addressed.get('admitted') is True

        def current():
            # Microphone arbitration gates new samples, not a completed
            # utterance's own lease while another camera collects audio.
            return bool(
                self.voice_preferences() == policy
                and self.current(captured, epoch)
                and not VOICE_INPUT_PAUSED.exists()
                and (explicit_wake if cue_only else admitted and (
                    explicit_wake or attending and owner is not None and owner in self.attention.latest))
            )

        return current

    def _wake_focus(self, text, captured, epoch, policy, source=None):
        if (self.camera_session is None
                or not self._voice_guard(text, captured, epoch, False, policy, cue_only=True)()):
            return False
        if source is not None:
            self.camera_focus = None
            confirmed = time.monotonic()
            self.wake_camera = WakeCameraCue(captured, self.camera_session, source, policy.revision,
                                            confirmed_mono=confirmed)
            self.voice_timings['voice_segment_to_wake_confirm'].record_ms((confirmed - captured) * 1000)
            self.note_voice("wake_confirmed", correlation=self.wake_camera.correlation)
            return True
        # 没有采集凭据时只保留原有的单人构图提示。
        visible = self.visible_tracks
        target = next(iter(visible)) if len(visible) == 1 else None
        self.camera_focus = (dict(track=target, at=time.monotonic(), session_id=self.camera_session)
                             if target is not None else None)
        return self.camera_focus is not None

    def current_wake(self, now):
        cue = self.wake_camera
        if cue is None:
            return None
        began = cue.confirmed_mono if cue.confirmed_mono is not None else cue.captured
        if (began <= now < began + WAKE_CAMERA_SECONDS
                and not VOICE_INPUT_PAUSED.exists() and self.input_allowed()):
            try:
                if self.voice_preferences().revision == cue.voice_revision:
                    return cue
            except (OSError, ValueError):
                pass
        self.wake_camera = None
        return None

    def wake_motion_cue(self, sample, now):
        cue = self.current_wake(now)
        # 短时缺少识别帧不代表采集已换源；真正停止或恢复识别时由采集所有者撤销。
        if cue is None or sample.get('status') != 'live':
            return None
        if sample.get('session_id') != cue.session or sample.get('_wake_source') != cue.source:
            self.wake_camera = None
            return None
        return WakeCameraPause(cue) if self.speaking or self.voice_busy else cue

    async def speak_reply(self, text, current):
        self.reply_until = time.monotonic() + min(30, max(3.5, len(text) / 4.5 + 1.5))
        audible = self.voice_routing is not None and not VOICE_PAUSED.exists()
        with self.voice_routing.replying(self.reply_until) if audible else nullcontext():
            await self.speaker.say(text, current=current)

    async def answer_question(self, text, event, captured, epoch, attending, policy, command, *, owner=None):
        current = self._voice_guard(text, captured, epoch, attending, policy, owner)
        if not current():
            command.update(state='expired',updated_at=time.time())
            self.state['reason']='这次提问已过期，请重新说一次'
            return
        answer = await bridge.question(text,camera_id=self.camera_id,
            request_id=hashlib.sha256(f'voice-query:{self.camera_id}:{event}:{epoch}:{captured}'.encode()).hexdigest(),
            captured_mono=captured,voice_revision=policy.revision,attending=attending)
        if not current() or answer.valid_until <= datetime.now(UTC):
            command.update(state='expired',updated_at=time.time())
            self.state['reason']='这次提问已过期，请重新说一次'
            return
        report = answer.report
        result = report.domain.get('query') if report.domain else None
        completed = report.phase == 'result' and result and result.get('status') != 'refused'
        command.update(route='query',state='answered' if completed else 'clarify',reply=answer.reply,
            receipt={'status':report.code,'read_only':True,'device_authority':False},updated_at=time.time())
        self.state.update(last_result=answer.model_dump(mode='json'),last_route='query',reason=answer.reply)
        await self.speak_reply(answer.reply, lambda: current() and answer.valid_until > datetime.now(UTC))

    async def prepare_task(self,text,event,captured,epoch,attending,policy,command,owner=None):
        current = self._voice_guard(text, captured, epoch, attending, policy, owner)
        if not current():
            command.update(state='expired',updated_at=time.time())
            return
        answer=await bridge.task(text,camera_id=self.camera_id,
            request_id=hashlib.sha256(f'voice-task:{self.camera_id}:{event}:{epoch}:{captured}'.encode()).hexdigest(),
            captured_mono=captured,voice_revision=policy.revision,attending=attending)
        if not current() or answer.valid_until<=datetime.now(UTC):
            if answer.proposal_ref:await bridge.discard_task(answer.proposal_ref)
            command.update(state='expired',updated_at=time.time())
            return
        command.update(route='task',state='awaiting_confirmation' if answer.status=='pending_confirmation' else 'clarify',
            reply=answer.reply,receipt={'status':answer.status,'configured':False,'device_authority':False},updated_at=time.time())
        self.state.update(last_result=answer.model_dump(mode='json'),last_route='task',reason=answer.reply)
        await self.speak_reply(answer.reply, lambda: current() and answer.valid_until > datetime.now(UTC))

    async def transcribe(self, pcm, owner, captured, attending=False, epoch=None, source=None, wake_only=False):
        epoch = self.epoch if epoch is None else epoch
        began = time.monotonic()
        result_kind, result_code = "ok", "none"
        command = None
        self.runtime_log.record("embodied.voice", "start")
        try:
            self.state["voice"] = "transcribing"
            policy=self.voice_preferences()
            audio = base64.b64encode(pcm).decode()
            self.asr_attempts += 1
            asr_started = time.monotonic()
            try:
                result = await self.request("transcribe", pcm=audio, attending=attending,
                                            voice_revision=policy.revision, wake_only=wake_only)
            finally:
                self.voice_timings['asr_request'].record_ms((time.monotonic() - asr_started) * 1000)
            self.asr_results += 1
            if result.get('voice_revision',0)!=policy.revision or self.voice_preferences()!=policy:
                self.state['reason']='唤醒词已更新，请重新说一次'
                return
            self.state["asr_ms"] = result.get("latency_ms")
            self.state["laya_result"] = None
            self.state["asr_quality"] = result.get("asr_quality")
            route = result.get("address", {})
            self.state["last_address"] = route
            refusal = ASR_REFUSALS.get(route.get('reason'))
            if refusal:
                result_kind, result_code = 'refused', refusal
                self.state["reason"] = "等待清晰的语音指令"
                if not VOICE_INPUT_PAUSED.exists() and (
                        self.voice_routing is not None or self.current(captured, epoch)):
                    return RETRY_OTHER_MIC
                return
            if not self.current(captured, epoch) or time.monotonic() - captured > 10:
                result_kind, result_code = "refused", "stale_input"
                self.state["reason"] = "指令已过期或操作者变化"
                if self.voice_routing is not None and not VOICE_INPUT_PAUSED.exists():
                    return RETRY_OTHER_MIC
                return
            if wake_only or not route.get("admitted"):
                if (route.get('reason') == 'waiting_instruction'
                        and address(result.get('text'), False, wake_word=policy.wake_word).get('reason')
                        == 'waiting_instruction'):
                    self.state["reason"] = "请继续说出指令"
                    return self._wake_focus(result['text'], captured, epoch, policy, source)
                if wake_only:
                    result_kind, result_code = "refused", "voice_asr_rejected"
                    self.state["reason"] = "等待清晰的语音指令"
                    return RETRY_OTHER_MIC
                elif route.get("reason") == "need_wake_word_without_attention":
                    result_kind, result_code = "refused", "voice_not_addressed"
                    self.state["reason"] = f"未确认注视，请先说 {policy.wake_word}"
                    return RETRY_OTHER_MIC
                else:
                    self.state["reason"] = "请继续说出指令"
                return
            if result.get("text"):
                self.state["last_transcript"] = result["text"]
            event = "voice:" + hashlib.sha256(pcm).hexdigest()[:24]
            command = {
                "id": event,
                "text": result["text"],
                "state": "processing",
                "started_at": time.time(),
                "updated_at": time.time(),
            }
            self.state["voice_command"] = command
            current = self._voice_guard(result['text'], captured, epoch, attending, policy, owner)
            if not current():
                command.update(state="expired", updated_at=time.time())
                self.state["reason"] = "指令已过期或操作者变化"
                return
            self._wake_focus(result['text'], captured, epoch, policy, source)
            decision_started = time.monotonic()
            addressed = address(result['text'], attending, wake_word=policy.wake_word)
            body = addressed.get('body', '')
            if is_task_request(body):
                await self.prepare_task(result['text'],event,captured,epoch,attending,policy,command,owner=owner)
                return
            if is_home_question(body):
                await self.answer_question(result['text'],event,captured,epoch,attending,policy,command,owner=owner)
                return
            fast = household_plan(result["text"], attending,wake_word=policy.wake_word)
            general = basic_command(result["text"], attending,wake_word=policy.wake_word)
            if not fast and general:
                fast = {
                    "is_command": True,
                    "route": general["route"],
                    "device": general.get("device"),
                    "action": general.get("action"),
                    "reply": general.get("reply", "正在处理家居指令。"),
                }
            if fast:
                task = {
                    "type": "task_result",
                    "provider": "local_rules",
                    "model": None,
                    "simulated": False,
                    "plan": fast,
                    "text": fast["reply"],
                    "execution_scope": "local_allowlisted_actuator",
                    "latency_ms": round((time.monotonic() - decision_started) * 1000, 2),
                }
            else:
                request_id = hashlib.sha256(f'voice-intent:{self.camera_id}:{event}:{epoch}:{captured}'.encode()).hexdigest()
                laya = None
                if self.voice_decisions is not None and source is not None and addressed.get('wake_word') is True:
                    laya = await self.voice_decisions.classify(
                        body, request_id=request_id, current=current, deadline=min(time.monotonic() + 1.6, captured + 10),
                    )
                if current() and laya is None:
                    classified = await self.request('classify', text=result['text'], attending=attending,
                                                    voice_revision=policy.revision)
                    laya = classified.get('laya') or {}
                if not current():
                    command.update(state='expired', updated_at=time.time())
                    self.state['reason'] = '指令已过期或操作者变化'
                    return
                self.state['laya_result'] = laya
                if laya.get("choice") == "闲聊或转述" and laya.get("confidence", 0) >= 0.75:
                    command.update(state="ignored", updated_at=time.time())
                    self.state["reason"] = "闲聊，未触发任务"
                    return
                self.state["reason"] = "正在理解指令"
                task = await self.request(
                    "delegate", text=result["text"], context=dict(route, person_id=owner), laya=laya,
                    request_id=request_id,
                    voice_revision=policy.revision,
                )
                self.state["codex_runtime"] = task.get("runtime")
                if task.get("type") != "task_result" or task.get("simulated") is not False:
                    self.state["last_failure"] = {
                        "stage": "arl_codex",
                        "code": task.get("code") or task.get("error_class"),
                        "at": time.time(),
                    }
                    raise RuntimeError("arl_task_unavailable")
            plan = parse_plan(json.dumps(task.get("plan")))
            if plan["route"] == "curtain" and not any(name in result["text"] for name in ("窗帘", "梦幻帘")):
                plan = {
                    "is_command": True,
                    "route": "clarify",
                    "device": None,
                    "action": None,
                    "reply": "我没有听清要控制哪个设备，请再说一下。",
                }
                task = dict(task, plan=plan, text=plan["reply"])
            if not current():
                command.update(state="expired", updated_at=time.time())
                self.state["reason"] = "任务返回时交互已结束，未执行"
                return
            task["returned_at"] = time.time()
            self.state["last_result"] = task
            self.state["last_route"] = plan["route"]
            self.state["intent_provider"] = task.get("provider")
            self.state["intent_ms"] = task.get("latency_ms")
            command.update(route=plan["route"], state="executing", updated_at=time.time())
            if plan["route"] == "curtain":
                self.state["reason"] = "正在控制窗帘"
                receipt = await self.command(
                    plan["action"],
                    event,
                    "local_voice" if task.get("provider") == "local_rules" else "arl_codex_voice",
                )
                self.state["reason"] = (
                    "窗帘位置已读回"
                    if receipt.get("reached_target")
                    else "窗帘命令已发送，位置后台核对"
                    if receipt.get("acknowledged")
                    else "窗帘执行暂不可用"
                )
            elif plan["route"] == "home":
                receipt = await bridge.command(plan["device"], plan["action"], event, camera_id=self.camera_id)
                self.state["last_event"] = {"source": "voice", "receipt": receipt, "at": time.time()}
                self.state["reason"] = (
                    "已读回目标状态"
                    if receipt.get("reached_target")
                    else "家居指令已发送"
                    if receipt.get("acknowledged")
                    else receipt.get("reply", "设备暂不可用")
                )
            else:
                self.state["reason"] = plan["reply"]
            if plan["route"] in {"curtain", "home"}:
                command.update(
                    state=voice_command_outcome(receipt),
                    receipt={key: receipt.get(key) for key in ("status", "acknowledged", "reached_target")},
                    updated_at=time.time(),
                )
            else:
                command.update(
                    state="clarify" if plan["route"] == "clarify" else "ignored",
                    reply=plan["reply"],
                    updated_at=time.time(),
                )
            if plan["is_command"] or plan["route"] == "clarify":
                spoken = (
                    (self.state["reason"] if plan["route"] == "home" else plan["reply"])
                    if plan["route"] != "curtain"
                    else (
                        "已发送打开窗帘的指令。"
                        if plan["action"] == "open"
                        else "已发送关闭窗帘的指令。"
                    )
                    if receipt.get("acknowledged") or receipt.get("reached_target")
                    else "窗帘暂时没有响应。"
                )
                await self.speak_reply(spoken, current)
        except asyncio.CancelledError:
            if command and command["state"] in {"processing", "executing"}:
                command.update(state="unknown_outcome", updated_at=time.time())
            result_kind, result_code = "cancelled", "cancelled"
            raise
        except Exception as e:
            if command and command["state"] in {"processing", "executing"}:
                command.update(state="unknown_outcome" if command["state"] == "executing" else "failed", updated_at=time.time())
            result_kind, result_code = "unavailable", "runtime_error"
            self.state.update(reason="语音处理暂不可用", error_class=type(e).__name__)
        finally:
            self.runtime_log.record(
                "embodied.voice",
                "result",
                result=result_kind,
                code=result_code,
                elapsed_ms=(time.monotonic() - began) * 1000,
            )
            self.voice_busy = False
            self.refresh_voice()
            pending = self.pending_audio
            self.pending_audio = None
            if (
                pending
                and self.state["voice"] == "ready"
                and time.monotonic() > self.reply_until
                and self.current(pending[2], pending[4])
                and time.monotonic() - pending[2] < 10
            ):
                self.voice_busy = True
                self.spawn(self.transcribe(*pending))

    async def close(self):
        self.selected = False
        self.enabled = self.armed = False
        self.epoch += 1
        self.camera_session = None
        self.camera_focus = None
        self.wake_camera = None
        self.pending_audio = None
        self.discard_audio()
        if self.voice_routing is not None:
            self.voice_routing.discard(self.camera_id)
        if self.voice_process:
            self.voice_process.stop()
        tasks = [t for t in [self.poller, self.audio_task, *self.jobs] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.voice_process:
            await self.voice_process.close()
        await self.curtain.close()
        self.enabled = False
        self.armed = False
        self.state.update(enabled=False, voice="stopped")
