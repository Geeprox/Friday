"""Reviewed ARL candidates into existing local Plan/Diff, never an effect path.

The normal Console has no planning execution policy. A controlled composition
must authorize the exact immutable review; public metadata identity is not enough.
Raw input lives only in a bounded, expiring local review cache, not the journal.
"""

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from .arl import ARLError, safe_code
from .contracts import Contract
from .core_rules import CoreRulePlan, CoreRuleProposal
from .rule_compiler import digest, remove_instruction
from .rule_contracts import TransitionTrigger
from .rule_deployment import RemoveRuleAction, RuleDeployment
from .rule_text import RuleTextChoice, parse_removal
from .simulator import Simulator

MODEL = "gpt-6.1-sol"  # Exact upstream source candidate; not account availability.


class RuleAnalysisRequest(Contract):
    text: str = Field(min_length=1, max_length=1024)
    expected_context: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)
    expires_at: AwareDatetime
    record_ids: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def budget(self):
        if len(self.text.encode("utf8")) > 4096 or any(
            ord(c) < 32 and c not in "\n\t" or 127 <= ord(c) <= 159 for c in self.text
        ):
            raise ValueError("invalid_analysis_text")
        if len(set(self.record_ids)) != len(self.record_ids) or any(
            not 1 <= len(v) <= 192 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in v)
            for v in self.record_ids
        ):
            raise ValueError("invalid_analysis_scope")
        return self


class RuleAnalysisContext(Contract):
    fingerprint: str
    available: bool
    simulated: bool
    backend: Literal["simulator", "home_core", "unavailable"]
    execution_policy_configured: bool
    analysis_enabled: bool = False
    tool: Literal["friday.rule-removal.plan.v1"] = "friday.rule-removal.plan.v1"
    model: Literal["gpt-6.1-sol"] = MODEL
    model_inference_attempted: Literal[False] = False
    external_rules_intercepted: Literal[False] = False


class RuleAnalysisReview(Contract):
    id: str
    boot_id: str
    context: RuleAnalysisContext
    expires_at: AwareDatetime
    approval_digest: str
    request: dict
    choices: list[RuleTextChoice] = Field(max_length=16)
    model: Literal["gpt-6.1-sol"] = MODEL
    provider: Literal["codex_app_server"] = "codex_app_server"
    model_inference_attempted: Literal[False] = False
    real_write_attempted: Literal[False] = False
    data_destination: Literal["existing_codex_account_via_arl"] = "existing_codex_account_via_arl"


class RuleAnalysisApproval(Contract):
    review_id: str = Field(min_length=1, max_length=128)
    approval_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class RuleAnalysisResult(Contract):
    review_id: str
    status: Literal[
        "planned", "clarification", "unsupported", "rejected", "expired", "needs_review", "in_progress"
    ]
    code: str
    context: RuleAnalysisContext
    planner: Literal["arl_reviewed_removal_v1"] = "arl_reviewed_removal_v1"
    planner_call_attempted: bool = False
    model_inference_attempted: bool | None = False
    model_receipt: dict | None = None
    summary: str | None = Field(default=None, max_length=300)
    baseline_comparison: Literal["not_applicable", "matched", "disagreed", "ambiguous"] = "not_applicable"
    choices: list[RuleTextChoice] = Field(default_factory=list, max_length=16)
    plan: RuleDeployment | CoreRulePlan | None = None
    real_write_attempted: Literal[False] = False
    physically_confirmed: Literal[False] = False


class FingerprintProjection:
    """Only the event loop reads domain/SQLite state; the SDK worker reads this."""

    def __init__(self, value):
        self._value, self._lock = value, threading.Lock()

    def get(self):
        with self._lock:
            return self._value

    def invalidate(self):
        with self._lock:
            self._value = None


def planning_sdk():
    try:
        from arl.friday_planning import checked_proposal, rendered
        from arl.friday_planning_client import ReviewedRemoval, plan_removal
    except ImportError:
        raise ARLError("planning_sdk_contract_unavailable") from None
    return ReviewedRemoval, plan_removal, checked_proposal, rendered


