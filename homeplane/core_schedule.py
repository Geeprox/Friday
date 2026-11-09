"""Owned calendar input into the existing CoreRules effect/receipt path.

This producer is private to the current runtime. A model, public event or stored
run cannot impersonate its in-memory owner. Occurrence consumption is durable
before effect work and survives action edits, pause/re-enable and restarts.
"""

import asyncio
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta

from .contracts import Event
from .rule_compiler import digest, evaluate_rule
from .rule_contracts import RuleTrial, OnceTrigger, CLOCK_TRIGGERS
from .rule_schedule import clock_event_matches, clock_payload, due_occurrence, next_occurrence, once_occurrence


class CoreSchedule:
    def __init__(self, runtime):
        self.runtime, self.manager = runtime, runtime.manager
        self.store = runtime.store
        self.epoch = object()
        self.last_sample = None
        self.quiet_until = 0
        self.next_wake = None
        self.generation = -1
        if self.manager.owner.owned:
            with self.store.connection:
                self.store.connection.execute(
                    "CREATE TABLE IF NOT EXISTS core_clock_slots "
                    "(rule_id TEXT, schedule TEXT, day TEXT NOT NULL, PRIMARY KEY(rule_id,schedule))"
                )

    def sample(self, now, mono):
        if self.last_sample is not None:
            previous, steady = self.last_sample
            if abs((now - previous).total_seconds() - (mono - steady)) > 2 or mono - steady > 45:
                self.epoch = object()
                self.quiet_until = mono + 30
        self.last_sample = (now, mono)
        return mono >= self.quiet_until

    def current(self, item):
        return (
            item.get("clock_owner") is self.epoch
            and self.sample(datetime.now(UTC), time.monotonic())
            and item.get("clock_owner") is self.epoch
            and clock_event_matches(item["record"].rule.trigger, item["event"])
        )

    def last_day(self, rule_id, schedule):
        row = self.store.connection.execute(
            "SELECT day FROM core_clock_slots WHERE rule_id=? AND schedule=?", (rule_id, schedule)
        ).fetchone()
        if row is None:
            return None
        if not isinstance(row[0], str) or date.fromisoformat(row[0]).isoformat() != row[0]:
            raise ValueError("schedule_receipt_unavailable")
        return row[0]

    def _prune(self, records):
        if self.generation == self.manager.generation:
            return
        keep = {
            (r.rule.id, digest(r.rule.trigger))
            for r in records
            if isinstance(r.rule.trigger, CLOCK_TRIGGERS)
        }
        with self.store.connection:
            for rule_id, signature in self.store.connection.execute(
                "SELECT rule_id,schedule FROM core_clock_slots"
            ).fetchall():
                if (rule_id, signature) not in keep:
                    self.store.connection.execute(
                        "DELETE FROM core_clock_slots WHERE rule_id=? AND schedule=?", (rule_id, signature)
                    )
        self.generation = self.manager.generation

    async def poll(self):
        from .core_rules import CoreRuleRun

        runtime = self.runtime
        async with self.manager.lock:
            if runtime.closed or runtime.failure or not self.manager.owner.owned:
                return
            records = self.manager.records()
            self._prune(records)
            active = [r for r in records if r.enabled and isinstance(r.rule.trigger, CLOCK_TRIGGERS)]
            self.next_wake = None
            if not active:
                self.last_sample = None
                return
            now, mono = datetime.now(UTC), time.monotonic()
            if not self.sample(now, mono):
                self.next_wake = self.quiet_until
                return
            for record in active:
                trigger = record.rule.trigger
                grace = record.rule.execution.max_event_age_ms / 1000
                signature = digest(trigger)
                occurrence = due_occurrence(trigger, now, grace_seconds=grace)
                missed = False
                if occurrence is None and isinstance(trigger, OnceTrigger):
                    candidate = once_occurrence(trigger)
                    if now > candidate.at + timedelta(seconds=grace):
                        occurrence, missed = candidate, True
                last = self.last_day(record.rule.id, signature)
                if occurrence is None or last is not None and occurrence.day <= last:
                    continue
                adapter = self.manager.core.adapter
                connection = getattr(getattr(adapter, "_stream", None), "connection_id", None)
                event_id = "clock:" + digest([record.rule.id, signature, occurrence.day])
                event = None if missed else Event(
                    event_id=event_id,
                    source="friday.core.clock",
                    kind="source-report",
                    correlation_id=record.rule.id,
                    source_seq=(connection + ":clock") if connection else None,
                    boot_id=adapter._boot,
                    wall_time=occurrence.at,
                    received_at=now,
                    monotonic_time=mono - (now - occurrence.at).total_seconds(),
                    time_basis="local_event",
                    payload=clock_payload(trigger, occurrence),
                    simulated=False,
                )
                snapshot, code = None, None
                if missed:
                    code = "expired_event_no_replay"
                elif not self.manager.available():
                    code = "source_or_runtime_unavailable"
                else:
                    try:
                        async with asyncio.timeout(2):
                            snapshot = await self.manager.core.snapshot()
                    except Exception:
                        code = "source_or_runtime_unavailable"
                current, steady = datetime.now(UTC), time.monotonic()
                remaining = -1 if event is None else min(
                    grace - (current - event.wall_time).total_seconds(),
                    grace - (steady - event.monotonic_time),
                )
                if remaining <= 0:
                    code = "expired_event_no_replay"
                evaluated = (
                    evaluate_rule(
                        record.rule,
                        snapshot,
                        RuleTrial(event=event, modes=runtime.modes),
                        now=current,
                        allow_core=True,
                    )
                    if snapshot is not None and code is None
                    else None
                )
                matched = evaluated is not None and evaluated.outcome == "matched"
                run = CoreRuleRun(
                    id="clock-run:" + digest([event_id, record.rule.id]),
                    record_id=record.id,
                    rule_id=record.rule.id,
                    fingerprint=record.fingerprint,
                    event_id=event_id,
                    boot_id=runtime.boot_id,
                    created_at=current,
                    expires_at=occurrence.at + timedelta(seconds=grace),
                    status="running" if matched else "suppressed",
                    code=code or ("live_event_admitted" if matched else evaluated.reasons[0]),
                )
                if self.store.connection.execute("SELECT count(*) FROM core_rule_runs").fetchone()[0] >= 1000:
                    raise ValueError("core_run_capacity")
                with self.store.connection:
                    # A slot and its first diagnostic receipt commit together;
                    # no await or effect occurs before this durable boundary.
                    self.store.connection.execute(
                        "INSERT OR REPLACE INTO core_clock_slots VALUES (?,?,?)",
                        (record.rule.id, signature, occurrence.day),
                    )
                    saved = runtime._save(run, new=True)
                if not matched or not saved:
                    continue
                runtime._cancel(record.rule.id, "new_event_cancels_pending")
                item = dict(
                    run=run,
                    record=record,
                    adapter=adapter,
                    event=event,
                    cursor=0,
                    deadline=event.monotonic_time + grace,
                    due=steady,
                    clock_owner=self.epoch,
                )
                failure = runtime._guard(item)
                if failure or self.manager.scope(record.rule, snapshot) != record.source_scope:
                    runtime._stage(run, "suppressed", failure or "source_binding_changed")
                else:
                    runtime.pending[record.rule.id] = item
            # Sleep until the next actual calendar instant, bounded by the
            # original runtime's health wake. Do not poll every camera frame.
            now, mono = datetime.now(UTC), time.monotonic()
            waits = []
            for record in active:
                last = self.last_day(record.rule.id, digest(record.rule.trigger))
                due = next_occurrence(record.rule.trigger, now, after_day=last)
                if due:
                    waits.append(max(0.05, (due.at - now).total_seconds()))
            self.next_wake = mono + min(waits) if waits else None

    def wait_seconds(self):
        return min(30, max(0.05, self.next_wake - time.monotonic())) if self.next_wake is not None else 30

    def one_off_state(self, record, now):
        trigger = record.rule.trigger
        result = dict(record_id=record.id, fingerprint=record.fingerprint,
                      at=trigger.at, timezone=trigger.timezone, state='unavailable')
        if not self.manager.owner.owned:
            return result
        due = once_occurrence(trigger)
        try:
            consumed = self.last_day(record.rule.id, digest(trigger))
            if consumed is not None and due.day <= consumed:
                event_id = 'clock:' + digest([record.rule.id, digest(trigger), due.day])
                try:
                    run = self.runtime.get('clock-run:' + digest([event_id, record.rule.id]))
                except ValueError as exc:
                    if str(exc) != 'not_found':
                        raise
                    result['state'] = 'finished'
                else:
                    if run.fingerprint == record.fingerprint:
                        result['code'] = run.code
                    result['state'] = (
                        'finished' if run.fingerprint != record.fingerprint else
                        'completed' if run.status == 'completed' else
                        'unconfirmed' if run.status == 'unknown_outcome' else
                        'running' if run.status in {'running', 'waiting'} else
                        'missed' if run.code == 'expired_event_no_replay' else 'not_executed'
                    )
            elif not record.enabled:
                result['state'] = 'paused' if now < due.at else 'cancelled'
            elif now > due.at + timedelta(milliseconds=record.rule.execution.max_event_age_ms):
                result['state'] = 'missed'
            else:
                result['state'] = 'scheduled'
        except (ValueError, sqlite3.Error):
            pass
        return result

    def status(self):
        records, now = self.manager.records(), datetime.now(UTC)
        one_off = {r.id: self.one_off_state(r, now) for r in records if isinstance(r.rule.trigger, OnceTrigger)}
        count = sum(r.enabled and isinstance(r.rule.trigger, CLOCK_TRIGGERS)
                    and (not isinstance(r.rule.trigger, OnceTrigger)
                         or one_off[r.id]['state'] in {'scheduled', 'running'}) for r in records)
        return dict(
            scheduled_count=count,
            one_off_tasks=one_off,
            state="off"
            if not count
            else "recovering"
            if time.monotonic() < self.quiet_until
            else "waiting"
            if self.last_sample is None
            else "ready",
            catch_up=False,
        )
