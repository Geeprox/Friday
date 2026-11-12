"""Nonblocking home interactions over fresh tracks, attention and private PCM."""

import asyncio
import base64
import hashlib
import json
import time
from datetime import UTC, datetime

import numpy as np

from homeplane.embodied import bridge
from homeplane.embodied.camera_audio import MAX_AGE as CAMERA_INPUT_MAX_AGE
from homeplane.embodied.camera_audio import WakeCameraCue, offer_voice_window
from homeplane.embodied.household import basic_command
from homeplane.embodied.intent import address, parse_plan, utterance_focus
from homeplane.embodied.local_intent import household_plan
from homeplane.embodied.paths import DATA, PRIVATE, PYTHON
from homeplane.embodied.smart_home import Attention, Curtain, WindowGesture
from homeplane.embodied.speech import Speaker
from homeplane.embodied.voice_activity import VoiceActivity
from homeplane.embodied.voice_preferences import read_preferences
from homeplane.embodied.voice_process import VoiceProcess
from homeplane.runtime_log import RuntimeLog, observe
from homeplane.home_questions import is_home_question
from homeplane.task_composition import is_task_request

VOICE_PYTHON = str(PYTHON)
VOICE_PAUSED = PRIVATE / "voice-paused"
VOICE_INPUT_PAUSED = PRIVATE / 'voice-input-paused'
WAKE_CAMERA_SECONDS = 16.0


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
    def __init__(self, camera_id=None, voice_provider=None, *, voice_preferences=None):
        self.camera_id = camera_id
        self.voice_provider = voice_provider
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
        self.vad = []
        self.speaking = False
        self.silence = 0.0
        self.duration = 0.0
        self.voiced_duration = 0.0
        self.noise = 0.004
        self.last_audio = None
        self.audio_input_at = None
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
            self.reset_audio()
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
        selected = bool(selected and self.enabled and self.armed and session is not None)
        continuous = 0 <= time.monotonic() - self.last_video < CAMERA_INPUT_MAX_AGE
        source_changed = session != self.camera_session or not continuous
        if selected != self.selected or source_changed:
            self.epoch += 1
            self.reset_audio()
            self.pending_audio = None
            self.camera_focus = None
        if source_changed:
            # 恢复后的首帧不能延续断流前的注视时长。
            self.attention.update([], [], now)
            self.gate_sent = -100.0
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
        if capture and capture.stdin and now - self.gate_sent > 0.4:
            self.gate_sent = now
            # Keep the wake-word path available even with no qualified face. Audio is
            # bounded in RAM and only forwarded while this mode is selected.
            gate = bool(
                not VOICE_INPUT_PAUSED.exists() and (sound_review is True or
                selected and self.audio_allowed and self.voice_status() in ("ready", "transcribing")
                and now > self.reply_until)
            )
            offer_voice_window(capture.stdin, now=time.monotonic(), captured=self.last_video, enabled=gate,
                attention=bool(people and registration.get('status') not in {'moving', 'settling'}))

    def reset_audio(self):
        """Discard a partial segment; completed admitted requests have their own lease."""
        self.vad = []
        self.speaking = False
        self.silence = self.duration = self.voiced_duration = 0.0
        self.utterance_owner = None
        self.utterance_attention = []
        self.state['voice_activity'] = False
        try:
            getattr(self.activity, 'reset', lambda: None)()
        except Exception:
            self.activity = None
            self.audio_retry_at = time.monotonic()+5
            self.state['audio_analysis'] = dict(status='unavailable',code='local_audio_model_unavailable')

    async def audio(self, item):
        now = time.monotonic()
        if self.last_audio is not None and now - self.last_audio > 0.6:
            self.reset_audio()
        self.last_audio = now
        self.state["audio_packets"] = self.state.get("audio_packets", 0) + 1
        if (
            not self.selected or not self.audio_allowed or VOICE_INPUT_PAUSED.exists()
            or self.voice_status() not in ("ready", "transcribing")
            or now < self.reply_until
            or not 0 <= now - self.last_video < CAMERA_INPUT_MAX_AGE
        ):
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
        try:
            if self.activity is None:
                self.activity = VoiceActivity()
            speech, ratio, rms = self.activity.measure(raw)
        except Exception:
            # Optional audio inference must not terminate the camera receiver,
            # which also carries video and motor acknowledgements. No raw model
            # error, PCM or transcript is added to status/diagnostic output.
            self.activity = None
            self.reset_audio()
            self.audio_retry_at = now+5
            self.state['audio_analysis'] = dict(status='unavailable',code='local_audio_model_unavailable',retry_ms=5000)
            return
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
        if not self.speaking:
            if not speech:
                self.noise = 0.98 * self.noise + 0.02 * min(rms, 0.018)
            self.vad = (self.vad + [raw])[-5:]
            if not speech:
                return
            self.speaking = True
            self.duration = sum(len(p) for p in self.vad) / 32000.0
            self.silence = 0.0
            self.utterance_owner = self.voice_owner
            self.utterance_attention = [(self.voice_owner, duration)]
            self.voiced_duration = duration * ratio
            self.state["reason"] = "正在聆听（音频仅在内存）"
            return
        self.vad.append(raw)
        self.duration += duration
        self.silence = 0.0 if speech else self.silence + duration
        if speech:
            self.utterance_attention.append((self.voice_owner, duration))
            self.voiced_duration += duration * ratio
        if self.duration >= 8 or self.silence >= 0.65:
            pcm = b"".join(self.vad)
            owner = self.utterance_owner
            self.vad = []
            self.speaking = False
            self.duration = 0.0
            owner, attending = utterance_focus(self.utterance_attention)
            if len(pcm) >= 16000 and self.voiced_duration >= 0.45:
                source = (item.get('capture_session'), item.get('camera_source_context'))
                if not all(isinstance(value, str) and value for value in source):
                    source = None
                segment = (pcm, owner, now, attending, self.epoch, source)
                if self.voice_busy:
                    self.pending_audio = segment
                else:
                    self.voice_busy = True
                    self.spawn(self.transcribe(*segment))
            elif not self.voice_busy:
                self.state["reason"] = "等待清晰的语音指令"

    def current(self, captured, epoch):
        now = time.monotonic()
        return bool(
            self.selected
            and self.armed
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
            return
        if source is not None:
            self.camera_focus = None
            self.wake_camera = WakeCameraCue(captured, self.camera_session, source, policy.revision)
            return
        # 没有采集凭据时只保留原有的单人构图提示。
        visible = self.visible_tracks
        target = next(iter(visible)) if len(visible) == 1 else None
        self.camera_focus = (dict(track=target, at=time.monotonic(), session_id=self.camera_session)
                             if target is not None else None)

    def wake_motion_cue(self, sample, now):
        cue = self.wake_camera
        if cue is None:
            return None
        if (not 0 <= now - cue.captured < WAKE_CAMERA_SECONDS
                or sample.get('session_id') != cue.session
                or VOICE_INPUT_PAUSED.exists()):
            self.wake_camera = None
            return None
        if (sample.get('status') != 'live' or sample.get('_wake_source') != cue.source
                or self.speaking or self.voice_busy):
            return None
        try:
            if self.voice_preferences().revision == cue.voice_revision:
                return cue
        except (OSError, ValueError):
            pass
        self.wake_camera = None
        return None

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
        self.reply_until=time.monotonic()+min(30,max(3.5,len(answer.reply)/4.5+1.5))
        await self.speaker.say(answer.reply, current=lambda: current() and answer.valid_until > datetime.now(UTC))

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
        await self.speaker.say(answer.reply,current=lambda:current() and answer.valid_until>datetime.now(UTC))

    async def transcribe(self, pcm, owner, captured, attending=False, epoch=None, source=None):
        epoch = self.epoch if epoch is None else epoch
        began = time.monotonic()
        result_kind, result_code = "ok", "none"
        command = None
        self.runtime_log.record("embodied.voice", "start")
        try:
            self.state["voice"] = "transcribing"
            policy=self.voice_preferences()
            result = await self.request("transcribe", pcm=base64.b64encode(pcm).decode(), attending=attending,voice_revision=policy.revision)
            if result.get('voice_revision',0)!=policy.revision or self.voice_preferences()!=policy:
                self.state['reason']='唤醒词已更新，请重新说一次'
                return
            self.state["asr_ms"] = result.get("latency_ms")
            self.state["laya_result"] = result.get("laya")
            self.state["asr_quality"] = result.get("asr_quality")
            route = result.get("address", {})
            self.state["last_address"] = route
            if route.get("reason") in (
                "no_speech",
                "unsupported_script",
                "no_chinese_task",
                "low_asr_confidence",
                "likely_no_speech",
                "invalid_text",
            ):
                self.state["reason"] = "等待清晰的语音指令"
                return
            if not self.current(captured, epoch) or time.monotonic() - captured > 10:
                self.state["reason"] = "指令已过期或操作者变化"
                return
            if not route.get("admitted"):
                if (route.get('reason') == 'waiting_instruction'
                        and address(result.get('text'), False, wake_word=policy.wake_word).get('reason')
                        == 'waiting_instruction'):
                    self._wake_focus(result['text'], captured, epoch, policy, source)
                self.state["reason"] = (
                    f"未确认注视，请先说 {policy.wake_word}"
                    if route.get("reason") == "need_wake_word_without_attention"
                    else "请继续说出指令"
                )
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
            laya = result.get("laya") or {}
            body = address(result['text'],attending,wake_word=policy.wake_word).get('body','')
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
                self.reply_until = time.monotonic() + min(30, max(3.5, len(spoken) / 4.5 + 1.5))
                await self.speaker.say(spoken, current=current)
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
        if self.voice_process:
            self.voice_process.stop()
        tasks = [t for t in [self.poller, *self.jobs] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.voice_process:
            await self.voice_process.close()
        await self.curtain.close()
        self.enabled = False
        self.armed = False
        self.state.update(enabled=False, voice="stopped")