class ARLRuleChanges:
    def __init__(self, text, core_rules, runtime, *, authorizer=None, planning_executor=None):
        self.text, self.core_rules, self.runtime = text, core_rules, runtime
        self.store = text.manager.store
        self.authorizer = authorizer  # Internal composition only; no HTTP enablement.
        # A trusted host can supply its sealed-process executor. This is never a
        # caller-selected transport, public enablement or actuator permission.
        if planning_executor is not None and not callable(planning_executor):
            raise ValueError("invalid_planning_executor")
        self.planning_executor = planning_executor
        self.boot_id = str(uuid4())
        self.reviews, self.deadlines, self.projections = {}, {}, {}
        self.lock, self.closed = asyncio.Lock(), False
        with self.store.connection:
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS arl_rule_reviews (id TEXT PRIMARY KEY, key TEXT UNIQUE, digest TEXT, created TEXT, status TEXT, body TEXT)"
            )
            # An interrupted/lost inference cannot be replayed on startup.
            self.store.connection.execute(
                "UPDATE arl_rule_reviews SET status='needs_review' WHERE status='analyzing'"
            )
        self.prune()

    def context(self):
        simulated = isinstance(self.text.manager.core.adapter, Simulator)
        source = self.text.context().model_dump(mode="json") if simulated else self.core_rules.context()
        return RuleAnalysisContext(
            fingerprint=digest([self.boot_id, source["fingerprint"]]),
            available=bool(source["available"] and not self.closed),
            simulated=simulated,
            backend="simulator" if simulated else "home_core" if source["available"] else "unavailable",
            execution_policy_configured=self.authorizer is not None,
            analysis_enabled=self.authorizer is not None,
        )

    def prune(self):
        now = datetime.now(UTC)
        for identifier, review in list(self.reviews.items()):
            if review.expires_at <= now or time.monotonic() >= self.deadlines[identifier]:
                self.reviews.pop(identifier, None)
                self.deadlines.pop(identifier, None)
                with self.store.connection:
                    self.store.connection.execute(
                        "UPDATE arl_rule_reviews SET status='expired' WHERE id=? AND status='prepared'",
                        (identifier,),
                    )
        with self.store.connection:
            self.store.connection.execute(
                "DELETE FROM arl_rule_reviews WHERE created < ? AND status IN ('prepared','expired','completed','rejected','cancelled')",
                ((now - timedelta(days=7)).isoformat(),),
            )

    def close(self):
        self.closed = True
        for projection in self.projections.values():
            projection.invalidate()
        self.reviews.clear()
        self.deadlines.clear()
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE arl_rule_reviews SET status='needs_review' WHERE status='analyzing'"
            )

    def _records(self):
        return self.text.manager.backend.records() if self.context().simulated else self.core_rules.records()

    def _choices(self, records, snapshot, selected):
        names = {e.id: e.name for e in snapshot.entities}
        if selected and set(selected) - {r.id for r in records if r.enabled}:
            raise ValueError("analysis_scope_unavailable")
        choices = []
        for record in records:
            if not record.enabled or selected and record.id not in selected:
                continue
            if len(record.rule.actions) < 2:
                continue  # Removing the last action is a separate whole-rule review.

            def label(action):
                parameters = getattr(action, "desired", None) or {
                    key: getattr(action, key)
                    for key in ("power", "brightness")
                    if getattr(action, key, None) is not None
                }
                return (
                    names.get(getattr(action, "target", None), action.type)
                    + " · "
                    + getattr(action, "operation", action.type)
                    + " "
                    + json.dumps(parameters, ensure_ascii=False, sort_keys=True)
                )

            for action in record.rule.actions:
                # Only enabled illumination/comfort effects are eligible, no locks,
                # scripts, feedback, delayed nodes or arbitrary mutation output.
                eligible = (
                    action.type in {"light.set", "climate.set"}
                    and getattr(action, "power", None) == "on"
                    or action.type == "device.set"
                    and (
                        action.operation == "light.set"
                        and action.desired.get("power") == "on"
                        or action.operation == "climate.mode"
                        and action.desired.get("hvac_mode") not in {None, "off"}
                    )
                )
                if not eligible:
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
                    raise ValueError("analysis_scope_too_broad")
        return choices

    async def prepare(self, request):
        async with self.lock:
            self.prune()
            context = self.context()
            if not context.available:
                raise ValueError("analysis_owned_source_unavailable")
            if request.expected_context != context.fingerprint:
                raise ValueError("analysis_context_changed")
            now = datetime.now(UTC)
            remaining = (request.expires_at - now).total_seconds()
            if not 0 < remaining <= 60:
                raise ValueError("analysis_request_expired")
            input_digest = digest(request)
            old = self.store.connection.execute(
                "SELECT id,digest,status FROM arl_rule_reviews WHERE key=?",
                (request.idempotency_key,),
            ).fetchone()
            if old:
                if old[1] != input_digest:
                    raise ValueError("analysis_request_conflict")
                if old[0] in self.reviews and old[2] == "prepared":
                    return self.reviews[old[0]].model_copy(deep=True)
                raise ValueError("analysis_request_not_resumable")
            if (
                len(self.reviews) >= 64
                or self.store.connection.execute("SELECT count(*) FROM arl_rule_reviews").fetchone()[0] >= 256
            ):
                raise ValueError("analysis_review_capacity")
            deadline = time.monotonic() + remaining
            snapshot = await self.text.manager.core.snapshot()
            if self.context().fingerprint != context.fingerprint:
                raise ValueError("analysis_context_changed")
            if time.monotonic() >= deadline or datetime.now(UTC) >= request.expires_at:
                raise ValueError("analysis_request_expired")
            choices = self._choices(self._records(), snapshot, request.record_ids)
            if not choices:
                raise ValueError("analysis_no_eligible_owned_action")
            identifier = str(uuid4())
            wire = {
                "schema_version": 1,
                "execution_id": identifier,
                "text": request.text,
                "context_fingerprint": context.fingerprint,
                "captured_at_ms": int(now.timestamp() * 1000),
                "expires_at_ms": int(request.expires_at.timestamp() * 1000),
                "choices": [
                    c.model_dump(
                        include={
                            "record_id",
                            "action_id",
                            "expected_fingerprint",
                            "rule_label",
                            "action_label",
                        }
                    )
                    for c in choices
                ],
            }
            ReviewedRemoval, _, _, render = planning_sdk()
            try:
                reviewed = ReviewedRemoval(wire, MODEL)
                render(reviewed.request)  # Upstream input budget; no inference or truncation.
            except Exception as exc:
                raise ARLError(safe_code(exc)) from None
            review = RuleAnalysisReview(
                id=identifier,
                boot_id=self.boot_id,
                context=context,
                expires_at=request.expires_at,
                approval_digest=digest([self.boot_id, context.backend, reviewed.digest, MODEL]),
                request=reviewed.request,
                choices=choices,
            )
            if len(review.model_dump_json().encode()) > 16384:
                raise ValueError("analysis_review_budget")
            # Persist identity/budget only. User sentence is not diagnostic data.
            metadata = {
                "boot_id": self.boot_id,
                "approval_digest": review.approval_digest,
                "context": context.fingerprint,
                "request_sha256": reviewed.digest,
                "expires_at": request.expires_at.isoformat(),
                "model": MODEL,
            }
            with self.store.connection:
                self.store.connection.execute(
                    "INSERT INTO arl_rule_reviews VALUES (?,?,?,?,?,?)",
                    (
                        identifier,
                        request.idempotency_key,
                        input_digest,
                        now.isoformat(),
                        "prepared",
                        json.dumps(metadata),
                    ),
                )
            self.reviews[identifier], self.deadlines[identifier] = review.model_copy(deep=True), deadline
            return review

    def _result(self, identifier, status, code, **extra):
        return RuleAnalysisResult(
            review_id=identifier, status=status, code=code, context=self.context(), **extra
        )

    def _save_result(self, result, status):
        if self.closed:
            return
        body = result.model_dump_json()
        if len(body.encode()) > 65536:
            raise ValueError("analysis_result_budget")
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE arl_rule_reviews SET status=?,body=? WHERE id=?", (status, body, result.review_id)
            )

    def _previous_attempt(self, identifier):
        row = self.store.connection.execute(
            "SELECT status,body FROM arl_rule_reviews WHERE id=?", (identifier,)
        ).fetchone()
        if not row or row[0] not in {"analyzing", "needs_review", "completed"}:
            return {"planner_call_attempted": False, "model_inference_attempted": False}
        try:
            saved = RuleAnalysisResult.model_validate_json(row[1])
            inferred = saved.model_inference_attempted
        except ValueError:
            inferred = None
        return {"planner_call_attempted": True, "model_inference_attempted": inferred}

    async def _watch(self, projection, fingerprint):
        while not self.closed:
            if self.context().fingerprint != fingerprint or not self.context().available:
                projection.invalidate()
                return
            await asyncio.sleep(0.05)

    def cancel(self, approval):
        review = self.reviews.get(approval.review_id)
        if not review or approval.approval_digest != review.approval_digest:
            return self._result(approval.review_id, "rejected", "analysis_approval_mismatch")
        row = self.store.connection.execute(
            "SELECT status FROM arl_rule_reviews WHERE id=?", (review.id,)
        ).fetchone()
        if row[0] not in {"prepared", "analyzing"}:
            return self._result(
                review.id, "rejected", "analysis_already_finished", **self._previous_attempt(review.id)
            )
        projection = self.projections.get(review.id)
        if projection:
            projection.invalidate()
        started = row[0] == "analyzing"
        result = self._result(
            review.id,
            "needs_review" if started else "rejected",
            "analysis_cancelled_no_replay" if started else "analysis_cancelled_before_call",
            planner_call_attempted=started,
            model_inference_attempted=None if started else False,
        )
        self._save_result(result, "needs_review" if started else "cancelled")
        return result

    def _baseline(self, review):
        intent = parse_removal(review.request["text"])
        if not intent or intent["scope"] == "这个习惯":
            return None
        records = {r.id: r for r in self._records()}
        matches = []
        for choice in review.choices:
            record = records[choice.record_id]
            trigger = record.rule.trigger
            if not (
                isinstance(trigger, TransitionTrigger)
                and trigger.capability == "contact"
                and trigger.from_value == "closed"
                and trigger.to_value == "open"
            ):
                continue  # Do not invent arrival semantics for arbitrary HA events.
            action = next(a for a in record.rule.actions if a.id == choice.action_id)
            wanted = "climate.set" if intent["target"] == "空调" else "light.set"
            if action.type != wanted:
                continue
            if intent["target"] not in {"空调", "灯"} and intent["target"] not in choice.action_label:
                return None  # Existing localized aliases are not expanded by a model.
            matches.append(choice)
        return matches or None

    async def analyze(self, approval):
        if self.lock.locked():
            return self._result(approval.review_id, "in_progress", "analysis_busy")
        async with self.lock:
            self.prune()
            saved_review = self.reviews.get(approval.review_id)
            review = saved_review.model_copy(deep=True) if saved_review else None
            if not review:
                previous = self._previous_attempt(approval.review_id)
                return self._result(
                    approval.review_id,
                    "needs_review" if previous["planner_call_attempted"] else "expired",
                    "analysis_review_not_resumable",
                    **previous,
                )
            if approval.approval_digest != review.approval_digest:
                return self._result(review.id, "rejected", "analysis_approval_mismatch")
            context = self.context()
            if not context.available or context.fingerprint != review.context.fingerprint:
                return self._result(review.id, "rejected", "analysis_context_changed")
            row = self.store.connection.execute(
                "SELECT status,body FROM arl_rule_reviews WHERE id=?", (review.id,)
            ).fetchone()
            if row[0] in {"completed", "cancelled"}:
                result = RuleAnalysisResult.model_validate_json(row[1])
                if result.plan and datetime.now(UTC) >= result.plan.expires_at:
                    return self._result(
                        review.id, "expired", "analysis_plan_expired", **self._previous_attempt(review.id)
                    )
                return result
            if row[0] != "prepared":
                return self._result(
                    review.id,
                    "needs_review",
                    "analysis_previous_attempt_not_replayed",
                    planner_call_attempted=True,
                    model_inference_attempted=None,
                )
            if self.authorizer is None:
                return self._result(review.id, "unsupported", "analysis_controlled_actor_not_authorized")
            ReviewedRemoval, plan_removal, checked_proposal, render = planning_sdk()
            reviewed = ReviewedRemoval(review.request, MODEL)
            if digest([self.boot_id, context.backend, reviewed.digest, MODEL]) != review.approval_digest:
                return self._result(review.id, "rejected", "analysis_review_changed")
            if self.authorizer(review.model_copy(deep=True)) is not True:
                return self._result(review.id, "unsupported", "analysis_controlled_actor_not_authorized")
            if self.context().fingerprint != context.fingerprint or not self.context().available:
                return self._result(review.id, "rejected", "analysis_context_changed")
            deadline = min(self.deadlines[review.id], time.monotonic() + 30)
            try:
                with self.store.connection:
                    claimed = self.store.connection.execute(
                        "UPDATE arl_rule_reviews SET status='analyzing' WHERE id=? AND status='prepared'",
                        (review.id,),
                    )
                    if claimed.rowcount != 1:
                        raise ValueError("analysis_claim_conflict")
            except sqlite3.Error:
                return self._result(review.id, "rejected", "analysis_journal_unavailable")
            projection = FingerprintProjection(context.fingerprint)
            self.projections[review.id] = projection
            watcher = asyncio.create_task(self._watch(projection, context.fingerprint))
            inference = None
            try:
                receipt = await (self.planning_executor or plan_removal)(
                    self.runtime, reviewed, deadline=deadline, current_context=projection.get
                )
                # The SDK binds its actual job. Repeat the typed correlation here
                # so a private actor executor cannot bypass the local receipt gate.
                if (
                    type(receipt) is not dict
                    or set(receipt)
                    != {
                        "schema_version",
                        "owner",
                        "job_id",
                        "format",
                        "execution_id",
                        "context_fingerprint",
                        "request_sha256",
                        "prompt_sha256",
                        "proposal",
                        "provider",
                        "model",
                        "thread_id",
                        "turn_id",
                        "simulated",
                        "usage",
                        "token_hard_cap_supported",
                        "action_authority",
                        "captured_at_ms",
                        "expires_at_ms",
                    }
                    or type(receipt.get("schema_version")) is not int
                    or receipt["schema_version"] != 1
                    or receipt.get("format") != "friday.rule-removal.proposal.v1"
                    or receipt.get("owner") != "friday"
                    or receipt.get("execution_id") != reviewed.execution_id
                    or receipt.get("context_fingerprint") != context.fingerprint
                    or receipt.get("request_sha256") != reviewed.digest
                    or receipt.get("model") != reviewed.model
                    or receipt.get("provider") != "codex_app_server"
                    or receipt.get("action_authority") is not False
                    or receipt.get("simulated") is not False
                    or receipt.get("expires_at_ms") != reviewed.request["expires_at_ms"]
                    or len(json.dumps(receipt, ensure_ascii=False, allow_nan=False).encode()) > 8192
                    or receipt.get("prompt_sha256")
                    != hashlib.sha256(render(reviewed.request).encode()).hexdigest()
                    or receipt.get("captured_at_ms") != reviewed.request["captured_at_ms"]
                    or receipt.get("usage") != {"tokens": None, "cost": None}
                    or receipt.get("token_hard_cap_supported") is not False
                    or any(
                        type(receipt.get(k)) is not str
                        or not 1 <= len(receipt[k]) <= 100
                        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in receipt[k])
                        for k in ("job_id", "thread_id", "turn_id")
                    )
                ):
                    raise ARLError("analysis_receipt_mismatch")
                candidate = checked_proposal(receipt["proposal"], reviewed.request)
                inference = True
                snapshot = await self.text.manager.core.snapshot()
                if (
                    self.closed
                    or projection.get() != context.fingerprint
                    or self.context().fingerprint != context.fingerprint
                    or not self.context().available
                    or time.monotonic() >= deadline
                    or datetime.now(UTC) >= review.expires_at
                ):
                    raise ARLError("analysis_context_changed_or_expired")
                # Labels/eligibility are re-read on the owning event loop as well.
                fresh = self._choices(
                    self._records(), snapshot, sorted({c.record_id for c in review.choices})
                )
                if [c.model_dump() for c in fresh] != [c.model_dump() for c in review.choices]:
                    raise ARLError("analysis_choice_changed")
                evidence = {
                    k: receipt[k]
                    for k in (
                        "job_id",
                        "execution_id",
                        "request_sha256",
                        "prompt_sha256",
                        "model",
                        "provider",
                        "thread_id",
                        "turn_id",
                        "captured_at_ms",
                        "expires_at_ms",
                    )
                }
                extra = {
                    "planner_call_attempted": True,
                    "model_inference_attempted": True,
                    "model_receipt": evidence,
                    "summary": candidate["summary"],
                }
                if candidate["status"] != "proposal":
                    result = self._result(
                        review.id, candidate["status"], "analysis_abstained", choices=review.choices, **extra
                    )
                else:
                    choice = next(
                        c
                        for c in fresh
                        if c.record_id == candidate["record_id"]
                        and c.action_id == candidate["action_id"]
                        and c.expected_fingerprint == candidate["expected_fingerprint"]
                    )
                    baseline = self._baseline(review)
                    comparison = (
                        "not_applicable"
                        if baseline is None
                        else "ambiguous"
                        if len(baseline) != 1
                        else "matched"
                        if baseline[0] == choice
                        else "disagreed"
                    )
                    extra["baseline_comparison"] = comparison
                    if comparison in {"disagreed", "ambiguous"}:
                        result = self._result(
                            review.id,
                            "rejected" if comparison == "disagreed" else "clarification",
                            "analysis_candidate_disagrees_with_baseline"
                            if comparison == "disagreed"
                            else "analysis_baseline_ambiguous",
                            choices=review.choices,
                            **extra,
                        )
                        self._save_result(result, "completed")
                        return result
                    if context.simulated:
                        plan = await self.text.manager.plan_remove(
                            RemoveRuleAction(
                                record_id=choice.record_id,
                                action_id=choice.action_id,
                                expected_fingerprint=choice.expected_fingerprint,
                                idempotency_key="arl-" + review.id,
                            ),
                            deadline=deadline,
                        )
                    else:
                        record = self.core_rules.read(choice.record_id)
                        if not record or record.fingerprint != choice.expected_fingerprint:
                            raise ARLError("analysis_choice_changed")
                        changed = remove_instruction(record.rule, choice.action_id, digest(record.rule))
                        plan = await self.core_rules.plan(
                            CoreRuleProposal(
                                rule=changed,
                                expected_context=self.core_rules.context()["fingerprint"],
                                idempotency_key="arl-" + review.id,
                                expires_at=review.expires_at,
                                record_id=choice.record_id,
                                expected_fingerprint=choice.expected_fingerprint,
                            )
                        )
                    if (
                        self.context().fingerprint != context.fingerprint
                        or projection.get() != context.fingerprint
                        or time.monotonic() >= deadline
                        or datetime.now(UTC) >= review.expires_at
                    ):
                        raise ARLError("analysis_context_changed_or_expired")
                    result = self._result(
                        review.id,
                        "planned",
                        "review_diff_before_deployment",
                        choices=[choice],
                        plan=plan,
                        **extra,
                    )
                self._save_result(result, "completed")
                return result
            except asyncio.CancelledError:
                self._save_result(
                    self._result(
                        review.id,
                        "needs_review",
                        "analysis_cancelled_no_replay",
                        planner_call_attempted=True,
                        model_inference_attempted=inference,
                    ),
                    "needs_review",
                )
                raise
            except Exception as exc:
                result = self._result(
                    review.id,
                    "needs_review",
                    safe_code(exc),
                    planner_call_attempted=True,
                    model_inference_attempted=inference,
                )
                try:
                    self._save_result(result, "needs_review")
                except sqlite3.Error:
                    result = self._result(
                        review.id,
                        "needs_review",
                        "analysis_journal_unavailable",
                        planner_call_attempted=True,
                        model_inference_attempted=inference,
                    )
                return result
            finally:
                projection.invalidate()
                self.projections.pop(review.id, None)
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
