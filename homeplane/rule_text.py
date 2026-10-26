"""Scoped text -> restricted mutation -> existing review/Saga. Never a raw writer.

The shipped parser is a deterministic removal baseline, not a language model.
Other intentions abstain until ARL advertises a qualified structured planner.
Raw text is not persisted or transmitted to a model by this module.
"""

import asyncio
import re
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .contracts import Contract
from .rule_compiler import digest
from .rule_contracts import TransitionTrigger
from .rule_deployment import RemoveRuleAction, RuleDeployment
from .simulator import Simulator


class RuleTextRequest(Contract):
    text: str = Field(min_length=1, max_length=1024)
    expected_context: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)
    expires_at: AwareDatetime
    record_id: str | None = Field(default=None, min_length=1, max_length=192)
    action_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bounded_choice(self):
        if len(self.text.encode()) > 4096 or any(ord(c) < 32 and c not in "\n\t" for c in self.text):
            raise ValueError("invalid_text_budget")
        if any(
            v is not None for v in (self.record_id, self.action_id, self.expected_fingerprint)
        ) and not all(v is not None for v in (self.record_id, self.action_id, self.expected_fingerprint)):
            raise ValueError("complete_rule_choice_required")
        return self


class RuleTextChoice(Contract):
    record_id: str
    action_id: str
    expected_fingerprint: str
    rule_label: str
    action_label: str
    retained_actions: list[str]


class RuleTextContext(Contract):
    fingerprint: str
    available: bool
    simulated: bool
    supported_operation: str = "remove_one_enabled_light_or_climate_action"
    planner: str = "deterministic_baseline"
    model_inference_attempted: bool = False
    coverage: list[str]


class RuleTextResult(Contract):
    status: Literal["planned", "clarification", "unsupported", "expired", "rejected"]
    code: str
    context: RuleTextContext
    choices: list[RuleTextChoice] = Field(default_factory=list, max_length=16)
    plan: RuleDeployment | None = None
    planner: str = "deterministic_baseline"
    model_inference_attempted: Literal[False] = False
    real_write_attempted: Literal[False] = False


def parse_removal(text):
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).rstrip("。.")
    # Full match: compound requests, questions, qualifications and instructions
    # cannot be silently dropped to get an apparently successful narrower edit.
    found = re.fullmatch(
        r"(?:以后|今后|从现在起)?(?:我)?(?P<scope>回家|到家|进门|这个习惯)(?:时|后)?(?:可以)?(?:不再|不要|不|别)(?:自动)?(?:打开|开启|开)(?P<target>空调|玄关灯|主灯|灯)(?:了)?",
        text,
    )
    return found.groupdict() if found else None


