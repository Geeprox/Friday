"""Nonblocking home interactions over fresh tracks, attention and private PCM."""

import asyncio
import base64
import hashlib
import json
import time
from collections import deque
from contextlib import nullcontext
from dataclasses import replace
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
from homeplane.embodied.pointing_voice import (
    POINTING_FOLLOWUP_SECONDS, POINTING_HOLD_SECONDS, PointingReply, PointingSpeech, pointing_command,
)
from homeplane.embodied.smart_home import Attention, Curtain, WindowGesture
from homeplane.embodied.speech import Speaker
from homeplane.embodied.voice_activity import VoiceActivity
from homeplane.embodied.voice_preferences import read_preferences
from homeplane.embodied.voice_process import VoiceProcess
from homeplane.embodied.voice_routing import RETRY_OTHER_MIC, RetryWithOwnAttention, RetryWithOwnPointing, Utterance
from homeplane.runtime_log import RuntimeLog, observe
from homeplane.home_questions import is_home_question
from homeplane.task_composition import is_task_request

VOICE_PYTHON = str(PYTHON)
VOICE_PAUSED = PRIVATE / "voice-paused"
VOICE_INPUT_PAUSED = PRIVATE / 'voice-input-paused'
WAKE_CAMERA_SECONDS = 16.0
WAKE_VOICED_SECONDS = .25
COMMAND_VOICED_SECONDS = .45
VOICE_PACKET_BACKLOG = 2
CURRENT_ATTENTION = object()


