"""Standalone NAS guest song station. Python stdlib only; no household or audio APIs."""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import socketserver
import sqlite3
import stat
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    from integrations.ktv_public.coordinator import PERSISTENT_EXPIRY, Coordinator, private_origin
    from integrations.ktv_public.management import Management
except ImportError:  # Standalone NAS export, alongside this module.
    from coordinator import PERSISTENT_EXPIRY, Coordinator, private_origin
    from management import Management

ID = re.compile(r"[1-9][0-9]{0,17}\Z")
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
STATES = {"queued", "acquiring", "separating", "transcoding", "ready", "failed", "cancelled"}
CODES = {
    "none",
    "cancelled",
    "interrupted",
    "source_unavailable",
    "source_changed",
    "download_unavailable",
    "download_timeout",
    "download_identity_mismatch",
    "download_cancelled",
    "decode_failed",
    "separation_failed",
    "transcode_failed",
    "invalid_audio",
    "storage_full",
    "internal_error",
    "source_login_required",
    "source_rights_denied",
    "source_api_unavailable",
    "source_file_invalid",
}
TERMINAL = {"ready", "failed", "cancelled"}
THEMES = {"content", "festival", "live"}
VOCAL_PRESETS = {"dry", "pop", "ktv", "power", "opera", "studio", "echo"}
SFX_ACTIONS = {"sfx_applause", "sfx_cheer", "sfx_stop"}
SFX_TTL = 5
SERVICE_EVENT_LIMIT = 1024 * 1024
SERVICE_OPERATIONS = {"status", "search", "discover", "playlist", "guide", "monitor", "songs", "join",
                      "profile", "theme", "controls", "command", "order", "retry"}
AUDIO_PLAYBACK_FIELDS = {"audio_capabilities", "vocal_preset", "vocal_preset_state",
                         "sfx_seq", "sfx_action", "sfx_state"}
NAMES = (
    "汽水猫",
    "夜航兔",
    "慢慢鲸",
    "摇摆熊",
    "月球旅人",
    "小幽灵",
    "唱片君",
    "星星糖",
    "柠檬汽水",
    "仙人掌",
    "北极企鹅",
    "节拍小子",
)
ARTISTS = ["周杰伦", "陈奕迅", "林俊杰", "五月天", "孙燕姿", "张惠妹", "张学友", "王菲", "邓紫棋", "薛之谦", "陶喆", "林宥嘉", "李荣浩", "郭顶", "告五人", "许嵩", "周深", "张杰", "赵雷", "陈粒", "苏打绿", "王力宏", "刘德华", "梁静茹"]


class Refusal(ValueError):
    def __init__(self, status, code):
        self.status, self.code = status, code


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def audio_playback(value):
    """Fixed optional TV reports; missing fields keep pre-0.48 clients unavailable."""
    caps = value.get("audio_capabilities", {"vocal_presets": ["dry"], "sfx": False})
    if (not isinstance(caps, dict) or (not {"vocal_presets", "sfx"} <= set(caps) or set(caps) - {"vocal_presets", "sfx", "voice_protection"})
            or not isinstance(caps["vocal_presets"], list) or not 1 <= len(caps["vocal_presets"]) <= len(VOCAL_PRESETS)
            or any(not isinstance(p, str) or p not in VOCAL_PRESETS for p in caps["vocal_presets"])
            or len(set(caps["vocal_presets"])) != len(caps["vocal_presets"])
            or "dry" not in caps["vocal_presets"] or type(caps["sfx"]) is not bool):
        raise ValueError("invalid_audio_capabilities")
    voice = caps.get("voice_protection")
    if "voice_protection" in caps and (
            not isinstance(voice, dict) or (not {"state", "effective_gain"} <= set(voice) or set(voice)-{"state", "effective_gain", "mute_confirmed"})
            or not isinstance(voice["state"], str)
            or voice["state"] not in {"unavailable", "off", "preparing", "monitoring", "attenuated", "muted", "failed"}
            or ("mute_confirmed" in voice and type(voice["mute_confirmed"]) is not bool)
            or type(voice["effective_gain"]) is not int or not 0 <= voice["effective_gain"] <= 100):
        raise ValueError("invalid_voice_protection")
    preset, preset_state = value.get("vocal_preset", "dry"), value.get("vocal_preset_state", "unavailable")
    seq, action, state = value.get("sfx_seq", 0), value.get("sfx_action", ""), value.get("sfx_state", "idle")
    if (not isinstance(preset, str) or preset not in VOCAL_PRESETS
            or not isinstance(preset_state, str) or preset_state not in {"unavailable", "pending", "requested", "applied", "failed"}
            or type(seq) is not int or not 0 <= seq <= value["last_command"]
            or not isinstance(action, str) or action not in SFX_ACTIONS | {""}
            or not isinstance(state, str) or state not in {"idle", "pending", "playing", "stopped", "expired", "failed"}
            or (seq == 0 and (action != "" or state != "idle"))
            or (seq > 0 and (action not in SFX_ACTIONS or state == "idle"))
            or (action == "sfx_stop" and state in {"pending", "playing"})):
        raise ValueError("invalid_audio_playback")
    return {"audio_capabilities": {"vocal_presets": list(caps["vocal_presets"]), "sfx": caps["sfx"],
                                    **({"voice_protection": dict(voice)} if voice is not None else {})},
            "vocal_preset": preset, "vocal_preset_state": preset_state,
            "sfx_seq": seq, "sfx_action": action, "sfx_state": state}


def song_title(value):
    # Kept standalone for the stdlib-only NAS deployment.
    title = value.strip()
    if not re.search(r"\.(flac|mp3|m4a|wav|aac|ogg|ape|alac)$", title, re.I):
        return title
    title = re.sub(r"\.(flac|mp3|m4a|wav|aac|ogg|ape|alac)$", "", title, flags=re.I)
    title = re.sub(r"^\d{1,3}[.、 _-]+", "", title)
    if " - " in title:
        artist, candidate = title.split(" - ", 1)
        if artist and candidate:
            title = candidate
    return title.strip()