class RuleTextChanges:
    def __init__(self, manager, runtime):
        self.manager, self.runtime = manager, runtime
        self.lock = asyncio.Lock()
        with manager.store.connection:
            manager.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS rule_text_requests (key TEXT PRIMARY KEY, digest TEXT, context TEXT, created TEXT, body TEXT)"
            )

    def context(self):
        adapter = self.manager.core.adapter
        available = self.manager.owned and isinstance(adapter, Simulator)
        records = self.manager.backend.records() if available else []
        return RuleTextContext(
            fingerprint=digest(
                [
                    self.manager.boot_id,
                    getattr(adapter, "boot_id", None),
                    type(adapter).__name__,
                    getattr(adapter, "instance_id", None),
                    getattr(adapter, "url", None),
                    digest(adapter.fixture) if isinstance(adapter, Simulator) else None,
                    [(r.id, r.fingerprint) for r in records],
                ]
            ),
            available=available,
            simulated=isinstance(adapter, Simulator),
            coverage=[
                "Only project-owned normalized rules are editable.",
                "External Xiaomi/Geek sources can be invisible/opaque; a change is not a guarantee that AC can never turn on.",
                "Native HA/Geek rule deployment and ARL Codex structured planning remain unsupported.",
            ],
        )

    def _result(self, context, status, code, **extra):
        return RuleTextResult(context=context, status=status, code=code, **extra)

    async def plan(self, request):
        async with self.lock:
            context = self.context()
            if request.expected_context != context.fingerprint:
                return self._result(context, "rejected", "rule_context_changed")
            now = datetime.now(UTC)
            remaining = (request.expires_at - now).total_seconds()
            if not 0 < remaining <= 60:
                return self._result(
                    context,
                    "expired" if remaining <= 0 else "rejected",
                    "text_request_expired" if remaining <= 0 else "text_ttl_unsupported",
                )
            deadline = time.monotonic() + remaining
            db = self.manager.store.connection
            # Deletion is opportunistic retention of our own diagnostic receipts,
            # never old external rules, recordings or household media.
            with db:
                db.execute(
                    "DELETE FROM rule_text_requests WHERE created < ?",
                    ((now - timedelta(days=7)).isoformat(),),
                )
            old = db.execute(
                "SELECT digest,context,body FROM rule_text_requests WHERE key=?", (request.idempotency_key,)
            ).fetchone()
            if old:
                if old[0] != digest(request) or old[1] != context.fingerprint:
                    return self._result(context, "rejected", "text_request_conflict")
                receipt = RuleTextResult.model_validate_json(old[2])
                if receipt.plan and (
                    receipt.plan.boot_id != self.manager.boot_id
                    or datetime.now(UTC) >= receipt.plan.expires_at
                    or time.monotonic() >= self.manager.deadlines.get(receipt.plan.id, 0)
                ):
                    return self._result(context, "expired", "text_plan_expired")
                return receipt
            if not context.available:
                return self._result(context, "unsupported", "native_rule_source_unavailable")
            if db.execute("SELECT count(*) FROM rule_text_requests").fetchone()[0] >= 256:
                return self._result(context, "rejected", "text_request_capacity")
            intent = parse_removal(request.text)
            if not intent:
                # No prompts/inference/paid fallback: the currently advertised
                # completion primitive is not a verified household plan contract.
                return self._result(context, "unsupported", "structured_planner_unavailable")
            snapshot = await self.manager.core.snapshot()
            if time.monotonic() >= deadline or datetime.now(UTC) >= request.expires_at:
                return self._result(self.context(), "expired", "text_request_expired")
            if self.context().fingerprint != context.fingerprint:
                return self._result(self.context(), "rejected", "rule_context_changed")
            names = {e.id: e.name for e in snapshot.entities}
            aliases = {"Hall light": "玄关灯", "Living air conditioner": "客厅空调"}

            def label(action):
                name = aliases.get(
                    names.get(getattr(action, "target", None), ""),
                    names.get(getattr(action, "target", None), ""),
                )
                return ("打开 " if getattr(action, "power", None) == "on" else "保留 ") + (
                    name or action.type
                )

            choices = []
            for record in self.manager.backend.records():
                if not record.enabled:
                    continue
                trigger = record.rule.trigger
                if intent["scope"] != "这个习惯" and not (
                    isinstance(trigger, TransitionTrigger)
                    and trigger.capability == "contact"
                    and trigger.from_value == "closed"
                    and trigger.to_value == "open"
                ):
                    continue
                for action in record.rule.actions:
                    kind = "climate.set" if intent["target"] == "空调" else "light.set"
                    if action.type != kind or getattr(action, "power", None) != "on":
                        continue
                    if intent["target"] not in {"空调", "灯"} and intent["target"] not in label(action):
                        continue
                    choices.append(
                        RuleTextChoice(
                            record_id=record.id,
                            action_id=action.id,
                            expected_fingerprint=record.fingerprint,
                            rule_label=record.rule.intent,
                            action_label=label(action),
                            retained_actions=[label(a) for a in record.rule.actions if a.id != action.id],
                        )
                    )
                    if len(choices) > 16:
                        return self._result(context, "rejected", "text_scope_too_broad")
            if request.record_id is not None:
                choices = [
                    c
                    for c in choices
                    if c.record_id == request.record_id
                    and c.action_id == request.action_id
                    and c.expected_fingerprint == request.expected_fingerprint
                ]
            if not choices:
                result = self._result(context, "unsupported", "no_matching_managed_action")
            elif len(choices) > 1 or intent["scope"] == "这个习惯" and request.record_id is None:
                result = self._result(context, "clarification", "choose_exact_rule_action", choices=choices)
            elif not choices[0].retained_actions:
                result = self._result(
                    context, "unsupported", "whole_rule_disable_requires_separate_review", choices=choices
                )
            else:
                chosen = choices[0]
                try:
                    plan = await self.manager.plan_remove(
                        RemoveRuleAction(
                            record_id=chosen.record_id,
                            action_id=chosen.action_id,
                            expected_fingerprint=chosen.expected_fingerprint,
                            idempotency_key="text-" + digest(request),
                        ),
                        deadline=deadline,
                    )
                except ValueError as exc:
                    code = (
                        str(exc)
                        if str(exc) in {"source_conflict", "expired_request", "unsupported_compilation"}
                        else "rule_change_unavailable"
                    )
                    return self._result(
                        self.context(), "expired" if code == "expired_request" else "rejected", code
                    )
                result = self._result(
                    context, "planned", "review_diff_before_deployment", plan=plan, choices=choices
                )
            body = result.model_dump_json()
            if len(body.encode()) > 65536:
                return self._result(context, "rejected", "text_response_budget")
            with db:
                db.execute(
                    "INSERT INTO rule_text_requests VALUES (?,?,?,?,?)",
                    (request.idempotency_key, digest(request), context.fingerprint, now.isoformat(), body),
                )
            return result
