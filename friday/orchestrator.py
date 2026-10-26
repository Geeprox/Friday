"""Whole-home planning and execution. All model output crosses a deterministic boundary."""

import asyncio
import hashlib
import math
import operator
import re
import time
from dataclasses import dataclass, replace
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from homeplane.attention_policy import attention_policy
from homeplane.arl import current_model_task, model_task
from homeplane.interaction_contracts import AttentionAdmission, InteractionBinding
from homeplane.interaction_sessions import FocusFence, InteractionSessions

from .core import interaction_map_revision, parse_command
from .home import Action, validate_action


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str
    attribute: str | None = None
    operation: Literal["eq", "ne", "gt", "ge", "lt", "le"]
    value: str | float | bool


class Step(Action):
    conditions: list[Condition] = Field(default_factory=list, max_length=8)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=500)
    clarification: str | None = Field(default=None, max_length=500)
    steps: list[Step] = Field(default_factory=list, max_length=64)


def household_model_class(text, entities):
    """Priority intent for an explicit finite supported household command, never action authority."""
    action, name, _ = parse_command(text)
    matched = [entity for entity in entities.values() if entity.name == name]
    if len(matched) != 1:
        return "interactive"
    entity = matched[0]
    if entity.state in {"unknown", "unavailable"}:
        return "interactive"
    if action in {"on", "off"} and ("turn_on" if action == "on" else "turn_off") in entity.capabilities:
        return "realtime"
    if action in {"dim", "brighten"} and entity.domain == "light" and entity.state == "on" and "turn_on" in entity.capabilities:
        return "realtime"
    return "interactive"


@dataclass
class Pending:
    id: str
    plan: Plan
    snapshots: dict
    generation: int
    expires: float
    event_id: str
    interaction: InteractionBinding | None = None
    fence: FocusFence | None = None
    is_current: Callable[[], bool] | None = None
    deadline_mono: float | None = None
    attention_current: Callable[[], bool] | None = None


def condition_matches(condition, snapshot):
    value = (
        snapshot["state"] if condition.attribute is None else snapshot["attributes"].get(condition.attribute)
    )
    if value is None or value in ("unknown", "unavailable"):
        return False
    expected = condition.value
    if isinstance(expected, (float, int)) and not isinstance(expected, bool):
        try:
            value = float(value)
            if not math.isfinite(value) or not math.isfinite(expected):
                return False
        except (ValueError, TypeError):
            return False
    try:
        return {
            "eq": operator.eq,
            "ne": operator.ne,
            "gt": operator.gt,
            "ge": operator.ge,
            "lt": operator.lt,
            "le": operator.le,
        }[condition.operation](value, expected)
    except TypeError:
        return False


def expected_state(action):
    """Verification derived from capability, never from the planner's success claim."""
    service, params = action.service, action.parameters
    expected = {}
    if service in ("turn_on", "turn_off") and not action.entity_id.startswith(("scene.", "script.")):
        expected["state"] = "on" if service == "turn_on" else "off"
        if action.entity_id.startswith("media_player.") and service == "turn_on":
            expected["state"] = ["on", "idle", "playing", "paused", "buffering"]
    if service in ("lock", "unlock"):
        expected["state"] = "locked" if service == "lock" else "unlocked"
    if service == "open_cover":
        expected["state"] = "open"
    if service == "close_cover":
        expected["state"] = "closed"
    if service == "media_play":
        expected["state"] = "playing"
    if service == "media_pause":
        expected["state"] = "paused"
    if service == "start" and action.entity_id.startswith("vacuum."):
        expected["state"] = "cleaning"
    if service == "pause" and action.entity_id.startswith("vacuum."):
        expected["state"] = "paused"
    for key, value in params.items():
        mapped = {
            "brightness_pct": "brightness",
            "position": "current_position",
            "hvac_mode": "state",
            "option": "state",
            "value": "state",
        }.get(key, key)
        expected[mapped] = round(value / 100 * 255) if key == "brightness_pct" else value
    return expected


def state_matches(expected, observed):
    if not expected:
        return False
    for key, target in expected.items():
        actual = observed["state"] if key == "state" else observed["attributes"].get(key)
        if isinstance(target, (float, int)) and not isinstance(target, bool):
            try:
                if not math.isfinite(float(actual)) or not math.isfinite(target):
                    return False
                if abs(float(actual) - target) > (
                    3 if key == "brightness" else 0.5 if key == "current_position" else 0.05
                ):
                    return False
            except (ValueError, TypeError):
                return False
        elif isinstance(target, list):
            if actual not in target:
                return False
        elif actual != target:
            return False
    return True


