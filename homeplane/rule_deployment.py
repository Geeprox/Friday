"""Durable Simulator Saga. No HA/Geek rule writer is fabricated or exposed."""

import asyncio
import fcntl
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from .contracts import Contract, Event
from .rule_compiler import compile_rule, digest, evaluate_rule, remove_instruction
from .rule_contracts import ModeLease, RuleCompilation, RuleEvaluation, RuleIR, RuleTrial
from .simulator import Simulator


class ManagedRuleRecord(Contract):
    id: str
    rule: RuleIR
    enabled: bool
    fingerprint: str
    source: Literal["project:simulator"] = "project:simulator"
    simulated: Literal[True] = True


class RuleDeployment(Contract):
    id: str
    key: str
    request_digest: str
    boot_id: str
    source_version: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    old: ManagedRuleRecord
    new_id: str
    new_rule: RuleIR
    compilation: RuleCompilation
    approval_digest: str
    status: Literal[
        "validated",
        "awaiting_approval",
        "staging",
        "staged_disabled",
        "pausing_old",
        "old_paused",
        "activating",
        "activated",
        "verified",
        "needs_reconcile",
        "rolling_back",
        "rolled_back",
        "failed",
        "expired",
    ]
    code: str
    history: list[dict] = Field(default_factory=list)
    simulation: RuleEvaluation | None = None
    reconciled: dict | None = None
    real_write_attempted: Literal[False] = False
    physically_confirmed: Literal[False] = False
    simulated: Literal[True] = True


class RemoveRuleAction(Contract):
    record_id: str = Field(min_length=1, max_length=192)
    action_id: str = Field(min_length=1, max_length=128)
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class RulePlanRef(Contract):
    plan_id: str = Field(min_length=1, max_length=128)


class RuleApproval(RulePlanRef):
    approval_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def record(identifier, rule, enabled):
    return ManagedRuleRecord(
        id=identifier,
        rule=rule,
        enabled=enabled,
        fingerprint=digest({"rule": rule.model_dump(mode="json"), "enabled": enabled}),
    )


class SimulatedRuleBackend:
    """A persistent fake backend with genuine compare-and-swap, never a vendor adapter."""

    def __init__(self, store):
        self.store = store
        with store.connection:
            store.connection.execute(
                "CREATE TABLE IF NOT EXISTS simulated_rules (id TEXT PRIMARY KEY, fingerprint TEXT, body TEXT)"
            )
        self.failpoint = None  # In-process test injection only; no API accepts it.

    def records(self):
        return [
            ManagedRuleRecord.model_validate_json(r[0])
            for r in self.store.connection.execute("SELECT body FROM simulated_rules ORDER BY rowid")
        ]

    def read(self, identifier):
        row = self.store.connection.execute(
            "SELECT body FROM simulated_rules WHERE id=?", (identifier,)
        ).fetchone()
        return ManagedRuleRecord.model_validate_json(row[0]) if row else None

    async def create_disabled(self, identifier, rule):
        item = record(identifier, rule, False)
        body = item.model_dump_json()
        if len(body.encode()) > 16384:
            raise ValueError("rule_budget_exceeded")
        with self.store.connection:
            if self.store.connection.execute("SELECT count(*) FROM simulated_rules").fetchone()[0] >= 64:
                raise ValueError("rule_capacity_exceeded")
            self.store.connection.execute(
                "INSERT INTO simulated_rules VALUES (?,?,?)", (identifier, item.fingerprint, body)
            )
        return item

    async def toggle(self, identifier, expected, enabled):
        current = self.read(identifier)
        if current is None or current.fingerprint != expected:
            raise ValueError("source_conflict")
        if self.failpoint == ("enable_new" if enabled else "pause_old"):
            self.failpoint = None
            raise OSError("SIMULATED backend response lost")
        updated = record(identifier, current.rule, enabled)
        with self.store.connection:
            result = self.store.connection.execute(
                "UPDATE simulated_rules SET fingerprint=?,body=? WHERE id=? AND fingerprint=?",
                (updated.fingerprint, updated.model_dump_json(), identifier, expected),
            )
            if result.rowcount != 1:
                raise ValueError("source_conflict")
        return updated


