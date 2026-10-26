"""Small deterministic control boundary; models provide observations, never tool calls."""

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from homeplane.attention_policy import attention_policy
from homeplane.interaction_contracts import AttentionAdmission, InteractionBinding
from homeplane.interaction_sessions import InteractionSessions

from .config import Device, Settings


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    live_speaker: bool = False
    people_count: int = Field(default=0, ge=0, le=30)
    facing_camera: bool = False
    pointing: bool = False
    attention: float = Field(default=0, ge=0, le=1)
    target_id: str | None = None
    target_confidence: float = Field(default=0, ge=0, le=1)


@dataclass
class Observation:
    event_id: str
    text: str
    evidence: Evidence
    captured_at: float
    session: str
    camera_id: str
    interaction: InteractionBinding | None = None
    admission: AttentionAdmission | None = None
    is_current: Callable[[], bool] | None = None


class Result(BaseModel):
    event_id: str
    status: str
    reason: str
    device_id: str | None = None
    action: str | None = None
    value: bool | int | None = None
    latency_ms: int = 0
    input_kind: str = "ambient"
    producer_state: str = "unavailable"


def parse_command(text: str) -> tuple[str | None, str, bool]:
    text = re.sub(r"[\s，。！、,.!]", "", text.strip().lower())
    wake = text.startswith("friday")
    if wake:
        text = text[6:]
    # Full-string grammar prevents questions, negations, quotes and background narration.
    target = r"(?P<target>[\u4e00-\u9fffA-Za-z0-9_-]{0,30}?)"
    forms = [
        ("on", rf"(?:请|帮我|麻烦)?(?:把)?{target}(?:打开|开一下|开启|开)(?:一下|吧)?"),
        ("on", rf"(?:请|帮我|麻烦)?(?:打开|开启){target}(?:一下|吧)?"),
        ("off", rf"(?:请|帮我|麻烦)?(?:把)?{target}(?:关闭|关掉|关一下|关)(?:一下|吧)?"),
        ("off", rf"(?:请|帮我|麻烦)?(?:关闭|关掉){target}(?:一下|吧)?"),
        ("dim", rf"(?:请|帮我|麻烦)?(?:把)?{target}(?:暗一点|调暗一点|调暗|暗一些)"),
        ("brighten", rf"(?:请|帮我|麻烦)?(?:把)?{target}(?:亮一点|调亮一点|调亮|亮一些)"),
    ]
    for action, pattern in forms:
        if match := re.fullmatch(pattern, text):
            name = match.group("target")
            if any(
                s in name for s in ("不", "别", "没", "如果", "说", "怎么", "能否", "可以", "他", "她", "先")
            ):
                return None, "", wake
            return action, name, wake
    return None, "", wake


