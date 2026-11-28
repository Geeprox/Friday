"""Standalone NAS guest song station. Python stdlib only; no household or audio APIs."""

import argparse
import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import socketserver
import sqlite3
import stat
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import unicodedata
import uuid
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    from integrations.ktv_public.audio_format import (
        AUDIO_PLAYBACK_FIELDS, SFX_ACTIONS, SFX_TTL, VOCAL_PRESETS, audio_playback, song_title,
    )
    from integrations.ktv_public.coordinator import PERSISTENT_EXPIRY, Coordinator, private_origin
    from integrations.ktv_public.management import Management
    from integrations.ktv_public.tv_update_inbox import TvUpdateInbox
    from integrations.ktv_public.tv_updates import APK_CONTENT_TYPE, UPDATE_APK_PATH, UPDATE_INFO_PATH, TvUpdateStore
except ImportError:  # 独立 NAS 部署在同一目录加载。
    from audio_format import AUDIO_PLAYBACK_FIELDS, SFX_ACTIONS, SFX_TTL, VOCAL_PRESETS, audio_playback, song_title
    from coordinator import PERSISTENT_EXPIRY, Coordinator, private_origin
    from management import Management
    from tv_update_inbox import TvUpdateInbox
    from tv_updates import APK_CONTENT_TYPE, UPDATE_APK_PATH, UPDATE_INFO_PATH, TvUpdateStore

ID = re.compile(r"[1-9][0-9]{0,17}\Z")
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
ROOM_ROUTE = re.compile(
    r"/api/rooms/([a-f0-9-]{36})/(status|events|search|artwork|discover|playlist|guide|songs|join|profile|theme|controls|command|order|retry)"
)
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
SERVICE_EVENT_LIMIT = 1024 * 1024
SERVICE_OPERATIONS = {"status", "search", "artwork", "discover", "playlist", "guide", "monitor", "songs", "join",
                      "profile", "theme", "controls", "command", "order", "retry"}
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
    "鸣人",
    "柯南",
    "路飞",
    "孙悟空",
    "卡卡西",
    "樱木花道",
    "琪琪",
    "龙猫",
    "齐天大圣",
    "哪吒",
    "猪八戒",
    "唐僧",
    "白素贞",
    "嫦娥",
    "李白",
    "鲁智深",
    "史努比",
    "哆啦A梦",
    "月野兔",
    "小丸子",
)
GUEST_NAMES = NAMES + (
    "山茶晚风", "海盐汽水", "弯月小岛", "白桃列车", "银杏信箱", "雾蓝唱片", "月亮贝壳", "柚子雨",
    "青苔小鹿", "杏仁云", "露台灯", "铃兰邮差", "晚班星光", "松针小熊", "蔷薇车站", "晴空纸船",
    "橘子电台", "雪松泡泡", "河畔回声", "莓果音符", "蒲公英", "山间小调", "晴雨伞", "冰岛月光",
    "银河旅伴", "海风留声", "薄荷岛", "春天胶卷", "时差小猫", "夜色柠檬", "琥珀麦穗", "微风来信",
    "暖橘月台", "云朵口袋", "山野萤火", "雨声漫步", "湖畔晨星", "月光咖啡", "风铃晚班", "浪花回信",
    "露水花园", "海边落日", "星星邮局", "云边积木", "暮色电车", "青柠月亮", "小岛晴天", "咖啡雨",
)
AVATAR_COUNT = len(NAMES)
DEVICE_KEY = re.compile(r"[a-f0-9]{64}\Z")
SEARCH_PAGE_SIZE = 30
SEARCH_MAX_AGE = 600
ARTISTS = ["周杰伦", "陈奕迅", "林俊杰", "五月天", "孙燕姿", "张惠妹", "张学友", "王菲", "邓紫棋", "薛之谦", "陶喆", "林宥嘉", "李荣浩", "郭顶", "告五人", "许嵩", "周深", "张杰", "赵雷", "陈粒", "苏打绿", "王力宏", "刘德华", "梁静茹"]


class Refusal(ValueError):
    def __init__(self, status, code):
        self.status, self.code = status, code


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def guest_origin(value):
    if not isinstance(value, str) or not re.fullmatch(r"https://[a-z0-9.-]+(?::[1-9][0-9]{0,4})?", value):
        return None
    try:
        origin = urllib.parse.urlsplit(value)
        port = origin.port
    except ValueError:
        return None
    if not origin.hostname or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in origin.hostname.split(".")
    ):
        return None
    return value[:-4] if port == 443 else value


def guest_profile(payload):
    if (
        not isinstance(payload, dict) or set(payload) != {"nickname", "avatar"}
        or not isinstance(payload["nickname"], str)
        or not 1 <= len(payload["nickname"].strip()) <= 24
        or any(ord(c) < 32 or ord(c) == 127 for c in payload["nickname"])
        or type(payload["avatar"]) is not int
        or not 0 <= payload["avatar"] < AVATAR_COUNT
    ):
        raise Refusal(400, "invalid_profile")
    return payload["nickname"].strip(), payload["avatar"]


class NoImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Store:
    def __init__(self, root, origin, base_path="/ktv", *, worker_origin=None, allow_loopback_worker=False,
                 recovery_enabled=False, background_health=False, previous_guest_origin=None, public_invite_origin=None):
        self.previous_guest_origin = guest_origin(previous_guest_origin)
        if previous_guest_origin is not None and self.previous_guest_origin is None:
            raise ValueError("invalid_previous_guest_origin")
        invite_origin = guest_origin(public_invite_origin)
        if public_invite_origin is not None and invite_origin is None:
            raise ValueError("invalid_public_invite_origin")
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root:
            raise ValueError("unsafe_root")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.db = self.root / "state.sqlite"
        self.tv_updates = TvUpdateStore(self.root)
        self.origin, self.base_path = guest_origin(origin.rstrip("/")) or origin.rstrip("/"), base_path.rstrip("/")
        self.public_invite_origin = invite_origin
        self._backup_before_guest_queue_upgrade()
        self.lock = threading.RLock()
        self.rate_lock = threading.Lock()
        self.event_lock = threading.Lock()
        self.limits = {}
        self.search_cache = {}
        self.search_pending = {}
        self.catalog_lock = threading.Lock()
        self.search_slots = threading.BoundedSemaphore(4)
        self.artwork_cache = {}
        self.thumbnail_cache = {}
        self.artwork_slots = threading.BoundedSemaphore(2)
        self.next_poll_maintenance = 0
        self.recommendations = None
        self.discovery_lock = threading.Lock()
        self.recommendations_refreshing = False
        self.next_recommendations_refresh = 0
        self.playlist_cache = {}
        self.artist_refreshing = False
        self.next_artist_refresh = 0
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
            CREATE TABLE IF NOT EXISTS guest_devices(device TEXT PRIMARY KEY,nickname TEXT NOT NULL,avatar INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS guest_frequent(device TEXT NOT NULL,source TEXT NOT NULL,song_id TEXT NOT NULL,
              title TEXT NOT NULL,artwork TEXT NOT NULL,plays INTEGER NOT NULL,last_played INTEGER NOT NULL,
              PRIMARY KEY(device,source,song_id));
            CREATE TABLE IF NOT EXISTS retries(room TEXT NOT NULL,request_id TEXT NOT NULL,song TEXT NOT NULL,PRIMARY KEY(room,request_id));
            CREATE TABLE IF NOT EXISTS commands(seq INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
              request_id TEXT NOT NULL, action TEXT NOT NULL, song TEXT NOT NULL, created INTEGER NOT NULL,
              UNIQUE(room,request_id));
            CREATE TABLE IF NOT EXISTS song_requests(room TEXT NOT NULL, request_id TEXT NOT NULL,
              guest_id TEXT NOT NULL, song_id TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
              queue_id TEXT NOT NULL, was_duplicate INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(room,request_id));
            CREATE TABLE IF NOT EXISTS song_withdrawals(room TEXT NOT NULL, guest_id TEXT NOT NULL,
              request_id TEXT NOT NULL, queue_id TEXT NOT NULL, queue_removed INTEGER NOT NULL,
              PRIMARY KEY(room,guest_id,request_id));
            CREATE TABLE IF NOT EXISTS cloud_catalog(song_id TEXT PRIMARY KEY,title TEXT NOT NULL,artists TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_recordings(song_id TEXT PRIMARY KEY,public_id TEXT NOT NULL,
              title TEXT NOT NULL,artists TEXT NOT NULL,album TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS song_catalog(source TEXT NOT NULL,song_id TEXT NOT NULL,title TEXT NOT NULL,
              artists TEXT NOT NULL,album TEXT NOT NULL,artwork TEXT NOT NULL,PRIMARY KEY(source,song_id));
            CREATE TABLE IF NOT EXISTS cloud_staging(position INTEGER PRIMARY KEY,song_id TEXT UNIQUE,title TEXT NOT NULL,artists TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_display_staging(song_id TEXT PRIMARY KEY,album TEXT NOT NULL,artwork TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_sync(id INTEGER PRIMARY KEY,revision TEXT NOT NULL,total INTEGER NOT NULL,received INTEGER NOT NULL,updated INTEGER NOT NULL);
            """)
            for table, name, kind in (
                ("rooms", "theme", "TEXT NOT NULL DEFAULT 'content'"),
                ("rooms", "theme_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("guests", "nickname_custom", "INTEGER NOT NULL DEFAULT -1"),
                ("guests", "device", "TEXT NOT NULL DEFAULT ''"),
                ("guests", "device_token_hash", "TEXT NOT NULL DEFAULT ''"),
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
                ("songs", "frequent_recorded", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_sha", "TEXT NOT NULL DEFAULT ''"),
                ("songs", "guide_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_duration", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_reference_sha", "TEXT NOT NULL DEFAULT ''"),
                ("songs", "guide_reference_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_reference_duration", "INTEGER NOT NULL DEFAULT 0"),
                ("songs", "guide_reference_kind", "TEXT NOT NULL DEFAULT ''"),
                ("rooms", "queue_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("rooms", "music_gain", "INTEGER NOT NULL DEFAULT 70"),
                ("rooms", "music_mute_revision", "INTEGER NOT NULL DEFAULT 0"),
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
                ("rooms", "backing_original", "INTEGER"),
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
    def _default_guest_profile(occupied, *, varied=False):
        """A room-local display name, independent of the guest's credentials."""
        if varied:
            available_names = [name for name in GUEST_NAMES if name not in occupied]
            if available_names:
                return available_names[secrets.randbelow(len(available_names))], secrets.randbelow(len(NAMES))
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
        # 请求计数不等待制备、归档或歌单写事务。
        with self.rate_lock:
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
        db.execute("DELETE FROM coordinator_lyric_status WHERE song NOT IN (SELECT song FROM coordinator_manifests)")
        for table in ("room_tokens", "coordinator_start_requests", "coordinator_session", "coordinator_playback_starts",
                      "retries", "commands", "song_withdrawals"):
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
                             and self.guest_record(db, room_id, guest_key))
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
            room_id, key = self.insert_room(db)
        return {
            "room_id": room_id,
            "expires": PERSISTENT_EXPIRY,
            "persistent": True,
            "theme": "content",
            "theme_revision": 0,
            "join_url": self.join_url(room_id, key),
        }

    def insert_room(self, db):
        room_id, key = str(uuid.uuid4()), secrets.token_urlsafe(32)
        db.execute("INSERT INTO rooms(id,token_hash,expires,mic_muted) VALUES(?,?,?,1)",
                   (room_id, sha(key), PERSISTENT_EXPIRY))
        if getattr(self, "management", None):
            defaults = self.management.preferences()
            db.execute("UPDATE rooms SET " + ",".join(key + "=?" for key in defaults) + " WHERE id=?",
                       (*defaults.values(), room_id))
        return room_id, key

    def join_url(self, room_id, key):
        return (self.public_invite_origin or self.origin) + self.base_path + "/#" + urllib.parse.urlencode({"r": room_id, "k": key})

    @staticmethod
    def guest_record(db, room_id, key):
        digest = sha(key)
        return db.execute("SELECT id,nickname,avatar,device FROM guests WHERE room=? AND "
                          "(token_hash=? OR device_token_hash=?)", (room_id, digest, digest)).fetchone()

    def join(self, room, guest_key=None, *, profile=None, existing_key="", device_key=None):
        if guest_key is not None and (not isinstance(guest_key, str) or not re.fullmatch(r"[a-f0-9]{64}", guest_key)):
            raise Refusal(400, "invalid_request")
        if not isinstance(existing_key, str) or (existing_key and not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", existing_key)):
            raise Refusal(400, "invalid_request")
        if device_key is not None and (not isinstance(device_key, str) or not DEVICE_KEY.fullmatch(device_key)):
            raise Refusal(400, "invalid_request")
        requested = guest_profile(profile) if profile is not None else None
        room_id = room["id"]
        device = sha(device_key) if device_key else ""
        scoped_key = (hmac.new(bytes.fromhex(device_key), ("Friday KTV room credential v2/" + room_id).encode(),
                               hashlib.sha256).hexdigest() if device_key else "")
        legacy_key = (hmac.new(bytes.fromhex(device_key), ("Friday KTV guest room v1/" + room_id).encode(),
                               hashlib.sha256).hexdigest() if device_key else "")
        tokens = tuple(dict.fromkeys(filter(None, (scoped_key, existing_key, guest_key, legacy_key))))

        def find(db):
            active = db.execute("SELECT closed,played FROM rooms WHERE id=?", (room_id,)).fetchone()
            if not active or active["closed"]:
                raise Refusal(410, "room_closed")
            for token in tokens:
                row = self.guest_record(db, room_id, token)
                if row:
                    return active, row, token
            return active, None, ""

        # 日常扫码重入只读，正在归档时也无需等待 SQLite 的写事务。
        with self.connection() as db:
            active, existing, key = find(db)
        if existing and (not device or existing["device"]):
            return {"id": existing["id"], "key": key, "nickname": existing["nickname"], "avatar": existing["avatar"]}
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active, existing, key = find(db)
            if existing:
                if device and not existing["device"]:
                    db.execute("INSERT OR IGNORE INTO guest_devices VALUES(?,?,?)",
                               (device, existing["nickname"], existing["avatar"]))
                    db.execute("UPDATE guests SET device=?,device_token_hash=? WHERE id=?",
                               (device, sha(scoped_key), existing["id"]))
                    self.remember_plays(db, room_id, json.loads(active["played"]), guest_id=existing["id"])
                return {"id": existing["id"], "key": key, "nickname": existing["nickname"], "avatar": existing["avatar"]}
            if db.execute("SELECT count(*) FROM guests WHERE room=?", (room_id,)).fetchone()[0] >= 60:
                raise Refusal(429, "guest_capacity")
            self.budget("join:" + room_id, 60, 14400)
            saved = db.execute("SELECT nickname,avatar FROM guest_devices WHERE device=?", (device,)).fetchone() if device else None
            guest_id, key = str(uuid.uuid4()), scoped_key or guest_key or secrets.token_urlsafe(32)
            occupied = {row[0] for row in db.execute("SELECT nickname FROM guests WHERE room=?", (room_id,))}
            nickname, avatar = saved or requested or self._default_guest_profile(occupied, varied=True)
            if device and not saved:
                db.execute("INSERT INTO guest_devices VALUES(?,?,?)", (device, nickname, avatar))
            db.execute(
                "INSERT INTO guests(id,room,token_hash,nickname,avatar,created,nickname_custom,device) VALUES(?,?,?,?,?,?,?,?)",
                (guest_id, room_id, sha(key), nickname, avatar, int(time.time()), int(bool(saved or requested)), device),
            )
        return {"id": guest_id, "key": key, "nickname": nickname, "avatar": avatar}

    def guest(self, room, key):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", key):
            raise Refusal(403, "guest_unavailable")
        with self.connection() as db:
            value = self.guest_record(db, room["id"], key)
        if not value:
            raise Refusal(403, "guest_unavailable")
        return {k: value[k] for k in ("id", "nickname", "avatar")}

    def profile(self, room, guest, payload):
        nickname, avatar = guest_profile(payload)
        with self.lock, self.connection() as db:
            db.execute(
                "UPDATE guests SET nickname=?,avatar=?,nickname_custom=1 WHERE id=? AND room=?",
                (nickname, avatar, guest["id"], room["id"]),
            )
            db.execute(
                "UPDATE songs SET nickname=?,avatar=? WHERE guest_id=? AND room=?",
                (nickname, avatar, guest["id"], room["id"]),
            )
            db.execute("UPDATE guest_devices SET nickname=?,avatar=? WHERE device=(SELECT device FROM guests WHERE id=?)",
                       (nickname, avatar, guest["id"]))
        return {"id": guest["id"], "nickname": nickname, "avatar": avatar}

    def remember_plays(self, db, room_id, ids, *, guest_id=None):
        if not ids:
            return
        sql = ("SELECT s.id,s.source,s.song_id,s.title,s.artwork,g.device FROM songs s "
               "JOIN guests g ON g.id=s.guest_id AND g.room=s.room JOIN rooms r ON r.id=s.room "
               "WHERE s.room=? AND r.closed=0 AND s.state='ready' AND s.frequent_recorded=0 AND g.device!='' "
               "AND s.id IN (" + ",".join("?" for _ in ids) + ")")
        params = (room_id, *ids)
        if guest_id:
            sql += " AND g.id=?"
            params += (guest_id,)
        for row in db.execute(sql, params).fetchall():
            db.execute("UPDATE songs SET frequent_recorded=1 WHERE id=?", (row["id"],))
            db.execute("INSERT INTO guest_frequent VALUES(?,?,?,?,?,1,?) "
                       "ON CONFLICT(device,source,song_id) DO UPDATE SET plays=plays+1,title=excluded.title,"
                       "artwork=CASE WHEN excluded.artwork='' THEN artwork ELSE excluded.artwork END,last_played=excluded.last_played",
                       (row["device"], row["source"], row["song_id"], song_title(row["title"]), row["artwork"], int(time.time())))

    def frequent(self, room, guest):
        with self.connection() as db:
            rows = db.execute("SELECT source,song_id,title,artwork,plays FROM guest_frequent WHERE "
                              "device=(SELECT device FROM guests WHERE id=? AND room=?) "
                              "ORDER BY plays DESC,last_played DESC,song_id LIMIT 40", (guest["id"], room["id"])).fetchall()
        return {"songs": [dict(row) for row in rows]}

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
            row = db.execute("SELECT theme_revision,closed FROM rooms WHERE id=?", (room["id"],)).fetchone()
            if not row or row["closed"]:
                raise Refusal(410, "room_closed")
        return {"theme": payload["theme"], "theme_revision": row["theme_revision"]}

    def close(self, room_id):
        if not UUID.fullmatch(room_id):
            raise Refusal(400, "invalid_room")
        with self.lock, self.connection() as db:
            db.execute("UPDATE rooms SET closed=1,expires=? WHERE id=?", (int(time.time()), room_id))
            self._expire(db)
        return {"closed": True}

    def online(self, db):
        # 会话查询复用当前事务，避免持有手机状态快照时与电视心跳提交互相阻塞。
        if self.coordinator is not None and self.coordinator.active(db):
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
                "SELECT s.id,s.song_id,s.title,s.state,s.code,s.created,s.guest_id,s.nickname,s.avatar,"
                "coalesce(nullif(ct.artwork,''),s.artwork,'') AS artwork,s.progress,s.cache_hit,s.source,"
                "coalesce(nullif(ct.artists,'[]'),cl.artists,'[]') AS artists,coalesce(nullif(ct.album,''),cr.album,'') AS album,"
                "coalesce(ls.ready,0) AS lyric_state,coalesce(ls.ready=1,0) AS synced_lyrics "
                "FROM songs s LEFT JOIN coordinator_lyric_status ls ON ls.song=s.id "
                "LEFT JOIN song_catalog ct ON ct.song_id=s.song_id AND ct.source=s.source "
                "LEFT JOIN cloud_catalog cl ON cl.song_id=s.song_id AND s.source='cloud' "
                "LEFT JOIN cloud_recordings cr ON cr.song_id=cl.song_id AND cr.title=cl.title AND cr.artists=cl.artists "
                "WHERE s.room=? AND s.removed=0 ORDER BY s.position,s.rowid LIMIT 80",
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
                    {**dict(r), "title": song_title(r["title"]), "lyrics_pending": r["lyric_state"] != -1
                     and (r["id"] == theme["lyrics_waiting"] or r["state"] == "ready" and not r["synced_lyrics"]),
                     "progress": json.loads(r["progress"]), "cache_hit": bool(r["cache_hit"]),
                     "artists": json.loads(r["artists"])}
                    for r in rows
                ],
                "theme": theme["theme"],
                "theme_revision": theme["theme_revision"],
                "queue_revision": theme["queue_revision"],
                "queue_withdraw_ready": self.coordinator.queue_claims_ready(db, room["id"]),
                "upcoming": self.upcoming_ids(rows, theme),
                "playable": self.coordinator.playable_ids(db, rows, theme),
                "controls": {
                    k: theme[k]
                    for k in ("music_gain", "music_mute_revision", "mic_gain", "mic_muted", "original", "control_revision", "mic_revision", "vocal_preset", "sfx_gain")
                },
                "playback": {
                    "online": time.time() - theme["playback_at"] < 12,
                    "current": theme["current_song"],
                    "played": json.loads(theme["played"]),
                    "state": theme["playback_state"],
                    "safety_latched": bool(theme["safety_latched"]),
                    "mic_muted": bool(theme["actual_mic_muted"]),
                    "applied_revision": theme["applied_revision"],
                    "backing_original": theme["backing_original"],
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

    @staticmethod
    def upcoming_ids(rows, room, *, playable_only=False):
        played = set(json.loads(room["played"]))
        return [row["id"] for row in rows if row["id"] != room["current_song"]
                and row["id"] not in played and row["state"] not in {"failed", "cancelled"}
                and (not playable_only or row["state"] == "ready" and row["synced_lyrics"])]

    def song_controls(self, room, payload):
        expected_mute = payload.get("music_mute_revision")
        if (set(payload) - {"music_gain", "music_mute_revision", "sfx_gain", "original"}
                or ("music_mute_revision" in payload and
                    ("music_gain" not in payload or type(expected_mute) is not int or expected_mute < 0))):
            raise Refusal(400, "invalid_control")
        values = {key: value for key, value in payload.items() if key != "music_mute_revision"}
        return self.controls(room, values, expected_mute=expected_mute)

    def controls(self, room, payload, *, expected_mute=None):
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
            db.execute("BEGIN IMMEDIATE")
            room_state = db.execute("SELECT music_mute_revision,closed FROM rooms WHERE id=?", (room["id"],)).fetchone()
            if not room_state or room_state["closed"]:
                raise Refusal(410, "room_closed")
            if payload.get("music_gain", 0) > 0 and expected_mute is not None and expected_mute != room_state["music_mute_revision"]:
                raise Refusal(409, "music_mute_changed")
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
                + (",music_mute_revision=music_mute_revision+1" if payload.get("music_gain") == 0 else "")
                + " WHERE id=? AND closed=0",
                (*payload.values(), room["id"]),
            )
            revision, mute_revision = db.execute(
                "SELECT control_revision,music_mute_revision FROM rooms WHERE id=?", (room["id"],)).fetchone()
            if "mic_muted" in payload:
                db.execute("UPDATE rooms SET mic_request_at=?,mic_request_revision=? WHERE id=?",
                           (int(time.time()) if payload["mic_muted"] == 0 else 0, revision, room["id"]))
        return {"accepted": True, "revision": revision, "music_mute_revision": mute_revision}

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
                "SELECT playback_at,current_song,audio_capabilities,original,closed FROM rooms WHERE id=?", (room["id"],)
            ).fetchone()
            if not state or state["closed"]:
                raise Refusal(410, "room_closed")
            if time.time() - state["playback_at"] >= 12:
                raise Refusal(409, "tv_offline")
            if payload["action"] in SFX_ACTIONS:
                if payload["action"] != "sfx_stop" and not json.loads(state["audio_capabilities"]).get("sfx", False):
                    raise Refusal(409, "sfx_unavailable")
            elif payload["action"] == "play":
                target = payload["song"]
                item = db.execute(
                    "SELECT state,song_id FROM songs WHERE id=? AND room=? AND removed=0", (target, room["id"])
                ).fetchone()
                if not item or item["state"] != "ready":
                    raise Refusal(409, "song_not_ready")
                if target != state["current_song"] and db.execute(
                    "SELECT 1 FROM coordinator_session WHERE room=?", (room["id"],)
                ).fetchone():
                    if not db.execute(
                        "SELECT 1 FROM coordinator_lyric_status WHERE song=? AND ready=1", (target,)
                    ).fetchone():
                        raise Refusal(409, "lyrics_not_ready")
                    if target not in self.coordinator.available_track_ids(db, (target,), state["original"]):
                        raise Refusal(409, "media_not_ready")
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
            not required <= set(payload) or set(payload) - required - {"position_ms", "lyrics_waiting", "playback_epoch", "backing_original"} - AUDIO_PLAYBACK_FIELDS
            or ("backing_original" in payload and (type(payload["backing_original"]) is not int or payload["backing_original"] not in (-1, 0, 1)))
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
            ids = {r["id"]: r["removed"] for r in db.execute("SELECT id,removed FROM songs WHERE room=?", (room_id,))}
            if ((payload["current"] and payload["current"] not in ids) or set(payload["played"]) - ids.keys()
                    or (payload.get("lyrics_waiting") and payload["lyrics_waiting"] not in ids)):
                raise Refusal(400, "invalid_playback")
            if payload["current"] and ids[payload["current"]]:
                raise Refusal(409, "song_unavailable")
            previous = db.execute("SELECT played FROM rooms WHERE id=?", (room_id,)).fetchone()
            existing_history = set(json.loads(previous["played"])) if previous else set()
            played = [song for song in payload["played"] if not ids[song] or song in existing_history]
            db.execute("UPDATE rooms SET lyrics_waiting=? WHERE id=? AND closed=0", (payload.get("lyrics_waiting", ""), room_id))
            db.execute(
                "UPDATE rooms SET playback_at=?,current_song=?,played=?,playback_state=?,last_command=max(last_command,?),"
                "safety_latched=?,applied_revision=?,actual_mic_muted=?,backing_original=? WHERE id=? AND closed=0",
                (
                    int(time.time()),
                    payload["current"],
                    json.dumps(played),
                    payload["state"],
                    payload["last_command"],
                    payload["safety_latched"],
                    payload["applied_revision"],
                    payload["mic_muted"],
                    payload.get("backing_original"),
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
            if payload["state"] == "playing" and payload["current"]:
                self.remember_plays(db, room_id, (payload["current"],))
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
            if guest is not None:
                withdrawn = db.execute(
                    "SELECT queue_id FROM song_withdrawals WHERE room=? AND guest_id=? AND request_id=?",
                    (room["id"], guest["id"], request_id),
                ).fetchone()
                if withdrawn:
                    return {"id": withdrawn["queue_id"], "state": "cancelled"}
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
            archived = self.management.verified_edition(song_id, source, db)
            direct = self.coordinator.active(db)
            if archived is None and not (direct and direct["room"] == room["id"]) and not self.online(db):
                raise Refusal(503, "mac_offline")
            if db.execute("SELECT count(*) FROM songs WHERE room=? AND removed=0", (room["id"],)).fetchone()[0] >= 80:
                raise Refusal(429, "queue_full")
            if archived is None and (
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
            with self.catalog_lock:
                artwork = self.artwork_cache.get(song_id, (0, ""))[1] if source != "mv" else ""
                if source == "mv":
                    artwork = next((s["artwork"] for _, page in self.search_cache.values()
                                    for s in page["songs"] if s["source"] == source
                                    and s["song_id"] == song_id and s.get("artwork")), "")
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
            if archived is not None:
                self.management.attach_edition(archived, row_id, title, source, profile, db)
                db.execute("UPDATE songs SET state='ready',cache_hit=1 WHERE id=?", (row_id,))
                return {"id": row_id, "state": "ready", "cache_hit": True}
            return {"id": row_id, "state": "queued"}

    def withdraw_song(self, room, payload, guest):
        keys = set(payload)
        if keys not in ({"withdraw_id"}, {"withdraw_request_id"}):
            raise Refusal(400, "invalid_request")
        field, value = next(iter(payload.items()))
        if not isinstance(value, str) or not UUID.fullmatch(value):
            raise Refusal(400, "invalid_request")
        room_id, guest_id = room["id"], guest["id"]
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT current_song,played,last_command,queue_revision FROM rooms WHERE id=? AND closed=0", (room_id,)
            ).fetchone()
            if not active:
                raise Refusal(410, "room_closed")
            if field == "withdraw_id":
                revision = self._withdraw_queue_song(db, room_id, guest_id, value, active)
                return {"withdrawn": True, "id": value, "queue_removed": True, "queue_revision": revision}
            stored = db.execute(
                "SELECT queue_id,queue_removed FROM song_withdrawals WHERE room=? AND guest_id=? AND request_id=?",
                (room_id, guest_id, value),
            ).fetchone()
            if stored:
                return {"withdrawn": True, "id": stored["queue_id"], "queue_removed": bool(stored["queue_removed"]),
                        **({"queue_revision": active["queue_revision"]} if stored["queue_removed"] else {})}
            request = db.execute(
                "SELECT guest_id,queue_id,was_duplicate FROM song_requests WHERE room=? AND request_id=?",
                (room_id, value),
            ).fetchone()
            if request and request["guest_id"] != guest_id:
                raise Refusal(403, "song_not_owned")
            self.budget("add-receipt:" + room_id, 120)
            queue_id = request["queue_id"] if request else str(uuid.uuid4())
            removed = bool(request and not request["was_duplicate"])
            revision = self._withdraw_queue_song(db, room_id, guest_id, queue_id, active) if removed else None
            # 请求可能仍在路上，先落同访客凭据，后到的点歌不会重新进入歌单。
            db.execute("INSERT INTO song_withdrawals VALUES(?,?,?,?,?)", (room_id, guest_id, value, queue_id, int(removed)))
            return {"withdrawn": True, "id": queue_id, "queue_removed": removed,
                    **({"queue_revision": revision} if removed else {})}

    def _withdraw_queue_song(self, db, room_id, guest_id, song_id, active):
        item = db.execute(
            "SELECT guest_id,removed,state FROM songs WHERE id=? AND room=?", (song_id, room_id)
        ).fetchone()
        if not item:
            raise Refusal(404, "song_unavailable")
        if item["guest_id"] != guest_id:
            raise Refusal(403, "song_not_owned")
        if item["removed"]:
            return active["queue_revision"]
        now = int(time.time())
        starting = db.execute(
            "SELECT 1 FROM commands WHERE room=? AND song=? AND action='play' AND seq>? AND created>? AND created<=?",
            (room_id, song_id, active["last_command"], now - 20, now),
        ).fetchone()
        if (song_id == active["current_song"] or song_id in json.loads(active["played"]) or starting
                or self.coordinator.playback_claimed(db, room_id, song_id)):
            raise Refusal(409, "song_already_started")
        if item["state"] == "ready" and not self.coordinator.queue_claims_ready(db, room_id):
            raise Refusal(409, "song_start_unavailable")
        db.execute("UPDATE songs SET removed=1,state='cancelled',code='cancelled',lease=0 WHERE id=?", (song_id,))
        db.execute("UPDATE rooms SET queue_revision=queue_revision+1 WHERE id=?", (room_id,))
        return active["queue_revision"] + 1

    def poll(self):
        with self.lock, self.connection() as db:
            now = int(time.time())
            monotonic = time.monotonic()
            heartbeat_due = monotonic >= self.next_poll_maintenance
            expiry_due = db.execute(
                "SELECT 1 FROM rooms WHERE (closed=1 AND expires<?) "
                "OR (closed=0 AND mic_muted=0 AND mic_request_at>0 AND mic_request_at<=?) LIMIT 1",
                (now - 86400, now - 5),
            ).fetchone()
            if heartbeat_due or expiry_due:
                # 空轮询只读。定期续租兼容心跳和清理，仍然立即领取新任务。
                db.execute("BEGIN IMMEDIATE")
                self._expire(db)
            if heartbeat_due:
                db.execute("INSERT OR REPLACE INTO heartbeat VALUES(1,?)", (now,))
                self.next_poll_maintenance = monotonic + 2
            direct_row = db.execute("SELECT session.* FROM coordinator_session AS session JOIN rooms ON rooms.id=session.room WHERE session.id=1 AND rooms.closed=0").fetchone()
            direct = dict(direct_row) if direct_row else None
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
                    join_url = self.join_url(direct["room"], direct["secret"])
                    info.update(join_url=join_url, tv_instance=direct["instance"], direct=True)
                    info["qr_ready"] = self.coordinator.qr_ready(direct)
                    playback = db.execute("SELECT current_song,played FROM rooms WHERE id=?", (r["room_id"],)).fetchone()
                    info["playback"] = {"current": playback["current_song"], "played": json.loads(playback["played"])}
                rooms.append(info)
            archives = self.coordinator.archives(direct["room"], db) if direct else []
            lyrics = self.coordinator.lyric_tasks(direct["room"], db) if direct else []
            return {"tasks": tasks, "cancelled": [r[0] for r in cancelled], "rooms": rooms, "archives": archives,
                    "lyric_tasks": lyrics, "backgrounds_enabled": self.coordinator.backgrounds_enabled,
                    "archive_videos": True, "playback_encoding": self.coordinator.playback_encoding(direct)}

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
            direct = self.coordinator.active(db)
            if not (direct and direct["room"] == room["id"]) and not self.online(db):
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
                    "waiting_storage",
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
        rows, display = [], []
        for row in payload["songs"]:
            if (not isinstance(row, dict) or not {"song_id", "title", "artists", "source"} <= set(row)
                    or set(row) - {"song_id", "title", "artists", "source", "album", "artwork"}
                    or row["source"] != "cloud" or not isinstance(row["song_id"], str) or not ID.fullmatch(row["song_id"])
                    or not isinstance(row["title"], str) or not 1 <= len(row["title"]) <= 120
                    or not isinstance(row["artists"], list) or len(row["artists"]) > 8
                    or any(not isinstance(a, str) or len(a) > 100 for a in row["artists"])
                    or not isinstance(row.get("album", ""), str) or len(row.get("album", "")) > 160
                    or not isinstance(row.get("artwork", ""), str) or len(row.get("artwork", "")) > 2048
                    or any(ord(c) < 32 or ord(c) == 127 for c in row["title"] + "".join(row["artists"]) + row.get("album", ""))):
                raise Refusal(400, "invalid_catalog")
            rows.append((row["song_id"], row["title"], json.dumps(row["artists"], ensure_ascii=False)))
            display.append((row["song_id"], row.get("album", ""), self.artwork(row.get("artwork", ""))))
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            sync = db.execute("SELECT * FROM cloud_sync WHERE id=1").fetchone()
            if payload["offset"] == 0:
                db.execute("DELETE FROM cloud_staging")
                db.execute("DELETE FROM cloud_display_staging")
                db.execute("INSERT OR REPLACE INTO cloud_sync VALUES(1,?,?,0,?)",
                           (payload["revision"], payload["total"], int(time.time())))
            elif (not sync or sync["revision"] != payload["revision"] or sync["received"] != payload["offset"]
                  or sync["total"] != payload["total"]):
                raise Refusal(409, "catalog_changed")
            for i, row in enumerate(rows):
                db.execute("INSERT INTO cloud_staging VALUES(?,?,?,?)", (payload["offset"] + i, *row))
            db.executemany("INSERT INTO cloud_display_staging VALUES(?,?,?)", display)
            received = payload["offset"] + len(rows)
            db.execute("UPDATE cloud_sync SET received=?,updated=? WHERE id=1", (received, int(time.time())))
            if received == payload["total"]:
                db.execute("DELETE FROM cloud_catalog")
                db.execute("INSERT INTO cloud_catalog SELECT song_id,title,artists FROM cloud_staging")
                # 显示资料随完整云盘快照一起发布，残缺分页不能替换正在使用的资料。
                db.execute("INSERT INTO song_catalog SELECT 'cloud',c.song_id,c.title,c.artists,d.album,d.artwork "
                           "FROM cloud_catalog c JOIN cloud_display_staging d ON d.song_id=c.song_id WHERE 1 "
                           "ON CONFLICT(source,song_id) DO UPDATE SET title=excluded.title,artists=excluded.artists,"
                           "album=CASE WHEN excluded.album!='' THEN excluded.album WHEN title=excluded.title AND artists=excluded.artists THEN album ELSE '' END,"
                           "artwork=CASE WHEN excluded.artwork!='' THEN excluded.artwork WHEN title=excluded.title AND artists=excluded.artists THEN artwork ELSE '' END")
        with self.catalog_lock:
            for ident, _, _ in display:
                self.artwork_cache.pop("cloud:" + ident, None)
        return {"accepted": True, "complete": received == payload["total"], "count": received}

    def search(self, query, source="song", offset=0):
        if not isinstance(query, str) or not (0 if source == "cloud" else 1) <= len(query.strip()) <= 120:
            raise Refusal(400, "invalid_query")
        if source not in {"song", "cloud", "mv"}:
            raise Refusal(400, "invalid_source")
        if type(offset) is not int or not 0 <= offset <= 100000:
            raise Refusal(400, "invalid_query")
        query = query.strip()
        if source == "cloud":
            terms = query.casefold().split()
            with self.connection() as db:
                rows = db.execute("SELECT * FROM cloud_catalog ORDER BY title").fetchall()
                sync = db.execute("SELECT updated FROM cloud_sync WHERE id=1").fetchone()
            matches = [r for r in rows if all(t in (r["title"] + " " + " ".join(json.loads(r["artists"]))).casefold() for t in terms)]
            with self.connection() as db:
                catalog = self.catalog_index(db)
            songs = [{"song_id": r["song_id"], "title": song_title(r["title"]), "artists": json.loads(r["artists"]),
                      "source": "cloud", **{name: catalog[("cloud", r["song_id"])][name] for name in ("album", "artwork")
                                              if catalog[("cloud", r["song_id"])].get(name)}}
                     for r in matches[offset:offset + 30]]
            return {"songs": songs, "source": "cloud", "catalog_count": len(rows), "updated": sync[0] if sync else 0,
                    "offset": offset, "next_offset": offset + 30, "has_more": offset + 30 < len(matches), "total": len(matches)}
        cache_key = (source, query.casefold(), offset)
        with self.catalog_lock:
            cached = self.search_cache.get(cache_key)
            age = time.monotonic() - cached[0] if cached else float("inf")
            if age < SEARCH_MAX_AGE:
                return cached[1]
            stale = cached[1] if age < 86400 else None
            future = self.search_pending.get(cache_key)
            owner = future is None
            if owner:
                if not self.search_slots.acquire(blocking=False):
                    if stale is not None:
                        return stale
                    raise Refusal(429, "search_busy")
                future = self.search_pending[cache_key] = Future()
        if not owner:
            try:
                return future.result(timeout=5.5)
            except FutureTimeout:
                if stale is not None:
                    return stale
                raise Refusal(503, "search_unavailable") from None
        try:
            pages = self.catalog_pages(query, source, offset)
            if source == "song":
                self.save_catalog([song for page in pages for song in page["songs"]])
            stamp = time.monotonic()
            with self.catalog_lock:
                for page in pages:
                    page_key = (source, query.casefold(), page["offset"])
                    self.search_cache.pop(page_key, None)
                    self.search_cache[page_key] = (stamp, page)
                    if len(self.search_cache) > 128:
                        self.search_cache.pop(next(iter(self.search_cache)))
                    if source == "song":
                        self.remember_artwork({row["song_id"]: row["artwork"] for row in page["songs"] if row.get("artwork")}, stamp)
            future.set_result(pages[0])
            return pages[0]
        except (OSError, ValueError, Refusal):
            if stale is not None:
                future.set_result(stale)
                return stale
            error = Refusal(503, "search_unavailable")
            future.set_exception(error)
            raise error from None
        finally:
            with self.catalog_lock:
                self.search_pending.pop(cache_key, None)
            self.search_slots.release()

    def catalog_pages(self, query, source, offset):
        page_size = SEARCH_PAGE_SIZE
        # 曲库一次返回两页时，手机翻页只需读取 NAS 缓存。
        limit = page_size * 2 if source == "song" else page_size
        result = self.catalog_search(source, {"s": query, "type": 1004 if source == "mv" else 1,
                                              "limit": limit, "offset": offset})
        rows = result.get("mvs" if source == "mv" else "songs", [])[:limit]
        count = result.get("mvCount" if source == "mv" else "songCount")
        pages = []
        for start in range(0, max(1, len(rows)), page_size):
            batch = rows[start:start + page_size]
            songs = self.catalog_songs(batch, source)
            next_offset = offset + start + page_size
            page = {"songs": songs, "source": source, "offset": offset + start, "next_offset": next_offset,
                    "has_more": next_offset < count if type(count) is int else len(batch) == page_size}
            if type(count) is int and count >= 0:
                page["total"] = count
            pages.append(page)
        return pages

    def catalog_songs(self, rows, source):
        songs = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            song_id, title = str(row.get("id", "")), row.get("name")
            if ID.fullmatch(song_id) and isinstance(title, str) and 0 < len(title) <= 120:
                artists = [
                    a["name"][:100]
                    for a in (row["artists"][:8] if isinstance(row.get("artists"), list) else [])
                    if isinstance(a, dict) and isinstance(a.get("name"), str)
                ]
                song = {"song_id": song_id, "title": title, "artists": artists, "source": source}
                album = row.get("album")
                if isinstance(album, dict) and isinstance(album.get("name"), str):
                    song["album"] = album["name"][:160]
                artwork = self.artwork(album.get("picUrl") if isinstance(album, dict) else row.get("cover"))
                if artwork:
                    song["artwork"] = artwork
                songs.append(song)
        return songs

    def catalog_search(self, source, params):
        # 同一个官方曲库的备用查询路径接管首轮超时。
        paths = (("/api/search/get", 5),) if source == "mv" else (
            ("/api/search/get", 2), ("/api/search/get/web", 3))
        field = "mvs" if source == "mv" else "songs"
        for path, timeout in paths:
            try:
                result = self.public_metadata(path, params, timeout=timeout).get("result", {})
            except (OSError, ValueError, Refusal):
                continue
            if isinstance(result, dict) and isinstance(result.get(field, []), list):
                return result
        raise Refusal(503, "search_unavailable")

    def remember_artwork(self, covers, stamp):
        for ident, cover in covers.items():
            # 较晚结束的详细信息请求可能没有封面，保留已知有效的封面。
            previous = self.artwork_cache.pop(ident, None)
            if not cover and previous and previous[1]:
                stamp, cover = previous
            self.artwork_cache[ident] = (stamp, cover)
            if len(self.artwork_cache) > 512:
                self.artwork_cache.pop(next(iter(self.artwork_cache)))

    def save_catalog(self, tracks):
        if not tracks:
            return
        with self.connection() as db:
            db.executemany("INSERT INTO song_catalog VALUES(?,?,?,?,?,?) "
                           "ON CONFLICT(source,song_id) DO UPDATE SET title=excluded.title,artists=excluded.artists,"
                           "album=excluded.album,artwork=excluded.artwork "
                           "WHERE title!=excluded.title OR artists!=excluded.artists OR album!=excluded.album OR artwork!=excluded.artwork",
                           [(track["source"], track["song_id"], track["title"], json.dumps(track["artists"], ensure_ascii=False),
                             track.get("album", ""), track.get("artwork", "")) for track in tracks])

    def catalog_index(self, db):
        tracks = {(row["source"], row["song_id"]): {**dict(row), "artists": json.loads(row["artists"])}
                  for row in db.execute("SELECT * FROM song_catalog")}
        for row in db.execute("SELECT cl.*,coalesce(cr.album,'') AS album FROM cloud_catalog cl "
                              "LEFT JOIN cloud_recordings cr ON cr.song_id=cl.song_id AND cr.title=cl.title AND cr.artists=cl.artists"):
            key = ("cloud", row["song_id"])
            previous = tracks.get(key, {})
            tracks[key] = {**previous, "source": "cloud", "song_id": row["song_id"], "title": song_title(row["title"]),
                           "artists": json.loads(row["artists"]), "album": previous.get("album") or row["album"]}
        return tracks

    def cloud_recordings(self, ids):
        marks = ",".join("?" for _ in ids)
        with self.connection() as db:
            rows = db.execute("SELECT * FROM cloud_catalog WHERE song_id IN (" + marks + ")", ids).fetchall()
            known = {r["song_id"]: dict(r) for r in db.execute(
                "SELECT cr.* FROM cloud_recordings cr JOIN cloud_catalog c "
                "ON c.song_id=cr.song_id AND c.title=cr.title AND c.artists=cr.artists "
                "WHERE cr.song_id IN (" + marks + ")", ids)}
        missing = [dict(row) for row in rows if row["song_id"] not in known]

        def normalized(value):
            return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())

        def resolve(row):
            title, artists = song_title(row["title"]), json.loads(row["artists"])
            result = self.catalog_search("song", {"s": " ".join([title, *artists]), "type": 1, "limit": 30, "offset": 0})
            matches = [song for song in self.catalog_songs(result.get("songs", []), "song")
                       if normalized(song["title"]) == normalized(title)
                       and (not artists or set(map(normalized, artists)) & set(map(normalized, song["artists"])))]
            if not matches or not artists and len(matches) != 1:
                return None
            song = matches[0]
            value = {**row, "public_id": song["song_id"], "album": song.get("album", "")}
            with self.lock, self.connection() as db:
                db.execute("INSERT OR REPLACE INTO cloud_recordings VALUES(?,?,?,?,?)",
                           (row["song_id"], song["song_id"], row["title"], row["artists"], value["album"]))
            return value

        with ThreadPoolExecutor(max_workers=3) as pool:
            for value in pool.map(resolve, missing):
                if value:
                    known[value["song_id"]] = value
        return known

    def search_artwork(self, song_ids, *, inline=False, source="song", refresh=False):
        if not isinstance(song_ids, str) or len(song_ids) > 569:
            raise Refusal(400, "invalid_query")
        if source not in {"song", "cloud"} or type(refresh) is not bool:
            raise Refusal(400, "invalid_query")
        ids = song_ids.split(",")
        if not 1 <= len(ids) <= 30 or any(not ID.fullmatch(i) for i in ids):
            raise Refusal(400, "invalid_query")
        now = time.monotonic()
        prefix = "cloud:" if source == "cloud" else ""
        with self.catalog_lock:
            cached = {i: self.artwork_cache[prefix + i][1] for i in dict.fromkeys(ids)
                      if prefix + i in self.artwork_cache and now - self.artwork_cache[prefix + i][0]
                      < (86400 if self.artwork_cache[prefix + i][1] else 90)}
        if refresh:
            cached = {}
        if source == "cloud":
            with self.connection() as db:
                index = self.catalog_index(db)
            # 云盘自带的显示封面可以直接使用，不必把上传 ID 当成公开录音再查一次。
            for ident in ids:
                cover = self.artwork(index.get((source, ident), {}).get("artwork", ""))
                if cover:
                    cached[ident] = cover
        missing = list(dict.fromkeys(i for i in ids if i not in cached))
        albums = {}
        if missing:
            if not self.artwork_slots.acquire(blocking=False):
                raise Refusal(429, "search_busy")
            try:
                recordings = self.cloud_recordings(missing) if source == "cloud" else {}
                mapped = {ident: row["public_id"] for ident, row in recordings.items()} if source == "cloud" else dict.fromkeys(missing)
                public_ids = list(dict.fromkeys(mapped.values())) if source == "cloud" else missing
                albums = {ident: row["album"] for ident, row in recordings.items() if row["album"]}
                details = self.public_metadata("/api/v3/song/detail",
                                               {"c": json.dumps([{"id": int(i)} for i in public_ids], separators=(",", ":"))},
                                               timeout=5) if public_ids else {"songs": []}
            except (OSError, ValueError, Refusal):
                raise Refusal(503, "search_unavailable") from None
            finally:
                self.artwork_slots.release()
            songs = details.get("songs")
            if not isinstance(songs, list):
                raise Refusal(503, "search_unavailable")
            resolved = dict.fromkeys(missing, "")
            tracks = []
            for song in songs[:30]:
                if not isinstance(song, dict):
                    continue
                ident, album = str(song.get("id", "")), song.get("al")
                if isinstance(album, dict):
                    if source == "song" and ident in resolved:
                        resolved[ident] = self.artwork(album.get("picUrl"))
                    if source == "cloud":
                        for own, public in mapped.items():
                            if ident == public:
                                resolved[own] = self.artwork(album.get("picUrl"))
                public = self.catalog_songs([{"id": song.get("id"), "name": song.get("name"),
                                              "artists": song.get("ar", []), "album": album}], "song")
                if public and ident in public_ids:
                    track = public[0]
                    if source == "song":
                        tracks.append(track)
                    else:
                        for own, recording in recordings.items():
                            if recording["public_id"] == ident:
                                tracks.append({**track, "song_id": own, "source": "cloud",
                                               "title": song_title(recording["title"]), "artists": json.loads(recording["artists"])})
            self.save_catalog(tracks)
            with self.catalog_lock:
                self.remember_artwork({prefix + ident: value for ident, value in resolved.items()}, time.monotonic())
                cached.update({ident: self.artwork_cache[prefix + ident][1] for ident in resolved})
        covers = {ident: cover for ident, cover in cached.items() if cover}
        with self.connection() as db:
            index = self.catalog_index(db)
            tracks = {ident: {field: index[(source, ident)].get(field, "" if field != "artists" else [])
                              for field in ("title", "artists", "album", "source")}
                      for ident in ids if (source, ident) in index}
        albums.update({ident: track["album"] for ident, track in tracks.items() if track["album"]})
        if not inline:
            return {"covers": covers, **({"tracks": tracks} if tracks else {})}
        def thumbnail(item):
            ident, url = item
            url = url.replace("?param=240y240", "?param=112y112")
            with self.catalog_lock:
                saved = self.thumbnail_cache.get(url)
                if refresh:
                    self.thumbnail_cache.pop(url, None)
            if saved and not refresh:
                return ident, saved
            try:
                request = urllib.request.Request(url, headers={"Referer": "https://music.163.com/"})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoImageRedirect())
                with opener.open(request, timeout=3) as image:
                    data = image.read(1048577)
                # 官方 CDN 有时把 PNG 声明成 image/jpg，按实际文件签名返回类型。
                kind = "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if data.startswith(b"\xff\xd8\xff") else ""
                if len(data) > 1048576 or not kind:
                    return ident, ""
                value = "data:" + kind + ";base64," + base64.b64encode(data).decode("ascii")
                with self.catalog_lock:
                    if len(self.thumbnail_cache) >= 512:
                        self.thumbnail_cache.pop(next(iter(self.thumbnail_cache)))
                    self.thumbnail_cache[url] = value
                return ident, value
            except (OSError, ValueError):
                return ident, ""
        with ThreadPoolExecutor(max_workers=6) as pool:
            images = dict(pool.map(thumbnail, covers.items()))
        return {"covers": {ident: image for ident, image in images.items() if image},
                "pending": [ident for ident, image in images.items() if not image],
                **({"albums": albums} if albums else {}), **({"tracks": tracks} if tracks else {})}

    @staticmethod
    def public_metadata(path, params, *, timeout=8):
        if path not in {"/api/search/get", "/api/search/get/web", "/api/v6/playlist/detail", "/api/v3/song/detail"}:
            raise Refusal(400, "invalid_query")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        base = "https://music.163.com" + path
        request = urllib.request.Request(base + "?" + urllib.parse.urlencode(params), headers={"Referer": "https://music.163.com/"})
        with urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({})).open(request, timeout=timeout) as reply:
            if reply.geturl().split("?", 1)[0] != base:
                raise Refusal(503, "search_unavailable")
            data = reply.read(2097153)
        if len(data) > 2097152:
            raise Refusal(503, "search_unavailable")
        value = json.loads(data)
        if not isinstance(value, dict) or value.get("code") != 200:
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

    def _discover_cache(self, filename, field, kind):
        path = self.root / filename
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size < 32768:
                value = json.loads(path.read_text())
                if (isinstance(value, dict) and type(value.get("updated")) is int
                        and isinstance(value.get(field), kind)):
                    return value
        except (OSError, ValueError):
            pass
        return {"updated": 0, field: kind()}

    def discover(self):
        # 上游慢或离线时仍返回现有歌单和歌手。并发访客共用一个刷新任务。
        now, clock = time.time(), time.monotonic()
        with self.discovery_lock:
            if self.recommendations is None:
                self.recommendations = self._discover_cache("recommendations.json", "playlists", list)
            cached = self.recommendations
            if (not self.recommendations_refreshing and clock >= self.next_recommendations_refresh
                    and (not cached["playlists"] or now - cached["updated"] > 21600)):
                self.recommendations_refreshing = True
                threading.Thread(target=self.refresh_recommendations, daemon=True).start()
            portraits = self._discover_cache("artist-portraits.json", "photos", dict)
            if (not self.artist_refreshing and clock >= self.next_artist_refresh
                    and now - portraits["updated"] > 604800):
                self.artist_refreshing = True
                threading.Thread(target=self.refresh_artists, args=(portraits["photos"],), daemon=True).start()
            refreshing = self.recommendations_refreshing or self.artist_refreshing
        return {**cached, "artists": ARTISTS, "artist_photos": portraits["photos"], "refreshing": refreshing}

    def refresh_recommendations(self):
        try:
            value = self.public_metadata("/api/search/get/web",
                                         {"s": "KTV必点", "type": 1000, "limit": 8, "offset": 0})
            rows = []
            for row in value.get("result", {}).get("playlists", [])[:8]:
                if (isinstance(row, dict) and ID.fullmatch(str(row.get("id", "")))
                        and isinstance(row.get("name"), str)):
                    rows.append({"id": str(row["id"]), "title": row["name"][:120],
                                 "artwork": self.artwork(row.get("coverImgUrl")), "tracks": row.get("trackCount", 0)})
            if not rows:
                return
            cached = {"updated": int(time.time()), "playlists": rows}
            with self.discovery_lock:
                self.recommendations = cached
            temporary = self.root / "recommendations.part"
            temporary.write_text(json.dumps(cached, ensure_ascii=False))
            temporary.chmod(0o600)
            temporary.replace(self.root / "recommendations.json")
        except (OSError, ValueError, TypeError, AttributeError, Refusal):
            pass
        finally:
            with self.discovery_lock:
                self.next_recommendations_refresh = time.monotonic() + 15
                self.recommendations_refreshing = False

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
            if photos:
                path = self.root / "artist-portraits.part"
                path.write_text(json.dumps({"updated": int(time.time()), "photos": photos}, ensure_ascii=False))
                path.chmod(0o600)
                path.replace(self.root / "artist-portraits.json")
        except OSError:
            pass
        finally:
            with self.discovery_lock:
                self.next_artist_refresh = time.monotonic() + 90
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


def make_handler(store, agent_key, assets, *, guest_only=False):
    scripts = {"app.js": "50", "add-queue.js": "25", "party-audio.js": "27",
               "guide.js": "24", "status-feed.js": "25", "style.css": "33"}
    images = {"avatars.png", "content.png", "festival.png", "live.png"}

    @lru_cache(maxsize=24)
    def static_bytes(name, zipped, bundle=False):
        path = assets / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise Refusal(404, "not_found")
        data = path.read_bytes()
        if bundle:
            dependencies = ("guide.js", "party-audio.js", "add-queue.js", "status-feed.js")
            parts = [(assets / dependency).read_bytes() for dependency in dependencies]
            data = b"\n".join([*parts, data])
        return gzip.compress(data, compresslevel=6, mtime=0) if zipped else data

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # No request paths, room links, queries, headers, IPs or exception messages in logs.

        def reply(self, status, value, content_type="application/json; charset=utf-8", *, extra_headers=None):
            is_json = not isinstance(value, bytes)
            data = (
                json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
                if not isinstance(value, bytes)
                else value
            )
            compressed = is_json and len(data) > 1024 and self.accepts_gzip()
            if compressed:
                data = gzip.compress(data, compresslevel=3, mtime=0)
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
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
            headers.update(extra_headers or {})
            if compressed:
                headers.update({"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def accepts_gzip(self):
            encoding = self.headers.get("Accept-Encoding", "")
            return bool(re.search(r"(?:^|,)\s*gzip\s*(?:;\s*q=(?:1(?:\.0*)?|0\.\d*[1-9]\d*))?\s*(?:,|$)", encoding, re.I))

        def static(self, name, query):
            if name in images:
                compact = query == "v=4"
                if compact:
                    source = name.replace(".png", "-small.webp")
                    return self.reply(200, static_bytes(source, False), "image/webp",
                                      extra_headers={"Cache-Control": "public, max-age=31536000, immutable"})
                return self.reply(200, static_bytes(name, False), "image/png")
            content_type = ("text/html" if name == "index.html" else "text/css" if name.endswith(".css") else "text/javascript")
            zipped = self.accepts_gzip()
            headers = {"Vary": "Accept-Encoding"}
            if zipped:
                headers["Content-Encoding"] = "gzip"
            versioned = name in scripts and query == "v=" + scripts[name]
            if versioned:
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            bundle = name == "app.js" and (versioned or query in {"v=40", "v=41", "v=42", "v=43", "v=44", "v=45", "v=46", "v=47"})
            return self.reply(200, static_bytes(name, zipped, bundle=bundle),
                              content_type + "; charset=utf-8", extra_headers=headers)

        def join_guest(self, room, *, current=None):
            payload = self.body()
            if set(payload) - {"guest_key", "profile", "device_key", "snapshot"} or (
                    "guest_key" in payload and not isinstance(payload["guest_key"], str)) or (
                    "device_key" in payload and not isinstance(payload["device_key"], str)) or (
                    "snapshot" in payload and type(payload["snapshot"]) is not bool):
                raise Refusal(400, "invalid_request")
            if "profile" in payload and not isinstance(payload["profile"], dict):
                raise Refusal(400, "invalid_profile")
            identity = store.join(room, payload.get("guest_key"), profile=payload.get("profile"),
                                  existing_key=self.headers.get("X-KTV-Guest", ""), device_key=payload.get("device_key"))
            if current is not None:
                identity = {"room": current, "guest": identity}
            if payload.get("snapshot"):
                identity["status"] = store.status(room)
            return self.reply(201, identity)

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
            with os.fdopen(os.open(str(file), os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                if os.fstat(stream.fileno()).st_size != size:
                    raise Refusal(409, "media_changed")
                return self.media_stream_reply(stream, kind, size)

        def media_stream_reply(self, stream, kind, size, *, etag=None):
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
            self.send_response(status)
            headers = {"Content-Type": kind, "Content-Length": str(last - first + 1), "Accept-Ranges": "bytes",
                       "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
            if status == 206:
                headers["Content-Range"] = "bytes " + str(first) + "-" + str(last) + "/" + str(size)
            if etag:
                headers["ETag"] = etag
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

        def tv_update(self, path):
            expected = {"GET"} if path == UPDATE_INFO_PATH else {"GET", "HEAD"}
            if self.command not in expected:
                raise Refusal(405, "method_refused")
            if self.headers.get("Origin"):
                raise Refusal(403, "origin_refused")
            updates = store.tv_updates
            if not updates.download_slots.acquire(blocking=False):
                raise Refusal(503, "tv_update_busy")
            try:
                opened = updates.open_current()
                if opened is None:
                    raise Refusal(404, "tv_update_unavailable")
                release, stream = opened
                with stream:
                    if path == UPDATE_INFO_PATH:
                        return self.reply(200, release.public())
                    etag = '"' + release.sha256 + '"'
                    provided = self.headers.get("If-Match")
                    if provided not in {None, "*", etag}:
                        raise Refusal(412, "tv_update_changed")
                    return self.media_stream_reply(stream, APK_CONTENT_TYPE, release.bytes, etag=etag)
            finally:
                updates.download_slots.release()

        def dispatch(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if len(self.path) > 2048:
                raise Refusal(414, "request_too_large")
            if self.command not in {"GET", "POST", "HEAD"}:
                raise Refusal(405, "method_refused")
            origin = self.headers.get("Origin")
            room_route = ROOM_ROUTE.fullmatch(path)
            current_room = path == "/api/rooms/current" or (path == "/" and parsed.query == "room=current")
            if (origin and origin != store.origin and
                    (not (room_route or current_room) or origin not in (store.previous_guest_origin, store.public_invite_origin))):
                raise Refusal(403, "origin_refused")
            if current_room and self.command in {"GET", "POST"}:
                active = store.coordinator.active()
                if not active:
                    raise Refusal(404, "no_active_room")
                current = {"room": active["room"], "key": active["secret"]}
                if self.command == "POST":
                    room = store.room(current["room"], current["key"])
                    store.budget("room-read:" + room["id"], 4000)
                    return self.join_guest(room, current=current)
                return self.reply(200, current)
            if self.command in {"GET", "HEAD"} and (path == "/" or path[1:] in scripts or path[1:] in images):
                return self.static("index.html" if path == "/" else path[1:], parsed.query)
            if path == "/health" and self.command in {"GET", "HEAD"}:
                return self.reply(200, {"service": "friday-ktv-public", "protocol": 1})
            if guest_only and not room_route:
                raise Refusal(404, "not_found")
            if path.startswith("/v1/") and self.headers.get("X-KTV-TV-Key") is not None:
                if not store.coordinator.authenticate(self.headers.get("X-KTV-TV-Key")):
                    raise Refusal(401, "unauthorized")
                if path in {UPDATE_INFO_PATH, UPDATE_APK_PATH}:
                    return self.tv_update(path)
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
                if path == "/v1/tv/playback-start" and self.command == "POST":
                    return self.reply(200, coordinator.playback_start(self.body(), instance))
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
                    kind = media.group(3)
                    content_type = ("image/" + spec["encoding"] if kind == "background"
                                    else "video/mp4" if kind == "video" else "audio/mp4" if spec.get("encoding") == "aac_lc_m4a" else "audio/wav")
                    return self.media_reply(file, content_type, spec["bytes"])
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
                if path == "/agent/library-manifests" and self.command == "POST" and not origin:
                    return self.reply(200, store.coordinator.manifest(self.body(196608), library=True))
                if path == "/agent/library-status" and self.command == "POST" and not origin:
                    return self.reply(200, store.management.prepared_status(self.body()))
                if path == "/agent/shared-copy" and self.command == "POST" and not origin:
                    return self.reply(200, store.management.shared_copy(self.body()))
                if path == "/agent/lyrics" and self.command == "POST":
                    store.budget("lyrics-repair", 120)
                    return self.reply(200, store.coordinator.repair_lyrics(self.body(145000)))
                if path == "/agent/backgrounds":
                    if self.command == "GET":
                        return self.reply(200, store.coordinator.background_tasks())
                    if self.command == "POST":
                        return self.reply(202, store.coordinator.repair_background(self.body()))
                if path == "/agent/archive-check" and self.command == "POST":
                    store.budget("archive-check", 120)
                    return self.reply(200, store.coordinator.archive_check(self.body()))
                if path == "/agent/archive-videos" and self.command == "POST" and not origin:
                    return self.reply(200, store.coordinator.archive_videos(self.body()))
                if path == "/agent/coordinator" and self.command == "GET":
                    return self.reply(200, store.coordinator.status())
                if path == "/agent/qr" and self.command == "POST":
                    return self.reply(200, store.coordinator.qr(self.body(196608)))
                if path == "/agent/updates" and self.command == "POST":
                    return self.reply(200, store.update(self.body()))
                if path == "/agent/cloud" and self.command == "POST":
                    return self.reply(200, store.cloud_update(self.body(262144)))
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
                    room_id, operation = match.groups()
                    with store.lock:
                        if store.coordinator.direct_room(room_id):
                            raise Refusal(409, "direct_tv_owned")
                        if operation == "controls":
                            receipt = store.tv_controls(room_id, payload)
                        elif operation == "close":
                            receipt = store.close(room_id)
                        else:
                            receipt = store.playback(room_id, payload)
                    return self.reply(200, receipt)
                raise Refusal(404, "not_found")
            match = room_route
            if not match:
                raise Refusal(404, "not_found")
            self.operation = match.group(2)
            room = store.room(match.group(1), self.headers.get("X-KTV-Room", ""), self.headers.get("X-KTV-Guest", ""))
            # Reverse proxies share a loopback IP; admitted party guests must not exhaust one IP bucket.
            store.budget("room-read:" + room["id"], 4000)
            operation = match.group(2)
            if operation == "join" and self.command == "POST":
                return self.join_guest(room)
            if operation in {"profile", "theme", "controls", "command", "order", "retry"} or (
                operation == "songs" and self.command == "POST"
            ):
                guest = store.guest(room, self.headers.get("X-KTV-Guest", ""))
                if operation == "profile":
                    if self.command == "GET":
                        if parsed.query == "frequent=1":
                            return self.reply(200, store.management.with_cached_accompaniment(store.frequent(room, guest)))
                        return self.reply(200, guest)
                    if self.command == "POST":
                        store.budget("profile:" + guest["id"], 20)
                        return self.reply(200, store.profile(room, guest, self.body()))
                if operation == "theme" and self.command == "POST":
                    store.budget("theme:" + room["id"], 10)
                    return self.reply(200, store.theme(room, self.body()))
                if operation in {"controls", "command", "order", "retry"} and self.command == "POST":
                    payload = self.body()
                    # 静音和停止氛围音各自保留发送额度。
                    bucket = operation
                    if operation == "command" and payload.get("action") == "sfx_stop":
                        bucket = "sfx-stop"
                    elif operation == "controls" and set(payload) == {"music_gain"} and type(payload["music_gain"]) is int and payload["music_gain"] == 0:
                        bucket = "music-mute"
                    store.budget(bucket + ":" + room["id"], 60 if bucket in {"controls", "sfx-stop", "music-mute"} else 30)
                    return self.reply(
                        200,
                        getattr(
                            store,
                            {
                                "controls": "song_controls",
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
                page = store.playlist(params.get("id", [""])[0], int(offset))
                return self.reply(200, store.management.with_cached_accompaniment(page))
            if operation == "artwork" and self.command == "GET":
                store.guest(room, self.headers.get("X-KTV-Guest", ""))
                params = urllib.parse.parse_qs(parsed.query, max_num_fields=4)
                if (not {"ids"} <= set(params) or set(params) - {"ids", "inline", "source", "refresh"}
                        or any(len(values) != 1 for values in params.values())
                        or params.get("inline", ["0"])[0] not in {"0", "1"}
                        or params.get("refresh", ["0"])[0] not in {"0", "1"}):
                    raise Refusal(400, "invalid_query")
                return self.reply(200, store.search_artwork(params["ids"][0], inline=params.get("inline") == ["1"],
                                  source=params.get("source", ["song"])[0], refresh=params.get("refresh") == ["1"]))
            if operation == "search" and self.command == "GET":
                params = urllib.parse.parse_qs(parsed.query, max_num_fields=4)
                source = params.get("source", ["song"])[0]
                if source == "cloud":
                    store.guest(room, self.headers.get("X-KTV-Guest", ""))
                raw_offset = params.get("offset", ["0"])[0]
                if not re.fullmatch(r"[0-9]{1,6}", raw_offset):
                    raise Refusal(400, "invalid_query")
                page = store.search(params.get("q", [""])[0], source, int(raw_offset))
                return self.reply(200, store.management.with_cached_accompaniment(page))
            if operation == "songs" and self.command == "POST":
                payload = self.body()
                if "withdraw_id" in payload or "withdraw_request_id" in payload:
                    return self.reply(200, store.withdraw_song(room, payload, guest))
                return self.reply(202, store.add(room, payload, guest))
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


class GuestSocketServer(Server):
    address_family = socket.AF_UNIX

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", 0


def guest_socket_listener(config_path, config, store, assets):
    name = config.get("guest_socket")
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("invalid_guest_socket")
    root = config_path.parent.resolve()
    path = root / name
    parent = path.parent
    if (root not in path.parents or parent.resolve() != parent or parent == root or
            path.suffix != ".sock" or not parent.is_dir()):
        raise ValueError("invalid_guest_socket")
    directory = parent.stat()
    if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o710:
        raise ValueError("private_guest_socket_directory_required")
    try:
        previous = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != directory.st_uid:
            raise ValueError("unexpected_guest_socket_file")
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(.2)
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise ValueError("guest_socket_in_use")
    server = GuestSocketServer(str(path), make_handler(store, "", assets, guest_only=True))
    try:
        os.chown(path, -1, directory.st_gid)
        path.chmod(0o660)
    except OSError:
        server.server_close()
        path.unlink(missing_ok=True)
        raise
    return server


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
    if not guest_origin(origin) or not re.fullmatch(
        r"[A-Za-z0-9_-]{43,100}", config["agent_key"]
    ):
        raise ValueError("invalid_config")
    store = Store(args.config.parent / "state", origin, config.get("base_path", "/ktv"), worker_origin=config.get("worker_origin"),
                  recovery_enabled=config.get("recover_worker", True) is True, background_health=True,
                  previous_guest_origin=config.get("previous_guest_origin"), public_invite_origin=config.get("public_invite_origin"))
    assets = Path(__file__).parent / "web"
    servers = create_listeners(config, store, config["agent_key"], assets)
    try:
        gateway = guest_socket_listener(args.config, config, store, assets)
        if gateway is not None:
            servers.insert(0, gateway)
    except (OSError, ValueError) as error:
        store.record_failure("guest_socket", error)
    workers = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .5}, daemon=True) for server in servers[:-1]]
    stop_inbox = threading.Event()
    inbox = TvUpdateInbox(store.tv_updates, config["agent_key"])
    inbox_worker = threading.Thread(target=inbox.run, args=(stop_inbox,), name="ktv-tv-update-inbox", daemon=True)
    room_worker = threading.Thread(target=store.coordinator.run_room_lifecycle, args=(stop_inbox,), name="ktv-room-lifecycle", daemon=True)
    inbox_worker.start()
    room_worker.start()
    try:
        for worker in workers:
            worker.start()
        servers[-1].serve_forever(poll_interval=.5)
    finally:
        stop_inbox.set()
        for server in servers[:-1]:
            server.shutdown()
        for server in servers:
            server.server_close()
        for worker in workers:
            worker.join(2)
        inbox_worker.join(2)
        room_worker.join(2)
        store.coordinator.close()
        store.management.close()


if __name__ == "__main__":
    main()
