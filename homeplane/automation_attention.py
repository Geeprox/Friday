"""Read-only task outcomes and exact, local acknowledgements; never an executor."""

from datetime import UTC, datetime

from .rule_compiler import digest


class AutomationAttention:
    LIMIT = 64
    ACK_LIMIT = 1024
    REVIEW_CODES = {'source_or_runtime_unavailable', 'source_binding_changed',
                    'authority_changed_requires_review', 'household_mode_source_unavailable',
                    'old_connection_event_no_replay', 'rule_change_requires_fresh_mode_window'}

    def __init__(self, runtime):
        self.runtime, self.manager, self.db = runtime, runtime.manager, runtime.store.connection
        with self.db:
            self.db.execute(
                'CREATE TABLE IF NOT EXISTS home_task_attention_seen ('
                'id TEXT PRIMARY KEY, record_id TEXT NOT NULL, fingerprint TEXT NOT NULL, '
                'run_id TEXT, acknowledged_at TEXT NOT NULL)'
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS core_rule_outcome ON core_rule_runs(json_extract(body,'$.status'))"
            )
            self.db.execute('CREATE TABLE IF NOT EXISTS home_task_run_titles (run_id TEXT PRIMARY KEY, title TEXT NOT NULL)')

    def remember(self, run):
        record = self.manager.read(run.record_id)
        if record and record.fingerprint == run.fingerprint:
            self.db.execute('INSERT OR IGNORE INTO home_task_run_titles VALUES (?,?)', (run.id, record.rule.intent))

    @staticmethod
    def one_off(record, state):
        kind = 'missed' if state['state'] == 'missed' else 'not_executed'
        return dict(id=digest(['task-attention-v1', record.id, record.fingerprint, kind]),
                    record_id=record.id, fingerprint=record.fingerprint, run_id=None,
                    kind=kind, title=record.rule.intent, at=state['at'],
                    timezone=state['timezone'], record_current=True)

    @staticmethod
    def unconfirmed(run, record, title=None):
        current = bool(record and record.fingerprint == run.fingerprint)
        return dict(id=digest(['task-attention-v1', run.id, run.fingerprint, 'unconfirmed']),
                    record_id=run.record_id, fingerprint=run.fingerprint, run_id=run.id,
                    kind='unconfirmed', title=title or (record.rule.intent if current else '先前的自动任务'),
                    at=run.created_at.isoformat(), timezone=None, record_current=current)

    def status(self, clock):
        if not self.manager.owner.owned:
            return dict(available=False, items=[], has_more=False)
        from .core_rules import CoreRuleRun

        records = {record.id: record for record in self.manager.records()}
        seen = {row[0] for row in self.db.execute('SELECT id FROM home_task_attention_seen')}
        items = []
        for identifier, state in clock.get('one_off_tasks', {}).items():
            record = records.get(identifier)
            actionable = state['state'] == 'missed' or (state['state'] == 'not_executed' and state.get('code') in self.REVIEW_CODES)
            if record and actionable and state['fingerprint'] == record.fingerprint:
                item = self.one_off(record, state)
                if item['id'] not in seen:
                    items.append(item)
        rows = self.db.execute(
            "SELECT r.body,t.title FROM core_rule_runs r LEFT JOIN home_task_run_titles t ON t.run_id=r.id "
            "WHERE json_extract(r.body,'$.status')='unknown_outcome' "
            "AND NOT EXISTS (SELECT 1 FROM home_task_attention_seen a WHERE a.run_id=r.id "
            "AND a.fingerprint=json_extract(r.body,'$.fingerprint')) ORDER BY r.rowid DESC LIMIT ?",
            (self.LIMIT + 1,),
        )
        for row in rows:
            run = CoreRuleRun.model_validate_json(row[0])
            items.append(self.unconfirmed(run, records.get(run.record_id), row[1]))
        items.sort(key=lambda item: item['at'], reverse=True)
        return dict(available=True, items=items[:self.LIMIT], has_more=len(items) > self.LIMIT)

    def prune(self):
        self.db.execute('DELETE FROM home_task_run_titles WHERE NOT EXISTS (SELECT 1 FROM core_rule_runs r WHERE r.id=home_task_run_titles.run_id)')
        records = {record.id: record for record in self.manager.records()}
        for identifier, record_id, fingerprint, run_id in self.db.execute(
            'SELECT id,record_id,fingerprint,run_id FROM home_task_attention_seen'
        ).fetchall():
            expired = (not self.db.execute('SELECT 1 FROM core_rule_runs WHERE id=?', (run_id,)).fetchone()
                       if run_id else record_id not in records or records[record_id].fingerprint != fingerprint)
            if expired:
                self.db.execute('DELETE FROM home_task_attention_seen WHERE id=?', (identifier,))

    def acknowledge(self, identifier, request):
        if not self.manager.owner.owned:
            raise ValueError('task_attention_unavailable')
        old = self.db.execute(
            'SELECT record_id,fingerprint,run_id FROM home_task_attention_seen WHERE id=?', (identifier,)
        ).fetchone()
        expected = (request.record_id, request.expected_fingerprint, request.run_id)
        if old:
            if tuple(old) != expected:
                raise ValueError('source_conflict')
        else:
            record = self.manager.read(request.record_id)
            if request.run_id:
                run = self.runtime.get(request.run_id)
                title = self.db.execute('SELECT title FROM home_task_run_titles WHERE run_id=?', (run.id,)).fetchone()
                item = self.unconfirmed(run, record, title[0] if title else None)
                if run.status != 'unknown_outcome':
                    raise ValueError('source_conflict')
            else:
                if record is None or record.rule.trigger.type != 'once':
                    raise ValueError('source_conflict')
                state = self.runtime.clock.one_off_state(record, datetime.now(UTC))
                if not (state['state'] == 'missed' or (state['state'] == 'not_executed' and state.get('code') in self.REVIEW_CODES)):
                    raise ValueError('source_conflict')
                item = self.one_off(record, state)
            if item['id'] != identifier or (item['record_id'], item['fingerprint'], item['run_id']) != expected:
                raise ValueError('source_conflict')
            with self.db:
                self.prune()
                if self.db.execute('SELECT count(*) FROM home_task_attention_seen').fetchone()[0] >= self.ACK_LIMIT:
                    raise ValueError('task_attention_capacity')
                self.db.execute('INSERT INTO home_task_attention_seen VALUES (?,?,?,?,?)',
                                (identifier, *expected, datetime.now(UTC).isoformat()))
        assert self.db.execute('SELECT 1 FROM home_task_attention_seen WHERE id=?', (identifier,)).fetchone()
        return dict(status='acknowledged', id=identifier, device_actions=False)
