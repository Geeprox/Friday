"""Bounded diagnostics. Never store speech, audio, images, tokens or remote responses."""

import json
import sqlite3
import time
from pathlib import Path


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, payload TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts REAL, status TEXT)")
        self.db.execute("UPDATE seen SET status='interrupted_unknown' WHERE status='processing'")
        self.db.commit()

    def reserve(self, event_id):
        # Deduplication retained for 24h; media older than max_age is independently rejected.
        self.db.execute("DELETE FROM seen WHERE ts < ?", (time.time() - 86400,))
        cur = self.db.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, 'processing')", (event_id, time.time())
        )
        self.db.commit()
        return cur.rowcount == 1

    def record(self, category, status, event_id=None, **fields):
        allowed = {
            k: v
            for k, v in fields.items()
            if k in ("error_type", "device_id", "action", "reason", "latency_ms", "value")
        }
        payload = {
            "time": time.time(),
            "category": category,
            "status": status,
            "event_id": event_id,
            **allowed,
        }
        self.db.execute("INSERT INTO events(ts,payload) VALUES(?,?)", (payload["time"], json.dumps(payload)))
        self.db.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)"
        )
        self.db.commit()

    def finish(self, result):
        self.db.execute("UPDATE seen SET status=? WHERE id=?", (result.status, result.event_id))
        fields = result.model_dump()
        fields.pop("event_id")
        status = fields.pop("status")
        self.record("command", status, result.event_id, **fields)

    def events(self, limit=60):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT payload FROM events ORDER BY id DESC LIMIT ?", (limit,))
        ]

    def close(self):
        self.db.close()
