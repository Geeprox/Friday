"""Explicit household intent supplies short, current Core execution leases.

The durable record is a user's operating choice, not detected human sleep.
It never grants device authority, replays events or survives a source/scope change.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import math
import sqlite3
import time
from typing import Literal

from pydantic import AwareDatetime, Field

from .contracts import Contract
from .ha import HAReadOnlyAdapter
from .rule_compiler import digest


class ModeIntent(Contract):
    revision: int = Field(ge=1, strict=True)
    binding: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    mode: Literal['awake', 'sleep', 'manual', 'unknown']
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    runtime_boot: str = Field(min_length=1, max_length=128)
    manual_hold_until: AwareDatetime | None = None


class HouseholdMode:
    def __init__(self, core, runtime, *, wall_now=None):
        self.core, self.runtime, self.store = core, runtime, core.store
        self.wall_now = wall_now or (lambda: datetime.now(UTC))
        self.lock = asyncio.Lock()
        self.owned_modes = None
        self.failed_revision = None
        runtime.mode_source_guard = self.valid
        runtime.on_manual_hold = self.record_manual_hold
        with self.store.connection:
            self.store.connection.execute('CREATE TABLE IF NOT EXISTS household_mode_intent '
                                          '(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, body TEXT NOT NULL)')

    def _row(self):
        return self.store.connection.execute('SELECT revision,body FROM household_mode_intent WHERE id=1').fetchone()

    @staticmethod
    def _intent(row):
        if row is None:
            return None
        if type(row[0]) is not int or row[0] < 1 or len(row[1]) > 4096:
            raise ValueError('household_mode_unavailable')
        value = ModeIntent.model_validate_json(row[1])
        if value.revision != row[0]:
            raise ValueError('household_mode_unavailable')
        return value

    def binding(self):
        manager, adapter = self.runtime.manager, self.core.adapter
        if not isinstance(adapter, HAReadOnlyAdapter) or not manager.available():
            return None
        return digest(['household-mode/1', adapter.url, adapter.instance_id,
                       self.core.actions.policy_report()['revision']])

    def status(self):
        row = self._row()
        revision = row[0] if row is not None and type(row[0]) is int else 0
        binding = self.binding()
        token = digest([self.runtime.boot_id, binding, row, self.runtime.manager.generation])
        result = dict(revision=revision, context=token, mode=None, persistent=False,
                      expires_at=None, state='unconfigured' if binding else 'unavailable')
        try:
            intent = self._intent(row)
            if intent is None or intent.mode == 'unknown':
                return result
            now = self.wall_now()
            result.update(mode=intent.mode, persistent=intent.expires_at is None,
                          expires_at=intent.expires_at.isoformat() if intent.expires_at else None)
            if binding is None:
                result['state'] = 'unavailable'
            elif intent.binding != binding:
                result['state'] = 'changed'
            elif self.failed_revision == revision or now < intent.created_at:
                result['state'] = 'unavailable'
            elif intent.expires_at and (now >= intent.expires_at or intent.runtime_boot != self.runtime.boot_id):
                result['state'] = 'expired'
            else:
                result['state'] = 'current'
        except (ValueError, TypeError):
            result['state'] = 'unavailable'
        return result

    def runtime_status(self):
        raw = self.runtime.status()
        # Conversation feedback needs mode evidence, not historical action traces.
        keys = ('available', 'simulated', 'modes', 'mode_revision_valid', 'mode_window_current',
                'execution_state', 'automatic_enabled', 'manual_hold_active')
        return {**{key: raw[key] for key in keys if key in raw}, 'mode_intent': self.status()}

    def valid(self):
        try:
            report = self.status()
            expected={'awake':(False,False),'sleep':(True,False),'manual':(False,True)}.get(report['mode'])
            actual=tuple(self.runtime.modes[key].value for key in ('home.sleep','home.manual'))
            return (report['state'] == 'current' and self.owned_modes is self.runtime.modes
                    and actual == expected)
        except (ValueError, OSError, TypeError, KeyError, sqlite3.Error):
            return False

    def record_manual_hold(self, seconds):
        """Retain manual priority across restart without changing the chosen mode."""
        row = None
        fenced = self.failed_revision is not None
        try:
            row = self._row()
            intent = self._intent(row)
            now = self.wall_now()
            until = now+timedelta(seconds=seconds)
            if intent is None:
                intent = ModeIntent(revision=1, mode='unknown', created_at=now,
                                    runtime_boot=self.runtime.boot_id, manual_hold_until=until)
            else:
                until = max(until, intent.manual_hold_until or until)
                intent = intent.model_copy(update={'revision':intent.revision+1,'manual_hold_until':until})
            with self.store.connection:
                if row is None:
                    self.store.connection.execute('INSERT INTO household_mode_intent VALUES (1,?,?)',
                                                  (intent.revision,intent.model_dump_json()))
                else:
                    changed=self.store.connection.execute(
                        'UPDATE household_mode_intent SET revision=?,body=? WHERE id=1 AND revision=? AND body=?',
                        (intent.revision,intent.model_dump_json(),*row)).rowcount
                    if changed != 1:raise ValueError('household_mode_changed')
            if fenced:
                self.failed_revision = intent.revision
            return True
        except (ValueError,OSError,sqlite3.Error):
            self.failed_revision = row[0] if row is not None else 0
            return False

    async def select(self, mode, *, ttl_seconds=None, expected_context=None, guard=lambda: True):
        if mode not in {'awake', 'sleep', 'manual', 'unknown'} or (
                ttl_seconds is not None and (type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 1800)):
            raise ValueError('invalid_mode_window')
        async with self.lock:
            current = self.status()
            if expected_context is not None and current['context'] != expected_context:
                raise ValueError('household_mode_changed')
            if guard() is not True or not self.runtime.manager.owner.owned:
                raise ValueError('household_mode_unavailable')
            binding = self.binding()
            if mode != 'unknown' and binding is None:
                raise ValueError('household_mode_unavailable')
            previous = self._row()
            self.failed_revision = current['revision']
            await self.runtime.invalidate('explicit_household_mode_changed')
            self.owned_modes = None
            if guard() is not True or self.binding() != binding or self._row() != previous:
                raise ValueError('household_mode_changed')
            now = self.wall_now()
            try:previous_intent=self._intent(previous)
            except ValueError:previous_intent=None
            intent = ModeIntent(revision=current['revision']+1, binding=binding, mode=mode,
                                created_at=now, expires_at=now+timedelta(seconds=ttl_seconds) if ttl_seconds else None,
                                runtime_boot=self.runtime.boot_id,
                                manual_hold_until=previous_intent.manual_hold_until if previous_intent else None)
            with self.store.connection:
                if guard() is not True:
                    raise ValueError('household_mode_changed')
                if previous is None:
                    self.store.connection.execute('INSERT INTO household_mode_intent VALUES (1,?,?)',
                                                  (intent.revision, intent.model_dump_json()))
                else:
                    changed = self.store.connection.execute(
                        'UPDATE household_mode_intent SET revision=?,body=? WHERE id=1 AND revision=? AND body=?',
                        (intent.revision, intent.model_dump_json(), *previous)).rowcount
                    if changed != 1:
                        raise ValueError('household_mode_changed')
            self.failed_revision = None
            await self._maintain(guard)
            return self.runtime_status()

    async def maintain(self):
        async with self.lock:
            await self._maintain(lambda: True)

    async def _maintain(self, guard):
        report = self.status()
        if report['state'] != 'current' or report['mode'] is None:
            if self.runtime.modes or self.runtime.queue or self.runtime.pending:
                await self.runtime.invalidate('household_mode_not_current')
            self.owned_modes = None
            return
        intent = self._intent(self._row())
        if intent.manual_hold_until:
            remaining_hold=(intent.manual_hold_until-self.wall_now()).total_seconds()
            if remaining_hold>0:
                self.runtime.manual_until=max(self.runtime.manual_until,time.monotonic()+min(300,remaining_hold))
        def current():
            later = self.status()
            return (guard() is True and later['state'] == 'current'
                    and later['revision'] == intent.revision and later['context'] == report['context'])
        ttl = 60
        if intent.expires_at:
            ttl = min(ttl, math.floor((intent.expires_at-self.wall_now()).total_seconds()))
        if ttl < 1 or not current():
            await self.runtime.invalidate('household_mode_expired')
            self.owned_modes = None
            return
        remaining = self.runtime.mode_deadline-time.monotonic()
        if (self.owned_modes is self.runtime.modes and self.runtime.modes and remaining > 0
                and self.runtime.mode_generation == self.runtime.manager.generation):
            if remaining <= 20:
                await self.runtime.renew_intent(self.owned_modes, ttl, current)
                self.owned_modes = self.runtime.modes
        else:
            if not current():
                return
            await self.runtime.set_mode(intent.mode, ttl)
            if not current():
                await self.runtime.invalidate('household_mode_changed')
                return
            self.owned_modes = self.runtime.modes