def interaction_map_revision(settings):
    """Camera/config changes invalidate a captured focus even during an await."""
    value = [settings.room, settings.camera_id, settings.camera_fixed_view,
             settings.wake_mode, settings.attention_threshold, settings.max_age_seconds,
             settings.context_seconds, settings.mode,
             [d.model_dump(mode="json") for d in settings.devices]]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Controller:
    def __init__(self, settings: Settings, adapter, journal, *, allow_simulated_admission=False):
        self.settings, self.adapter, self.journal = settings, adapter, journal
        self.running = False
        self.generation = 0
        self.interactions = InteractionSessions()
        self.allow_simulated_admission = allow_simulated_admission
        self.recent: dict[tuple, float] = {}
        self.lock = asyncio.Lock()

    @property
    def focus(self):
        return self.interactions.latest

    @focus.setter
    def focus(self, value):
        if value is not None:
            raise ValueError("focus_requires_private_binding_and_domain_result")
        self.interactions.invalidate()

    def binding(self, obs, generation):
        binding = obs.interaction
        if not binding or binding.input_kind != "ambient" or (
            binding.room_id != self.settings.room or binding.camera_id != obs.camera_id
            or binding.session_id != obs.session or binding.generation != generation
        ):
            return None
        return replace(binding, map_revision=hashlib.sha256(
            (binding.map_revision + interaction_map_revision(self.settings)).encode()
        ).hexdigest())

    def current(self, obs, generation, fence, deadline):
        binding = self.binding(obs, generation)
        return bool(
            self.running and generation == self.generation and binding
            and binding.key == fence.key and self.interactions.current(fence)
            and time.monotonic() < deadline
            and -2 <= time.time() - obs.captured_at <= self.settings.max_age_seconds
            and (obs.is_current is None or obs.is_current())
            and attention_policy(
                evidence=obs.evidence, admission=obs.admission, binding=binding,
                explicit_command=True, wake=parse_command(obs.text)[2],
                wake_required=self.settings.wake_mode == "required",
                threshold=self.settings.attention_threshold, max_age_seconds=self.settings.max_age_seconds,
                allow_simulated=self.allow_simulated_admission,
            ).admitted
        )

    def start(self):
        self.generation += 1
        self.running = True
        self.focus = None
        self.journal.record("lifecycle", "started")

    def stop(self):
        self.running = False
        self.generation += 1
        self.focus = None
        self.journal.record("lifecycle", "stopped")

    def resolve(self, name: str, obs: Observation, resolution=None) -> tuple[Device | None, str]:
        devices = [d for d in self.settings.devices if d.room == self.settings.room]
        generic = name in ("", "灯", "这个", "这个灯", "那个", "那个灯", "它")
        if not generic:
            candidates = [d for d in devices if name in [d.name, *d.aliases]]
            return (candidates[0], "named") if len(candidates) == 1 else (None, "unknown_or_ambiguous_name")
        ev = obs.evidence
        if ev.pointing:
            if not obs.admission or not obs.admission.pointing_admitted:
                return None, "pointing_producer_unavailable"
            if self.settings.mode == "live" and not self.settings.camera_fixed_view:
                return None, "camera_view_not_fixed"
            candidates = [
                d
                for d in devices
                if d.id == ev.target_id and any(a.camera_id == obs.camera_id for a in d.anchors)
            ]
            if len(candidates) == 1 and ev.target_confidence >= self.settings.attention_threshold:
                return candidates[0], "pointed"
            return None, "pointing_ambiguous"
        if resolution and resolution.target_id and name != "灯":
            match = next((d for d in devices if d.id == resolution.target_id), None)
            if match:
                return match, "context"
        if len(devices) == 1 and name in ("灯", ""):
            return devices[0], "only_light"
        return None, "target_ambiguous"

    async def process(self, obs: Observation, generation: int) -> Result:
        started = time.monotonic()
        result = Result(event_id=obs.event_id, status="ignored", reason="unsupported_command")
        async with self.lock:
            if not self.journal.reserve(obs.event_id):
                return Result(event_id=obs.event_id, status="ignored", reason="duplicate_event")
            try:
                await self._process(obs, generation, result)
            except Exception as exc:
                if result.status != "unverified":
                    result.status, result.reason = "error", "adapter_failure"
                self.focus = None
                self.journal.record("adapter", "failure", obs.event_id, error_type=type(exc).__name__)
            result.latency_ms = round((time.monotonic() - started) * 1000)
            self.journal.finish(result)
        return result

    async def _process(self, obs: Observation, generation: int, result: Result):
        if not self.running or generation != self.generation:
            result.reason = "paused"
            return
        age = time.time() - obs.captured_at
        if age < -2 or age > self.settings.max_age_seconds or obs.camera_id != self.settings.camera_id:
            result.reason = "stale_or_wrong_camera"
            return
        action, name, wake = parse_command(obs.text)
        if not action:
            return
        binding = self.binding(obs, generation)
        resolved = self.interactions.resolve(binding)
        attention = attention_policy(
            evidence=obs.evidence, admission=obs.admission, binding=binding,
            explicit_command=True, wake=wake, wake_required=self.settings.wake_mode == "required",
            threshold=self.settings.attention_threshold, max_age_seconds=self.settings.max_age_seconds,
            allow_simulated=self.allow_simulated_admission,
        )
        result.producer_state = attention.producer_state
        if not attention.admitted or not resolved.fence:
            if binding:
                self.interactions.invalidate(slot=binding.slot)
            result.reason = attention.code
            return
        fence = resolved.fence
        deadline = min(time.monotonic() + self.settings.context_seconds, fence.deadline_mono)
        device, resolution = self.resolve(name, obs, resolved)
        if resolution == "context":
            deadline = min(deadline, resolved.expires_mono)
        if device is None:
            result.status, result.reason = "clarify", resolution
            self.focus = None
            return
        result.device_id, result.action = device.id, action
        key = (obs.camera_id, device.id, action)
        now = time.monotonic()
        self.recent = {k: v for k, v in self.recent.items() if now - v < 2}
        if key in self.recent:
            result.reason = "cooldown"
            return
        state = await self.adapter.read(device)
        if not self.current(obs, generation, fence, deadline):
            result.reason = "cancelled_before_dispatch"
            self.interactions.invalidate(slot=fence.slot)
            return
        if action in ("dim", "brighten"):
            if (not state.get("on") or type(state.get("brightness")) is not int
                    or not 0 <= state["brightness"] <= 100):
                result.status, result.reason = "clarify", "brightness_unavailable_or_light_off"
                return
            value = max(1, min(100, state["brightness"] + (-20 if action == "dim" else 20)))
            prop = "brightness"
        else:
            value, prop = action == "on", "on"
        result.value = value
        if not self.current(obs, generation, fence, deadline):
            result.reason = "cancelled_before_dispatch"
            self.interactions.invalidate(slot=fence.slot)
            return
        if state.get(prop) == value:
            result.status, result.reason = "unchanged", "already_at_target"
        else:
            self.recent[key] = now
            self.journal.record("action", "dispatch", obs.event_id, device_id=device.id, action=action)
            result.status, result.reason = "unverified", "dispatch_result_unknown"
            await self.adapter.write(device, prop, value)
            result.status, result.reason = "unverified", "state_not_confirmed"
            for attempt in range(4):
                if attempt:
                    await asyncio.sleep(0.35)
                if not self.current(obs, generation, fence, deadline):
                    break
                observed = await self.adapter.read(device)
                if not self.current(obs, generation, fence, deadline):
                    break
                actual = observed.get(prop)
                if actual is not None and (actual == value if prop == "on" else abs(actual - value) <= 2):
                    result.status = "simulated" if self.settings.mode == "demo" else "verified"
                    result.reason = "state_confirmed"
                    break
        if self.current(obs, generation, fence, deadline):
            self.interactions.commit(
                fence, device.id, status=result.status, ttl_seconds=self.settings.context_seconds
            )
        else:
            self.interactions.invalidate(slot=fence.slot)
