"""Nonblocking home interactions over fresh tracks, attention and private PCM."""

import asyncio
import base64
import hashlib
import json
import time

import numpy as np

from homeplane.embodied import bridge
from homeplane.embodied.household import basic_command
from homeplane.embodied.intent import parse_plan, utterance_focus
from homeplane.embodied.local_intent import household_plan
from homeplane.embodied.paths import DATA, PRIVATE, PYTHON
from homeplane.embodied.smart_home import Attention, Curtain, WindowGesture
from homeplane.embodied.speech import Speaker
from homeplane.embodied.voice_activity import VoiceActivity
from homeplane.runtime_log import RuntimeLog, observe

VOICE_PYTHON = str(PYTHON)
VOICE_PAUSED = PRIVATE / "voice-paused"
VOICE_INPUT_PAUSED = PRIVATE / 'voice-input-paused'


class HomeService:
    def __init__(self, camera_id=None, voice_provider=None):
        self.camera_id = camera_id
        self.voice_provider = voice_provider
        self.audio_allowed = True
        self.runtime_log = RuntimeLog(DATA / "logs")
        self.enabled = False
        self.armed = False
        self.retry_voice_at = 0
        self.curtain = Curtain()
        self.gesture = WindowGesture()
        self.attention = Attention()
        self.worker = None
        self.reader = None
        self.poller = None
        self.jobs = set()
        self.futures = {}
        self.lock = asyncio.Lock()
        self.sequence = 0
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
        self.voice_busy = False
        self.last_video = -100.0
        self.camera_focus = None
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
            "laya": "command_gate_with_local_or_codex_intent",
            "arl_scope": "codex_plan_local_allowlisted_actuator",
            "audio_persisted": False,
            "wake_policy": "attention_optional_wake_otherwise_required",
        }

    def public(self):
        return {
            **self.state,
            "enabled": self.enabled and self.armed,
            "owner_track": self.owner,
            "curtain": dict(self.curtain.state),
            "attention": list(self.attention.public),
            "window_gesture": dict(self.gesture.public),
            "speaker": dict(self.speaker.state),
            "diagnostics": self.runtime_log.export(),
        }

    async def enable(self):
        self.armed = True
        # Muting Friday's replies must not silently turn off gaze/voice input.
        if VOICE_INPUT_PAUSED.exists():
            if not self.enabled:
                self.enabled = True
                self.poller = asyncio.create_task(self.poll_curtain())
            self.state.update(
                enabled=True, voice="paused", reason="语音指令已暂停，手势控制仍可用"
            )
            return
        if self.enabled:
            if self.voice_provider is not None or self.worker and self.worker.returncode is None:
                return
            if time.monotonic() < self.retry_voice_at:
                return
            self.retry_voice_at = time.monotonic() + 30
            if self.reader:
                await asyncio.gather(self.reader, return_exceptions=True)
            if self.poller:
                self.poller.cancel()
                await asyncio.gather(self.poller, return_exceptions=True)
        self.enabled = True
        if self.voice_provider is not None:
            await self.voice_provider.enable()
            self.state.update(enabled=True, voice=self.voice_provider.state['voice'])
            return
        self.state.update(enabled=True, voice="loading_local_models", reason="本地语音模型启动中")
        self.poller = asyncio.create_task(self.poll_curtain())
        self.worker = await asyncio.create_subprocess_exec(
            VOICE_PYTHON,
            "-B",
            "-m",
            "homeplane.embodied.voice_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=100000,
        )
        self.reader = asyncio.create_task(self.read_worker())

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

    async def read_worker(self):
        try:
            while line := await self.worker.stdout.readline():
                item = json.loads(line)
                if item.get("type") == "ready":
                    self.state.update(
                        asr_model=item.get("asr"),
                        intelligence=item.get("intelligence"),
                        voice="ready",
                        reason="看向镜头直接说指令；未注视时先说 Friday",
                    )
                    continue
                future = self.futures.get(item.get("id"))
                if future and not future.done():
                    future.set_result(item)
        finally:
            self.state["voice"] = "unavailable"
            for future in self.futures.values():
                if not future.done():
                    future.set_exception(RuntimeError("voice_worker_closed"))

    async def request(self, op, **kwargs):
        if self.voice_provider is not None:
            return await self.voice_provider.request(op, **kwargs)
        async with self.lock:
            if not self.worker or self.worker.returncode is not None or not self.worker.stdin:
                raise RuntimeError("voice_not_ready")
            self.sequence += 1
            seq = self.sequence
            future = asyncio.get_running_loop().create_future()
            self.futures[seq] = future
            try:
                self.worker.stdin.write((json.dumps(dict(kwargs, op=op, id=seq)) + "\n").encode())
                await self.worker.stdin.drain()
                return await asyncio.wait_for(future, 60)
            finally:
                self.futures.pop(seq, None)

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
        self, people, faces, registration, now, capture, selected, preferred=None, gestures_enabled=False
    ):
        selected = bool(selected and self.enabled and self.armed)
        if selected != self.selected:
            self.epoch += 1
            self.vad = []
            self.speaking = False
            self.duration = 0.0
            self.voiced_duration = 0.0
            self.pending_audio = None
        self.selected = selected
        if self.voice_provider is not None and self.state['voice'] != 'transcribing':
            self.state['voice'] = self.voice_provider.state['voice']
        self.last_video = now
        good = registration.get("status") not in {"moving", "settling", "lost", "stale"}
        self.attention.update(people if good else [], faces if good else [], now)
        ids = {p["id"] for p in people}
        self.visible_tracks = ids
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
                selected and self.audio_allowed and not VOICE_INPUT_PAUSED.exists()
                and self.state["voice"] in ("ready", "transcribing") and now > self.reply_until
            )
            capture.stdin.write(
                (
                    json.dumps({"action": "voice_window", "enabled": gate,
                        "attention": bool(people and registration.get('status') not in {'moving','settling'}
                            and 0 <= time.monotonic()-now <= 1.5)}) + "\n"
                ).encode()
            )
            await capture.stdin.drain()

    async def audio(self, item):
        now = time.monotonic()
        if self.last_audio is not None and now - self.last_audio > 0.6:
            getattr(self.activity, "reset", lambda: None)()
        self.last_audio = now
        self.state["audio_packets"] = self.state.get("audio_packets", 0) + 1
        if (
            not self.selected or not self.audio_allowed or VOICE_INPUT_PAUSED.exists()
            or self.state["voice"] not in ("ready", "transcribing")
            or now < self.reply_until
            or now - self.last_video > 1.5
        ):
            self.vad = []
            self.speaking = False
            self.duration = 0.0
            self.voiced_duration = 0.0
            getattr(self.activity, "reset", lambda: None)()
            return
        raw = base64.b64decode(item["pcm"])
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        duration = len(samples) / 16000.0
        rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
        self.state["audio_rms"] = round(rms, 4)
        if self.activity is None:
            self.activity = VoiceActivity()
        speech, ratio, rms = self.activity.measure(raw)
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
                if self.voice_busy:
                    self.pending_audio = (pcm, owner, now, attending, self.epoch)
                else:
                    self.voice_busy = True
                    self.spawn(self.transcribe(pcm, owner, now, attending, self.epoch))
            elif not self.voice_busy:
                self.state["reason"] = "等待清晰的语音指令"

    def current(self, captured, epoch):
        return bool(
            self.selected
            and self.armed
            and epoch == self.epoch
            and time.monotonic() - captured < 45
            and time.monotonic() - self.last_video < 1.5
        )

    async def transcribe(self, pcm, owner, captured, attending=False, epoch=None):
        epoch = self.epoch if epoch is None else epoch
        began = time.monotonic()
        result_kind, result_code = "ok", "none"
        self.runtime_log.record("embodied.voice", "start")
        try:
            self.state["voice"] = "transcribing"
            result = await self.request("transcribe", pcm=base64.b64encode(pcm).decode(), attending=attending)
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
            if route.get('wake_word'):
                visible = getattr(self,'visible_tracks',set())
                # Visual attention is not speaker localization. With this
                # microphone, only a unique visible person is a framing cue.
                target = next(iter(visible)) if len(visible)==1 else None
                self.camera_focus = dict(track=target,at=time.monotonic()) if target is not None else None
            if not route.get("admitted"):
                self.state["reason"] = (
                    "未确认注视，请先说 Friday"
                    if route.get("reason") == "need_wake_word_without_attention"
                    else "请继续说出指令"
                )
                return
            if result.get("text"):
                self.state["last_transcript"] = result["text"]
            decision_started = time.monotonic()
            laya = result.get("laya") or {}
            fast = household_plan(result["text"], attending)
            general = basic_command(result["text"], attending)
            if not fast and general:
                fast = {
                    "is_command": True,
                    "route": general["route"],
                    "device": general.get("device"),
                    "action": general.get("action"),
                    "reply": general.get("reply", "正在处理家居指令。"),
                }
            event = "voice:" + hashlib.sha256(pcm).hexdigest()[:24]
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
                if laya.get("status") == "unavailable":
                    self.state["reason"] = "理解服务暂不可用，请稍后重试"
                    return
                if laya.get("choice") == "闲聊或转述" and laya.get("confidence", 0) >= 0.75:
                    self.state["reason"] = "闲聊，未触发任务"
                    return
                self.state["reason"] = "ARL Codex 正在理解指令"
                task = await self.request(
                    "delegate", text=result["text"], context=dict(route, person_id=owner), laya=laya
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
            if not self.current(captured, epoch) or (
                attending and (owner is None or owner not in self.attention.latest)
            ):
                self.state["reason"] = "任务返回时交互已结束，未执行"
                return
            task["returned_at"] = time.time()
            self.state["last_result"] = task
            self.state["last_route"] = plan["route"]
            self.state["intent_provider"] = task.get("provider")
            self.state["intent_ms"] = task.get("latency_ms")
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
                await self.speaker.say(spoken)
        except asyncio.CancelledError:
            result_kind, result_code = "cancelled", "cancelled"
            raise
        except Exception as e:
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
            self.state["voice"] = (self.voice_provider.state['voice'] if self.voice_provider else
                                   "ready" if self.worker and self.worker.returncode is None else "unavailable")
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
        tasks = [t for t in [self.poller, self.reader, *self.jobs] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.worker and self.worker.returncode is None:
            self.worker.terminate()
            try:
                await asyncio.wait_for(self.worker.wait(), 3)
            except asyncio.TimeoutError:
                self.worker.kill()
                await self.worker.wait()
        await self.curtain.close()
        self.enabled = False
        self.armed = False
        self.state.update(enabled=False, voice="stopped")
