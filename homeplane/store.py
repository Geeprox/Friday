"""Bounded household trace store. Local SQLite only; no media or credentials."""
import os
import re
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .action_contracts import LightExecutionTrace
from .contracts import ActionTrace, Event


def local_database_path(path: Path):
    """Existing output-location policy, with no directory creation or database open."""
    path = path.expanduser().resolve()
    # Conservative local location allowlist. Symlinks are resolved before checking.
    allowed = [Path.cwd().resolve() / "data", Path("/private/tmp"), Path("/tmp").resolve()]
    import tempfile
    allowed.append(Path(tempfile.gettempdir()).resolve())
    if not any(path.is_relative_to(root) for root in allowed):
        raise ValueError("local_database_path_required: use workspace data/ or local temporary directory")
    if str(path).startswith(("/Volumes/", "/Network/", "/net/")):
        raise ValueError("network_database_unsupported")
    # macOS statvfs exposes ST_LOCAL; reject network mounts even behind symlinks.
    existing = path.parent
    while not existing.exists():
        existing = existing.parent
    # Verify actual mounted filesystem, including bind/symlink locations.
    if __import__("sys").platform == "darwin":
        mounted = subprocess.run(["/sbin/mount"], capture_output=True, text=True, check=True).stdout
        candidates = []
        for line in mounted.splitlines():
            match = re.match(r".+ on (.+) \((.+)\)$", line)
            if match and existing.is_relative_to(Path(match[1])):
                candidates.append((len(match[1]), match[2].split(", ")))
        if not candidates or "local" not in max(candidates)[1]:
            raise ValueError("network_database_unsupported")
    flag = getattr(os, "ST_LOCAL", None)
    if flag is not None and not os.statvfs(existing).f_flag & flag:
        raise ValueError("network_database_unsupported")
    return path


class TraceStore:
    def __init__(self, path: Path, *, max_rows=1000, retention_days=7, wall_now=None):
        self.wall_now = wall_now or (lambda: datetime.now(UTC))
        path = local_database_path(path)
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA secure_delete=ON")
        self.connection.execute("PRAGMA journal_size_limit=1048576")
        self.connection.execute("PRAGMA max_page_count=4096")
        self.connection.execute("CREATE TABLE IF NOT EXISTS traces (id TEXT PRIMARY KEY, key TEXT UNIQUE, created TEXT, body TEXT)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, created TEXT, body TEXT)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS light_actions (id TEXT PRIMARY KEY, key TEXT UNIQUE, created TEXT, status TEXT, body TEXT)")
        self.max_rows, self.retention_days = max_rows, retention_days
        self.prune()

    def prune(self):
        cutoff = (self.wall_now() - timedelta(days=self.retention_days)).isoformat()
        with self.connection:
            self.connection.execute("DELETE FROM traces WHERE created < ?", (cutoff,))
            self.connection.execute("DELETE FROM light_actions WHERE created < ? AND status NOT IN ('sent','accepted')", (cutoff,))
            self.connection.execute("DELETE FROM events WHERE created < ?", (cutoff,))
            self.connection.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY rowid DESC LIMIT ?)", (self.max_rows,))
            self.connection.execute("DELETE FROM traces WHERE id NOT IN (SELECT id FROM traces ORDER BY rowid DESC LIMIT ?)", (self.max_rows,))
            self.connection.execute("DELETE FROM light_actions WHERE status NOT IN ('sent','accepted') AND id NOT IN (SELECT id FROM light_actions ORDER BY rowid DESC LIMIT ?)", (self.max_rows,))

    def light_action(self, action_id=None, *, key=None):
        self.prune()
        column, value = ("key", key) if key is not None else ("id", action_id)
        row = self.connection.execute(f"SELECT body FROM light_actions WHERE {column}=?", (value,)).fetchone()
        return LightExecutionTrace.model_validate_json(row[0]) if row else None

    def save_light_action(self, trace, *, expected_status=None):
        body = trace.model_dump_json()
        if len(body.encode()) > 32768:
            raise ValueError("trace_budget_exceeded")
        with self.connection:
            if expected_status is None:
                if self.connection.execute("SELECT count(*) FROM light_actions").fetchone()[0] >= self.max_rows:
                    raise ValueError("action_capacity_exceeded")
                self.connection.execute("INSERT INTO light_actions VALUES (?,?,?,?,?)", (
                    trace.action_id, trace.request.idempotency_key, trace.created_at.isoformat(), trace.status, body))
            else:
                result = self.connection.execute("UPDATE light_actions SET status=?,body=? WHERE id=? AND status=?", (
                    trace.status, body, trace.action_id, expected_status))
                if result.rowcount != 1:
                    raise ValueError("source_conflict")
            # Persist stage and corresponding journal event in the same local transaction.
            for event in trace.events:
                event_body = event.model_dump_json()
                if len(event_body.encode()) > 16384:
                    raise ValueError("event_budget_exceeded")
                self.connection.execute("INSERT OR IGNORE INTO events VALUES (?,?,?)", (
                    event.event_id, event.wall_time.isoformat(), event_body))

    def unresolved_light_actions(self):
        return [LightExecutionTrace.model_validate_json(row[0]) for row in self.connection.execute(
            "SELECT body FROM light_actions WHERE status IN ('sent','accepted')")]

    def get(self, action_id: str) -> ActionTrace | None:
        self.prune()
        row = self.connection.execute("SELECT body FROM traces WHERE id=?", (action_id,)).fetchone()
        return ActionTrace.model_validate_json(row[0]) if row else None

    def by_key(self, key: str) -> ActionTrace | None:
        self.prune()
        row = self.connection.execute("SELECT body FROM traces WHERE key=?", (key,)).fetchone()
        return ActionTrace.model_validate_json(row[0]) if row else None

    def save(self, trace: ActionTrace):
        body = trace.model_dump_json()
        if len(body.encode()) > 32768:
            raise ValueError("trace_budget_exceeded")
        with self.connection:
            self.connection.execute("INSERT INTO traces VALUES (?,?,?,?)", (
                trace.action_id, trace.request.idempotency_key, datetime.now(UTC).isoformat(), body,
            ))
        self.prune()

    def append_events(self, events: list[Event]):
        with self.connection:
            for event in events:
                body = event.model_dump_json()
                if len(body.encode()) > 16384:
                    raise ValueError("event_budget_exceeded")
                self.connection.execute("INSERT OR IGNORE INTO events VALUES (?,?,?)", (
                    event.event_id, event.wall_time.astimezone(UTC).isoformat(), body,
                ))
        self.prune()

    def recent_events(self, *, simulated: bool):
        self.prune()
        events = [Event.model_validate_json(row[0]) for row in self.connection.execute(
            "SELECT body FROM events ORDER BY rowid DESC LIMIT 200"
        )]
        return sorted((e for e in events if e.simulated == simulated), key=lambda e: e.wall_time)

    def recent(self):
        self.prune()
        return [ActionTrace.model_validate_json(row[0]) for row in self.connection.execute(
            "SELECT body FROM traces ORDER BY rowid DESC LIMIT 100"
        )]

    def close(self):
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.connection.close()
