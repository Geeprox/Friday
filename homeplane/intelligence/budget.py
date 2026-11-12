"""跨进程原子预留；中断或未知结果保留预算，不重放付费请求。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .settings import private_path

COUNTERS = ("requests", "text_tokens", "images", "voice_seconds")


class EnhancementUnavailable(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class UsageLedger:
    def __init__(self, path, *, now=None):
        self.path = path
        self.now = now or (lambda: datetime.now(UTC))
        private_path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.transaction() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS usage_window (
                    id INTEGER PRIMARY KEY CHECK(id=1), start REAL, end REAL,
                    requests INTEGER, text_tokens INTEGER, images INTEGER, voice_seconds INTEGER);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, window REAL, text_tokens INTEGER, status TEXT, voice_seconds INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS cooldown (
                    capability TEXT PRIMARY KEY, code TEXT, until REAL);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            if "voice_seconds" not in {row[1] for row in db.execute("PRAGMA table_info(attempts)")}:
                db.execute("ALTER TABLE attempts ADD COLUMN voice_seconds INTEGER DEFAULT 0")
        path.chmod(0o600)

    @contextmanager
    def transaction(self):
        private_path(self.path)
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _window(self, db, config):
        now = self.now()
        timestamp = now.timestamp()
        row = db.execute("SELECT * FROM usage_window WHERE id=1").fetchone()
        if row is None or timestamp >= row["end"]:
            zone = ZoneInfo(config.timezone)
            day = now.astimezone(zone).date()
            start = datetime.combine(day, time(), zone).timestamp()
            end = datetime.combine(day + timedelta(days=1), time(), zone).timestamp()
            db.execute("INSERT OR REPLACE INTO usage_window VALUES(1,?,?,0,0,0,0)", (start, end))
            db.execute("DELETE FROM attempts WHERE window < ?", (start - 7 * 86400,))
            row = db.execute("SELECT * FROM usage_window WHERE id=1").fetchone()
        if timestamp < row["start"]:
            raise EnhancementUnavailable("usage_clock_regressed")
        return row

    def reserve(self, identifier, capability, config, *, text_tokens=0, images=0, voice_seconds=0):
        amounts = dict(requests=1, text_tokens=text_tokens, images=images, voice_seconds=voice_seconds)
        if any(type(v) is not int or v < 0 for v in amounts.values()):
            raise ValueError("invalid_usage_reservation")
        with self.transaction() as db:
            window = self._window(db, config)
            block = db.execute(
                "SELECT code FROM cooldown WHERE capability IN (?, 'all') AND until > ? LIMIT 1",
                (capability, self.now().timestamp()),
            ).fetchone()
            if block:
                raise EnhancementUnavailable(block["code"])
            if db.execute("SELECT 1 FROM attempts WHERE id=?", (identifier,)).fetchone():
                raise EnhancementUnavailable("request_already_attempted")
            for name in COUNTERS:
                if amounts[name] and window[name] + amounts[name] > getattr(config, "daily_" + name):
                    raise EnhancementUnavailable("daily_" + name + "_reached")
            db.execute(
                "UPDATE usage_window SET requests=requests+1,text_tokens=text_tokens+?,"
                "images=images+?,voice_seconds=voice_seconds+? WHERE id=1",
                (text_tokens, images, voice_seconds),
            )
            db.execute(
                "INSERT INTO attempts(id,window,text_tokens,status,voice_seconds) VALUES(?,?,?,'reserved',?)",
                (identifier, window["start"], text_tokens, voice_seconds),
            )

    def settle(self, identifier, *, text_tokens=None, voice_seconds=None):
        with self.transaction() as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (identifier,)).fetchone()
            if not attempt or attempt["status"] != "reserved":
                return
            if type(text_tokens) is int and text_tokens >= 0:
                db.execute(
                    "UPDATE usage_window SET text_tokens=max(0,text_tokens+?) WHERE id=1 AND start=?",
                    (text_tokens - attempt["text_tokens"], attempt["window"]),
                )
            if type(voice_seconds) is int and voice_seconds >= 0:
                db.execute(
                    "UPDATE usage_window SET voice_seconds=max(0,voice_seconds+?) WHERE id=1 AND start=?",
                    (voice_seconds - attempt["voice_seconds"], attempt["window"]),
                )
            db.execute(
                "UPDATE attempts SET status=? WHERE id=?",
                (
                    "accounted" if text_tokens is not None or voice_seconds is not None else "uncertain",
                    identifier,
                ),
            )

    def block(self, capability, code, seconds):
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO cooldown VALUES(?,?,?)",
                (capability, code, self.now().timestamp() + seconds),
            )

    def clear_blocks(self):
        with self.transaction() as db:
            db.execute("DELETE FROM cooldown")

    def status(self, config):
        with self.transaction() as db:
            row = self._window(db, config)
            return {
                "used": {name: row[name] for name in COUNTERS},
                "limits": {name: getattr(config, "daily_" + name) for name in COUNTERS},
                "resets_at": datetime.fromtimestamp(row["end"], UTC).isoformat(),
                "uncertain_requests": db.execute(
                    "SELECT count(*) FROM attempts WHERE window=? AND status!='accounted'", (row["start"],)
                ).fetchone()[0],
                "blocks": [
                    dict(item)
                    for item in db.execute(
                        "SELECT capability,code,until FROM cooldown WHERE until>?", (self.now().timestamp(),)
                    )
                ],
            }