class Orchestrator:
    def __init__(self, settings, home, planner, decision, lifecycle, journal, *,
                 allow_simulated_admission=False):
        self.settings, self.home, self.planner, self.decision = settings, home, planner, decision
        self.lifecycle, self.journal = lifecycle, journal
        self.lock = asyncio.Lock()
        self.pending = {}
        self.interactions = InteractionSessions()
        self.allow_simulated_admission = allow_simulated_admission
        self.latest = None
        self.recent = {}

    @property
    def focus(self):
        return self.interactions.latest

    @focus.setter
    def focus(self, value):
        if value is not None:
            raise ValueError("focus_requires_private_binding_and_domain_result")
        self.interactions.invalidate()

    def binding(self, binding, generation, camera_id, session):
        if binding is None:
            return None
        if (binding.generation != generation or binding.camera_id != camera_id
                or binding.session_id != session or binding.room_id != self.settings.room):
            return None
        return replace(binding, map_revision=hashlib.sha256(
            (binding.map_revision + interaction_map_revision(self.settings)).encode()
        ).hexdigest())

    def current(self, pending):
        if not self.alive(pending.generation) or time.time() >= pending.expires:
            return False
        if pending.is_current is not None and not pending.is_current():
            return False
        if pending.attention_current is not None and not pending.attention_current():
            return False
        if pending.deadline_mono is not None and time.monotonic() >= pending.deadline_mono:
            return False
        if pending.interaction is None:
            return True  # Legacy text has no reusable focus, identity or new producer qualification.
        binding = self.binding(pending.interaction, pending.generation,
                               pending.interaction.camera_id, pending.interaction.session_id)
        return bool(binding and pending.fence and binding.key == pending.fence.key
                    and self.interactions.current(pending.fence))

    def cancel(self):
        self.focus = None
        self.pending.clear()
        self.recent.clear()

    def alive(self, generation):
        return self.lifecycle.running and generation == self.lifecycle.generation

    def fast_plan(self, text, camera_id, session, evidence, resolution=None, admission=None):
        action, name, _ = parse_command(text)
        if not action:
            return None
        entities = list(self.home.entities.values())
        generic = name in ("", "这个", "那个", "它", "灯", "这个灯", "那个灯")
        candidates = []
        if not generic:
            mapped = [d.entity_id for d in self.settings.devices if name in (d.name, *d.aliases)]
            candidates = [e for e in entities if e.name == name or e.id in mapped]
        elif evidence and evidence.pointing:
            if not admission or not admission.pointing_admitted:
                return Plan(summary="指向来源不可用", clarification="请明确选择设备或说出设备名称。")
            if (
                not self.settings.camera_fixed_view
                or evidence.target_confidence < self.settings.attention_threshold
            ):
                return Plan(
                    summary="指向不明确", clarification="请先固定摄像头视角并绑定设备，或说出设备名称。"
                )
            ids = [
                d.entity_id
                for d in self.settings.devices
                if d.id == evidence.target_id and any(a.camera_id == camera_id for a in d.anchors)
            ]
            candidates = [e for e in entities if e.id in ids]
        elif resolution and resolution.target_id:
            candidates = [e for e in entities if e.id == resolution.target_id
                          and e.room == self.settings.room]
        if len(candidates) != 1:
            return None
        entity = candidates[0]
        if action in ("on", "off"):
            service = "turn_on" if action == "on" else "turn_off"
            if service not in entity.capabilities:
                return None
            return Plan(
                summary=f"{'开启' if action == 'on' else '关闭'}{entity.name}",
                steps=[Step(entity_id=entity.id, service=service)],
            )
        if (
            entity.domain == "light"
            and entity.state == "on"
            and entity.attributes.get("brightness") is not None
        ):
            value = max(
                1,
                min(
                    100,
                    round(entity.attributes["brightness"] / 255 * 100)
                    + (20 if action == "brighten" else -20),
                ),
            )
            return Plan(
                summary=f"调整{entity.name}亮度至 {value}%",
                steps=[Step(entity_id=entity.id, service="turn_on", parameters={"brightness_pct": value})],
            )
        return None

    async def handle(
        self, text, event_id, generation, *, camera_id="text", session="text", evidence=None,
        captured_at=None, interaction: InteractionBinding | None = None,
        admission: AttentionAdmission | None = None, is_current=None,
    ):
        captured = time.time() if captured_at is None else captured_at
        deadline = captured + self.settings.model_timeout_seconds
        if evidence is not None:
            deadline = min(deadline, captured + self.settings.max_age_seconds,
                           admission.expires_at if isinstance(admission, AttentionAdmission) else captured)
        model_deadline = time.monotonic() + (deadline - time.time())
        model_deadline_at_ms = math.floor(deadline * 1000) if math.isfinite(deadline) else 0
        parent_model_task = current_model_task()
        if parent_model_task:
            model_deadline = min(model_deadline, parent_model_task.deadline)
            model_deadline_at_ms = min(model_deadline_at_ms, parent_model_task.deadline_at_ms)

        async with self.lock:
            if not self.journal.reserve(event_id):
                return {"status": "ignored", "reason": "重复事件已忽略", "event_id": event_id}
            if not self.alive(generation):
                return self.result(event_id, "ignored", "监听已暂停")
            bound = self.binding(interaction, generation, camera_id, session)
            resolved = self.interactions.resolve(bound)
            if evidence is not None:
                action, _, wake = parse_command(text)
                attention = attention_policy(
                    evidence=evidence, admission=admission, binding=bound,
                    explicit_command=bool(action), wake=wake,
                    wake_required=self.settings.wake_mode == "required",
                    threshold=self.settings.attention_threshold,
                    max_age_seconds=self.settings.max_age_seconds,
                    allow_simulated=self.allow_simulated_admission,
                )
                if not attention.admitted or not resolved.fence:
                    self.focus = None
                    return self.result(event_id, "ignored", attention.code)
            elif interaction and (not bound or bound.input_kind != "console_text"):
                self.focus = None
                return self.result(event_id, "ignored", "可信输入来源不可用")
            digest = hashlib.sha256((camera_id + session + text.strip()).encode()).hexdigest()
            now = time.monotonic()
            self.recent = {key: ts for key, ts in self.recent.items() if now - ts < 2}
            if digest in self.recent:
                return self.result(event_id, "ignored", "重复触发已忽略")
            self.recent[digest] = now
            try:
                await self.home.discover()
                self.journal.record("pipeline", "inventory_ready", event_id)
                requested_model_kind = parent_model_task.task_kind if parent_model_task else "household.response"
                requested_model_class = parent_model_task.requested_class if parent_model_task else household_model_class(text, self.home.entities)
                with model_task(requested_model_kind, requested_model_class, deadline=model_deadline,
                        deadline_at_ms=model_deadline_at_ms,
                        current=lambda: self.alive(generation) and (is_current is None or is_current())
                            and (parent_model_task is None or parent_model_task.current is None or parent_model_task.current())):
                    route = await self.decision.classify(text)
                self.journal.record("pipeline", "route_" + route["label"], event_id)
                if route["label"] == "ignore" and route["confidence"] >= 0.9:
                    return self.result(event_id, "ignored", "识别为日常对话或非控制请求")
                plan = self.fast_plan(text, camera_id, session, evidence, resolved, admission)
                brightness_state = None
                command, _, _ = parse_command(text)
                if plan and plan.steps and command in ("brighten", "dim"):
                    target = plan.steps[0].entity_id
                    brightness_state = await self.home.read(target)
                    brightness = brightness_state["attributes"].get("brightness")
                    if (brightness_state["state"] != "on" or type(brightness) is not int
                            or not 0 <= brightness <= 255):
                        return self.result(event_id, "clarify", "当前合格亮度不可用，请明确选择绝对值。")
                    value = max(1, min(100, round(brightness / 255 * 100)
                                       + (20 if command == "brighten" else -20)))
                    plan.steps[0].parameters = {"brightness_pct": value}
                    plan.summary = f"调整亮度至 {value}%"
                if plan and plan.clarification:
                    return self.result(event_id, "clarify", plan.clarification)
                pointed = None
                if (
                    evidence and admission and admission.pointing_admitted
                    and evidence.pointing
                    and self.settings.camera_fixed_view
                    and evidence.target_confidence >= self.settings.attention_threshold
                ):
                    pointed = next(
                        (
                            d.entity_id
                            for d in self.settings.devices
                            if d.id == evidence.target_id and any(a.camera_id == camera_id for a in d.anchors)
                        ),
                        None,
                    )
                focus = resolved.target_id
                if re.search(r"这个|那个|它", text) and not (pointed or focus):
                    return self.result(
                        event_id, "clarify", "还不能确定你指的设备，请说出名称或先绑定画面位置。"
                    )
                if plan is None or route["label"] != "simple" or route["confidence"] < 0.9:
                    catalog = [
                        e.model_dump()
                        for e in self.home.entities.values()
                        if e.capabilities
                        or (
                            e.domain in ("sensor", "binary_sensor")
                            and (e.room == self.settings.room or e.name in text)
                        )
                    ]
                    context = {
                        "request": text,
                        "camera_room": self.settings.room,
                        "devices": catalog,
                        "pointing_target": pointed,
                        "previous_target": focus,
                    }
                    self.journal.record("pipeline", "planning", event_id)
                    with model_task(requested_model_kind, requested_model_class, deadline=model_deadline,
                            deadline_at_ms=model_deadline_at_ms,
                            current=lambda: self.alive(generation) and (is_current is None or is_current())
                                and (parent_model_task is None or parent_model_task.current is None or parent_model_task.current())):
                        plan = Plan.model_validate(await self.planner.plan(context, Plan.model_json_schema()))
                if not self.alive(generation):
                    return self.result(event_id, "ignored", "规划期间已暂停，未执行")
                if plan.clarification or not plan.steps:
                    self.focus = None
                    return self.result(event_id, "clarify", plan.clarification or plan.summary)
                confirm, ids = False, set()
                for step in plan.steps:
                    confirm = validate_action(step, self.home.entities) or confirm
                    ids.add(step.entity_id)
                    for condition in step.conditions:
                        if condition.entity_id not in self.home.entities:
                            raise ValueError("Unknown condition entity")
                        if (
                            condition.attribute
                            and condition.attribute not in self.home.entities[condition.entity_id].attributes
                        ):
                            raise ValueError("Unknown condition attribute")
                        ids.add(condition.entity_id)
                snapshots = dict(
                    zip(ids, await asyncio.gather(*(self.home.read(i) for i in ids)), strict=True)
                )
                if brightness_state is not None and plan.steps:
                    # CAS compares against the same read used to derive the absolute target.
                    snapshots[plan.steps[0].entity_id] = brightness_state
                if any(
                    snapshots[step.entity_id]["state"] in ("unavailable", "unknown") for step in plan.steps
                ):
                    return self.result(event_id, "error", "计划中包含不可用设备，未执行")
                if is_current is not None and not is_current():
                    self.focus = None
                    return self.result(event_id, "cancelled", "输入来源已变化，未执行")
                pending = Pending(
                    str(uuid4()),
                    plan,
                    snapshots,
                    generation,
                    min(deadline, time.time() + self.settings.plan_ttl_seconds),
                    event_id,
                    interaction, resolved.fence, is_current,
                    min(resolved.fence.deadline_mono,
                        resolved.expires_mono if focus else float("inf")) if resolved.fence else None,
                    (lambda: attention_policy(
                        evidence=evidence, admission=admission,
                        binding=self.binding(interaction, generation, camera_id, session),
                        explicit_command=bool(parse_command(text)[0]), wake=parse_command(text)[2],
                        wake_required=self.settings.wake_mode == "required",
                        threshold=self.settings.attention_threshold,
                        max_age_seconds=self.settings.max_age_seconds,
                        allow_simulated=self.allow_simulated_admission,
                    ).admitted) if evidence is not None else None,
                )
                if not self.current(pending):
                    self.focus = None
                    return self.result(event_id, "cancelled", "输入来源或计划已失效，未执行")
                if confirm:
                    self.pending = {
                        key: value for key, value in self.pending.items() if value.expires > time.time()
                    }
                    self.pending[pending.id] = pending
                    return self.result(
                        event_id,
                        "confirmation_required",
                        plan.summary,
                        plan_id=pending.id,
                        steps=[step.model_dump() for step in plan.steps],
                    )
                result = await self.execute(pending)
                if (self.current(pending) and len(plan.steps) == 1 and resolved.fence
                        and self.home.entities[plan.steps[0].entity_id].room == self.settings.room):
                    self.interactions.commit(
                        resolved.fence, plan.steps[0].entity_id, status=result["status"],
                        ttl_seconds=self.settings.context_seconds,
                    )
                elif resolved.fence:
                    self.interactions.invalidate(slot=resolved.fence.slot)
                return result
            except Exception as exc:
                self.focus = None
                self.journal.record("pipeline", "failure", event_id, error_type=type(exc).__name__)
                return self.result(event_id, "error", "接入或规划失败，请查看服务状态与诊断")

    def result(self, event_id, status, reason, **details):
        from .core import Result

        self.journal.finish(Result(event_id=event_id, status=status, reason="plan_" + status))
        self.latest = {"event_id": event_id, "status": status, "reason": reason, **details}
        return self.latest

    async def confirm(self, plan_id):
        async with self.lock:
            pending = self.pending.pop(plan_id, None)
            if not pending:
                raise ValueError("计划不存在或已处理")
            try:
                return await self.execute(pending)
            except Exception as exc:
                self.journal.record("pipeline", "failure", pending.event_id, error_type=type(exc).__name__)
                return self.result(pending.event_id, "error", "状态读取失败，未继续执行")

    async def execute(self, pending):
        outcomes = []
        targets = {step.entity_id for step in pending.plan.steps}
        # Compare before any write. Live sensor conditions are re-evaluated separately.
        for eid in targets:
            if not self.current(pending):
                return self.result(pending.event_id, "cancelled", "输入来源或计划已失效，未执行")
            current = await self.home.read(eid)
            if not self.current(pending):
                return self.result(pending.event_id, "cancelled", "输入来源或计划已失效，未执行")
            old = pending.snapshots[eid]
            relevant = {
                "brightness",
                "temperature",
                "fan_mode",
                "percentage",
                "current_position",
                "volume_level",
                "humidity",
                "preset_mode",
            }
            if current["state"] != old["state"] or any(
                current["attributes"].get(k) != old["attributes"].get(k) for k in relevant
            ):
                return self.result(pending.event_id, "cancelled", "设备状态已变化，请重新发出指令")
        for step in pending.plan.steps:
            if not self.current(pending):
                return self.result(
                    pending.event_id,
                    "partial" if outcomes else "cancelled",
                    "已暂停或计划过期",
                    steps=outcomes,
                )
            conditions = []
            for condition in step.conditions:
                observed = await self.home.read(condition.entity_id)
                if not self.current(pending):
                    return self.result(
                        pending.event_id, "partial" if outcomes else "cancelled",
                        "输入来源或计划已失效，未继续执行", steps=outcomes,
                    )
                conditions.append(condition_matches(condition, observed))
            if not all(conditions):
                outcomes.append({"entity_id": step.entity_id, "status": "skipped", "reason": "条件不满足"})
                continue
            expectation = expected_state(step)
            before = await self.home.read(step.entity_id)
            if not self.current(pending):
                return self.result(
                    pending.event_id, "partial" if outcomes else "cancelled",
                    "输入来源或计划已失效，未继续执行", steps=outcomes,
                )
            if before["state"] in ("unknown", "unavailable"):
                return self.result(
                    pending.event_id, "partial" if outcomes else "error", "设备已不可用", steps=outcomes
                )
            if state_matches(expectation, before):
                outcomes.append({"entity_id": step.entity_id, "status": "unchanged"})
                continue
            if not self.current(pending):
                return self.result(
                    pending.event_id, "partial" if outcomes else "cancelled", "已暂停", steps=outcomes
                )
            self.journal.record(
                "action", "dispatch", pending.event_id, device_id=step.entity_id, action=step.service
            )
            try:
                await self.home.execute(step)
                verified = False
                for attempt in range(8):
                    if not self.current(pending):
                        break
                    if attempt:
                        await asyncio.sleep(0.5)
                        if not self.current(pending):
                            break
                    observed = await self.home.read(step.entity_id)
                    if not self.current(pending):
                        break
                    if state_matches(expectation, observed):
                        verified = True
                        break
                outcomes.append(
                    {
                        "entity_id": step.entity_id,
                        "service": step.service,
                        "status": "verified" if verified else "unverified",
                    }
                )
                self.journal.record(
                    "action", outcomes[-1]["status"], pending.event_id, device_id=step.entity_id
                )
                if not verified:
                    return self.result(
                        pending.event_id,
                        "partial" if len(outcomes) > 1 else "unverified",
                        "设备状态尚未确认，后续步骤已停止",
                        steps=outcomes,
                    )
            except Exception as exc:
                outcomes.append({"entity_id": step.entity_id, "status": "unverified"})
                self.journal.record("action", "unknown", pending.event_id, error_type=type(exc).__name__)
                return self.result(
                    pending.event_id,
                    "partial" if len(outcomes) > 1 else "unverified",
                    "连接中断，当前动作结果未知；后续步骤已停止",
                    steps=outcomes,
                )
        return self.result(
            pending.event_id,
            "verified" if any(o["status"] in ("verified", "unchanged") for o in outcomes) else "skipped",
            pending.plan.summary,
            steps=outcomes,
        )