class Store:
    def __init__(self, root, origin, base_path="/ktv", *, worker_origin=None, allow_loopback_worker=False, recovery_enabled=False, background_health=False):
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root:
            raise ValueError("unsafe_root")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.db = self.root / "state.sqlite"
        self.origin, self.base_path = origin.rstrip("/"), base_path.rstrip("/")
        self._backup_before_guest_queue_upgrade()
        self.lock = threading.RLock()
        self.event_lock = threading.Lock()
        self.limits = {}
        self.search_cache = {}
        self.recommendations = None
        self.playlist_cache = {}
        self.artist_refreshing = False
        self.coordinator = None
        with self.connection() as db:
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
              expires INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS songs(id TEXT PRIMARY KEY, room TEXT NOT NULL, song_id TEXT NOT NULL,
              title TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
              code TEXT NOT NULL DEFAULT 'none', created INTEGER NOT NULL, lease INTEGER NOT NULL DEFAULT 0,
              UNIQUE(room,request_id));
            CREATE INDEX IF NOT EXISTS songs_room ON songs(room,created);
            CREATE TABLE IF NOT EXISTS heartbeat(id INTEGER PRIMARY KEY, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS guests(id TEXT PRIMARY KEY, room TEXT NOT NULL, token_hash TEXT NOT NULL,
              nickname TEXT NOT NULL, avatar INTEGER NOT NULL, created INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS guests_room ON guests(room);
            CREATE TABLE IF NOT EXISTS retries(room TEXT NOT NULL,request_id TEXT NOT NULL,song TEXT NOT NULL,PRIMARY KEY(room,request_id));
            CREATE TABLE IF NOT EXISTS commands(seq INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
              request_id TEXT NOT NULL, action TEXT NOT NULL, song TEXT NOT NULL, created INTEGER NOT NULL,
              UNIQUE(room,request_id));
            CREATE TABLE IF NOT EXISTS song_requests(room TEXT NOT NULL, request_id TEXT NOT NULL,
              guest_id TEXT NOT NULL, song_id TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
              queue_id TEXT NOT NULL, was_duplicate INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(room,request_id));
            CREATE TABLE IF NOT EXISTS cloud_catalog(song_id TEXT PRIMARY KEY,title TEXT NOT NULL,artists TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_staging(position INTEGER PRIMARY KEY,song_id TEXT UNIQUE,title TEXT NOT NULL,artists TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_sync(id INTEGER PRIMARY KEY,revision TEXT NOT NULL,total INTEGER NOT NULL,received INTEGER NOT NULL,updated INTEGER NOT NULL);
            """)
            for table, name, kind in (
                ("rooms", "theme", "TEXT NOT NULL DEFAULT 'content'"),
                ("rooms", "theme_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("guests", "nickname_custom", "INTEGER NOT NULL DEFAULT -1"),
                ("songs", "guest_id", "TEXT NOT NULL DEFAULT ''"),
                ("songs", "nickname", "TEXT NOT NULL DEFAULT '朋友'"),
                ("songs", "avatar", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "artwork", "TEXT NOT NULL DEFAULT ''"),
                ("songs", "position", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "attempt", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "progress", "TEXT NOT NULL DEFAULT '{}'"),
                ("songs", "cache_hit", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "source", "TEXT NOT NULL DEFAULT 'song'"),
                ("songs", "removed", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_sha", "TEXT NOT NULL DEFAULT ''"),
                ("songs", "guide_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_duration", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_reference_sha", "TEXT NOT NULL DEFAULT ''"),
                ("songs", "guide_reference_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_reference_duration", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_reference_kind", "TEXT NOT NULL DEFAULT ''"),
                ("rooms", "queue_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "music_gain", "INTEGER NOT NULL DEFAULT 70"),
                ("rooms", "mic_gain", "INTEGER NOT NULL DEFAULT 50"),
                ("rooms", "control_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "mic_muted", "INTEGER NOT NULL DEFAULT 1"),
                ("rooms", "mic_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "original", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "vocal_preset", "TEXT NOT NULL DEFAULT 'dry'"),
                ("rooms", "sfx_gain", "INTEGER NOT NULL DEFAULT 60"),
                ("rooms", "audio_capabilities", "TEXT NOT NULL DEFAULT '{}'"),
                ("rooms", "actual_vocal_preset", "TEXT NOT NULL DEFAULT 'dry'"),
                ("rooms", "vocal_preset_state", "TEXT NOT NULL DEFAULT 'unavailable'"),
                ("rooms", "sfx_seq", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "sfx_action", "TEXT NOT NULL DEFAULT ''"),
                ("rooms", "sfx_state", "TEXT NOT NULL DEFAULT 'idle'"),
                ("rooms", "playback_at", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "current_song", "TEXT NOT NULL DEFAULT ''"),
                ("rooms", "played", "TEXT NOT NULL DEFAULT '[]'"),
                ("rooms", "last_command", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "playback_state", "TEXT NOT NULL DEFAULT 'idle'"),
                ("rooms", "safety_latched", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "actual_mic_muted", "INTEGER NOT NULL DEFAULT 1"),
                ("rooms", "applied_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "position_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "position_at", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "lyrics_waiting", "TEXT NOT NULL DEFAULT ''"),
                ("rooms", "playback_epoch", "TEXT NOT NULL DEFAULT ''"),
                ("rooms", "mic_request_at", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "mic_request_revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}:
                    db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + kind)
            db.execute("UPDATE songs SET position=rowid WHERE position=0")
            db.execute("INSERT OR IGNORE INTO song_requests(room,request_id,guest_id,song_id,title,source,queue_id) "
                "SELECT room,request_id,guest_id,song_id,title,source,id FROM songs")
            self._upgrade_guest_names(db)
            # Session lifetime, online presence and QR credentials are separate.
            # Preserve active rows/guests/songs when upgrading the old 4h lease.
            db.execute("UPDATE rooms SET expires=? WHERE closed=0", (PERSISTENT_EXPIRY,))
        self.db.chmod(0o600)
        self.coordinator = Coordinator(self, Refusal, worker_origin=worker_origin,
                                       allow_loopback=allow_loopback_worker, recovery_enabled=recovery_enabled,
                                       background_health=background_health)
        self.management = Management(self)
        if background_health:
            self.management.start()

    @staticmethod
    def _logical_database_hash(connection):
        digest = hashlib.sha256()
        for setting in ("user_version", "application_id"):
            digest.update(str(connection.execute("PRAGMA " + setting).fetchone()[0]).encode())
            digest.update(b"\n")
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _regular_private_file(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("unsafe_migration_backup")
        return info

    def _independent_backup_file(self, path):
        info = self._regular_private_file(path)
        live = self._regular_private_file(self.db)
        if (info.st_dev, info.st_ino) == (live.st_dev, live.st_ino):
            raise ValueError("unsafe_migration_backup")
        return info

    def _checked_backup(self, path):
        self._independent_backup_file(path)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("invalid_migration_backup")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"rooms", "songs", "guests"} <= tables:
                raise ValueError("invalid_migration_backup")
            logical_hash = self._logical_database_hash(connection)
        finally:
            connection.close()
        return logical_hash

    def _backup_before_guest_queue_upgrade(self):
        """Run on the NAS itself, before any schema/data mutation of an old DB."""
        try:
            info = self._regular_private_file(self.db)
        except FileNotFoundError:
            return
        if info.st_size == 0:
            return
        # A previous process may have exited during a transaction. SQLite needs
        # write access to recover its hot rollback journal before even reading
        # the schema. query_only still forbids application writes; no migration
        # runs until the consistent pre-migration backup is complete.
        source = sqlite3.connect(self.db.as_uri() + "?mode=rw", uri=True, timeout=5)
        temporary = None
        try:
            source.execute("PRAGMA query_only=ON")
            source.execute("BEGIN")
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                return
            columns = {row[1] for row in source.execute("PRAGMA table_info(guests)")}
            if "nickname_custom" in columns and "song_requests" in tables:
                return
            if not {"rooms", "songs", "guests"} <= tables:
                raise ValueError("invalid_migration_source")
            source_hash = self._logical_database_hash(source)
            fixed = self.root / "state-before-guest-queue-v2.sqlite"
            if fixed.exists() or fixed.is_symlink():
                self._independent_backup_file(fixed)
            receipt_path = self.root / "guest-queue-v2-backup.json"
            candidate = fixed
            if receipt_path.exists() or receipt_path.is_symlink():
                receipt_info = self._regular_private_file(receipt_path)
                if receipt_info.st_size > 4096:
                    raise ValueError("invalid_migration_receipt")
                receipt = json.loads(receipt_path.read_text())
                if not isinstance(receipt, dict):
                    raise ValueError("invalid_migration_receipt")
                name = receipt.get("file", "")
                if (receipt.get("schema") != "guest-queue-v2" or not isinstance(name, str)
                        or not re.fullmatch(r"state-before-guest-queue-v2(?:-[0-9]{8}T[0-9]{6}-[a-f0-9]{32})?\.sqlite", name)):
                    raise ValueError("invalid_migration_receipt")
                candidate = self.root / name
                self._independent_backup_file(candidate)
                if not isinstance(receipt.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", receipt["sha256"]):
                    raise ValueError("invalid_migration_receipt")
                if hashlib.sha256(candidate.read_bytes()).hexdigest() != receipt["sha256"]:
                    raise ValueError("invalid_migration_backup")
            reuse = False
            if candidate.exists() or candidate.is_symlink():
                reuse = self._checked_backup(candidate) == source_hash
            elif candidate != fixed:
                raise ValueError("missing_migration_backup")
            if not reuse:
                target = fixed if not fixed.exists() and not fixed.is_symlink() else self.root / (
                    "state-before-guest-queue-v2-" + time.strftime("%Y%m%dT%H%M%S", time.gmtime())
                    + "-" + uuid.uuid4().hex + ".sqlite")
                # A pre-existing fixed backup must be safe even when a later receipt
                # points at another snapshot. Historical files are never replaced.
                if fixed.exists() or fixed.is_symlink():
                    self._checked_backup(fixed)
                descriptor, name = tempfile.mkstemp(prefix=".guest-queue-v2-", suffix=".sqlite", dir=self.root)
                os.close(descriptor)
                temporary = Path(name)
                destination = sqlite3.connect(str(temporary), timeout=5)
                try:
                    source.backup(destination)
                    destination.execute("PRAGMA journal_mode=DELETE")
                finally:
                    destination.close()
                if self._checked_backup(temporary) != source_hash:
                    raise ValueError("invalid_migration_backup")
                with temporary.open("rb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    os.fsync(stream.fileno())
                # link() publishes the completed file atomically and refuses any
                # existing destination; replace() would silently overwrite it.
                os.link(temporary, target)
                temporary.unlink()
                temporary = None
                candidate = target
            self._independent_backup_file(candidate)
            candidate.chmod(0o600)
            receipt = {"schema": "guest-queue-v2", "file": candidate.name,
                "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                "logical_sha256": source_hash, "created_at": int(candidate.stat().st_mtime),
                "verified_at": int(time.time()), "reused": reuse}
            descriptor, name = tempfile.mkstemp(prefix=".guest-queue-v2-", suffix=".json", dir=self.root)
            temporary = Path(name)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(receipt, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, receipt_path)
            temporary = None
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            source.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _default_guest_profile(occupied):
        """A room-local display name, independent of the guest's credentials."""
        available = [avatar for avatar, name in enumerate(NAMES) if name not in occupied]
        avatar = available[secrets.randbelow(len(available))] if available else secrets.randbelow(len(NAMES))
        name = NAMES[avatar]
        suffix = 2
        while name in occupied:
            name = f"{NAMES[avatar]} · {suffix}"
            suffix += 1
        return name, avatar

    def _upgrade_guest_names(self, db):
        # -1 identifies pre-marker rows only. A saved profile is never inferred
        # to be generated, even if it deliberately uses one of the templates.
        guests = db.execute(
            "SELECT id,room,nickname,avatar,nickname_custom FROM guests ORDER BY created,rowid"
        ).fetchall()
        occupied, first, protected, counts = {}, {}, {}, {}
        for guest in guests:
            occupied.setdefault(guest["room"], set()).add(guest["nickname"])
            group = (guest["room"], guest["nickname"])
            first.setdefault(group, guest["id"])
            legacy_template = (guest["nickname_custom"] == -1 and 0 <= guest["avatar"] < len(NAMES)
                and guest["nickname"] == NAMES[guest["avatar"]])
            if not legacy_template:
                protected.setdefault(group, guest["id"])
            counts[group] = counts.get(group, 0) + 1
        changed_rooms = set()
        for guest in guests:
            if guest["nickname_custom"] != -1:
                continue
            group = (guest["room"], guest["nickname"])
            generated_collision = (0 <= guest["avatar"] < len(NAMES)
                and guest["nickname"] == NAMES[guest["avatar"]] and counts[group] > 1)
            if generated_collision and guest["id"] != protected.get(group, first[group]):
                nickname, avatar = self._default_guest_profile(occupied[guest["room"]])
                occupied[guest["room"]].add(nickname)
                db.execute(
                    "UPDATE guests SET nickname=?,avatar=? WHERE id=?",
                    (nickname, avatar, guest["id"]),
                )
                db.execute(
                    "UPDATE songs SET nickname=?,avatar=? WHERE guest_id=? AND room=?",
                    (nickname, avatar, guest["id"], guest["room"]),
                )
                changed_rooms.add(guest["room"])
            # Freeze all other legacy names. Do not guess again on a later
            # restart, or strip suffixes that may belong to an edited name.
            db.execute("UPDATE guests SET nickname_custom=? WHERE id=?",
                (0 if generated_collision else 1, guest["id"]))
        for room_id in changed_rooms:
            db.execute("UPDATE rooms SET queue_revision=queue_revision+1 WHERE id=?", (room_id,))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.db), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            changed = db.total_changes > 0
            db.close()
            if changed and getattr(self, "coordinator", None) is not None:
                self.coordinator.changed()

    def budget(self, key, count=40, seconds=60):
        now = time.monotonic()
        with self.lock:
            if len(self.limits) > 4096:
                self.limits = {k: v for k, v in self.limits.items() if v[0] > now}
                if len(self.limits) > 4096:
                    raise Refusal(429, "busy")
            deadline, used = self.limits.get(key, (now + seconds, 0))
            if now > deadline:
                deadline, used = now + seconds, 0
            if used >= count:
                raise Refusal(429, "rate_limited")
            self.limits[key] = (deadline, used + 1)

    def record_failure(self, operation, error):
        """Private fixed categories only; never write exception text or request data."""
        names = {"OperationalError", "DatabaseError", "IntegrityError", "ProgrammingError",
                 "DataError", "InterfaceError", "NotSupportedError", "JSONDecodeError",
                 "KeyError", "TypeError", "ValueError", "OSError", "UnicodeDecodeError", "UnicodeEncodeError"}
        kind = type(error).__name__
        category = kind if kind in names else "OtherError"
        sqlite_code = getattr(error, "sqlite_errorcode", None)
        sqlite_names = {sqlite3.SQLITE_BUSY: "SQLITE_BUSY", sqlite3.SQLITE_LOCKED: "SQLITE_LOCKED",
            sqlite3.SQLITE_ERROR: "SQLITE_ERROR", sqlite3.SQLITE_IOERR: "SQLITE_IOERR",
            sqlite3.SQLITE_CANTOPEN: "SQLITE_CANTOPEN", sqlite3.SQLITE_CORRUPT: "SQLITE_CORRUPT",
            sqlite3.SQLITE_READONLY: "SQLITE_READONLY", sqlite3.SQLITE_FULL: "SQLITE_FULL",
            sqlite3.SQLITE_SCHEMA: "SQLITE_SCHEMA", sqlite3.SQLITE_CONSTRAINT: "SQLITE_CONSTRAINT"}
        record = {"event": "request_failed", "at": int(time.time()),
            "operation": operation if operation in SERVICE_OPERATIONS else "other", "exception": category,
            "sqlite": sqlite_names.get(sqlite_code & 255, "SQLITE_OTHER") if type(sqlite_code) is int else ""}
        path, previous = self.root / "service-events.jsonl", self.root / "service-events.previous.jsonl"
        try:
            with self.event_lock:
                if path.is_symlink() or previous.is_symlink():
                    return
                if path.exists() and (not path.is_file() or path.stat().st_size >= SERVICE_EVENT_LIMIT):
                    if not path.is_file() or (previous.exists() and not previous.is_file()):
                        return
                    os.replace(path, previous)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                try:
                    if stat.S_ISREG(os.fstat(descriptor).st_mode):
                        os.fchmod(descriptor, 0o600)
                        os.write(descriptor, (json.dumps(record, separators=(",", ":")) + "\n").encode())
                finally:
                    os.close(descriptor)
        except OSError:
            pass  # Diagnostic storage must not replace the original response.

    def _expire(self, db):
        now = int(time.time())
        # An unacknowledged enable gesture is short-lived. A stale poll or
        # resumed network connection cannot turn it into a delayed unmute.
        db.execute("UPDATE rooms SET mic_muted=1,control_revision=control_revision+1,mic_revision=mic_revision+1,mic_request_at=0 WHERE closed=0 AND mic_muted=0 AND mic_request_at>0 AND mic_request_at<=?", (now - 5,))
        db.execute(
            "UPDATE songs SET state='cancelled',code='cancelled' WHERE state NOT IN ('ready','failed','cancelled') "
            "AND room IN (SELECT id FROM rooms WHERE closed=1)"
        )
        # Retention may clean explicitly closed sessions only. Paused, empty,
        # warming and disconnected sessions never expire implicitly.
        expired_rooms = "SELECT id FROM rooms WHERE closed=1 AND expires<?"
        expired_songs = "SELECT id FROM songs WHERE room IN (" + expired_rooms + ")"
        for table in ("coordinator_manifest_versions", "coordinator_manifests"):
            db.execute("DELETE FROM " + table + " WHERE song IN (" + expired_songs + ") OR song NOT IN (SELECT id FROM songs)",
                       (now - 86400,))
        for table in ("room_tokens", "coordinator_start_requests", "coordinator_session", "retries", "commands"):
            db.execute("DELETE FROM " + table + " WHERE room IN (" + expired_rooms + ") OR room NOT IN (SELECT id FROM rooms)",
                       (now - 86400,))
        # Content-addressed media/cache verification records are independent
        # of session/requester metadata and remain available for later songs.
        db.execute("DELETE FROM songs WHERE room IN (SELECT id FROM rooms WHERE closed=1 AND expires<?)", (now - 86400,))
        db.execute("DELETE FROM song_requests WHERE room IN (SELECT id FROM rooms WHERE closed=1 AND expires<?)", (now - 86400,))
        db.execute("DELETE FROM guests WHERE room IN (SELECT id FROM rooms WHERE closed=1 AND expires<?)", (now - 86400,))
        db.execute("DELETE FROM rooms WHERE closed=1 AND expires<?", (now - 86400,))

    def expire_unmute(self, room_id):
        cutoff = int(time.time()) - 5
        with self.connection() as db:
            row = db.execute("SELECT mic_muted,mic_request_at FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not row or row["mic_muted"] or not 0 < row["mic_request_at"] <= cutoff:
            return
        with self.lock, self.connection() as db:
            db.execute("UPDATE rooms SET mic_muted=1,control_revision=control_revision+1,mic_revision=mic_revision+1,mic_request_at=0 WHERE id=? AND closed=0 AND mic_muted=0 AND mic_request_at>0 AND mic_request_at<=?", (room_id, cutoff))

    def room(self, room_id, key, guest_key=""):
        if not UUID.fullmatch(room_id) or not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", key):
            raise Refusal(403, "room_unavailable")
        with self.connection() as db:
            # Authentication reads must not contend for the SQLite writer lock.
            # Expiration is checked below; write paths retain cleanup duties.
            value = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
            valid = value and hmac.compare_digest(value["token_hash"], sha(key))
            if value and not valid and guest_key:
                # A new QR does not evict visitors who already joined with the
                # previous QR. It cannot admit a new visitor with an old link.
                valid = bool(db.execute("SELECT 1 FROM room_tokens WHERE room=? AND token_hash=?", (room_id, sha(key))).fetchone()
                             and db.execute("SELECT 1 FROM guests WHERE room=? AND token_hash=?", (room_id, sha(guest_key))).fetchone())
            if not valid:
                raise Refusal(403, "room_unavailable")
            if value["closed"]:
                raise Refusal(410, "room_closed")
            return dict(value)

    def create(self, ttl=14400):
        if type(ttl) is not int or not 300 <= ttl <= 28800:
            raise Refusal(400, "invalid_ttl")
        with self.lock, self.connection() as db:
            self._expire(db)
            if db.execute("SELECT count(*) FROM rooms WHERE closed=0").fetchone()[0] >= 8:
                raise Refusal(429, "room_capacity")
            room_id, key, expires = str(uuid.uuid4()), secrets.token_urlsafe(32), PERSISTENT_EXPIRY
            muted = 1  # A new scene never grants microphone enable implicitly.
            db.execute("INSERT INTO rooms(id,token_hash,expires,mic_muted) VALUES(?,?,?,?)", (room_id, sha(key), expires, muted))
            if getattr(self, "management", None):
                defaults = self.management.preferences()
                db.execute("UPDATE rooms SET " + ",".join(key + "=?" for key in defaults) + " WHERE id=?",
                           (*defaults.values(), room_id))
        return {
            "room_id": room_id,
            "expires": expires,
            "persistent": True,
            "theme": "content",
            "theme_revision": 0,
            "join_url": self.origin
            + self.base_path
            + "/#"
            + urllib.parse.urlencode({"r": room_id, "k": key}),
        }

    def join(self, room):
        with self.lock, self.connection() as db:
            self._expire(db)
            active = db.execute("SELECT closed,expires FROM rooms WHERE id=?", (room["id"],)).fetchone()
            if not active or active["closed"]:
                raise Refusal(410, "room_closed")
            if db.execute("SELECT count(*) FROM guests WHERE room=?", (room["id"],)).fetchone()[0] >= 60:
                raise Refusal(429, "guest_capacity")
            guest_id, key = str(uuid.uuid4()), secrets.token_urlsafe(32)
            occupied = {row[0] for row in db.execute("SELECT nickname FROM guests WHERE room=?", (room["id"],))}
            nickname, avatar = self._default_guest_profile(occupied)
            db.execute(
                "INSERT INTO guests(id,room,token_hash,nickname,avatar,created,nickname_custom) VALUES(?,?,?,?,?,?,0)",
                (guest_id, room["id"], sha(key), nickname, avatar, int(time.time())),
            )
        return {"id": guest_id, "key": key, "nickname": nickname, "avatar": avatar}

    def guest(self, room, key):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", key):
            raise Refusal(403, "guest_unavailable")
        with self.connection() as db:
            value = db.execute(
                "SELECT id,nickname,avatar FROM guests WHERE room=? AND token_hash=?", (room["id"], sha(key))
            ).fetchone()
        if not value:
            raise Refusal(403, "guest_unavailable")
        return dict(value)

    def profile(self, room, guest, payload):
        if (
            set(payload) != {"nickname", "avatar"}
            or not isinstance(payload["nickname"], str)
            or not 1 <= len(payload["nickname"].strip()) <= 24
            or any(ord(c) < 32 or ord(c) == 127 for c in payload["nickname"])
            or type(payload["avatar"]) is not int
            or not 0 <= payload["avatar"] < len(NAMES)
        ):
            raise Refusal(400, "invalid_profile")
        nickname, avatar = payload["nickname"].strip(), payload["avatar"]
        with self.lock, self.connection() as db:
            db.execute(
                "UPDATE guests SET nickname=?,avatar=?,nickname_custom=1 WHERE id=? AND room=?",
                (nickname, avatar, guest["id"], room["id"]),
            )
            db.execute(
                "UPDATE songs SET nickname=?,avatar=? WHERE guest_id=? AND room=?",
                (nickname, avatar, guest["id"], room["id"]),
            )
        return {"id": guest["id"], "nickname": nickname, "avatar": avatar}

    def theme(self, room, payload):
        if (
            set(payload) != {"theme"}
            or not isinstance(payload["theme"], str)
            or payload["theme"] not in THEMES
        ):
            raise Refusal(400, "invalid_theme")
        with self.lock, self.connection() as db:
            db.execute(
                "UPDATE rooms SET theme=?,theme_revision=theme_revision+1 WHERE id=? AND closed=0",
                (payload["theme"], room["id"]),
            )
        return {"theme": payload["theme"]}

    def close(self, room_id):
        if not UUID.fullmatch(room_id):
            raise Refusal(400, "invalid_room")
        with self.lock, self.connection() as db:
            db.execute("UPDATE rooms SET closed=1,expires=? WHERE id=?", (int(time.time()), room_id))
            self._expire(db)
        return {"closed": True}

    def online(self, db):
        if self.coordinator is not None and self.coordinator.active():
            return self.coordinator.worker_healthy()
        value = db.execute("SELECT at FROM heartbeat WHERE id=1").fetchone()
        return bool(value and time.time() - value[0] < 30)

    def status(self, room):
        self.expire_unmute(room["id"])
        readiness = self.coordinator.readiness() if self.coordinator is not None else {"state": "unavailable", "ready": False}
        with self.connection() as db:
            db.execute("BEGIN")
            theme = db.execute("SELECT * FROM rooms WHERE id=?", (room["id"],)).fetchone()
            if not theme or theme["closed"]:
                raise Refusal(410, "room_closed")
            rows = db.execute(
                "SELECT id,song_id,title,state,code,created,guest_id,nickname,avatar,artwork,progress,cache_hit,source FROM songs WHERE room=? AND removed=0 ORDER BY position,rowid LIMIT 80",
                (room["id"],),
            ).fetchall()
            guide = db.execute("SELECT guide_sha,guide_bytes,guide_duration,guide_reference_sha,guide_reference_bytes,guide_reference_duration,guide_reference_kind FROM songs WHERE room=? AND id=?", (room["id"], theme["current_song"])).fetchone()
            guide_spec = None
            if guide and guide["guide_sha"]:
                guide_spec = {"sha256": guide["guide_sha"], "bytes": guide["guide_bytes"], "duration_ms": guide["guide_duration"]}
                if guide["guide_reference_sha"] and guide["guide_reference_kind"] == "accompaniment":
                    guide_spec["reference"] = {"sha256": guide["guide_reference_sha"], "bytes": guide["guide_reference_bytes"],
                        "duration_ms": guide["guide_reference_duration"], "kind": "accompaniment"}
            return {
                "server_time_ms": round(time.time() * 1000),
                "expires": room["expires"],
                "mac_online": self.online(db),
                "microphone_available": not (self.root / "microphone-hold.json").exists(),
                "readiness": readiness,
                "songs": [
                    {**dict(r), "title": song_title(r["title"]), "lyrics_pending": r["id"] == theme["lyrics_waiting"], "progress": json.loads(r["progress"]), "cache_hit": bool(r["cache_hit"])}
                    for r in rows
                ],
                "theme": theme["theme"],
                "theme_revision": theme["theme_revision"],
                "queue_revision": theme["queue_revision"],
                "controls": {
                    k: theme[k]
                    for k in ("music_gain", "mic_gain", "mic_muted", "original", "control_revision", "mic_revision", "vocal_preset", "sfx_gain")
                },
                "playback": {
                    "online": time.time() - theme["playback_at"] < 12,
                    "current": theme["current_song"],
                    "played": json.loads(theme["played"]),
                    "state": theme["playback_state"],
                    "safety_latched": bool(theme["safety_latched"]),
                    "mic_muted": bool(theme["actual_mic_muted"]),
                    "applied_revision": theme["applied_revision"],
                    "last_command": theme["last_command"],
                    "audio_capabilities": json.loads(theme["audio_capabilities"]),
                    "vocal_preset": theme["actual_vocal_preset"],
                    "vocal_preset_state": theme["vocal_preset_state"],
                    "sfx_seq": theme["sfx_seq"],
                    "sfx_action": theme["sfx_action"],
                    "sfx_state": theme["sfx_state"],
                    "position_ms": theme["position_ms"],
                    "position_at": theme["position_at"],
                    "playback_epoch": theme["playback_epoch"],
                    "guide": guide_spec,
                },
            }

    def controls(self, room, payload):
        limits = {"music_gain": 100, "mic_gain": 100, "mic_muted": 1, "original": 1, "sfx_gain": 100}
        if (
            not payload
            or set(payload) - set(limits) - {"vocal_preset"}
            or any(type(v) is not int or not 0 <= v <= limits[k] for k, v in payload.items() if k in limits)
            or ("vocal_preset" in payload and (not isinstance(payload["vocal_preset"], str)
                                              or payload["vocal_preset"] not in VOCAL_PRESETS))
        ):
            raise Refusal(400, "invalid_control")
        if (self.root / "microphone-hold.json").exists() and (payload.get("mic_muted") == 0 or payload.get("mic_gain", 0) > 0):
            raise Refusal(409, "microphone_update_required")
        if (payload.get("mic_muted") == 0 and self.coordinator is not None
                and self.coordinator.direct_room(room["id"]) and not self.coordinator.direct_live(room["id"])):
            raise Refusal(409, "tv_offline")
        with self.lock, self.connection() as db:
            if "vocal_preset" in payload and payload["vocal_preset"] != "dry":
                state = db.execute("SELECT audio_capabilities,playback_at FROM rooms WHERE id=?", (room["id"],)).fetchone()
                if (time.time() - state["playback_at"] >= 12
                        or payload["vocal_preset"] not in json.loads(state["audio_capabilities"]).get("vocal_presets", [])):
                    raise Refusal(409, "vocal_preset_unavailable")
            db.execute(
                "UPDATE rooms SET "
                + ",".join(k + "=?" for k in payload)
                + ",control_revision=control_revision+1"
                + (",mic_revision=mic_revision+1" if "mic_muted" in payload else "")
                + " WHERE id=? AND closed=0",
                (*payload.values(), room["id"]),
            )
            revision = db.execute("SELECT control_revision FROM rooms WHERE id=?", (room["id"],)).fetchone()[0]
            if "mic_muted" in payload:
                db.execute("UPDATE rooms SET mic_request_at=?,mic_request_revision=? WHERE id=?",
                           (int(time.time()) if payload["mic_muted"] == 0 else 0, revision, room["id"]))
        return {"accepted": True, "revision": revision}

    def tv_controls(self, room_id, payload):
        # The owner bridge has already authenticated; still require a live room.
        with self.lock, self.connection() as db:
            self._expire(db)
            room = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
            if not room or room["closed"]:
                raise Refusal(410, "room_closed")
            if (not isinstance(payload, dict) or not payload
                    or set(payload) - {"original", "mic_muted", "vocal_preset"}):
                raise Refusal(400, "invalid_control")
        self.budget("tv-controls:" + room_id, 60)
        return self.controls(dict(room), payload)

    def command(self, room, payload):
        if (
            set(payload) != {"action", "song", "request_id"}
            or not isinstance(payload["action"], str)
            or payload["action"] not in {"next", "play", "pause", "resume"} | SFX_ACTIONS
            or not isinstance(payload["request_id"], str)
            or not UUID.fullmatch(payload["request_id"])
            or not isinstance(payload["song"], str)
            or (payload["song"] and not UUID.fullmatch(payload["song"]))
            or (payload["action"] in SFX_ACTIONS and payload["song"] != "")
        ):
            raise Refusal(400, "invalid_command")
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT seq,action,song FROM commands WHERE room=? AND request_id=?",
                (room["id"], payload["request_id"]),
            ).fetchone()
            if old:
                if old["action"] != payload["action"] or old["song"] != payload["song"]:
                    raise Refusal(409, "idempotency_conflict")
                return {"seq": old["seq"]}
            state = db.execute(
                "SELECT playback_at,current_song,audio_capabilities FROM rooms WHERE id=?", (room["id"],)
            ).fetchone()
            if time.time() - state["playback_at"] >= 12:
                raise Refusal(409, "tv_offline")
            if payload["action"] in SFX_ACTIONS:
                if payload["action"] != "sfx_stop" and not json.loads(state["audio_capabilities"]).get("sfx", False):
                    raise Refusal(409, "sfx_unavailable")
            elif payload["action"] == "play":
                item = db.execute(
                    "SELECT state,song_id FROM songs WHERE id=? AND room=? AND removed=0", (payload["song"], room["id"])
                ).fetchone()
                if not item or item["state"] != "ready":
                    raise Refusal(409, "song_not_ready")
                if item["song_id"] in self.management.deleting:
                    raise Refusal(409, "cache_in_use")
            elif payload["song"] != state["current_song"]:
                raise Refusal(409, "playback_changed")
            cursor = db.execute(
                "INSERT INTO commands(room,request_id,action,song,created) VALUES(?,?,?,?,?)",
                (room["id"], payload["request_id"], payload["action"], payload["song"], int(time.time())),
            )
            return {"seq": cursor.lastrowid}

    def reorder(self, room, payload):
        if (
            set(payload) != {"ids", "revision"}
            or type(payload["revision"]) is not int
            or not isinstance(payload["ids"], list)
            or len(payload["ids"]) > 80
            or any(not isinstance(i, str) or not UUID.fullmatch(i) for i in payload["ids"])
            or len(set(payload["ids"])) != len(payload["ids"])
        ):
            raise Refusal(400, "invalid_order")
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT queue_revision FROM rooms WHERE id=?", (room["id"],)).fetchone()[0]
            ids = {r[0] for r in db.execute("SELECT id FROM songs WHERE room=? AND removed=0", (room["id"],))}
            if revision != payload["revision"] or set(payload["ids"]) != ids:
                raise Refusal(409, "queue_changed")
            for at, song in enumerate(payload["ids"]):
                db.execute("UPDATE songs SET position=? WHERE id=? AND room=?", (at + 1, song, room["id"]))
            db.execute("UPDATE rooms SET queue_revision=queue_revision+1 WHERE id=?", (room["id"],))
        return {"accepted": True}

    def playback(self, room_id, payload):
        required = {
            "current",
            "played",
            "state",
            "last_command",
            "safety_latched",
            "applied_revision",
            "mic_muted",
        }
        if (
            not required <= set(payload) or set(payload) - required - {"position_ms", "lyrics_waiting", "playback_epoch"} - AUDIO_PLAYBACK_FIELDS
            or ("playback_epoch" in payload and (not isinstance(payload["playback_epoch"], str) or not UUID.fullmatch(payload["playback_epoch"])))
            or not isinstance(payload.get("lyrics_waiting", ""), str)
            or (payload.get("lyrics_waiting") and not UUID.fullmatch(payload["lyrics_waiting"]))
            or payload["state"] not in ("idle", "playing", "paused", "preparing")
            or not isinstance(payload["current"], str)
            or (payload["current"] and not UUID.fullmatch(payload["current"]))
            or not isinstance(payload["played"], list)
            or len(payload["played"]) > 80
            or any(not isinstance(i, str) or not UUID.fullmatch(i) for i in payload["played"])
            or any(
                type(payload[k]) is not int or not 0 <= payload[k] <= 10**12 for k in ("last_command", "applied_revision")
            )
            or type(payload["safety_latched"]) is not bool
            or type(payload["mic_muted"]) is not bool
            or type(payload.get("position_ms", 0)) is not int or not 0 <= payload.get("position_ms", 0) <= 1810000
        ):
            raise Refusal(400, "invalid_playback")
        try:
            audio = audio_playback(payload)
        except ValueError:
            raise Refusal(400, "invalid_audio_playback") from None
        with self.lock, self.connection() as db:
            if audio["sfx_seq"]:
                command = db.execute("SELECT action FROM commands WHERE room=? AND seq=?",
                                     (room_id, audio["sfx_seq"])).fetchone()
                if not command or command["action"] != audio["sfx_action"]:
                    raise Refusal(400, "invalid_sfx_ack")
            ids = {r[0] for r in db.execute("SELECT id FROM songs WHERE room=?", (room_id,))}
            if ((payload["current"] and payload["current"] not in ids) or set(payload["played"]) - ids
                    or (payload.get("lyrics_waiting") and payload["lyrics_waiting"] not in ids)):
                raise Refusal(400, "invalid_playback")
            db.execute("UPDATE rooms SET lyrics_waiting=? WHERE id=? AND closed=0", (payload.get("lyrics_waiting", ""), room_id))
            db.execute(
                "UPDATE rooms SET playback_at=?,current_song=?,played=?,playback_state=?,last_command=max(last_command,?),"
                "safety_latched=?,applied_revision=?,actual_mic_muted=? WHERE id=? AND closed=0",
                (
                    int(time.time()),
                    payload["current"],
                    json.dumps(payload["played"]),
                    payload["state"],
                    payload["last_command"],
                    payload["safety_latched"],
                    payload["applied_revision"],
                    payload["mic_muted"],
                    room_id,
                ),
            )
            if "playback_epoch" in payload:
                db.execute("UPDATE rooms SET playback_epoch=? WHERE id=?", (payload["playback_epoch"], room_id))
            if payload["safety_latched"]:
                db.execute(
                    "UPDATE rooms SET mic_muted=1,control_revision=control_revision+1,mic_revision=mic_revision+1 WHERE id=? AND mic_muted=0 AND control_revision=?",
                    (room_id, payload["applied_revision"]),
                )
            db.execute("UPDATE rooms SET position_ms=?,position_at=? WHERE id=?", (payload.get("position_ms", 0), round(time.time() * 1000), room_id))
            db.execute("UPDATE rooms SET audio_capabilities=?,actual_vocal_preset=?,vocal_preset_state=? WHERE id=? AND closed=0",
                       (json.dumps(audio["audio_capabilities"] if "audio_capabilities" in payload else {}), audio["vocal_preset"], audio["vocal_preset_state"], room_id))
            confirmed = audio["audio_capabilities"].get("voice_protection", {}).get("mute_confirmed", True)
            if not payload["mic_muted"] and confirmed:
                db.execute("UPDATE rooms SET mic_request_at=0 WHERE id=? AND mic_request_revision<=?",
                           (room_id, payload["applied_revision"]))
            db.execute("UPDATE rooms SET sfx_seq=?,sfx_action=?,sfx_state=? WHERE id=? AND closed=0 AND sfx_seq<=?",
                       (audio["sfx_seq"], audio["sfx_action"], audio["sfx_state"], room_id, audio["sfx_seq"]))
            db.execute("UPDATE managed_media SET last_used=? WHERE song_id=(SELECT song_id FROM songs WHERE id=?) AND last_used<?",
                       (int(time.time()), payload["current"], int(time.time()) - 60))
        return {"accepted": True}

    def guide_update(self, payload):
        if (not {"id", "sha256", "bytes", "duration_ms"} <= set(payload)
                or set(payload) - {"id", "sha256", "bytes", "duration_ms", "reference"}
                or not isinstance(payload["id"], str) or not UUID.fullmatch(payload["id"])
                or not isinstance(payload["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", payload["sha256"])
                or type(payload["bytes"]) is not int or not 1024 < payload["bytes"] <= 32 * 1024 * 1024
                or type(payload["duration_ms"]) is not int or not 1000 < payload["duration_ms"] <= 1800000):
            raise Refusal(400, "invalid_guide")
        reference = payload.get("reference")
        if reference is not None and (
                not isinstance(reference, dict) or set(reference) != {"sha256", "bytes", "duration_ms", "kind"}
                or reference["kind"] != "accompaniment"
                or not isinstance(reference["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"])
                or type(reference["bytes"]) is not int or not 1024 < reference["bytes"] <= 32 * 1024 * 1024
                or type(reference["duration_ms"]) is not int or not 1000 < reference["duration_ms"] <= 1800000
                or abs(reference["duration_ms"] - payload["duration_ms"]) > 100):
            raise Refusal(400, "invalid_guide")
        if "reference" in payload and reference is None:
            raise Refusal(400, "invalid_guide")
        reference = reference or {}
        with self.lock, self.connection() as db:
            db.execute("UPDATE songs SET guide_sha=?,guide_bytes=?,guide_duration=?,guide_reference_sha=?,guide_reference_bytes=?,guide_reference_duration=?,guide_reference_kind=? WHERE id=? AND state!='cancelled'",
                (payload["sha256"], payload["bytes"], payload["duration_ms"], reference.get("sha256", ""),
                 reference.get("bytes", 0), reference.get("duration_ms", 0), reference.get("kind", ""), payload["id"]))
        return {"accepted": True}

    def guide_file(self, room, song_id, variant="original"):
        if not isinstance(song_id, str) or not UUID.fullmatch(song_id) or variant not in {"original", "reference"}:
            raise Refusal(400, "invalid_guide")
        with self.connection() as db:
            current = db.execute("SELECT current_song,playback_at FROM rooms WHERE id=?", (room["id"],)).fetchone()
            row = db.execute("SELECT guide_sha,guide_bytes,guide_reference_sha,guide_reference_bytes,guide_reference_kind FROM songs WHERE room=? AND id=? AND state='ready'", (room["id"], song_id)).fetchone()
        if not row or not row["guide_sha"] or current["current_song"] != song_id or time.time() - current["playback_at"] >= 12:
            raise Refusal(409, "guide_unavailable")
        reference = variant == "reference"
        digest, size = row["guide_reference_sha" if reference else "guide_sha"], row["guide_reference_bytes" if reference else "guide_bytes"]
        if reference and (row["guide_reference_kind"] != "accompaniment" or not digest):
            raise Refusal(409, "guide_unavailable")
        path = self.root.parent / "guides" / (digest + ".mp3")
        if self.coordinator is not None:
            cached = self.coordinator.cached_file({"sha256": digest, "bytes": size})
            if cached is not None:
                # Already SHA-verified and still bound to the recorded file
                # identity; do not rehash a full MP3 in the phone request lane.
                return cached, size
        if path.resolve() != path or not path.is_file() or path.stat().st_size != size:
            raise Refusal(409, "guide_unavailable")
        with path.open("rb") as stream:
            checksum = hashlib.sha256()
            for block in iter(lambda: stream.read(65536), b""):
                checksum.update(block)
        if checksum.hexdigest() != digest:
            raise Refusal(409, "guide_unavailable")
        return path, size

    def monitor(self, room, guest, payload):
        fields = {"phase", "context", "primed", "session_playback", "node", "buffer_ms", "position_ms", "target_ms"}
        if (not fields <= set(payload) or set(payload) - fields - {"alignment"} or payload["phase"] not in {"off", "start", "loading", "playing", "blocked", "failed", "suspended"}
                or payload["context"] not in {"unknown", "running", "suspended", "closed", "interrupted"}
                or any(type(payload[k]) is not bool for k in ("primed", "session_playback", "node"))
                or any(type(payload[k]) is not int or not 0 <= payload[k] <= 1810000 for k in ("buffer_ms", "position_ms", "target_ms"))):
            raise Refusal(400, "invalid_monitor")
        alignment = payload.get("alignment")
        if alignment is not None:
            if (not isinstance(alignment, dict)
                    or set(alignment) != {"phase", "code", "frames", "score", "margin", "offset_ms"}
                    or alignment["phase"] not in {"idle", "requesting", "settling", "capturing", "matching", "matched", "failed", "cancelled"}
                    or alignment["code"] not in {"none", "matched", "denied", "unsupported", "quiet", "ambiguous", "changed", "cancelled", "permission_timeout", "capture_timeout", "capture_release_failed", "reference_unavailable", "busy", "audio_start_blocked", "unavailable"}
                    or any(type(alignment[k]) is not int or not low <= alignment[k] <= high
                           for k, low, high in (("frames", 0, 600), ("score", -1000, 1000), ("margin", -2000, 2000), ("offset_ms", -5000, 5000)))):
                raise Refusal(400, "invalid_monitor")
        with self.lock:
            path = self.root / "monitor.json"
            values = {}
            if path.is_file() and not path.is_symlink() and path.stat().st_size < 32768:
                values = json.loads(path.read_text())
            values.pop(guest["id"], None)
            values[guest["id"]] = {"at": int(time.time()), "room": room["id"], **payload}
            values = dict(list(values.items())[-32:])
            part = self.root / "monitor.part"
            part.write_text(json.dumps(values))
            part.chmod(0o600)
            part.replace(path)
        return {"accepted": True}

    def add(self, room, payload, guest=None):
        if not {"song_id", "title", "request_id"} <= set(payload) or set(payload) - {"song_id", "title", "request_id", "source"}:
            raise Refusal(400, "invalid_request")
        song_id, title, request_id = [payload.get(k) for k in ("song_id", "title", "request_id")]
        source = payload.get("source", "song")
        if (
            not isinstance(song_id, str)
            or not ID.fullmatch(song_id)
            or not isinstance(title, str)
            or len(title) > 120
            or any(ord(c) < 32 or ord(c) == 127 for c in title)
            or not isinstance(request_id, str)
            or not UUID.fullmatch(request_id)
            or source not in ("song", "cloud", "mv")
        ):
            raise Refusal(400, "invalid_request")
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            if song_id in self.management.deleting:
                raise Refusal(409, "cache_in_use")
            active = db.execute("SELECT closed,expires FROM rooms WHERE id=?", (room["id"],)).fetchone()
            if not active or active["closed"]:
                raise Refusal(410, "room_closed")
            receipt = db.execute(
                "SELECT request.*,song.state FROM song_requests AS request LEFT JOIN songs AS song "
                "ON song.id=request.queue_id AND song.room=request.room WHERE request.room=? AND request.request_id=?",
                (room["id"], request_id),
            ).fetchone()
            if receipt:
                if (receipt["song_id"] != song_id or receipt["title"] != title or receipt["source"] != source
                        or (guest is not None and receipt["guest_id"] != guest["id"])):
                    raise Refusal(409, "idempotency_conflict")
                if receipt["state"] is None:
                    raise Refusal(409, "idempotency_unavailable")
                return {"id": receipt["queue_id"], "state": receipt["state"],
                    **({"duplicate": True} if receipt["was_duplicate"] else {})}
            old = db.execute(
                "SELECT * FROM songs WHERE room=? AND request_id=?", (room["id"], request_id)
            ).fetchone()
            if old:
                if (
                    old["song_id"] != song_id
                    or old["title"] != title
                    or old["source"] != source
                    or (guest is not None and old["guest_id"] != guest["id"])
                ):
                    raise Refusal(409, "idempotency_conflict")
                return {"id": old["id"], "state": old["state"]}
            if source == "cloud" and not db.execute("SELECT 1 FROM cloud_catalog WHERE song_id=?", (song_id,)).fetchone():
                raise Refusal(409, "cloud_song_unavailable")
            # Deduplicate the pending/current queue across guests. A completed
            # or skipped song can be ordered again and reuse prepared media.
            played = set(json.loads(db.execute("SELECT played FROM rooms WHERE id=?", (room["id"],)).fetchone()[0]))
            candidates = db.execute(
                "SELECT id,state FROM songs WHERE room=? AND song_id=? AND source=? AND state!='cancelled' AND removed=0 ORDER BY rowid",
                (room["id"], song_id, source),
            ).fetchall()
            existing = next((item for item in candidates if item["id"] not in played), None)
            if existing:
                # This tap also needs a durable receipt. Otherwise a lost reply
                # retried after the existing song finishes would enqueue it again.
                self.budget("add-receipt:" + room["id"], 120)
                db.execute("INSERT INTO song_requests VALUES(?,?,?,?,?,?,?,1)",
                    (room["id"], request_id, guest["id"] if guest else "", song_id, title, source, existing["id"]))
                return {"id": existing["id"], "state": existing["state"], "duplicate": True}
            if not self.online(db):
                raise Refusal(503, "mac_offline")
            if db.execute("SELECT count(*) FROM songs WHERE room=? AND removed=0", (room["id"],)).fetchone()[0] >= 80:
                raise Refusal(429, "queue_full")
            if (
                db.execute(
                    "SELECT count(*) FROM songs WHERE state NOT IN ('ready','failed','cancelled')"
                ).fetchone()[0]
                >= 32
            ):
                raise Refusal(429, "queue_full")
            # A lost receipt or a repeated tap must not consume a new-add slot.
            self.budget("add:" + room["id"], 20)
            row_id = str(uuid.uuid4())
            profile = guest or {"id": "", "nickname": "朋友", "avatar": 0}
            artwork = next(
                (
                    s.get("artwork", "")
                    for cache in self.search_cache.values()
                    for results in [cache[1]]
                    for s in results
                    if s.get("source", "song") == source and s["song_id"] == song_id and s.get("artwork")
                ),
                "",
            )
            db.execute(
                "INSERT INTO songs(id,room,song_id,title,request_id,created,guest_id,nickname,avatar,artwork,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    room["id"],
                    song_id,
                    title,
                    request_id,
                    int(time.time()),
                    profile["id"],
                    profile["nickname"],
                    profile["avatar"],
                    artwork,
                    source,
                ),
            )
            db.execute(
                "UPDATE songs SET position=(SELECT coalesce(max(position),0)+1 FROM songs WHERE room=?) WHERE id=?",
                (room["id"], row_id),
            )
            db.execute("UPDATE rooms SET queue_revision=queue_revision+1 WHERE id=?", (room["id"],))
            db.execute("INSERT INTO song_requests VALUES(?,?,?,?,?,?,?,0)",
                (room["id"], request_id, profile["id"], song_id, title, source, row_id))
            return {"id": row_id, "state": "queued"}

    def poll(self):
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            direct_row = db.execute("SELECT session.* FROM coordinator_session AS session JOIN rooms ON rooms.id=session.room WHERE session.id=1 AND rooms.closed=0").fetchone()
            direct = dict(direct_row) if direct_row else None
            now = int(time.time())
            db.execute("INSERT OR REPLACE INTO heartbeat VALUES(1,?)", (now,))
            rows = db.execute(
                "SELECT id,song_id,title,guest_id,nickname,avatar,attempt,source FROM songs WHERE state NOT IN ('ready','failed','cancelled') "
                "AND lease<=? " + ("AND room=? " if direct else "") + "ORDER BY position,rowid LIMIT 8",
                (now, direct["room"]) if direct else (now,),
            ).fetchall()
            for value in rows:
                db.execute("UPDATE songs SET lease=? WHERE id=?", (now + 10, value["id"]))
            cancelled = db.execute(
                "SELECT id FROM songs WHERE state='cancelled' ORDER BY created DESC LIMIT 80"
            ).fetchall()
            tasks = [
                {
                    "id": r["id"],
                    "song_id": r["song_id"],
                    "title": r["title"],
                    "attempt": r["attempt"],
                    "source": r["source"],
                    "requester": {"id": r["guest_id"], "nickname": r["nickname"], "avatar": r["avatar"]},
                }
                for r in rows
            ]
            rooms = []
            for r in db.execute(
                "SELECT id AS room_id,expires,theme,theme_revision,music_gain,mic_gain,mic_muted,original,mic_revision,control_revision,queue_revision,last_command,vocal_preset,sfx_gain FROM rooms WHERE closed=0 LIMIT 8"
            ):
                info = dict(r)
                info["persistent"] = True
                info["order"] = [
                    v[0]
                    for v in db.execute(
                        "SELECT id FROM songs WHERE room=? AND removed=0 ORDER BY position,rowid", (r["room_id"],)
                    )
                ]
                info["commands"] = [
                    {**dict(v), **({"expires": v["created"] + SFX_TTL} if v["action"] in SFX_ACTIONS else {})}
                    for v in db.execute(
                        "SELECT seq,action,song,created FROM commands WHERE room=? AND seq>? AND created>? AND created<=? "
                        "AND (action NOT IN ('sfx_applause','sfx_cheer','sfx_stop') OR (created>? AND seq=("
                        "SELECT max(seq) FROM commands WHERE room=? AND action IN ('sfx_applause','sfx_cheer','sfx_stop') AND created>? AND created<=?))) "
                        "ORDER BY seq LIMIT 20",
                        (r["room_id"], r["last_command"], now - 20, now, now - SFX_TTL, r["room_id"], now - SFX_TTL, now),
                    )
                ]
                if direct and direct["room"] == r["room_id"]:
                    join_url = self.origin + self.base_path + "/#" + urllib.parse.urlencode({"r": direct["room"], "k": direct["secret"]})
                    info.update(join_url=join_url, tv_instance=direct["instance"], direct=True)
                    info["qr_ready"] = self.coordinator.qr_ready(direct)
                    playback = db.execute("SELECT current_song,played FROM rooms WHERE id=?", (r["room_id"],)).fetchone()
                    info["playback"] = {"current": playback["current_song"], "played": json.loads(playback["played"])}
                rooms.append(info)
            archives = self.coordinator.archives(direct["room"], db) if direct else []
            return {"tasks": tasks, "cancelled": [r[0] for r in cancelled], "rooms": rooms, "archives": archives}

    def retry(self, room, payload):
        if set(payload) != {"id", "request_id"} or any(
            not isinstance(payload[k], str) or not UUID.fullmatch(payload[k]) for k in payload
        ):
            raise Refusal(400, "invalid_request")
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT song FROM retries WHERE room=? AND request_id=?", (room["id"], payload["request_id"])
            ).fetchone()
            if old:
                if old[0] != payload["id"]:
                    raise Refusal(409, "idempotency_conflict")
                return {"accepted": True}
            value = db.execute(
                "SELECT state,attempt,song_id FROM songs WHERE room=? AND id=? AND removed=0", (room["id"], payload["id"])
            ).fetchone()
            if not value or value["state"] != "failed":
                raise Refusal(409, "retry_unavailable")
            if value["song_id"] in self.management.deleting:
                raise Refusal(409, "cache_in_use")
            if not self.online(db):
                raise Refusal(503, "mac_offline")
            if value["attempt"] >= 3:
                raise Refusal(429, "retry_limit")
            db.execute(
                "INSERT INTO retries VALUES(?,?,?)", (room["id"], payload["request_id"], payload["id"])
            )
            db.execute(
                "UPDATE songs SET state='queued',code='none',lease=0,attempt=attempt+1,progress='{}' WHERE room=? AND id=?",
                (room["id"], payload["id"]),
            )
        return {"accepted": True}

    def update(self, payload):
        if (
            set(payload) != {"updates"}
            or not isinstance(payload["updates"], list)
            or len(payload["updates"]) > 32
        ):
            raise Refusal(400, "invalid_update")
        with self.lock, self.connection() as db:
            self._expire(db)
            for row in payload["updates"]:
                if (
                    not isinstance(row, dict)
                    or not {"id", "state", "code"} <= set(row)
                    or set(row) - {"id", "state", "code", "progress", "cache_hit"}
                    or not isinstance(row["id"], str)
                    or not UUID.fullmatch(row["id"])
                    or not isinstance(row["state"], str)
                    or row["state"] not in STATES
                    or not isinstance(row["code"], str)
                    or row["code"] not in CODES
                ):
                    raise Refusal(400, "invalid_update")
                progress = row.get("progress", {})
                allowed = {
                    "stage",
                    "stage_started",
                    "updated",
                    "completed_steps",
                    "total_steps",
                    "fraction",
                    "failed_stage",
                    "source_phase",
                    "source_bytes",
                    "source_total",
                }
                if (
                    not isinstance(progress, dict)
                    or set(progress) - allowed
                    or type(row.get("cache_hit", False)) is not bool
                ):
                    raise Refusal(400, "invalid_update")
                stages = {
                    "",
                    "queued",
                    "acquiring",
                    "waiting_separation",
                    "waiting_memory",
                    "separating",
                    "waiting_transcode",
                    "transcoding",
                    "video",
                    "ready",
                    "failed",
                    "cancelled",
                }
                for field, number in progress.items():
                    if field in {"stage", "failed_stage"}:
                        if not isinstance(number, str) or number not in stages:
                            raise Refusal(400, "invalid_update")
                    elif field == "source_phase":
                        if number not in (
                            "",
                            "source_cache_hit",
                            "file_detected",
                            "decoding",
                            "official_request_sent",
                            "source_authorizing",
                            "source_downloading",
                        ):
                            raise Refusal(400, "invalid_update")
                    elif field == "fraction":
                        if number is not None:
                            raise Refusal(400, "invalid_update")
                    elif type(number) is not int or not 0 <= number <= 10**12:
                        raise Refusal(400, "invalid_update")
                # Closing a room is final, even if an old local preparation finishes later.
                db.execute(
                    "UPDATE songs SET state=?,code=?,lease=?,progress=?,cache_hit=? WHERE id=? AND state!='cancelled'",
                    (
                        row["state"],
                        row["code"],
                        int(time.time()) + 10,
                        json.dumps(progress),
                        row.get("cache_hit", False),
                        row["id"],
                    ),
                )
        return {"accepted": True}

    def cloud_update(self, payload):
        if (set(payload) != {"revision", "total", "offset", "songs"}
                or not isinstance(payload["revision"], str) or not re.fullmatch(r"[a-f0-9]{64}", payload["revision"])
                or type(payload["total"]) is not int or not 0 <= payload["total"] <= 5000
                or type(payload["offset"]) is not int or not 0 <= payload["offset"] <= payload["total"]
                or not isinstance(payload["songs"], list) or len(payload["songs"]) > 30
                or payload["offset"] + len(payload["songs"]) > payload["total"]
                or (not payload["songs"] and payload["total"] != 0)):
            raise Refusal(400, "invalid_catalog")
        rows = []
        for row in payload["songs"]:
            if (not isinstance(row, dict) or set(row) != {"song_id", "title", "artists", "source"}
                    or row["source"] != "cloud" or not isinstance(row["song_id"], str) or not ID.fullmatch(row["song_id"])
                    or not isinstance(row["title"], str) or not 1 <= len(row["title"]) <= 120
                    or not isinstance(row["artists"], list) or len(row["artists"]) > 8
                    or any(not isinstance(a, str) or len(a) > 100 for a in row["artists"])
                    or any(ord(c) < 32 or ord(c) == 127 for c in row["title"] + "".join(row["artists"]))):
                raise Refusal(400, "invalid_catalog")
            rows.append((row["song_id"], row["title"], json.dumps(row["artists"], ensure_ascii=False)))
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            sync = db.execute("SELECT * FROM cloud_sync WHERE id=1").fetchone()
            if payload["offset"] == 0:
                db.execute("DELETE FROM cloud_staging")
                db.execute("INSERT OR REPLACE INTO cloud_sync VALUES(1,?,?,0,?)",
                           (payload["revision"], payload["total"], int(time.time())))
            elif (not sync or sync["revision"] != payload["revision"] or sync["received"] != payload["offset"]
                  or sync["total"] != payload["total"]):
                raise Refusal(409, "catalog_changed")
            for i, row in enumerate(rows):
                db.execute("INSERT INTO cloud_staging VALUES(?,?,?,?)", (payload["offset"] + i, *row))
            received = payload["offset"] + len(rows)
            db.execute("UPDATE cloud_sync SET received=?,updated=? WHERE id=1", (received, int(time.time())))
            if received == payload["total"]:
                db.execute("DELETE FROM cloud_catalog")
                db.execute("INSERT INTO cloud_catalog SELECT song_id,title,artists FROM cloud_staging")
        return {"accepted": True, "complete": received == payload["total"], "count": received}

    def search(self, query, source="song", offset=0):
        if not isinstance(query, str) or not (0 if source == "cloud" else 1) <= len(query.strip()) <= 120:
            raise Refusal(400, "invalid_query")
        if source not in {"song", "cloud", "mv"}:
            raise Refusal(400, "invalid_source")
        if type(offset) is not int or not 0 <= offset <= 100000:
            raise Refusal(400, "invalid_query")
        if source == "cloud":
            terms = query.casefold().split()
            with self.connection() as db:
                rows = db.execute("SELECT * FROM cloud_catalog ORDER BY title").fetchall()
                sync = db.execute("SELECT updated FROM cloud_sync WHERE id=1").fetchone()
            matches = [r for r in rows if all(t in (r["title"] + " " + " ".join(json.loads(r["artists"]))).casefold() for t in terms)]
            songs = [{"song_id": r["song_id"], "title": song_title(r["title"]), "artists": json.loads(r["artists"]), "source": "cloud"}
                     for r in matches[offset:offset + 30]]
            return {"songs": songs, "source": "cloud", "catalog_count": len(rows), "updated": sync[0] if sync else 0,
                    "offset": offset, "next_offset": offset + 30, "has_more": offset + 30 < len(matches), "total": len(matches)}
        cache_key = source + ":" + str(offset) + ":" + query
        with self.lock:
            cached = self.search_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 60:
                return {"songs": cached[1], **cached[2]}
        search_path = "/api/search/get" if source == "mv" else "/api/search/get/web"
        url = "https://music.163.com" + search_path + "?" + urllib.parse.urlencode(
            {"s": query, "type": 1004 if source == "mv" else 1, "limit": 30, "offset": offset}
        )
        request = urllib.request.Request(url, headers={"Referer": "https://music.163.com/"})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=8) as response:
            if response.geturl().split("?", 1)[0] != "https://music.163.com" + search_path:
                raise Refusal(503, "search_unavailable")
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise Refusal(503, "search_unavailable")
        value = json.loads(raw)
        if value.get("code") != 200:
            raise Refusal(503, "search_unavailable")
        songs = []
        for row in value.get("result", {}).get("mvs" if source == "mv" else "songs", [])[:30]:
            song_id, title = str(row.get("id", "")), row.get("name")
            if ID.fullmatch(song_id) and isinstance(title, str) and 0 < len(title) <= 120:
                artists = [
                    a["name"][:100]
                    for a in row.get("artists", [])[:8]
                    if isinstance(a, dict) and isinstance(a.get("name"), str)
                ]
                songs.append({"song_id": song_id, "title": title, "artists": artists, "source": source})
        # Search returns image IDs; one bounded detail batch supplies public album art.
        if songs and source == "song":
            try:
                detail_url = "https://music.163.com/api/v3/song/detail?" + urllib.parse.urlencode(
                    {"c": json.dumps([{"id": int(s["song_id"])} for s in songs])}
                )
                with opener.open(
                    urllib.request.Request(detail_url, headers={"Referer": "https://music.163.com/"}),
                    timeout=3,
                ) as reply:
                    data = reply.read(1048577)
                if len(data) <= 1048576:
                    covers = {}
                    for item in json.loads(data).get("songs", [])[:30]:
                        url = urllib.parse.urlsplit(item.get("al", {}).get("picUrl", ""))
                        if (
                            url.scheme in {"https", "http"}
                            and url.hostname
                            and url.hostname.endswith(".music.126.net")
                            and not url.username
                            and not url.password
                            and url.port in {None, 443, 80}
                            and len(url.path) < 512
                        ):
                            covers[str(item["id"])] = urllib.parse.urlunsplit(
                                ("https", url.hostname, url.path, "param=160y160", "")
                            )
                    for song in songs:
                        if song["song_id"] in covers:
                            song["artwork"] = covers[song["song_id"]]
            except Exception:
                pass
        result = value.get("result", {})
        count = result.get("mvCount" if source == "mv" else "songCount")
        has_more = offset + 30 < count if type(count) is int else len(songs) == 30
        page = {"source": source, "offset": offset, "next_offset": offset + 30, "has_more": has_more}
        if type(count) is int and count >= 0:
            page["total"] = count
        with self.lock:
            if len(self.search_cache) >= 64:
                self.search_cache.pop(next(iter(self.search_cache)))
            self.search_cache[cache_key] = (time.monotonic(), songs, page)
        return {"songs": songs, **page}

    @staticmethod
    def public_metadata(path, params):
        if path not in {"/api/search/get", "/api/search/get/web", "/api/v6/playlist/detail", "/api/v3/song/detail"}:
            raise Refusal(400, "invalid_query")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        base = "https://music.163.com" + path
        request = urllib.request.Request(base + "?" + urllib.parse.urlencode(params), headers={"Referer": "https://music.163.com/"})
        with urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({})).open(request, timeout=8) as reply:
            if reply.geturl().split("?", 1)[0] != base:
                raise Refusal(503, "search_unavailable")
            data = reply.read(2097153)
        if len(data) > 2097152:
            raise Refusal(503, "search_unavailable")
        value = json.loads(data)
        if value.get("code") != 200:
            raise Refusal(503, "search_unavailable")
        return value

    @staticmethod
    def artwork(value):
        try:
            u = urllib.parse.urlsplit(value)
            if (u.scheme in {"http", "https"} and u.hostname and u.hostname.endswith(".music.126.net")
                    and not u.username and not u.password and u.port in {None, 80, 443} and len(u.path) < 512):
                return urllib.parse.urlunsplit(("https", u.hostname, u.path, "param=240y240", ""))
        except (ValueError, TypeError):
            pass
        return ""

    def discover(self):
        cached = self.recommendations
        if cached is None:
            try:
                path = self.root / "recommendations.json"
                if path.is_file() and not path.is_symlink() and path.stat().st_size < 32768:
                    cached = json.loads(path.read_text())
            except (OSError, ValueError):
                pass
        if not cached or time.time() - cached.get("updated", 0) > 21600:
            try:
                response = self.public_metadata("/api/search/get/web", {"s": "KTV必点", "type": 1000, "limit": 8, "offset": 0})
                rows = []
                for r in response.get("result", {}).get("playlists", [])[:8]:
                    if ID.fullmatch(str(r.get("id", ""))) and isinstance(r.get("name"), str):
                        rows.append({"id": str(r["id"]), "title": r["name"][:120], "artwork": self.artwork(r.get("coverImgUrl")), "tracks": r.get("trackCount", 0)})
                if not rows:
                    raise Refusal(503, "search_unavailable")
                cached = {"updated": int(time.time()), "playlists": rows}
                temporary = self.root / "recommendations.part"
                temporary.write_text(json.dumps(cached, ensure_ascii=False))
                temporary.chmod(0o600)
                temporary.replace(self.root / "recommendations.json")
            except Exception:
                cached = cached or {"updated": 0, "playlists": []}
        self.recommendations = cached
        portraits = {}
        path = self.root / "artist-portraits.json"
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size < 32768:
                portraits = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        with self.lock:
            if not self.artist_refreshing and time.time() - portraits.get("updated", 0) > 604800:
                self.artist_refreshing = True
                threading.Thread(target=self.refresh_artists, args=(portraits.get("photos", {}),), daemon=True).start()
        return {**cached, "artists": ARTISTS, "artist_photos": portraits.get("photos", {})}

    def refresh_artists(self, previous):
        photos = dict(previous)
        try:
            for name in ARTISTS:
                try:
                    result = self.public_metadata("/api/search/get", {"s": name, "type": 100, "limit": 5, "offset": 0})
                    exact = next((r for r in result.get("result", {}).get("artists", [])[:5] if r.get("name") == {"邓紫棋": "G.E.M.邓紫棋"}.get(name, name)), None)
                    if exact:
                        url = self.artwork(exact.get("img1v1Url") or exact.get("picUrl"))
                        if url:
                            photos[name] = url
                except Exception:
                    continue
            path = self.root / "artist-portraits.part"
            path.write_text(json.dumps({"updated": int(time.time()), "photos": photos}, ensure_ascii=False))
            path.chmod(0o600)
            path.replace(self.root / "artist-portraits.json")
        finally:
            self.artist_refreshing = False

    def playlist(self, playlist_id, offset=0):
        if not isinstance(playlist_id, str) or not ID.fullmatch(playlist_id) or type(offset) is not int or not 0 <= offset <= 100000:
            raise Refusal(400, "invalid_query")
        cached = self.playlist_cache.get(playlist_id)
        if not cached or time.monotonic() - cached[0] > 600:
            value = self.public_metadata("/api/v6/playlist/detail", {"id": playlist_id, "n": 0, "s": 0}).get("playlist", {})
            if str(value.get("id")) != playlist_id:
                raise Refusal(503, "search_unavailable")
            ids = [str(r.get("id")) for r in value.get("trackIds", []) if ID.fullmatch(str(r.get("id", "")))]
            cached = (time.monotonic(), value.get("name", "歌单")[:120], ids)
            if len(self.playlist_cache) >= 16:
                self.playlist_cache.pop(next(iter(self.playlist_cache)))
            self.playlist_cache[playlist_id] = cached
        ids = cached[2][offset:offset + 30]
        songs = []
        if ids:
            data = self.public_metadata("/api/v3/song/detail", {"c": json.dumps([{"id": int(i)} for i in ids])})
            by_id = {str(r.get("id")): r for r in data.get("songs", [])[:30]}
            for song_id in ids:
                r = by_id.get(song_id, {})
                if isinstance(r.get("name"), str):
                    songs.append({"song_id": song_id, "title": r["name"][:120], "artists": [a["name"][:100] for a in r.get("ar", [])[:8] if isinstance(a.get("name"), str)], "artwork": self.artwork(r.get("al", {}).get("picUrl")), "source": "song"})
        return {"songs": songs, "title": cached[1], "offset": offset, "next_offset": offset + 30, "has_more": offset + 30 < len(cached[2]), "total": len(cached[2])}


