"""Project-owned S1-B rules on Core; no HA YAML/Geek/foreign-rule mutation.

Reviewed local configuration uses atomic SQLite CAS. Only live HA ingress can
schedule work, with leases, source bindings and the existing Action Gateway.
Native/core-independent deployment is a separate, still unqualified backend.
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from .action_contracts import CoreRuleEffect
from .contracts import Contract, Event
from .ha import HAReadOnlyAdapter
from .rule_compiler import compile_rule, digest, evaluate_rule
from .rule_contracts import (
    CancelInstruction,
    DecisionCondition,
    DelayInstruction,
    DeviceInstruction,
    FeedbackInstruction,
    ModeLease,
    NumericCondition,
    RuleCompilation,
    RuleEvaluation,
    RuleIR,
    RuleTrial,
)


class CoreRuleRecord(Contract):
    id: str
    rule: RuleIR
    enabled: bool
    source_scope: str
    authority_revision: str
    fingerprint: str
    source: Literal["project:home_core"] = "project:home_core"
    simulated: Literal[False] = False


class CoreRuleProposal(Contract):
    rule: RuleIR
    expected_context: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)
    expires_at: AwareDatetime
    record_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def owned_scope(self):
        if self.rule.simulated or self.rule.execution.preferred_backend != "home_core":
            raise ValueError("explicit_real_home_core_scope_required")
        if bool(self.record_id) != bool(self.expected_fingerprint):
            raise ValueError("complete_source_choice_required")
        return self


class CoreRulePlan(Contract):
    id: str
    boot_id: str
    context_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    created_at: AwareDatetime
    expires_at: AwareDatetime
    old: CoreRuleRecord | None
    new: CoreRuleRecord
    compilation: RuleCompilation
    approval_digest: str
    status: Literal["validated", "awaiting_approval", "verified", "rolled_back", "failed", "expired"]
    code: str
    trial: RuleEvaluation | None = None
    real_write_attempted: Literal[False] = False
    physically_confirmed: Literal[False] = False
    simulated: Literal[False] = False


class CoreRuleApproval(Contract):
    plan_id: str = Field(min_length=1, max_length=128)
    approval_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CoreRuleRun(Contract):
    id: str
    record_id: str
    rule_id: str
    fingerprint: str
    event_id: str
    boot_id: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: Literal["running", "waiting", "completed", "cancelled", "suppressed", "unknown_outcome"]
    code: str
    effects: list[dict] = Field(default_factory=list, max_length=16)
    simulated: Literal[False] = False
    real_write_attempted: bool = False
    physically_confirmed: Literal[False] = False


def core_record(identifier, rule, enabled, scope, authority):
    body = dict(
        id=identifier,
        rule=rule.model_dump(mode="json"),
        enabled=enabled,
        source_scope=scope,
        authority_revision=authority,
    )
    return CoreRuleRecord(**body, fingerprint=digest(body))


class CoreRules:
    def __init__(self, core, owner):
        self.core, self.store, self.owner = core, core.store, owner
        self.boot_id, self.lock = str(uuid4()), asyncio.Lock()
        self.deadlines = {}
        self.generation = 0
        with self.store.connection:
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS core_owned_rules (id TEXT PRIMARY KEY, body TEXT)"
            )
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS core_rule_plans (id TEXT PRIMARY KEY, key TEXT UNIQUE, digest TEXT, created TEXT, body TEXT)"
            )
        if self.owner.owned:
            self.prune()

    def prune(self):
        """Expire diagnostic drafts; retained rule versions and rollback receipts are configuration."""
        cutoff = datetime.now(UTC) - timedelta(days=7)
        with self.store.connection:
            for identifier, body in self.store.connection.execute(
                "SELECT id,body FROM core_rule_plans"
            ).fetchall():
                plan = CoreRulePlan.model_validate_json(body)
                if plan.created_at < cutoff and plan.status != "verified":
                    self.store.connection.execute("DELETE FROM core_rule_plans WHERE id=?", (identifier,))
                    self.deadlines.pop(identifier, None)

    def available(self):
        adapter = self.core.adapter
        authority, _ = self.core.actions.authority() if self.core.actions else (None, None)
        return bool(
            self.owner.owned
            and isinstance(adapter, HAReadOnlyAdapter)
            and authority
            and authority.ha_url.rstrip("/") == adapter.url
            and authority.ha_instance == adapter.instance_id
            and adapter.health().get("continuous_subscription")
            and adapter.health().get("registry_available")
            and adapter.health().get("transport") == "online"
            and adapter.health().get("diagnostic_journal_available")
            and not adapter.health().get("observation_gap")
        )

    def context(self):
        a = self.core.adapter
        _, revision = self.core.actions.authority() if self.core.actions else (None, None)
        return {
            "available": self.available(),
            "owner_available": self.owner.owned,
            "boot_id": self.boot_id,
            "fingerprint": digest(
                [
                    self.boot_id,
                    type(a).__name__,
                    getattr(a, "_boot", None),
                    getattr(a, "url", None),
                    getattr(a, "instance_id", None),
                    revision,
                    [(r.id, r.fingerprint) for r in self.records()],
                ]
            ),
            "simulated": False,
            "backend": "home_core",
            "core_independent": False,
            "wan_independent": "unverified",
            "external_rules_intercepted": False,
            "real_write_attempted": False,
        }

    def records(self):
        return [
            CoreRuleRecord.model_validate_json(r[0])
            for r in self.store.connection.execute("SELECT body FROM core_owned_rules ORDER BY rowid")
        ]

    def read(self, identifier):
        row = self.store.connection.execute(
            "SELECT body FROM core_owned_rules WHERE id=?", (identifier,)
        ).fetchone()
        return CoreRuleRecord.model_validate_json(row[0]) if row else None

    def scope(self, rule, snapshot):
        a = self.core.adapter
        ids = (
            {rule.trigger.entity}
            | {c.entity for c in rule.conditions if isinstance(c, NumericCondition)}
            | {x.target for x in rule.actions if hasattr(x, "target")}
        )
        entities = {e.id: e for e in snapshot.entities + snapshot.derived_entities}
        return digest(
            [
                getattr(a, "url", None),
                getattr(a, "instance_id", None),
                [
                    (
                        i,
                        [(b.source, b.external_id) for b in entities[i].bindings],
                        entities[i].kind,
                        [c.model_dump(mode="json") for c in entities[i].capabilities],
                    )
                    for i in sorted(ids)
                    if i in entities
                ],
            ]
        )

    def _allowed(self):
        if not self.available():
            raise ValueError("core_rule_source_or_authority_unavailable")

    def _compilation(self, rule, snapshot):
        compilation = compile_rule(rule, snapshot)
        if (
            compilation.status != "requires_core"
            or compilation.simulated
            or any(isinstance(c, DecisionCondition) for c in rule.conditions)
            or any(isinstance(a, FeedbackInstruction) for a in rule.actions)
        ):
            raise ValueError("unsupported_core_compilation")
        return compilation

    def _acyclic(self, rule, old):
        rules = [r.rule for r in self.records() if r.enabled and (old is None or r.id != old.id)] + [rule]
        edges = {}
        for item in rules:
            edges.setdefault(item.trigger.entity, set()).update(
                a.target for a in item.actions if hasattr(a, "target")
            )
        done, visiting = set(), set()

        def visit(node):
            if node in visiting:
                raise ValueError("owned_rule_cycle_unsupported")
            if node in done:
                return
            visiting.add(node)
            for other in edges.get(node, ()):
                visit(other)
            visiting.remove(node)
            done.add(node)

        for node in edges:
            visit(node)

    def plan_get(self, identifier):
        row = self.store.connection.execute(
            "SELECT body FROM core_rule_plans WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise ValueError("not_found")
        return CoreRulePlan.model_validate_json(row[0])

    def _save_plan(self, plan, *, key=None, request_digest=None):
        body = plan.model_dump_json()
        if len(body.encode()) > 65536:
            raise ValueError("core_plan_budget")
        if key is None:
            self.store.connection.execute("UPDATE core_rule_plans SET body=? WHERE id=?", (body, plan.id))
        else:
            self.store.connection.execute(
                "INSERT INTO core_rule_plans VALUES (?,?,?,?,?)",
                (plan.id, key, request_digest, plan.created_at.isoformat(), body),
            )

    def _stage(self, plan, status, code, **changes):
        updated = plan.model_copy(update={**changes, "status": status, "code": code})
        with self.store.connection:
            self._save_plan(updated)
        return updated

    def _live(self, plan):
        self._allowed()
        if (
            plan.boot_id != self.boot_id
            or time.monotonic() >= self.deadlines.get(plan.id, 0)
            or datetime.now(UTC) >= plan.expires_at
        ):
            raise ValueError("expired_core_preview")

    async def plan(self, request):
        async with self.lock:
            self._allowed()
            self.prune()
            if request.expected_context != self.context()["fingerprint"]:
                raise ValueError("source_conflict")
            now = datetime.now(UTC)
            ttl = (request.expires_at - now).total_seconds()
            if not 0 < ttl <= 60:
                raise ValueError("expired_or_unsupported_ttl")
            deadline = time.monotonic() + ttl
            row = self.store.connection.execute(
                "SELECT digest,body FROM core_rule_plans WHERE key=?", (request.idempotency_key,)
            ).fetchone()
            if row:
                if row[0] != digest(request):
                    raise ValueError("source_conflict")
                previous = CoreRulePlan.model_validate_json(row[1])
                self._live(previous)
                return previous
            if self.store.connection.execute("SELECT count(*) FROM core_rule_plans").fetchone()[0] >= 256:
                raise ValueError("core_plan_capacity")
            old = self.read(request.record_id) if request.record_id else None
            if request.record_id and (not old or old.fingerprint != request.expected_fingerprint):
                raise ValueError("source_conflict")
            if old and (request.rule.id != old.rule.id or request.rule.revision != old.rule.revision + 1):
                raise ValueError("immutable_rule_identity_or_revision")
            if not old and any(r.rule.id == request.rule.id for r in self.records()):
                raise ValueError("rule_identity_exists")
            adapter = self.core.adapter
            snapshot = await self.core.snapshot()
            if adapter is not self.core.adapter or request.expected_context != self.context()["fingerprint"]:
                raise ValueError("source_conflict")
            if time.monotonic() >= deadline or datetime.now(UTC) >= request.expires_at:
                raise ValueError("expired_core_request")
            self._allowed()
            compilation = self._compilation(request.rule, snapshot)
            self._acyclic(request.rule, old)
            _, authority = self.core.actions.authority()
            identifier = str(uuid4())
            new = core_record(
                "core:version:" + identifier,
                request.rule,
                False,
                self.scope(request.rule, snapshot),
                authority,
            )
            plan = CoreRulePlan(
                id=identifier,
                boot_id=self.boot_id,
                context_fingerprint=request.expected_context,
                created_at=now,
                expires_at=now + timedelta(minutes=5),
                old=old,
                new=new,
                compilation=compilation,
                approval_digest=digest(
                    [
                        old.model_dump(mode="json") if old else None,
                        new.model_dump(mode="json"),
                        compilation.model_dump(mode="json"),
                        request.expected_context,
                    ]
                ),
                status="validated",
                code="awaiting_non_actuating_trial",
            )
            with self.store.connection:
                self._save_plan(plan, key=request.idempotency_key, request_digest=digest(request))
            self.deadlines[identifier] = time.monotonic() + 300
            return plan

    async def _check(self, plan):
        self._live(plan)
        if plan.context_fingerprint != self.context()["fingerprint"]:
            raise ValueError("source_conflict")
        adapter = self.core.adapter
        snapshot = await self.core.snapshot()
        self._live(plan)
        _, revision = self.core.actions.authority()
        if (
            adapter is not self.core.adapter
            or plan.context_fingerprint != self.context()["fingerprint"]
            or revision != plan.new.authority_revision
            or self.scope(plan.new.rule, snapshot) != plan.new.source_scope
            or digest(self._compilation(plan.new.rule, snapshot)) != digest(plan.compilation)
            or (plan.old and self.read(plan.old.id) != plan.old)
        ):
            raise ValueError("source_conflict")
        if any(
            r.rule.id == plan.new.rule.id and (plan.old is None or r.enabled and r.id != plan.old.id)
            for r in self.records()
        ):
            raise ValueError("conflicting_owned_version")
        self._acyclic(plan.new.rule, plan.old)
        return snapshot

    async def trial(self, identifier):
        async with self.lock:
            plan = self.plan_get(identifier)
            if plan.status not in {"validated", "awaiting_approval"}:
                return plan
            snapshot = await self._check(plan)
            now = datetime.now(UTC)
            trigger = plan.new.rule.trigger
            payload = {"entity_id": trigger.entity, "capability": trigger.capability}
            if trigger.type == "state_transition":
                payload.update({"from": trigger.from_value, "to": trigger.to_value})
            else:
                payload["value"] = trigger.value
            event = Event(
                source="SIMULATED-core-rule-trial",
                correlation_id=plan.id,
                kind="state-transition" if trigger.type == "state_transition" else "source-report",
                wall_time=now,
                monotonic_time=time.monotonic(),
                boot_id=self.core.adapter._boot,
                payload=payload,
                simulated=True,
            )
            modes = {
                k: ModeLease(value=False, issued_at=now, valid_until=now + timedelta(seconds=30))
                for k in ("home.sleep", "home.manual")
            }
            result = evaluate_rule(
                plan.new.rule,
                snapshot,
                RuleTrial(event=event, modes=modes),
                now=now,
                allow_core=True,
                preview=True,
            )
            known_false=bool(result.reasons) and all(reason.startswith('condition_false:') or reason=='outside_time_window' for reason in result.reasons)
            return self._stage(
                plan,
                "awaiting_approval" if result.outcome == "matched" or result.outcome=='suppressed' and known_false else "validated",
                "conditions_currently_false_no_device_write" if known_false else "assumed_awake_trial_no_device_write",
                trial=result,
            )

    async def apply(self, request):
        async with self.lock:
            plan = self.plan_get(request.plan_id)
            if plan.status != "awaiting_approval":
                return plan
            if request.approval_digest != plan.approval_digest:
                raise ValueError("approval_conflict")
            await self._check(plan)
            if self.store.connection.execute("SELECT count(*) FROM core_owned_rules").fetchone()[0] >= 64:
                raise ValueError("core_rule_capacity")
            enabled = core_record(
                plan.new.id, plan.new.rule, True, plan.new.source_scope, plan.new.authority_revision
            )
            with self.store.connection:
                if plan.old:
                    if self.read(plan.old.id) != plan.old:
                        raise ValueError("source_conflict")
                    paused = core_record(
                        plan.old.id, plan.old.rule, False, plan.old.source_scope, plan.old.authority_revision
                    )
                    self.store.connection.execute(
                        "UPDATE core_owned_rules SET body=? WHERE id=?", (paused.model_dump_json(), paused.id)
                    )
                self.store.connection.execute(
                    "INSERT INTO core_owned_rules VALUES (?,?)", (enabled.id, enabled.model_dump_json())
                )
                plan = plan.model_copy(
                    update={
                        "new": enabled,
                        "status": "verified",
                        "code": "atomic_core_configuration_verified_not_physical",
                    }
                )
                self._save_plan(plan)
            self.generation += 1
            return plan

    async def rollback(self, identifier):
        async with self.lock:
            if not self.owner.owned:
                raise ValueError("core_rule_owner_unavailable")
            plan = self.plan_get(identifier)
            if plan.status != "verified":
                return plan
            if self.read(plan.new.id) != plan.new:
                raise ValueError("source_edited_no_automatic_overwrite")
            if plan.old:
                old = self.read(plan.old.id)
                expected = core_record(
                    plan.old.id, plan.old.rule, False, plan.old.source_scope, plan.old.authority_revision
                )
                if old != expected:
                    raise ValueError("source_edited_no_automatic_overwrite")
            with self.store.connection:
                disabled = core_record(
                    plan.new.id, plan.new.rule, False, plan.new.source_scope, plan.new.authority_revision
                )
                self.store.connection.execute(
                    "UPDATE core_owned_rules SET body=? WHERE id=?", (disabled.model_dump_json(), disabled.id)
                )
                if plan.old:
                    self.store.connection.execute(
                        "UPDATE core_owned_rules SET body=? WHERE id=?",
                        (plan.old.model_dump_json(), plan.old.id),
                    )
                plan = plan.model_copy(
                    update={
                        "status": "rolled_back",
                        "code": "core_configuration_restored_no_device_compensation",
                    }
                )
                self._save_plan(plan)
            self.generation += 1
            return plan

    def listing(self):
        return {
            **self.context(),
            "records": [r.model_dump(mode="json") for r in self.records()],
            "plans": [
                json.loads(r[0])
                for r in self.store.connection.execute(
                    "SELECT body FROM core_rule_plans ORDER BY rowid DESC LIMIT 10"
                )
            ],
            "native_deployment": "unsupported",
            "limitation": "Only reviewed project-owned Core rules; no foreign rule/YAML/Graph writes.",
        }


class CoreRuleRuntime:
    def __init__(self, manager):
        self.manager, self.core, self.store = manager, manager.core, manager.store
        self.boot_id = str(uuid4())
        self.pending = {}
        self.queue = []
        self.modes = {}
        self.mode_deadline = 0
        self.mode_generation = -1
        self.execution_enabled = True
        self.manual_until = 0.0
        self.worker = None
        self.wake = asyncio.Event()
        self.closed = False
        self.failure = None
        self.seen_ingress = 0
        self.last_prune = 0
        with self.store.connection:
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS core_rule_runs (id TEXT PRIMARY KEY, event_id TEXT, rule_id TEXT, created TEXT, body TEXT, UNIQUE(event_id,rule_id))"
            )
        if manager.owner.owned:
            for row in self.store.connection.execute("SELECT body FROM core_rule_runs").fetchall():
                run = CoreRuleRun.model_validate_json(row[0])
                if run.status in {"running", "waiting"}:
                    attempted = any(
                        (trace := self.store.light_action(e.get("action_id"))) and trace.real_write_attempted
                        for e in run.effects
                        if e.get("action_id")
                    )
                    self._stage(
                        run,
                        "unknown_outcome" if attempted else "cancelled",
                        "restart_discards_pending_never_replays",
                        real_write_attempted=run.real_write_attempted or attempted,
                    )
            self.prune()

    def prune(self):
        if time.monotonic() - self.last_prune < 3600:
            return
        cutoff = datetime.now(UTC) - timedelta(days=7)
        with self.store.connection:
            for identifier, body in self.store.connection.execute(
                "SELECT id,body FROM core_rule_runs"
            ).fetchall():
                run = CoreRuleRun.model_validate_json(body)
                if run.created_at < cutoff and run.status not in {"running", "waiting"}:
                    self.store.connection.execute("DELETE FROM core_rule_runs WHERE id=?", (identifier,))
        self.last_prune = time.monotonic()

    def start(self):
        if self.worker is None and not self.closed and not self.failure and self.manager.owner.owned:
            self.worker = asyncio.create_task(self._worker(), name="home-core-rule-runtime")

    def enqueue(self, adapter, events):
        if adapter is not self.core.adapter or self.closed or self.failure:
            return
        self.seen_ingress += len(events)
        if any(e.kind == "observation-gap" for e in events):
            self.queue.clear()
            self.modes = {}
            self.mode_deadline = 0
            for key in list(self.pending):
                self._cancel(key, "observation_gap_no_replay")
        records = [r for r in self.manager.records() if r.enabled]
        for event in events:
            if (
                event.kind == "state-transition"
                and event.payload.get("trigger_eligible") is True
                and any(r.rule.trigger.entity == event.payload.get("entity_id") for r in records)
            ):
                if len(self.queue) >= 32:
                    self._fail("live_event_queue_overflow")
                    break
                self.queue.append((adapter, event))
        self.start()
        self.wake.set()

    def _save(self, run, *, new=False):
        if len(run.model_dump_json().encode()) > 16384:
            raise ValueError("core_run_budget")
        now = datetime.now(UTC)
        event = Event(
            source="core-rule-runtime",
            kind="action-result",
            causation_id=run.event_id,
            correlation_id=run.id,
            wall_time=now,
            monotonic_time=time.monotonic(),
            boot_id=self.boot_id,
            payload={
                "rule_run_id": run.id,
                "status": run.status,
                "code": run.code,
                "real_write_attempted": run.real_write_attempted,
                "physical_confirmation": False,
            },
        )
        with self.store.connection:
            if new:
                result = self.store.connection.execute(
                    "INSERT OR IGNORE INTO core_rule_runs VALUES (?,?,?,?,?)",
                    (run.id, run.event_id, run.rule_id, run.created_at.isoformat(), run.model_dump_json()),
                )
                if result.rowcount != 1:
                    return False
            else:
                self.store.connection.execute(
                    "UPDATE core_rule_runs SET body=? WHERE id=?", (run.model_dump_json(), run.id)
                )
            self.store.append_events([event])
        return True

    def _stage(self, run, status, code, **changes):
        updated = run.model_copy(update={**changes, "status": status, "code": code})
        self._save(updated)
        return updated

    def _cancel(self, key, code):
        item = self.pending.pop(key, None)
        if item:
            item["cancelled"] = True
            traces = [
                t
                for e in item["run"].effects
                if e.get("action_id") and (t := self.store.light_action(e["action_id"]))
            ]
            unknown = any(t.real_write_attempted and t.status != "reported_match" for t in traces)
            self._stage(
                item["run"],
                "unknown_outcome" if unknown else "cancelled",
                code,
                real_write_attempted=item["run"].real_write_attempted
                or any(t.real_write_attempted for t in traces),
            )

    def _fail(self, code):
        self.failure = code
        self.queue.clear()
        for key in list(self.pending):
            try:
                self._cancel(key, code + "_no_retry")
            except Exception:
                self.pending.pop(key, None)

    async def invalidate(self, code):
        self.queue.clear()
        self.modes = {}
        self.mode_deadline = 0
        async with self.manager.lock:
            for key in list(self.pending):
                self._cancel(key, code)

    async def set_execution(self, enabled, *, manual_seconds=0):
        """Automatic intent and temporary manual hold never rewrite household mode."""
        if type(enabled) is not bool or type(manual_seconds) is not int or not 0 <= manual_seconds <= 300:
            raise ValueError("invalid_mode_window")
        async with self.manager.lock:
            self.execution_enabled = enabled
            if manual_seconds:
                self.manual_until = time.monotonic() + manual_seconds
            self.queue.clear()
            for key in list(self.pending):
                self._cancel(key, "explicit_mode_changed")
        self.wake.set()
        return self.status()

    async def renew_awake(self, expected_modes):
        """A maintenance waiter cannot overwrite a newer sleep/unknown decision."""
        async with self.manager.lock:
            now = datetime.now(UTC)
            if (not self.execution_enabled or self.closed or self.failure
                    or self.modes is not expected_modes
                    or self.mode_generation != self.manager.generation
                    or time.monotonic() >= self.mode_deadline
                    or set(self.modes) != {'home.sleep', 'home.manual'}
                    or any(m.value or not m.issued_at <= now < m.valid_until for m in self.modes.values())):
                return
            self.manager._allowed()
            self.mode_deadline = time.monotonic() + 1800
            self.modes = {key: lease.model_copy(update={'valid_until': now + timedelta(seconds=1800)})
                          for key, lease in self.modes.items()}

    async def set_mode(self, mode, ttl=60):
        if (
            mode not in {"awake", "sleep", "manual", "unknown"}
            or type(ttl) is not int
            or not 1 <= ttl <= 1800
        ):
            raise ValueError("invalid_mode_window")
        self.queue.clear()
        self.modes = {}
        self.mode_deadline = 0
        async with self.manager.lock:
            if mode != "unknown":
                self.manager._allowed()
            if mode != "unknown" and (self.closed or self.failure):
                raise ValueError("core_runtime_unavailable")
            self.queue.clear()
            for key in list(self.pending):
                self._cancel(key, "explicit_mode_changed")
            now = datetime.now(UTC)
            self.mode_deadline = time.monotonic() + ttl
            self.mode_generation = self.manager.generation
            self.modes = (
                {}
                if mode == "unknown"
                else {
                    k: ModeLease(value=(mode == v), issued_at=now, valid_until=now + timedelta(seconds=ttl))
                    for k, v in [("home.sleep", "sleep"), ("home.manual", "manual")]
                }
            )
        self.start()
        self.wake.set()
        return self.status()

    def get(self, identifier):
        row = self.store.connection.execute(
            "SELECT body FROM core_rule_runs WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise ValueError("not_found")
        return CoreRuleRun.model_validate_json(row[0])

    def status(self):
        return {
            "available": self.manager.available() and not self.closed and not self.failure,
            "simulated": False,
            "backend": "home_core",
            "core_independent": False,
            "wan_independent": "unverified",
            "modes": {k: v.model_dump(mode="json") for k, v in self.modes.items()},
            "mode_revision_valid": self.mode_generation == self.manager.generation,
            "automatic_enabled": self.execution_enabled,
            "manual_hold_active": time.monotonic() < self.manual_until,
            "pending_count": len(self.pending),
            "queued_count": len(self.queue),
            "worker_running": bool(self.worker and not self.worker.done()),
            "worker_failure": self.failure,
            "seen_live_ingress_count": self.seen_ingress,
            "runs": [
                json.loads(r[0])
                for r in self.store.connection.execute(
                    "SELECT body FROM core_rule_runs ORDER BY rowid DESC LIMIT 20"
                )
            ],
            "external_rules_intercepted": False,
            "replay_policy": "never_actuate",
        }

    def _guard(self, item):
        now = datetime.now(UTC)
        mono = time.monotonic()
        record = item["record"]
        run = item["run"]
        if (
            self.closed
            or self.failure
            or item.get("cancelled")
            or not self.manager.available()
            or item["adapter"] is not self.core.adapter
        ):
            return "source_or_runtime_unavailable"
        if not self._current_connection(item["adapter"], item["event"]):
            return "old_connection_event_no_replay"
        if mono >= item["deadline"] or now < run.created_at or now >= run.expires_at:
            return "expired_event_no_replay"
        if not self.execution_enabled or mono < self.manual_until:
            return "guard_unknown_or_protected"
        if self.mode_generation != self.manager.generation:
            return "rule_change_requires_fresh_mode_window"
        if mono >= self.mode_deadline or any(
            k not in self.modes
            or not self.modes[k].issued_at <= now < self.modes[k].valid_until
            or (self.modes[k].value and not (k == "home.sleep" and record.rule.safety.allow_during_sleep))
            for k in ("home.sleep", "home.manual")
        ):
            return "guard_unknown_or_protected"
        if (
            item["event"].wall_time < self.modes["home.sleep"].issued_at
            or (item["event"].received_at or item["event"].wall_time) < self.modes["home.sleep"].issued_at
        ):
            return "event_precedes_current_mode_window"
        _, revision = self.core.actions.authority()
        if revision != record.authority_revision:
            return "authority_changed_requires_review"
        if self.manager.read(record.id) != record or not record.enabled:
            return "rule_paused_replaced_or_edited"
        return None

    @staticmethod
    def _current_connection(adapter, event):
        stream = getattr(adapter, "_stream", None)
        connection = getattr(stream, "connection_id", None)
        return bool(connection and event.source_seq and event.source_seq.startswith(connection + ":"))

    async def ingest(self, adapter, event):
        async with self.manager.lock:
            if (
                self.closed
                or self.failure
                or not self.manager.available()
                or adapter is not self.core.adapter
                or event.simulated
                or event.kind != "state-transition"
                or event.payload.get("trigger_eligible") is not True
                or event.boot_id != adapter._boot
                or not self._current_connection(adapter, event)
                or not any(e == event for e in adapter._events)
            ):
                return {"accepted": False, "code": "unregistered_replay_or_foreign_event"}
            self.prune()
            now = datetime.now(UTC)
            mono = time.monotonic()
            age = (now - event.wall_time).total_seconds()
            if not 0 <= age <= 30 or not 0 <= mono - event.monotonic_time <= 30:
                return {"accepted": False, "code": "expired_or_future_event"}
            async with asyncio.timeout(2):
                snapshot = await self.core.snapshot()
            if adapter is not self.core.adapter:
                return {"accepted": False, "code": "source_changed"}
            for record in self.manager.records():
                if not record.enabled or event.payload.get("entity_id") != record.rule.trigger.entity:
                    continue
                if self.store.connection.execute(
                    "SELECT 1 FROM core_rule_runs WHERE event_id=? AND rule_id=?",
                    (event.event_id, record.rule.id),
                ).fetchone():
                    continue
                self._cancel(record.rule.id, "new_event_cancels_pending")
                now = datetime.now(UTC)
                mono = time.monotonic()
                remaining = min(
                    record.rule.execution.max_event_age_ms / 1000 - (now - event.wall_time).total_seconds(),
                    record.rule.execution.max_event_age_ms / 1000 - (mono - event.monotonic_time),
                )
                if remaining <= 0:
                    continue
                evaluated = evaluate_rule(
                    record.rule, snapshot, RuleTrial(event=event, modes=self.modes), now=now, allow_core=True
                )
                run = CoreRuleRun(
                    id=str(uuid4()),
                    record_id=record.id,
                    rule_id=record.rule.id,
                    fingerprint=record.fingerprint,
                    event_id=event.event_id,
                    boot_id=self.boot_id,
                    created_at=now,
                    expires_at=event.wall_time
                    + timedelta(milliseconds=record.rule.execution.max_event_age_ms),
                    status="running" if evaluated.outcome == "matched" else "suppressed",
                    code="live_event_admitted" if evaluated.outcome == "matched" else evaluated.reasons[0],
                )
                if self.store.connection.execute("SELECT count(*) FROM core_rule_runs").fetchone()[0] >= 1000:
                    raise ValueError("core_run_capacity")
                if not self._save(run, new=True):
                    continue
                if evaluated.outcome == "matched":
                    item = {
                        "run": run,
                        "record": record,
                        "adapter": adapter,
                        "event": event,
                        "cursor": 0,
                        "deadline": mono + remaining,
                        "due": mono,
                    }
                    code = self._guard(item)
                    if code or self.manager.scope(record.rule, snapshot) != record.source_scope:
                        self._stage(run, "suppressed", code or "source_binding_changed")
                    else:
                        self.pending[record.rule.id] = item
        await self.tick()
        return {"accepted": True, "simulated": False}

    async def tick(self):
        async with self.manager.lock:
            for key, item in list(self.pending.items()):
                if code := self._guard(item):
                    self._cancel(key, code)
                    continue
                async with asyncio.timeout(2):
                    snapshot = await self.core.snapshot()
                if code := self._guard(item):
                    self._cancel(key, code)
                    continue
                if self.manager.scope(item["record"].rule, snapshot) != item["record"].source_scope:
                    self._cancel(key, "source_binding_changed")
                    continue
                guarded_rule = item["record"].rule.model_copy(
                    update={
                        "actions": [
                            a
                            for a in item["record"].rule.actions
                            if not isinstance(a, (DelayInstruction, CancelInstruction))
                        ]
                    }
                )
                checked = evaluate_rule(
                    guarded_rule,
                    snapshot,
                    RuleTrial(event=item["event"], modes=self.modes),
                    now=datetime.now(UTC),
                    allow_core=True,
                )
                if checked.outcome != "matched":
                    self._cancel(key, "revalidation:" + checked.reasons[0])
                    continue
                if time.monotonic() < item["due"]:
                    continue
                actions = item["record"].rule.actions
                while item["cursor"] < len(actions):
                    action = actions[item["cursor"]]
                    item["cursor"] += 1
                    if isinstance(action, DelayInstruction):
                        item["due"] = time.monotonic() + action.milliseconds / 1000
                        item["run"] = self._stage(item["run"], "waiting", "cancellable_delay")
                        break
                    if isinstance(action, CancelInstruction):
                        continue
                    async with asyncio.timeout(2):
                        fresh = await self.core.snapshot()
                    if code := self._guard(item):
                        self._cancel(key, code)
                        break
                    if self.manager.scope(item["record"].rule, fresh) != item["record"].source_scope:
                        self._cancel(key, "source_binding_changed")
                        break
                    result = evaluate_rule(
                        item["record"].rule.model_copy(update={"actions": [action]}),
                        fresh,
                        RuleTrial(event=item["event"], modes=self.modes),
                        now=datetime.now(UTC),
                        allow_core=True,
                    )
                    if result.outcome != "matched":
                        self._cancel(key, "send_guard:" + result.reasons[0])
                        break
                    command = result.commands[0]
                    entity = next(e for e in fresh.entities if e.id == command["target"])
                    operation = action.operation if isinstance(action, DeviceInstruction) else action.type
                    request = CoreRuleEffect(
                        target=entity.id,
                        operation=operation,
                        desired=command["desired"],
                        expected_state_version=entity.state_version,
                        expires_at=min(item["run"].expires_at, *[v.valid_until for v in self.modes.values()]),
                        idempotency_key="core:"
                        + digest([item["event"].event_id, item["record"].fingerprint, action.id]),
                        rule_id=item["record"].rule.id,
                        rule_fingerprint=item["record"].fingerprint,
                        run_id=item["run"].id,
                        source_event_id=item["event"].event_id,
                    )

                    def guard(current_snapshot):
                        if failure := self._guard(item):
                            return failure
                        if (
                            self.manager.scope(item["record"].rule, current_snapshot)
                            != item["record"].source_scope
                        ):
                            return "source_binding_changed"
                        evaluated = evaluate_rule(
                            item["record"].rule.model_copy(update={"actions": [action]}),
                            current_snapshot,
                            RuleTrial(event=item["event"], modes=self.modes),
                            now=datetime.now(UTC),
                            allow_core=True,
                        )
                        return None if evaluated.outcome == "matched" else "guard:" + evaluated.reasons[0]

                    trace = await self.core.actions.plan(request, managed_guard=guard)
                    item["run"] = self._stage(
                        item["run"],
                        "running",
                        "effect_prepared",
                        effects=[
                            *item["run"].effects,
                            {"action_id": trace.action_id, "instruction": command},
                        ],
                    )
                    if trace.status == "planned":
                        trace = await self.core.actions.execute(trace.action_id, managed_guard=guard)
                    effects = [
                        *item["run"].effects[:-1],
                        {"action_id": trace.action_id, "instruction": command, "status": trace.status},
                    ]
                    item["run"] = self._stage(
                        item["run"],
                        "running",
                        "effect_result_recorded",
                        effects=effects,
                        real_write_attempted=item["run"].real_write_attempted or trace.real_write_attempted,
                    )
                    if failure := self._guard(item):
                        self.pending.pop(key, None)
                        self._stage(
                            item["run"],
                            "unknown_outcome"
                            if trace.real_write_attempted and trace.status != "reported_match"
                            else "cancelled",
                            failure,
                        )
                        break
                    if trace.status != "reported_match":
                        self.pending.pop(key, None)
                        self._stage(
                            item["run"],
                            "unknown_outcome" if trace.real_write_attempted else "cancelled",
                            "effect_not_verified_no_retry",
                        )
                        break
                else:
                    self.pending.pop(key, None)
                    self._stage(item["run"], "completed", "ha_reported_sequence_complete_not_physical")

    async def _worker(self):
        while not self.closed and not self.failure:
            try:
                while self.queue:
                    adapter, event = self.queue.pop(0)
                    await self.ingest(adapter, event)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._fail("core_worker_failed")
                return
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=0.1 if self.pending else 30)
            except TimeoutError:
                pass
            finally:
                self.wake.clear()

    async def close(self):
        self.closed = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        await self.invalidate("runtime_closed_no_replay")