class RuleManager:
    def __init__(self, core):
        self.core, self.store = core, core.store
        self.boot_id, self.lock = str(uuid4()), asyncio.Lock()
        self.deadlines = {}
        self.backend = SimulatedRuleBackend(self.store)
        path = self.store.path.with_suffix(".rules.execution.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.owner = os.fdopen(fd, "a")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owned = True
        except BlockingIOError:
            self.owned = False
        with self.store.connection:
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS rule_deployments (id TEXT PRIMARY KEY, key TEXT UNIQUE, created TEXT, status TEXT, body TEXT)"
            )
        if self.owned:
            # Only terminal history is pruned. Active/uncertain deployments are never lost.
            cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
            with self.store.connection:
                self.store.connection.execute(
                    "DELETE FROM rule_deployments WHERE created < ? AND status IN ('rolled_back','failed','expired')",
                    (cutoff,),
                )
            for row in self.store.connection.execute(
                "SELECT body FROM rule_deployments WHERE status IN ('staging','staged_disabled','pausing_old','old_paused','activating','activated','rolling_back')"
            ).fetchall():
                self._stage(
                    RuleDeployment.model_validate_json(row[0]),
                    "needs_reconcile",
                    "restart_read_configuration_before_mutating",
                )
            if not self.backend.records():
                fixture = Path(__file__).resolve().parent.parent / "fixtures/homeplane/managed-arrival.json"
                item = record("simulated:arrival:v1", RuleIR.model_validate_json(fixture.read_text()), True)
                with self.store.connection:
                    self.store.connection.execute(
                        "INSERT INTO simulated_rules VALUES (?,?,?)",
                        (item.id, item.fingerprint, item.model_dump_json()),
                    )

    def close(self):
        self.owner.close()
        self.owned = False

    def _allowed(self):
        if not self.owned or not isinstance(self.core.adapter, Simulator):
            raise ValueError("simulation_only_or_owner_unavailable")

    def get(self, identifier=None, *, key=None):
        column, value = ("key", key) if key else ("id", identifier)
        row = self.store.connection.execute(
            f"SELECT body FROM rule_deployments WHERE {column}=?", (value,)
        ).fetchone()
        if not row:
            raise ValueError("not_found")
        return RuleDeployment.model_validate_json(row[0])

    def _save(self, deployment, *, previous=None):
        body = deployment.model_dump_json()
        if len(body.encode()) > 65536:
            raise ValueError("deployment_budget_exceeded")
        with self.store.connection:
            if previous is None:
                if (
                    self.store.connection.execute("SELECT count(*) FROM rule_deployments").fetchone()[0]
                    >= 256
                ):
                    raise ValueError("deployment_capacity_exceeded")
                self.store.connection.execute(
                    "INSERT INTO rule_deployments VALUES (?,?,?,?,?)",
                    (
                        deployment.id,
                        deployment.key,
                        deployment.created_at.isoformat(),
                        deployment.status,
                        body,
                    ),
                )
            else:
                updated = self.store.connection.execute(
                    "UPDATE rule_deployments SET status=?,body=? WHERE id=? AND status=?",
                    (deployment.status, body, deployment.id, previous),
                )
                if updated.rowcount != 1:
                    raise ValueError("source_conflict")
            event = Event(
                source="SIMULATED-rule-deployer",
                kind="action-result",
                correlation_id=deployment.id,
                wall_time=datetime.now(UTC),
                monotonic_time=time.monotonic(),
                boot_id=self.boot_id,
                simulated=True,
                payload={
                    "rule_plan_id": deployment.id,
                    "status": deployment.status,
                    "code": deployment.code,
                    "real_write_attempted": False,
                    "scope": "project-owned simulated rules only",
                },
            )
            self.store.connection.execute(
                "INSERT INTO events VALUES (?,?,?)",
                (event.event_id, event.wall_time.isoformat(), event.model_dump_json()),
            )
            self.store.connection.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY rowid DESC LIMIT ?)",
                (self.store.max_rows,),
            )

    def _stage(self, deployment, status, code, **changes):
        history = [*deployment.history, {"status": status, "code": code, "at": datetime.now(UTC).isoformat()}]
        if len(history) > 64:
            history = [history[0], *history[-63:]]
        updated = deployment.model_copy(
            update={
                **changes,
                "status": status,
                "code": code,
                "history": history,
            }
        )
        self._save(updated, previous=deployment.status)
        return updated

    def listing(self):
        available = self.owned and isinstance(self.core.adapter, Simulator)
        rows = (
            self.store.connection.execute(
                "SELECT body FROM rule_deployments ORDER BY rowid DESC LIMIT 10"
            ).fetchall()
            if available
            else []
        )
        deployments = []
        for row in rows:
            deployment = RuleDeployment.model_validate_json(row[0])
            if deployment.status == "verified":
                _, _, facts = self._configuration(deployment)
                if not (
                    facts["old_unchanged"]
                    and facts["new_unchanged"]
                    and not facts["old_enabled"]
                    and facts["new_enabled"]
                    and not facts["other_enabled_versions"]
                ):
                    deployment = self._stage(
                        deployment, "needs_reconcile", "configuration_changed_since_verify", reconciled=facts
                    )
            deployments.append(deployment.model_dump(mode="json"))
        return {
            "available": available,
            "simulated": True,
            "real_rule_writes": "unsupported",
            "records": [r.model_dump(mode="json") for r in self.backend.records()] if available else [],
            "deployments": deployments,
            "limitation": "Only project-owned Simulator rules; external Graphs remain opaque and untouched.",
        }

    async def plan_remove(self, request, *, deadline=None):
        async with self.lock:
            self._allowed()
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError("expired_request")
            try:
                existing = self.get(key=request.idempotency_key)
            except ValueError as exc:
                if str(exc) != "not_found":
                    raise
            else:
                if existing.request_digest != digest(request):
                    raise ValueError("source_conflict")
                return existing
            old = self.backend.read(request.record_id)
            if not old or not old.enabled or not old.simulated or old.source != "project:simulator":
                raise ValueError("unsupported_managed_source")
            if old.fingerprint != request.expected_fingerprint:
                raise ValueError("source_conflict")
            new = remove_instruction(old.rule, request.action_id, digest(old.rule))
            snapshot = await self.core.snapshot()
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError("expired_request")
            compilation = compile_rule(new, snapshot)
            if compilation.status != "supported" or not compilation.simulated:
                raise ValueError("unsupported_compilation")
            now = datetime.now(UTC)
            identifier = str(uuid4())
            approval = digest(
                {
                    "old": old.fingerprint,
                    "new": digest(new),
                    "compilation": compilation.model_dump(mode="json"),
                    "source_version": snapshot.version,
                }
            )
            deployment = RuleDeployment(
                id=identifier,
                key=request.idempotency_key,
                request_digest=digest(request),
                boot_id=self.boot_id,
                source_version=snapshot.version,
                created_at=now,
                expires_at=now + timedelta(minutes=5),
                old=old,
                new_id="simulated:version:" + identifier,
                new_rule=new,
                compilation=compilation,
                approval_digest=approval,
                status="validated",
                code="awaiting_simulation",
                history=[{"status": "validated", "at": now.isoformat()}],
            )
            self._save(deployment)
            self.deadlines[identifier] = time.monotonic() + 300
            return deployment

    def _live_preview(self, deployment):
        self._allowed()
        if (
            deployment.boot_id != self.boot_id
            or time.monotonic() >= self.deadlines.get(deployment.id, 0)
            or datetime.now(UTC) >= deployment.expires_at
        ):
            raise ValueError("expired_preview_requires_new_plan")

    async def simulate(self, identifier):
        async with self.lock:
            deployment = self.get(identifier)
            self._live_preview(deployment)
            if deployment.status not in {"validated", "awaiting_approval"}:
                return deployment
            snapshot = await self.core.snapshot()
            now = datetime.now(UTC)
            trigger = deployment.new_rule.trigger
            payload = {"entity_id": trigger.entity, "capability": trigger.capability}
            if trigger.type == "state_transition":
                payload.update({"from": trigger.from_value, "to": trigger.to_value})
            else:
                payload["value"] = trigger.value
            event = Event(
                source="SIMULATED-explicit-rule-trial",
                kind="state-transition" if trigger.type == "state_transition" else "source-report",
                correlation_id=deployment.id,
                wall_time=now,
                monotonic_time=time.monotonic(),
                boot_id=snapshot.events[-1].boot_id,
                payload=payload,
                simulated=True,
            )
            modes = {
                name: ModeLease(value=False, issued_at=now, valid_until=now + timedelta(seconds=30))
                for name in ("home.sleep", "home.manual")
            }
            result = evaluate_rule(
                deployment.new_rule, snapshot, RuleTrial(event=event, modes=modes), now=now
            )
            return self._stage(
                deployment,
                "awaiting_approval" if result.outcome == "matched" else "validated",
                "simulated_intents_matched" if result.outcome == "matched" else "simulation_suppressed",
                simulation=result,
            )

    async def _check_preview(self, deployment):
        self._live_preview(deployment)
        snapshot = await self.core.snapshot()
        old = self.backend.read(deployment.old.id)
        if (
            snapshot.version != deployment.source_version
            or not old
            or old.fingerprint != deployment.old.fingerprint
        ):
            raise ValueError("source_conflict")
        if any(r.enabled and r.rule.id == old.rule.id and r.id != old.id for r in self.backend.records()):
            raise ValueError("source_conflict")
        if digest(compile_rule(deployment.new_rule, snapshot)) != digest(deployment.compilation):
            raise ValueError("source_conflict")

    async def apply(self, request):
        async with self.lock:
            self._allowed()
            deployment = self.get(request.plan_id)
            if deployment.status != "awaiting_approval":
                return deployment
            if request.approval_digest != deployment.approval_digest:
                raise ValueError("source_conflict")
            try:
                await self._check_preview(deployment)
            except ValueError as exc:
                return self._stage(
                    deployment, "expired" if str(exc).startswith("expired") else "failed", str(exc)
                )
            deployment = self._stage(deployment, "staging", "create_disabled_intent")
            try:
                candidate = await self.backend.create_disabled(deployment.new_id, deployment.new_rule)
                if self.backend.read(candidate.id) != candidate:
                    raise ValueError("source_conflict")
                deployment = self._stage(
                    deployment, "staged_disabled", "disabled_candidate_readback_verified"
                )
                # Recheck the external source after staging, before pausing it.
                await self._check_preview(deployment)
                deployment = self._stage(deployment, "pausing_old", "pause_old_intent")
                await self.backend.toggle(deployment.old.id, deployment.old.fingerprint, False)
                if self.backend.read(deployment.old.id) != record(
                    deployment.old.id, deployment.old.rule, False
                ):
                    raise ValueError("source_conflict")
                deployment = self._stage(deployment, "old_paused", "old_disabled_readback_verified")
                deployment = self._stage(deployment, "activating", "enable_new_intent")
                if self.backend.read(deployment.old.id) != record(
                    deployment.old.id, deployment.old.rule, False
                ):
                    raise ValueError("source_conflict")
                await self.backend.toggle(candidate.id, candidate.fingerprint, True)
                if self.backend.read(candidate.id) != record(candidate.id, deployment.new_rule, True):
                    raise ValueError("source_conflict")
                deployment = self._stage(deployment, "activated", "candidate_enabled_readback_verified")
                return self._reconcile(deployment, verify=True)
            except asyncio.CancelledError:
                self._stage(
                    deployment, "needs_reconcile", "interrupted_switch_requires_configuration_readback"
                )
                raise
            except (OSError, ValueError):
                deployment = self._stage(
                    deployment, "needs_reconcile", "backend_or_source_change_requires_reconciliation"
                )
                # Read actual configuration before compensating; never overwrite an edited source.
                return await self._rollback(deployment)

    def _configuration(self, deployment):
        old, new = self.backend.read(deployment.old.id), self.backend.read(deployment.new_id)
        old_ok = old is not None and digest(old.rule) == digest(deployment.old.rule)
        new_ok = new is None or digest(new.rule) == digest(deployment.new_rule)
        others = [
            r.id
            for r in self.backend.records()
            if r.enabled
            and r.rule.id == deployment.old.rule.id
            and r.id not in {deployment.old.id, deployment.new_id}
        ]
        return (
            old,
            new,
            {
                "old_unchanged": old_ok,
                "new_unchanged": new_ok,
                "old_enabled": old.enabled if old else None,
                "new_enabled": new.enabled if new else None,
                "other_enabled_versions": others,
                "physical_compensation": False,
                "checked_at": datetime.now(UTC).isoformat(),
            },
        )

    def _reconcile(self, deployment, *, verify=False):
        _, _, facts = self._configuration(deployment)
        matched = (
            facts["old_unchanged"]
            and facts["new_unchanged"]
            and not facts["old_enabled"]
            and facts["new_enabled"]
            and not facts["other_enabled_versions"]
        )
        return self._stage(
            deployment,
            "verified" if verify and matched else "needs_reconcile",
            "SIMULATED_configuration_verified_not_physical"
            if verify and matched
            else "configuration_read_only_reconciled",
            reconciled=facts,
        )

    async def reconcile(self, identifier):
        async with self.lock:
            self._allowed()
            deployment = self.get(identifier)
            if deployment.status in {"validated", "awaiting_approval", "rolled_back", "failed", "expired"}:
                return deployment
            return self._reconcile(deployment, verify=True)

    async def _rollback(self, deployment):
        old, new, facts = self._configuration(deployment)
        if not facts["old_unchanged"] or not facts["new_unchanged"] or facts["other_enabled_versions"]:
            return self._stage(
                deployment, "needs_reconcile", "source_edited_no_automatic_overwrite", reconciled=facts
            )
        deployment = self._stage(
            deployment, "rolling_back", "configuration_only_rollback_intent", reconciled=facts
        )
        try:
            if new and new.enabled:
                await self.backend.toggle(new.id, new.fingerprint, False)
            # Preserve the original source; no old-rule deletion or device compensation.
            current = self.backend.read(old.id)
            if not current or digest(current.rule) != digest(deployment.old.rule):
                raise ValueError("source_conflict")
            if current and current.enabled != deployment.old.enabled:
                await self.backend.toggle(current.id, current.fingerprint, deployment.old.enabled)
            _, _, facts = self._configuration(deployment)
            if (
                not facts["old_unchanged"]
                or facts["old_enabled"] != deployment.old.enabled
                or facts["new_enabled"] is True
            ):
                raise ValueError("source_conflict")
            return self._stage(
                deployment, "rolled_back", "SIMULATED_old_configuration_restored", reconciled=facts
            )
        except (OSError, ValueError):
            return self._stage(deployment, "needs_reconcile", "rollback_unconfirmed_readback_required")

    async def rollback(self, identifier):
        async with self.lock:
            self._allowed()
            deployment = self.get(identifier)
            if deployment.status in {"validated", "awaiting_approval", "failed", "expired", "rolled_back"}:
                return deployment
            return await self._rollback(deployment)