def _speech_body(body):
    """忽略转写补上的句尾标点，保留动作、目标和否定词。"""
    return body.rstrip(" \t\n\r。！？.!?") if isinstance(body, str) else ""


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
                 input_allowed=None, pointing_frame=None):
        self.camera_id = camera_id
        self.voice_provider = voice_provider
        self.voice_routing = voice_routing
        self.input_allowed = input_allowed or (lambda: True)
        self.pointing_frame = pointing_frame or (lambda session, now: None)
        self.pointing_reply = None
        self.pointing_request = None
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
        self.gate_sent = -100.0
        self.gate_audio_until = 0.0
        self.vad = []
        self.speaking = False
        self.silence = 0.0
        self.duration = 0.0
        self.voiced_duration = 0.0
        self.noise = 0.004
        self.last_audio = None
        self.audio_input_at = None
        self.audio_backlog = deque(maxlen=VOICE_PACKET_BACKLOG)
        self.audio_task = None
        self.audio_revision = 0
        self.activity_gate = voice_provider.activity_gate if voice_provider else asyncio.Lock()
        self.audio_retry_at = 0.0
        self.voice_busy = False
        self.voice_needs_stable_view = False
        self.last_video = -100.0
        self.camera_focus = None
        self.wake_camera = None
        self.camera_session = None
        self.visible_tracks = set()
        self.activity = None  # Optional model loads only after admitted live audio.
        self.speaker = Speaker()
        self.epoch = 0
        self.view_revision = 0
        self.utterance_view_revision = 0
        self.utterance_attention = []
        self.utterance_pointing = None
        self.utterance_pointing_conflict = False
        self.utterance_started = self.utterance_ended = self.utterance_level = 0.0
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
            "enabled": self.enabled and self.armed,
            "owner_track": self.owner,
            "curtain": dict(self.curtain.state),
            "attention": list(self.attention.public),
            "window_gesture": dict(self.gesture.public),
            "speaker": dict(self.speaker.state),
            "diagnostics": self.runtime_log.export(),
        }

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
        if phase == 'paused' and self.voice_routing is not None:
            self.voice_routing.discard_followup(self.camera_id)
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
        receipt = await bridge.command("窗帘", action, event_id, camera_id=getattr(self,'camera_id',None))
        self.state["last_event"] = {"source": source, "action": action, "receipt": receipt, "at": time.time()}
        return receipt

    async def observe(
        self, people, faces, registration, now, capture, selected, preferred=None, gestures_enabled=False,
        sound_review=False, session_id=None, captured_mono=None,
    ):
        session = session_id if type(session_id) is str and 0 < len(session_id) <= 128 else None
        selected = bool(selected and self.enabled and self.armed and session is not None and self.input_allowed())
        continuous = 0 <= time.monotonic() - self.last_video < CAMERA_INPUT_MAX_AGE
        source_changed = session != self.camera_session or not continuous
        if selected != self.selected or source_changed:
            self.epoch += 1
            self.discard_audio()
            self.pending_audio = None
            self.camera_focus = None
            if self.voice_routing is not None:
                self.voice_routing.discard(self.camera_id)
        if source_changed:
            # 恢复后的首帧不能延续断流前的注视时长。
            self.attention.update([], [], now)
            self.gate_sent = -100.0
            self.gate_audio_until = 0.0
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
        if self.pointing_request is not None and not self.pointing_request[1]():
            self._revoke_pointing()
        if not selected or not good:
            self.gesture.reset()
        elif gestures_enabled:
            event = self.gesture.update(next((p for p in people if p["id"] == owner), None), now)
            if event:
                self.spawn(self.command(event["action"], event["id"], "window_gesture"))
        if capture and capture.stdin:
            gate_at = time.monotonic()
            # 清晰人脸缺失时仍可接收唤醒词，收音继续受本路授权和时效约束。
            gate = bool(
                not VOICE_INPUT_PAUSED.exists() and (sound_review is True or
                selected and self.audio_allowed and self.voice_status() in ("ready", "transcribing")
                and gate_at > self.reply_until and (
                    self.voice_routing is None or self.voice_routing.listening(gate_at)))
            )
            sent_until = offer_voice_window(
                capture.stdin, now=gate_at, captured=self.last_video, enabled=gate,
                attention=bool(people and registration.get('status') not in {'moving', 'settling'}),
                last_sent=self.gate_sent, last_audio_until=self.gate_audio_until,
            )
            if sent_until is not None:
                self.gate_sent = gate_at
                self.gate_audio_until = sent_until

    def clear_view_for_motion(self):
        """撤销画面依据，保留同一采集来源的收音。"""
        self.view_revision += 1
        self.gesture.reset()
        self.attention.update([], [], time.monotonic())
        self.visible_tracks.clear()
        self.owner = self.voice_owner = self.camera_focus = None
        self.pending_audio = None
        self._revoke_pointing()

    def _revoke_pointing(self):
        self.pointing_reply = None
        if self.pointing_request is not None:
            self.pointing_request[0].cancel()

    def _pointing_at_audio_ingress(self, stamp, source):
        frame = self.pointing_frame(self.camera_session, stamp)
        if frame is None:
            return None
        if 0 <= stamp - frame.target.observed_mono <= POINTING_HOLD_SECONDS:
            return PointingSpeech(frame.target, frame.single_person)
        reply = self.pointing_reply
        if (reply is not None and stamp <= reply.expires and reply.source == source
                and reply.view_revision == self.view_revision and reply.sequence == frame.target.sequence):
            return PointingSpeech(frame.target, frame.single_person, reply)
        return None

    def _accumulate_pointing(self, evidence):
        if evidence is None or self.utterance_pointing_conflict:
            return
        previous = self.utterance_pointing
        if previous is None:
            self.utterance_pointing = evidence
        elif previous.target != evidence.target:
            self.utterance_pointing = None
            self.utterance_pointing_conflict = True
        elif previous.single_person and not evidence.single_person:
            self.utterance_pointing = replace(previous, single_person=False)

    def reset_audio(self):
        """Discard a partial segment; completed admitted requests have their own lease."""
        self.audio_revision += 1
        self.vad = []
        self.speaking = False
        self.silence = self.duration = self.voiced_duration = 0.0
        self.utterance_owner = None
        self.utterance_attention = []
        self.utterance_pointing = None
        self.utterance_pointing_conflict = False
        self.utterance_started = self.utterance_ended = self.utterance_level = 0.0
        self.state['voice_activity'] = False
        try:
            getattr(self.activity, 'reset', lambda: None)()
        except Exception:
            self.activity = None
            self.audio_retry_at = time.monotonic()+5
            self.state['audio_analysis'] = dict(status='unavailable',code='local_audio_model_unavailable')

    def discard_audio(self):
        self.audio_backlog.clear()
        self._revoke_pointing()
        self.last_audio = self.audio_input_at = None
        if self.voice_routing is not None:
            self.voice_routing.discard_followup(self.camera_id)
        self.reset_audio()

    def offer_audio(self, admission):
        """取流只递交音频。声音识别慢时，旧片段失效，视频和云台继续接收。"""
        if (self.voice_routing is not None
                and (not isinstance(admission, AdmittedAudio) or admission.reset)):
            self.voice_routing.discard_followup(self.camera_id)
        if not isinstance(admission, AdmittedAudio) or admission.packet is None:
            self.audio_backlog.clear()
            self.last_audio = None
            self._revoke_pointing()
            prepare_voice_packet(self, admission)
            return
        if len(self.audio_backlog) == VOICE_PACKET_BACKLOG:
            self.discard_audio()
        if admission.reset:
            self._revoke_pointing()
        source = (admission.packet.get('capture_session'), admission.packet.get('camera_source_context'))
        stamp = admission.received_mono
        pointing = self._pointing_at_audio_ingress(stamp, source) if stamp is not None else None
        self.audio_backlog.append((self.epoch, self.view_revision, self.voice_owner, admission, pointing))
        if self.audio_task is None or self.audio_task.done():
            self.audio_task = asyncio.create_task(self._drain_audio(), name='friday-voice-packets')

    async def _drain_audio(self):
        try:
            while self.audio_backlog:
                epoch, view_revision, owner, admission, pointing = self.audio_backlog.popleft()
                if epoch != self.epoch:
                    continue
                received = admission.received_mono
                if received is None or not 0 <= time.monotonic() - received <= CAMERA_INPUT_MAX_AGE:
                    self.reset_audio()
                    continue
                await self.audio(prepare_voice_packet(self, admission), observed_owner=owner,
                                 observed_view=view_revision, observed_pointing=pointing)
        finally:
            self.audio_task = None

    async def _measure_audio(self, raw, current):
        await self.activity_gate.acquire()
        try:
            if not current():
                self.activity_gate.release()
                return None
            # 推理独占原模型状态。换源时事件循环可撤销结果，无需等待推理退出。
            previous, self.activity = self.activity, None

            def measure():
                model = previous if previous is not None else VoiceActivity()
                return model, model.measure(raw)

            job = asyncio.get_running_loop().run_in_executor(None, measure)
        except BaseException:
            self.activity_gate.release()
            raise

        def finished(future):
            self.activity_gate.release()
            if not future.cancelled():
                future.exception()

        job.add_done_callback(finished)
        return await asyncio.shield(job)

    def listening(self, now):
        return (self.selected and self.audio_allowed and self.input_allowed() and not VOICE_INPUT_PAUSED.exists()
                and self.voice_status() in ("ready", "transcribing")
                and now >= self.reply_until and 0 <= now - self.last_video < CAMERA_INPUT_MAX_AGE
                and (self.voice_routing is None or self.voice_routing.listening(now)))

    async def audio(self, item, *, observed_owner=CURRENT_ATTENTION, observed_view=None, observed_pointing=None):
        now = time.monotonic()
        if observed_view is None:
            observed_view = self.view_revision
        if observed_owner is CURRENT_ATTENTION:
            observed_owner = self.voice_owner
        if self.last_audio is not None and now - self.last_audio > 0.6:
            self.reset_audio()
        self.last_audio = now
        self.state["audio_packets"] = self.state.get("audio_packets", 0) + 1
        if not self.listening(now):
            self.reset_audio()
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
        epoch, audio_revision, received = self.epoch, self.audio_revision, self.audio_input_at

        def current():
            stamp = time.monotonic()
            return (self.epoch == epoch and self.audio_revision == audio_revision and self.listening(stamp)
                    and (received is None or 0 <= stamp - received <= CAMERA_INPUT_MAX_AGE))

        failed = False
        try:
            measured = await self._measure_audio(raw, current)
        except Exception:
            measured, failed = None, True
        if not current():
            if self.epoch == epoch and self.audio_revision == audio_revision:
                self.reset_audio()
            return
        if failed:
            self.reset_audio()
            self.audio_retry_at = time.monotonic() + 5
            self.state['audio_analysis'] = dict(status='unavailable',code='local_audio_model_unavailable',retry_ms=5000)
            return
        if measured is None:
            return
        self.activity, (speech, ratio, rms) = measured
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
        self.state["voice_activity"] = bool(speech)
        sample_at = now if received is None else received
        owner = observed_owner if observed_view == self.view_revision and observed_owner == self.voice_owner else None
        voiced = duration * ratio
        level = max(0.0, rms - self.noise) * voiced
        if not self.speaking:
            if not speech:
                self.noise = 0.98 * self.noise + 0.02 * min(rms, 0.018)
            self.vad = (self.vad + [raw])[-5:]
            if not speech:
                return
            self.speaking = True
            self.utterance_view_revision = self.view_revision
            self.duration = sum(len(p) for p in self.vad) / 32000.0
            self.silence = 0.0
            self.utterance_owner = owner
            self.utterance_attention = [(owner, duration)]
            self.utterance_pointing = None
            self.utterance_pointing_conflict = False
            if observed_view == self.view_revision:
                self._accumulate_pointing(observed_pointing)
            self.voiced_duration = voiced
            self.utterance_started = sample_at
            self.utterance_ended = min(now, sample_at + duration)
            self.utterance_level = level
            self.state["reason"] = "正在聆听（音频仅在内存）"
            return
        self.vad.append(raw)
        self.duration += duration
        self.silence = 0.0 if speech else self.silence + duration
        if speech:
            self.utterance_attention.append((owner, duration))
            if observed_view == self.utterance_view_revision == self.view_revision:
                self._accumulate_pointing(observed_pointing)
            self.voiced_duration += voiced
            self.utterance_ended = min(now, sample_at + duration)
            self.utterance_level += level
        if self.duration >= 8 or self.silence >= 0.65:
            pcm = b"".join(self.vad)
            self.vad = []
            self.speaking = False
            self.duration = 0.0
            owner, attending = utterance_focus(self.utterance_attention)
            pointing = self.utterance_pointing
            if self.utterance_view_revision != self.view_revision:
                owner, attending = None, False
                pointing = None
            if len(pcm) >= 16000 and self.voiced_duration >= WAKE_VOICED_SECONDS:
                source = (item.get('capture_session'), item.get('camera_source_context'))
                if not all(isinstance(value, str) and value for value in source):
                    source = None
                segment = (pcm, owner, now, attending, self.epoch, source)
                wake_only = self.voiced_duration < COMMAND_VOICED_SECONDS
                if self.voice_routing is not None:
                    if source is not None:
                        self.voice_routing.offer(self, Utterance(
                            *segment, self.utterance_started, self.utterance_ended,
                            self.utterance_level / self.voiced_duration, wake_only=wake_only,
                            view_revision=self.utterance_view_revision, pointing=pointing,
                            pointing_conflict=self.utterance_pointing_conflict))
                elif self.voice_busy:
                    self.pending_audio = (*segment, wake_only)
                else:
                    self.voice_busy = True
                    self.spawn(self.transcribe(*segment, wake_only))
            elif not self.voice_busy:
                self.state["reason"] = "等待清晰的语音指令"

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

    def _voice_guard(self, text, captured, epoch, attending, policy, owner=None, *, cue_only=False,
                     view_revision=None, followup=None):
        # Wake and gaze are alternative cues. Both still require the original
        # input source, policy and camera generation to remain current.
        addressed = address(text, attending, wake_word=policy.wake_word, after_wake=followup is not None)
        explicit_wake = addressed.get('wake_word') is True
        admitted = addressed.get('admitted') is True
        view_revision = self.view_revision if view_revision is None else view_revision

        def current():
            # Microphone arbitration gates new samples, not a completed
            # utterance's own lease while another camera collects audio.
            return bool(
                self.voice_preferences() == policy
                and self.current(captured, epoch)
                and not VOICE_INPUT_PAUSED.exists()
                and (explicit_wake if cue_only else admitted and (
                    explicit_wake
                    or followup is not None and self.audio_allowed and self.camera_session == followup.session
                    or attending and view_revision == self.view_revision
                    and owner is not None and owner in self.attention.latest))
            )

        return current

    def _wake_focus(self, text, captured, epoch, policy, source=None):
        if (self.camera_session is None
                or not self._voice_guard(text, captured, epoch, False, policy, cue_only=True)()):
            return False
        if source is not None:
            self.camera_focus = None
            self.wake_camera = WakeCameraCue(captured, self.camera_session, source, policy.revision, time.monotonic())
            return True
        # 没有采集凭据时只保留原有的单人构图提示。
        visible = self.visible_tracks
        target = next(iter(visible)) if len(visible) == 1 else None
        self.camera_focus = (dict(track=target, at=time.monotonic(), session_id=self.camera_session)
                             if target is not None else None)
        return self.camera_focus is not None

    def wake_motion_cue(self, sample, now):
        cue = self.wake_camera
        if cue is None:
            return None
        start = cue.confirmed_mono if cue.confirmed_mono is not None else cue.captured
        if (not 0 <= now - start < WAKE_CAMERA_SECONDS
                or sample.get('session_id') != cue.session
                or not self.input_allowed()
                or VOICE_INPUT_PAUSED.exists()):
            self.wake_camera = None
            return None
        if sample.get('status') != 'live' or sample.get('_wake_source') != cue.source:
            return None
        try:
            if self.voice_preferences().revision == cue.voice_revision:
                waiting = self.voice_routing is not None and self.voice_routing.waiting(self, cue.source, now)
                if self.speaking or self.voice_needs_stable_view or waiting:
                    return WakeCameraPause(cue)
                return cue
        except (OSError, ValueError):
            pass
        self.wake_camera = None
        return None

    async def speak_reply(self, text, current):
        self.reply_until = time.monotonic() + min(30, max(3.5, len(text) / 4.5 + 1.5))
        audible = self.voice_routing is not None and not VOICE_PAUSED.exists()
        with self.voice_routing.replying(self.reply_until) if audible else nullcontext():
            await self.speaker.say(text, current=current)

    async def answer_question(self, text, event, captured, epoch, attending, policy, command, *,
                              current, after_wake=False):
        if not current():
            command.update(state='expired',updated_at=time.time())
            self.state['reason']='这次提问已过期，请重新说一次'
            return
        answer = await bridge.question(text,camera_id=self.camera_id,
            request_id=hashlib.sha256(f'voice-query:{self.camera_id}:{event}:{epoch}:{captured}'.encode()).hexdigest(),
            captured_mono=captured,voice_revision=policy.revision,attending=attending,
            **({'after_wake': True} if after_wake else {}))
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

    async def prepare_task(self, text, event, captured, epoch, attending, policy, command, *,
                           current, after_wake=False):
        if not current():
            command.update(state='expired',updated_at=time.time())
            return
        answer=await bridge.task(text,camera_id=self.camera_id,
            request_id=hashlib.sha256(f'voice-task:{self.camera_id}:{event}:{epoch}:{captured}'.encode()).hexdigest(),
            captured_mono=captured,voice_revision=policy.revision,attending=attending,
            **({'after_wake': True} if after_wake else {}))
        if not current() or answer.valid_until<=datetime.now(UTC):
            if answer.proposal_ref:await bridge.discard_task(answer.proposal_ref)
            command.update(state='expired',updated_at=time.time())
            return
        command.update(route='task',state='awaiting_confirmation' if answer.status=='pending_confirmation' else 'clarify',
            reply=answer.reply,receipt={'status':answer.status,'configured':False,'device_authority':False},updated_at=time.time())
        self.state.update(last_result=answer.model_dump(mode='json'),last_route='task',reason=answer.reply)
        await self.speak_reply(answer.reply, lambda: current() and answer.valid_until > datetime.now(UTC))

    def pointing_frame_for(self, utterance, owner, attending):
        if (utterance is None or utterance.pointing is None or utterance.pointing_conflict
                or utterance.view_revision != self.view_revision or utterance.epoch != self.epoch):
            return None
        evidence = utterance.pointing
        frame = self.pointing_frame(self.camera_session, time.monotonic())
        if frame is None or frame.target != evidence.target:
            return None
        if attending:
            return frame if owner == evidence.target.owner and owner in self.attention.latest else None
        return frame if evidence.single_person and frame.single_person else None

    async def _command_pointed(self, intent, utterance, command, current, *, owner, attending, view_revision,
                               source, voice_revision, previous_reply, retry):
        self.state.update(intent_provider='local_rules', last_route='home')
        evidence = utterance.pointing if utterance is not None else None

        def current_frame():
            if source is None or utterance is None or utterance.source != source or not current():
                return None
            return self.pointing_frame_for(utterance, owner, attending)

        if intent.action == 'no_op':
            reply = '好的，保持设备当前状态。'
            command.update(route='no_op', state='ignored', reply=reply, updated_at=time.time())
            self.state.update(last_route='no_op', reason=reply)
            await self.speak_reply(reply, current)
            return
        frame = current_frame() if intent.action != 'clarify' else None
        if frame is not None and evidence.reply is not None:
            remembered = evidence.reply
            if (remembered is not previous_reply or remembered.source != source
                    or remembered.voice_revision != voice_revision
                    or not utterance.started <= remembered.expires):
                frame = None
        if frame is None:
            reply = '请指向一个已标定的设备，再说一次要做什么。'
            command.update(route='clarify', state='clarify', reply=reply, updated_at=time.time())
            self.state.update(last_route='clarify', reason=reply)
            if (retry is not None and evidence is None and utterance is not None
                    and not utterance.pointing_conflict and self.voice_routing is not None
                    and self.voice_routing.pointing_alternate(self.camera_id) is not None):
                return retry
            await self.speak_reply(reply, current)
            return
        target = evidence.target
        command.update(route='home', state='executing', updated_at=time.time())
        self.state['reason'] = '正在控制所指设备'
        job = asyncio.create_task(bridge.command(
            None, intent.action, command['id'], target=target.target, camera_id=self.camera_id,
            pointing=dict(binding=target.binding, revision=target.revision, observed_mono=frame.observed_mono,
                          voice_revision=voice_revision, camera_source_context=source[1], kind=intent.kind),
        ))
        self.pointing_request = (job, current_frame)
        try:
            receipt = await job
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # 请求可能已发出。取消视觉上下文后不重试，也不宣称设备未动。
            receipt = dict(status='unknown_outcome', acknowledged=False, reached_target=False)
        finally:
            self.pointing_request = None
        outcome = voice_command_outcome(receipt)
        if outcome == 'readback' and current_frame() is not None:
            self.pointing_reply = PointingReply(target.sequence, source, view_revision, voice_revision,
                                                time.monotonic() + POINTING_FOLLOWUP_SECONDS)
        reply = {
            'readback': '已读回目标状态。',
            'accepted': '指令已发送，设备状态待确认。',
            'unknown_outcome': '设备状态待确认。',
        }.get(outcome, '所指设备暂时无法执行这条指令。')
        command.update(state=outcome, reply=reply,
                       receipt={key: receipt.get(key) for key in ('status', 'acknowledged', 'reached_target')},
                       updated_at=time.time())
        self.state.update(reason=reply, last_event={'source': 'voice', 'receipt': receipt, 'at': time.time()})
        await self.speak_reply(reply, current)

    async def transcribe(self, pcm, owner, captured, attending=False, epoch=None, source=None, wake_only=False,
                         *, utterance=None, followup=None, gaze_retry=None, pointing_retry=None):
        epoch = self.epoch if epoch is None else epoch
        view_revision = utterance.view_revision if utterance is not None else None
        view_revision = self.view_revision if view_revision is None else view_revision
        began = time.monotonic()
        result_kind, result_code = "ok", "none"
        command = None
        self.runtime_log.record("embodied.voice", "start")
        try:
            self.voice_needs_stable_view = True
            self.state["voice"] = "transcribing"
            policy=self.voice_preferences()
            attending = attending and view_revision == self.view_revision
            if gaze_retry is not None and gaze_retry.voice_revision != policy.revision:
                attending, owner = False, None
            if followup is not None and (
                followup.revision != policy.revision
                or self.voice_routing.followup_for(self, utterance) is not followup
            ):
                followup = None
            result = await self.request("transcribe", pcm=base64.b64encode(pcm).decode(), attending=attending,
                                        voice_revision=policy.revision, wake_only=wake_only,
                                        **({'after_wake': True} if followup is not None else {}))
            if result.get('voice_revision',0)!=policy.revision or self.voice_preferences()!=policy:
                self.state['reason']='唤醒词已更新，请重新说一次'
                return
            self.state["asr_ms"] = result.get("latency_ms")
            self.state["laya_result"] = result.get("laya")
            self.state["asr_quality"] = result.get("asr_quality")
            worker_admission = result.get("address", {})
            if worker_admission.get("reason") in (
                "no_speech",
                "unsupported_script",
                "no_chinese_task",
                "low_asr_confidence",
                "likely_no_speech",
                "invalid_text",
                "wake_word_required",
            ):
                self.state["last_address"] = worker_admission
                self.state["reason"] = "等待清晰的语音指令"
                if self.voice_routing is not None and not VOICE_INPUT_PAUSED.exists():
                    return RETRY_OTHER_MIC
                return
            if not self.current(captured, epoch) or time.monotonic() - captured > 10:
                self.state["reason"] = "指令已过期或操作者变化"
                if self.voice_routing is not None and not VOICE_INPUT_PAUSED.exists():
                    return RETRY_OTHER_MIC
                return
            attending = attending and view_revision == self.view_revision
            if followup is not None and self.voice_routing.followup_for(self, utterance) is not followup:
                followup = None
            route = worker_admission
            if route.get('admitted') is True or route.get('reason') == 'waiting_instruction':
                route = address(result.get('text'), attending and not wake_only, wake_word=policy.wake_word,
                                after_wake=followup is not None and not wake_only)
            if (gaze_retry is not None and route.get('reason') == 'sustained_attention'
                    and _speech_body(route.get('body')) != gaze_retry.body):
                self.state['last_address'] = dict(admitted=False, reason='gaze_retry_transcript_mismatch')
                self.state['reason'] = '跨镜头语音不一致，请再说一次'
                return
            if pointing_retry is not None and (
                pointing_retry.voice_revision != policy.revision or not route.get('admitted')
                or _speech_body(route.get('body')) != pointing_retry.body
            ):
                self.state['last_address'] = dict(admitted=False, reason='pointing_retry_transcript_mismatch')
                self.state['reason'] = '跨镜头语音不一致，请再说一次'
                return
            self.state["last_address"] = route
            if wake_only or worker_admission.get("admitted") is not True:
                if (route.get('reason') == 'waiting_instruction'
                        and address(result.get('text'), False, wake_word=policy.wake_word).get('reason')
                        == 'waiting_instruction'):
                    self.state["reason"] = "请继续说出指令"
                    detected = self._wake_focus(result['text'], captured, epoch, policy, source)
                    if (detected and self.voice_routing is not None
                            and utterance is not None and self.wake_camera is not None):
                        self.voice_routing.open_followup(self, utterance, self.wake_camera)
                    return detected
                if wake_only:
                    self.state["reason"] = "等待清晰的语音指令"
                elif route.get("reason") == "need_wake_word_without_attention":
                    self.state["reason"] = f"未确认注视，请先说 {policy.wake_word}"
                else:
                    self.state["reason"] = "请继续说出指令"
                if self.voice_routing is not None and not VOICE_INPUT_PAUSED.exists():
                    if not wake_only and worker_admission.get('reason') == 'need_wake_word_without_attention':
                        body = _speech_body(address(result.get('text'), True, wake_word=policy.wake_word).get('body'))
                        if body:
                            return RetryWithOwnAttention(body, policy.revision)
                    return RETRY_OTHER_MIC
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
            after_wake = route.get('reason') == 'wake_followup'
            current = self._voice_guard(result['text'], captured, epoch, attending, policy, owner,
                                        view_revision=view_revision, followup=followup if after_wake else None)
            if not current():
                command.update(state="expired", updated_at=time.time())
                self.state["reason"] = "指令已过期或操作者变化"
                return
            if self.voice_routing is not None:
                self.voice_routing.discard_followup(self.camera_id)
            previous_reply, self.pointing_reply = self.pointing_reply, None
            body = route['body']
            pointed = pointing_command(body)
            if pointed is not None:
                self.wake_camera = self.camera_focus = None
                retry = (RetryWithOwnPointing(_speech_body(body), policy.revision)
                         if pointing_retry is None and pointed.action not in {'no_op', 'clarify'} else None)
                return await self._command_pointed(
                    pointed, utterance, command, current, owner=owner, attending=attending,
                    view_revision=view_revision, source=source, voice_revision=policy.revision,
                    previous_reply=previous_reply, retry=retry,
                )
            self._wake_focus(result['text'], captured, epoch, policy, source)
            # 明确唤醒或同源接续已经准入，设备读回无需占用视角。
            self.voice_needs_stable_view = not (route.get('wake_word') is True or after_wake)
            decision_started = time.monotonic()
            laya = result.get("laya") or {}
            if is_task_request(body):
                await self.prepare_task(result['text'], event, captured, epoch, attending, policy, command,
                                        current=current, after_wake=after_wake)
                return
            if is_home_question(body):
                await self.answer_question(result['text'], event, captured, epoch, attending, policy, command,
                                           current=current, after_wake=after_wake)
                return
            fast = household_plan(result["text"], attending, wake_word=policy.wake_word, after_wake=after_wake)
            general = basic_command(result["text"], attending, wake_word=policy.wake_word, after_wake=after_wake)
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
                if laya.get("choice") == "闲聊或转述" and laya.get("confidence", 0) >= 0.75:
                    command.update(state="ignored", updated_at=time.time())
                    self.state["reason"] = "闲聊，未触发任务"
                    return
                self.state["reason"] = "ARL Codex 正在理解指令"
                task = await self.request(
                    "delegate", text=result["text"], context=dict(route, person_id=owner), laya=laya,
                    request_id=hashlib.sha256(f'voice-intent:{self.camera_id}:{event}:{epoch}:{captured}'.encode()).hexdigest(),
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
            self.voice_needs_stable_view = False
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
