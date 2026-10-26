"""Owned Simulator Rule IR execution. No HA/Geek dispatch or recovered queue.

One bounded scheduler; SQLite claims precede effects. The same deployment lock
protects version checks, while source identity is checked again after snapshot IO.
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from .contracts import Contract, Event
from .rule_compiler import evaluate_rule
from .rule_contracts import CancelInstruction, DelayInstruction, ModeLease, RuleTrial
from .simulator import Simulator


class RuleRun(Contract):
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
    history: list[dict] = Field(default_factory=list, max_length=64)
    simulated: Literal[True] = True
    real_write_attempted: Literal[False] = False
    physically_confirmed: Literal[False] = False


class RuleRuntime:
    def __init__(self, manager, *, wall_now=None, monotonic=None):
        self.manager, self.core, self.store = manager, manager.core, manager.store
        self.now = wall_now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.boot_id = str(uuid4())
        self.pending = {}
        self.modes = {}
        self.worker = None
        self.wake = asyncio.Event()
        self.closed = False
        self.worker_failure = None
        with self.store.connection:
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS rule_runs (id TEXT PRIMARY KEY, event_id TEXT, record_id TEXT, rule_id TEXT, created TEXT, status TEXT, body TEXT, UNIQUE(event_id,rule_id))"
            )
        if self.manager.owned:
            # Retain execution evidence; never restore a timer/command after boot.
            for row in self.store.connection.execute(
                "SELECT body FROM rule_runs WHERE status IN ('running','waiting')"
            ).fetchall():
                old = RuleRun.model_validate_json(row[0])
                self._stage(old, "cancelled", "restart_discards_pending_never_replays")
        self._prune()

    def _prune(self):
        if self.manager.owned:
            with self.store.connection:
                self.store.connection.execute(
                    "DELETE FROM rule_runs WHERE created<? AND status NOT IN ('running','waiting')",
                    ((self.now() - timedelta(days=7)).isoformat(),),
                )

    def start(self):
        if self.worker is None and not self.closed and not self.worker_failure and self.manager.owned:
            self.worker = asyncio.create_task(self._worker(), name="simulated-rule-runtime")

    def _fail(self, code):
        self.worker_failure = code
        for key in list(self.pending):
            try:
                self._cancel(key, code + "_no_retry")
            except Exception:
                self.pending.pop(key, None)

    async def _snapshot(self):
        try:
            async with asyncio.timeout(2):
                return await self.core.snapshot()
        except Exception:
            self._fail("runtime_snapshot_failed")
            return None

    async def _worker(self):
        while not self.closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed worker is terminal. Never reconnect and drain a queue.
                self._fail("runtime_worker_failed")
                return
            if self.worker_failure:
                return
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=0.1 if self.pending else 30)
            except TimeoutError:
                pass
            finally:
                self.wake.clear()

    def _save(self, run, *, new=False):
        body = run.model_dump_json()
        if len(body.encode()) > 16384:
            raise ValueError("runtime_trace_budget")
        event = Event(
            source="SIMULATED-rule-runtime",
            kind="action-result",
            correlation_id=run.id,
            causation_id=run.event_id,
            wall_time=self.now(),
            received_at=self.now(),
            time_basis="local_event",
            monotonic_time=self.monotonic(),
            boot_id=self.boot_id,
            simulated=True,
            payload={
                "rule_run_id": run.id,
                "rule_id": run.rule_id,
                "status": run.status,
                "code": run.code,
                "simulated_effect_count": len(run.effects),
                "real_write_attempted": False,
            },
        )
        with self.store.connection:
            if new:
                result = self.store.connection.execute(
                    "INSERT OR IGNORE INTO rule_runs VALUES (?,?,?,?,?,?,?)",
                    (
                        run.id,
                        run.event_id,
                        run.record_id,
                        run.rule_id,
                        run.created_at.isoformat(),
                        run.status,
                        body,
                    ),
                )
                if result.rowcount != 1:
                    return False
            else:
                self.store.connection.execute(
                    "UPDATE rule_runs SET status=?,body=? WHERE id=?", (run.status, body, run.id)
                )
            self.store.connection.execute(
                "INSERT INTO events VALUES (?,?,?)",
                (event.event_id, event.wall_time.isoformat(), event.model_dump_json()),
            )
        self.store.prune()
        return True

    def _stage(self, run, status, code, **changes):
        next_run = run.model_copy(
            update={
                **changes,
                "status": status,
                "code": code,
                "history": [*run.history, {"at": self.now().isoformat(), "status": status, "code": code}][
                    -64:
                ],
            }
        )
        self._save(next_run)
        return next_run

    def _cancel(self, key, code):
        item = self.pending.pop(key, None)
        if item:
            self._stage(item["run"], "cancelled", code)

    async def invalidate(self, code="source_or_context_changed", *, reset_modes=False):
        async with self.manager.lock:
            for key in list(self.pending):
                self._cancel(key, code)
            if reset_modes:
                self.modes = {}
        self.wake.set()

    async def set_mode(self, mode):
        if mode not in {"awake", "sleep", "manual", "unknown"}:
            raise ValueError("unsupported_simulated_mode")
        async with self.manager.lock:
            self.manager._allowed()
            if self.closed or self.worker_failure:
                raise ValueError("runtime_unavailable")
            for key in list(self.pending):
                self._cancel(key, "explicit_mode_changed")
            now = self.now()
            self.modes = (
                {}
                if mode == "unknown"
                else {
                    name: ModeLease(
                        value=(mode == state), issued_at=now, valid_until=now + timedelta(seconds=30)
                    )
                    for name, state in (("home.sleep", "sleep"), ("home.manual", "manual"))
                }
            )
        self.start()
        self.wake.set()
        return self.status()

    def status(self):
        self._prune()
        rows = self.store.connection.execute(
            "SELECT body FROM rule_runs ORDER BY rowid DESC LIMIT 20"
        ).fetchall()
        return {
            "available": self.manager.owned
            and isinstance(self.core.adapter, Simulator)
            and not self.closed
            and not self.worker_failure,
            "simulated": True,
            "pending_count": len(self.pending),
            "worker_running": bool(self.worker and not self.worker.done()),
            "modes": {k: v.model_dump(mode="json") for k, v in self.modes.items()},
            "worker_failure": self.worker_failure,
            "runs": [json.loads(row[0]) for row in rows],
            "real_dispatch": "unsupported",
            "replay_policy": "never_actuate",
            "external_rules_intercepted": False,
        }

    def get(self, identifier):
        self._prune()
        row = self.store.connection.execute("SELECT body FROM rule_runs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("not_found")
        return RuleRun.model_validate_json(row[0])

    async def ingest(self, event, *, replay=False):
        async with self.manager.lock:
            self.manager._allowed()
            if self.closed or self.worker_failure:
                return {"accepted": False, "code": "runtime_unavailable"}
            adapter = self.core.adapter
            now, mono = self.now(), self.monotonic()
            # Caller must supply a live registered Simulator event, not a journal
            # snapshot / foreign boot or a manufactured event from the browser.
            if (
                replay
                or not event.simulated
                or event.boot_id != adapter.boot_id
                or event.kind not in {"state-transition", "source-report"}
                or not any(e.event_id == event.event_id and e == event for e in adapter.events)
            ):
                return {"accepted": False, "code": "replay_foreign_or_unregistered_event"}
            age = (now - event.wall_time).total_seconds()
            if not 0 <= age <= 30 or not 0 <= mono - event.monotonic_time <= 30:
                return {"accepted": False, "code": "expired_or_future_event"}
            snapshot = await self._snapshot()
            if snapshot is None:
                return {"accepted": False, "code": "runtime_snapshot_failed"}
            if adapter is not self.core.adapter:
                return {"accepted": False, "code": "source_changed"}
            now, mono = self.now(), self.monotonic()
            age = (now - event.wall_time).total_seconds()
            admitted = 0
            for current in self.manager.backend.records():
                rule = current.rule
                if not current.enabled or not rule.simulated:
                    continue
                p = event.payload
                if (p.get("entity_id"), p.get("capability")) != (
                    rule.trigger.entity,
                    rule.trigger.capability,
                ):
                    continue
                # A duplicate must never cancel a newer pending run.
                if self.store.connection.execute(
                    "SELECT 1 FROM rule_runs WHERE event_id=? AND rule_id=?", (event.event_id, rule.id)
                ).fetchone():
                    continue
                if age * 1000 > rule.execution.max_event_age_ms:
                    continue
                self._cancel(rule.id, "new_source_event_cancels_delay")
                trial = RuleTrial(event=event, modes=self.modes)
                evaluated = evaluate_rule(rule, snapshot, trial, now=now)
                run = RuleRun(
                    id=str(uuid4()),
                    record_id=current.id,
                    rule_id=rule.id,
                    fingerprint=current.fingerprint,
                    event_id=event.event_id,
                    boot_id=self.boot_id,
                    created_at=now,
                    expires_at=event.wall_time + timedelta(milliseconds=rule.execution.max_event_age_ms),
                    status="running" if evaluated.outcome == "matched" else "suppressed",
                    code="event_admitted" if evaluated.outcome == "matched" else evaluated.reasons[0],
                )
                self._prune()
                if (
                    self.store.connection.execute("SELECT count(*) FROM rule_runs").fetchone()[0] >= 1000
                    or len(self.pending) >= 64
                ):
                    return {"accepted": False, "code": "runtime_capacity_exceeded"}
                if not self._save(run, new=True):
                    continue
                admitted += 1
                if evaluated.outcome == "matched":
                    self.pending[rule.id] = {
                        "run": run,
                        "record": current,
                        "adapter": adapter,
                        "event": event,
                        "cursor": 0,
                        "due": mono,
                    }
                    # Deadline uses the tighter wall/monotonic residual lifetime.
                    self.pending[rule.id]["deadline"] = mono + min(
                        (run.expires_at - now).total_seconds(),
                        rule.execution.max_event_age_ms / 1000 - (mono - event.monotonic_time),
                    )
        self.start()
        self.wake.set()
        await self.tick()
        return {"accepted": True, "admitted_count": admitted, "simulated": True}

    async def tick(self):
        async with self.manager.lock:
            if not self.pending:
                return
            adapter = self.core.adapter
            if (
                not isinstance(adapter, Simulator)
                or not self.manager.owned
                or self.closed
                or self.worker_failure
            ):
                for key in list(self.pending):
                    self._cancel(key, "source_or_owner_unavailable")
                return
            snapshot = await self._snapshot()
            if snapshot is None:
                return
            for key, item in list(self.pending.items()):
                run, saved = item["run"], item["record"]
                current = self.manager.backend.read(saved.id)
                now, mono = self.now(), self.monotonic()
                if adapter is not self.core.adapter or item["adapter"] is not adapter:
                    self._cancel(key, "source_changed")
                    continue
                if (
                    current is None
                    or not current.enabled
                    or current.fingerprint != saved.fingerprint
                    or sum(r.enabled and r.rule.id == saved.rule.id for r in self.manager.backend.records())
                    != 1
                ):
                    self._cancel(key, "rule_paused_replaced_or_conflicting")
                    continue
                if now < run.created_at or now >= run.expires_at or mono >= item["deadline"]:
                    self._cancel(key, "expired_clock_or_worker_resume")
                    continue
                # Revalidate even while waiting: a degraded quality/mode does not
                # leave a dormant action that can become live upon recovery.
                evaluated = evaluate_rule(
                    saved.rule.model_copy(
                        update={
                            "actions": [
                                a
                                for a in saved.rule.actions
                                if not isinstance(a, (DelayInstruction, CancelInstruction))
                            ]
                        }
                    ),
                    snapshot,
                    RuleTrial(event=item["event"], modes=self.modes),
                    now=now,
                )
                if evaluated.outcome != "matched":
                    self._cancel(key, "revalidation:" + evaluated.reasons[0])
                    continue
                if mono < item["due"]:
                    continue
                actions = saved.rule.actions
                while item["cursor"] < len(actions):
                    action = actions[item["cursor"]]
                    item["cursor"] += 1
                    if isinstance(action, DelayInstruction):
                        item["due"] = self.monotonic() + action.milliseconds / 1000
                        item["run"] = self._stage(item["run"], "waiting", "cancellable_delay_started")
                        break
                    if isinstance(action, CancelInstruction):
                        item["run"] = self._stage(item["run"], "running", "cancel_scope_same_rule_only")
                        continue
                    fresh = await self._snapshot()
                    if fresh is None:
                        break
                    checked = self.manager.backend.read(saved.id)
                    if (
                        adapter is not self.core.adapter
                        or checked is None
                        or not checked.enabled
                        or checked.fingerprint != saved.fingerprint
                        or self.monotonic() >= item["deadline"]
                        or self.now() >= run.expires_at
                    ):
                        self._cancel(key, "send_guard_changed")
                        break
                    checked_result = evaluate_rule(
                        saved.rule.model_copy(update={"actions": [action]}),
                        fresh,
                        RuleTrial(event=item["event"], modes=self.modes),
                        now=self.now(),
                    )
                    if checked_result.outcome != "matched":
                        self._cancel(key, "send_guard:" + checked_result.reasons[0])
                        break
                    instruction = checked_result.commands[0]
                    item["run"] = self._stage(item["run"], "running", "effect_claimed_before_simulated_write")
                    try:
                        adapter.apply_managed(instruction, run_id=run.id)
                    except Exception:
                        self.pending.pop(key, None)
                        self._stage(item["run"], "unknown_outcome", "simulated_actuator_failed_no_retry")
                        break
                    item["run"] = self._stage(
                        item["run"],
                        "running",
                        "simulated_effect_applied",
                        effects=[*item["run"].effects, instruction],
                    )
                else:
                    self.pending.pop(key, None)
                    self._stage(item["run"], "completed", "simulated_sequence_completed")

    async def close(self):
        self.closed = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        await self.invalidate("runtime_closed_no_replay")