def make_handler(store, agent_key, assets):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # No request paths, room links, queries, headers, IPs or exception messages in logs.

        def reply(self, status, value, content_type="application/json; charset=utf-8"):
            data = (
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
                if not isinstance(value, bytes)
                else value
            )
            self.send_response(status)
            if self.close_connection:
                self.send_header("Connection", "close")
            headers = {
                "Content-Type": content_type,
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' data:; img-src 'self' data: https://*.music.126.net; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
            }
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def body(self, limit=32768):
            if self.headers.get("Transfer-Encoding"):
                raise Refusal(400, "invalid_body")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise Refusal(400, "invalid_body") from None
            if not 0 < length <= limit:
                self.close_connection = True
                raise Refusal(413, "invalid_body")
            try:
                value = json.loads(self.rfile.read(length), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid_number")))
            except (ValueError, UnicodeError):
                raise Refusal(400, "invalid_body") from None
            if not isinstance(value, dict):
                raise Refusal(400, "invalid_body")
            return value

        def media_reply(self, file, kind, size):
            first, last, status = 0, size - 1, 200
            supplied = self.headers.get("Range")
            if supplied is not None:
                match = re.fullmatch(r"bytes=([0-9]{0,12})-([0-9]{0,12})", supplied)
                invalid = not match or not any(match.groups())
                if not invalid:
                    lower, upper = match.groups()
                    if not lower:
                        suffix = int(upper)
                        invalid = suffix <= 0
                        first = max(0, size - suffix)
                    else:
                        first = int(lower)
                        last = min(size - 1, int(upper)) if upper else size - 1
                        invalid = first >= size or last < first
                if invalid:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */" + str(size))
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                status = 206
            with os.fdopen(os.open(str(file), os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                if os.fstat(stream.fileno()).st_size != size:
                    raise Refusal(409, "media_changed")
                self.send_response(status)
                headers = {"Content-Type": kind, "Content-Length": str(last - first + 1), "Accept-Ranges": "bytes",
                           "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
                if status == 206:
                    headers["Content-Range"] = "bytes " + str(first) + "-" + str(last) + "/" + str(size)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    stream.seek(first)
                    remaining = last - first + 1
                    while remaining:
                        block = stream.read(min(65536, remaining))
                        if not block:
                            self.close_connection = True
                            return
                        self.wfile.write(block)
                        remaining -= len(block)

        def dispatch(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if len(self.path) > 2048:
                raise Refusal(414, "request_too_large")
            if self.command not in {"GET", "POST", "HEAD"}:
                raise Refusal(405, "method_refused")
            origin = self.headers.get("Origin")
            if origin and origin != store.origin:
                raise Refusal(403, "origin_refused")
            if self.command in {"GET", "HEAD"} and path in {
                "/avatars.png",
                "/content.png",
                "/festival.png",
                "/live.png",
            }:
                asset = assets / path[1:]
                if asset.is_symlink() or not asset.is_file() or asset.stat().st_size > 8 * 1024 * 1024:
                    raise Refusal(404, "not_found")
                return self.reply(200, asset.read_bytes(), "image/png")
            if self.command in {"GET", "HEAD"} and path in {"/", "/app.js", "/add-queue.js", "/party-audio.js", "/guide.js", "/status-feed.js", "/style.css", "/sync.mjs", "/sync-dsp.mjs", "/sync-capture.mjs"}:
                name, kind = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/add-queue.js": ("add-queue.js", "text/javascript; charset=utf-8"),
                    "/party-audio.js": ("party-audio.js", "text/javascript; charset=utf-8"),
                    "/guide.js": ("guide.js", "text/javascript; charset=utf-8"),
                    "/status-feed.js": ("status-feed.js", "text/javascript; charset=utf-8"),
                    "/sync.mjs": ("sync.mjs", "text/javascript; charset=utf-8"),
                    "/sync-dsp.mjs": ("sync-dsp.mjs", "text/javascript; charset=utf-8"),
                    "/sync-capture.mjs": ("sync-capture.mjs", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                }[path]
                return self.reply(200, (assets / name).read_bytes(), kind)
            if path == "/health" and self.command in {"GET", "HEAD"}:
                return self.reply(200, {"service": "friday-ktv-public", "protocol": 1})
            if path.startswith("/v1/") and self.headers.get("X-KTV-TV-Key") is not None:
                if not store.coordinator.authenticate(self.headers.get("X-KTV-TV-Key")):
                    raise Refusal(401, "unauthorized")
                coordinator = store.coordinator
                instance = self.headers.get("X-KTV-TV-Instance", "")
                if path == "/v1/status" and self.command == "GET":
                    return self.reply(200, coordinator.status())
                if path == "/v1/party" and self.command == "GET":
                    return self.reply(200, coordinator.party())
                if path == "/v1/party" and self.command == "POST":
                    store.budget("tv-start", 30)
                    return self.reply(200, coordinator.start(self.body()))
                if path == "/v1/party/close" and self.command == "POST":
                    return self.reply(200, coordinator.close_party(instance, self.body()))
                if path == "/v1/party/qr" and self.command in {"GET", "HEAD"}:
                    return self.reply(200, coordinator.qr_file().read_bytes(), "image/png")
                if path == "/v1/tv/readiness" and self.command == "POST":
                    return self.reply(200, coordinator.tv_readiness(self.body()))
                if path in {"/v1/tv/playback", "/v1/tv/controls"} and self.command == "POST":
                    method = coordinator.playback if path.endswith("playback") else coordinator.tv_controls
                    return self.reply(200, method(self.body(), instance))
                if path == "/v1/tv/diagnostics":
                    if self.command == "POST":
                        coordinator._instance(instance)
                        coordinator.diagnostics = self.body(24576)
                        return self.reply(200, {"accepted": True})
                    if self.command == "GET":
                        return self.reply(200, coordinator.diagnostics or {})
                if path == "/v1/jobs" and self.command == "GET":
                    return self.reply(200, coordinator.jobs())
                media = re.fullmatch(r"/v1/jobs/([a-f0-9-]{36})/(lyrics|media/(original|instrumental|video|background))", path)
                if media and self.command in {"GET", "HEAD"}:
                    if media.group(2) == "lyrics":
                        return self.reply(200, coordinator.lyrics(media.group(1)))
                    file, spec = coordinator.media_file(media.group(1), media.group(3))
                    return self.media_reply(file, 'image/png' if media.group(3) == 'background' else "video/mp4" if media.group(3) == "video" else "audio/wav", spec["bytes"])
                raise Refusal(404, "not_found")
            if path.startswith("/agent/"):
                supplied = self.headers.get("Authorization", "")
                if len(supplied) > 256 or not hmac.compare_digest(supplied, "Bearer " + agent_key):
                    raise Refusal(401, "unauthorized")
                if path == "/agent/manage" and self.command == "POST":
                    if origin:
                        raise Refusal(403, "origin_refused")
                    store.budget("owner-manage", 240)
                    return self.reply(200, store.management.dispatch(self.body()))
                if path == "/agent/tasks" and self.command == "GET":
                    return self.reply(200, store.poll())
                if path == "/agent/worker" and self.command == "POST":
                    return self.reply(200, store.coordinator.register_worker(self.body()))
                if path == "/agent/manifests" and self.command == "POST":
                    return self.reply(200, store.coordinator.manifest(self.body(196608)))
                if path == "/agent/archive-check" and self.command == "POST":
                    store.budget("archive-check", 120)
                    return self.reply(200, store.coordinator.archive_check(self.body()))
                if path == "/agent/coordinator" and self.command == "GET":
                    return self.reply(200, store.coordinator.status())
                if path == "/agent/qr" and self.command == "POST":
                    return self.reply(200, store.coordinator.qr(self.body(196608)))
                if path == "/agent/updates" and self.command == "POST":
                    return self.reply(200, store.update(self.body()))
                if path == "/agent/cloud" and self.command == "POST":
                    return self.reply(200, store.cloud_update(self.body()))
                if path == "/agent/guides" and self.command == "POST":
                    return self.reply(200, store.guide_update(self.body()))
                if path == "/agent/rooms" and self.command == "POST":
                    payload = self.body()
                    if set(payload) - {"ttl"}:
                        raise Refusal(400, "invalid_request")
                    return self.reply(201, store.create(payload.get("ttl", 14400)))
                match = re.fullmatch(r"/agent/rooms/([a-f0-9-]{36})/(close|playback|controls)", path)
                if match and self.command == "POST":
                    payload = self.body()
                    if store.coordinator.direct_room(match.group(1)):
                        raise Refusal(409, "direct_tv_owned")
                    if match.group(2) == "controls":
                        return self.reply(200, store.tv_controls(match.group(1), payload))
                    return self.reply(
                        200,
                        store.close(match.group(1))
                        if match.group(2) == "close"
                        else store.playback(match.group(1), payload),
                    )
                raise Refusal(404, "not_found")
            match = re.fullmatch(
                r"/api/rooms/([a-f0-9-]{36})/(status|events|search|discover|playlist|guide|monitor|songs|join|profile|theme|controls|command|order|retry)",
                path,
            )
            if not match:
                raise Refusal(404, "not_found")
            self.operation = match.group(2)
            room = store.room(match.group(1), self.headers.get("X-KTV-Room", ""), self.headers.get("X-KTV-Guest", ""))
            # Reverse proxies share a loopback IP; admitted party guests must not exhaust one IP bucket.
            store.budget("room-read:" + room["id"], 4000)
            operation = match.group(2)
            if operation == "monitor" and self.command == "POST":
                guest = store.guest(room, self.headers.get("X-KTV-Guest", ""))
                store.budget("monitor:" + guest["id"], 30)
                return self.reply(200, store.monitor(room, guest, self.body()))
            if operation == "join" and self.command == "POST":
                if self.body():
                    raise Refusal(400, "invalid_request")
                store.budget("join:" + room["id"], 60, 14400)
                return self.reply(201, store.join(room))
            if operation in {"profile", "theme", "controls", "command", "order", "retry"} or (
                operation == "songs" and self.command == "POST"
            ):
                guest = store.guest(room, self.headers.get("X-KTV-Guest", ""))
                if operation == "profile":
                    if self.command == "GET":
                        return self.reply(200, guest)
                    if self.command == "POST":
                        store.budget("profile:" + guest["id"], 20)
                        return self.reply(200, store.profile(room, guest, self.body()))
                if operation == "theme" and self.command == "POST":
                    store.budget("theme:" + room["id"], 10)
                    return self.reply(200, store.theme(room, self.body()))
                if operation in {"controls", "command", "order", "retry"} and self.command == "POST":
                    payload = self.body()
                    # A burst of cheers must not consume the stop action's budget.
                    bucket = "sfx-stop" if operation == "command" and payload.get("action") == "sfx_stop" else operation
                    store.budget(bucket + ":" + room["id"], 60 if bucket in {"controls", "sfx-stop"} else 30)
                    return self.reply(
                        200,
                        getattr(
                            store,
                            {
                                "controls": "controls",
                                "command": "command",
                                "order": "reorder",
                                "retry": "retry",
                            }[operation],
                        )(room, payload),
                    )
            if operation == "status" and self.command == "GET":
                return self.reply(200, store.status(room))
            if operation == "events" and self.command == "GET":
                store.guest(room, self.headers.get("X-KTV-Guest", ""))
                params = urllib.parse.parse_qs(parsed.query, max_num_fields=2)
                if set(params) - {"after", "wait"} or any(len(v) != 1 for v in params.values()):
                    raise Refusal(400, "invalid_events")
                try:
                    after, wait = int(params.get("after", ["0"])[0]), float(params.get("wait", ["5"])[0])
                except ValueError:
                    raise Refusal(400, "invalid_events") from None
                return self.reply(200, store.coordinator.events(room, after, wait))
            if operation == "guide" and self.command in {"GET", "HEAD"}:
                guest = store.guest(room, self.headers.get("X-KTV-Guest", ""))
                store.budget("guide:" + guest["id"], 12)
                params = urllib.parse.parse_qs(parsed.query, max_num_fields=2)
                if set(params) - {"id", "variant"} or any(len(v) != 1 for v in params.values()):
                    raise Refusal(400, "invalid_guide")
                file, size = store.guide_file(room, params.get("id", [""])[0], params.get("variant", ["original"])[0])
                return self.media_reply(file, "audio/mpeg", size)
            if operation in {"discover", "playlist"} and self.command == "GET":
                store.guest(room, self.headers.get("X-KTV-Guest", ""))
                store.budget(operation + ":" + room["id"], 30)
                if operation == "discover":
                    return self.reply(200, store.discover())
                params = urllib.parse.parse_qs(parsed.query, max_num_fields=4)
                offset = params.get("offset", ["0"])[0]
                if not re.fullmatch(r"[0-9]{1,6}", offset):
                    raise Refusal(400, "invalid_query")
                return self.reply(200, store.playlist(params.get("id", [""])[0], int(offset)))
            if operation == "search" and self.command == "GET":
                store.budget("search:" + room["id"], 30)
                params = urllib.parse.parse_qs(parsed.query, max_num_fields=4)
                source = params.get("source", ["song"])[0]
                if source == "cloud":
                    store.guest(room, self.headers.get("X-KTV-Guest", ""))
                raw_offset = params.get("offset", ["0"])[0]
                if not re.fullmatch(r"[0-9]{1,6}", raw_offset):
                    raise Refusal(400, "invalid_query")
                return self.reply(200, store.search(params.get("q", [""])[0], source, int(raw_offset)))
            if operation == "songs" and self.command == "POST":
                return self.reply(202, store.add(room, self.body(), guest))
            raise Refusal(405, "method_refused")

        def handle_method(self):
            self.operation = "other"
            try:
                self.dispatch()
            except Refusal as error:
                self.close_connection = True
                self.reply(error.status, {"code": error.code})
            except (BrokenPipeError, ConnectionError, TimeoutError):
                self.close_connection = True
            except Exception as error:
                store.record_failure(self.operation, error)
                self.close_connection = True
                self.reply(503, {"code": "service_unavailable"})

        do_GET = handle_method
        do_POST = handle_method
        do_HEAD = handle_method
        do_OPTIONS = handle_method
        do_DELETE = handle_method

    return Handler


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args):
        self.capacity = threading.BoundedSemaphore(32)
        super().__init__(*args)

    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.capacity.release()


def listener_addresses(config):
    """Explicit LAN listener; never bind every interface or alter networking."""
    port = config.get("port", 19089)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid_listener_port")
    addresses = [("127.0.0.1", port)]
    lan = config.get("lan_host")
    if lan is not None:
        if not isinstance(lan, str):
            raise ValueError("explicit_private_lan_host_required")
        checked = private_origin("http://" + lan + ":" + str(port))
        if urllib.parse.urlsplit(checked).hostname != lan:
            raise ValueError("explicit_private_lan_host_required")
        addresses.append((lan, port))
    return addresses


def create_listeners(config, store, agent_key, assets, server_class=Server):
    servers = []
    handler = make_handler(store, agent_key, assets)
    try:
        for address in listener_addresses(config):
            servers.append(server_class(address, handler))
    except Exception:
        for server in servers:
            server.server_close()
        raise
    return servers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if args.config.is_symlink() or args.config.stat().st_mode & 0o077:
        raise ValueError("private_config_required")
    config = json.loads(args.config.read_text())
    origin = config["public_origin"]
    if not re.fullmatch(r"https://[a-z0-9.-]+(?::\d+)?", origin) or not re.fullmatch(
        r"[A-Za-z0-9_-]{43,100}", config["agent_key"]
    ):
        raise ValueError("invalid_config")
    store = Store(args.config.parent / "state", origin, config.get("base_path", "/ktv"), worker_origin=config.get("worker_origin"),
                  recovery_enabled=config.get("recover_worker", True) is True, background_health=True)
    servers = create_listeners(config, store, config["agent_key"], Path(__file__).parent / "web")
    workers = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .5}, daemon=True) for server in servers[:-1]]
    try:
        for worker in workers:
            worker.start()
        servers[-1].serve_forever(poll_interval=.5)
    finally:
        for server in servers[:-1]:
            server.shutdown()
        for server in servers:
            server.server_close()
        for worker in workers:
            worker.join(2)
        store.coordinator.close()
        store.management.close()


if __name__ == "__main__":
    main()
