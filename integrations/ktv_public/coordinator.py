"""NAS-owned KTV coordination, immutable media manifests and bounded local cache.

This module never opens devices, launches processes, wakes a TV, or handles source
account credentials. Only an authenticated TV opening a session requests readiness.
"""
import base64
import binascii
import copy
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

try:
    from .audio_format import AAC_FIELDS, compact_spec, verify_compact
except ImportError:
    from audio_format import AAC_FIELDS, compact_spec, verify_compact

UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
TICKET = re.compile(r"[A-Za-z0-9_.-]{32,128}\Z")
VIDEO_ENCODINGS = frozenset({"h264_mp4_video_only", "h264_mp4_silent_clock"})
LYRIC_LOOKUP_REVISION = 3
CAPABILITIES = frozenset({"source", "model", "download", "lyrics", "media"})
PERSISTENT_EXPIRY = 2147483647
MEDIA_LIMIT = 345600044
RECOVERY_WINDOW = 30
RECOVERY_RETRY_INTERVAL = 60


def fixed_media_path(value, kind):
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    return (len(parts) == 6 and parts[:3] == ["", "v1", "jobs"] and bool(UUID.fullmatch(parts[3]))
            and parts[4:] == ["media", kind])


def private_origin(value, allow_loopback=False):
    if not isinstance(value, str):
        raise ValueError("invalid_worker_origin")
    p = urllib.parse.urlsplit(value)
    if (p.scheme != "http" or p.username or p.password or p.query or p.fragment
            or p.path not in {"", "/"} or not p.hostname or not p.hostname.replace(".", "").isdigit()
            or p.port is None or not 1024 <= p.port <= 65535):
        raise ValueError("invalid_worker_origin")
    ip = ipaddress.ip_address(p.hostname)
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if ip.version != 4 or not (any(ip in ipaddress.ip_network(n) for n in networks)
                                or (allow_loopback and ip.is_loopback)):
        raise ValueError("invalid_worker_origin")
    return "http://" + str(ip) + ":" + str(p.port)


def atomic_private(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".coordinator-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Coordinator:
    def __init__(self, store, refusal, *, worker_origin=None, allow_loopback=False, recovery_enabled=False, background_health=False):
        self.store, self.error = store, refusal
        self.lock = threading.RLock()
        self.condition = threading.Condition()
        self.cursor = int(time.time() * 1000)
        self.waiters = threading.BoundedSemaphore(8)
        self.worker = None
        self.allow_loopback = allow_loopback
        self.worker_origin = None
        pin = store.root / "worker-origin.json"
        if worker_origin is not None:
            self.worker_origin = private_origin(worker_origin, allow_loopback)
            atomic_private(pin, {"origin": self.worker_origin})
        elif pin.is_file() and not pin.is_symlink():
            self.worker_origin = private_origin(json.loads(pin.read_text())["origin"], allow_loopback)
        self.media_root = store.root / "coordinator-media"
        self.media_root.mkdir(mode=0o700, exist_ok=True)
        if self.media_root.resolve() != self.media_root:
            raise ValueError("unsafe_media_cache")
        self.tv_key_path = store.root / "tv-key"
        if not self.tv_key_path.exists():
            fd = os.open(self.tv_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32))
        if self.tv_key_path.is_symlink() or self.tv_key_path.stat().st_mode & 0o077:
            raise ValueError("private_tv_key_required")
        self.tv_key = self.tv_key_path.read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,100}", self.tv_key):
            raise ValueError("invalid_tv_key")
        household_path = store.root / "household-id"
        household_existed = household_path.exists()
        if not household_path.exists():
            fd = os.open(household_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(str(uuid.uuid4()))
        if household_path.is_symlink() or not UUID.fullmatch(household_path.read_text().strip()):
            raise ValueError("invalid_household_identity")
        self.household_id = household_path.read_text().strip()
        self.qr_metadata = {}
        self.background_policy = store.root / "background-policy.json"
        self.backgrounds_enabled = True
        if self.background_policy.is_file() and not self.background_policy.is_symlink():
            try:
                value = json.loads(self.background_policy.read_text()).get("enabled")
                if type(value) is bool:
                    self.backgrounds_enabled = value
            except (OSError, ValueError, AttributeError):
                pass
        self.cache_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ktv-nas-cache")
        self.video_cache_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ktv-nas-video")
        self.cache_pending = set()
        self.verify_pending = set()
        self.closed = False
        self.recovery_enabled = recovery_enabled
        self.recovery_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ktv-worker-recovery")
        self.recovery_cancel = threading.Event()
        self.recovery_identity = None
        self.recovery_attempts = 0
        self.recovery_result = "not_requested"
        self.recovery_started_at = None
        self.probe = False
        self.data_probe = False
        self.health_at = 0
        self.health_pending = False
        self.background_health = background_health
        self.health_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ktv-nas-health")
        self.diagnostics = None
        with store.connection() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS coordinator_session(id INTEGER PRIMARY KEY CHECK(id=1),
                room TEXT NOT NULL, secret TEXT NOT NULL, instance TEXT NOT NULL, opened INTEGER NOT NULL,
                tv_at INTEGER NOT NULL DEFAULT 0, readiness TEXT NOT NULL DEFAULT '{}');
              CREATE TABLE IF NOT EXISTS coordinator_manifests(song TEXT PRIMARY KEY, version TEXT NOT NULL,
                instance TEXT NOT NULL, timeline TEXT NOT NULL, job TEXT NOT NULL, lyrics TEXT NOT NULL,
                origin TEXT NOT NULL, ticket TEXT NOT NULL, expires INTEGER NOT NULL, updated INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS coordinator_cache(sha TEXT PRIMARY KEY, bytes INTEGER NOT NULL,
                state TEXT NOT NULL, updated INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS coordinator_manifest_versions(song TEXT NOT NULL, version TEXT NOT NULL,
                PRIMARY KEY(song,version));
              CREATE TABLE IF NOT EXISTS coordinator_lyric_status(song TEXT PRIMARY KEY,version TEXT NOT NULL,ready INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS coordinator_start_requests(request_id TEXT PRIMARY KEY,
                instance TEXT NOT NULL, room TEXT NOT NULL, created INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS coordinator_playback_starts(room TEXT NOT NULL, instance TEXT NOT NULL,
                request_id TEXT NOT NULL, song TEXT NOT NULL, expires INTEGER NOT NULL, released INTEGER NOT NULL,
                PRIMARY KEY(room,instance,request_id));
              CREATE TABLE IF NOT EXISTS coordinator_household(id INTEGER PRIMARY KEY CHECK(id=1),
                initialized INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS room_tokens(room TEXT NOT NULL, token_hash TEXT NOT NULL,
                expires INTEGER NOT NULL, PRIMARY KEY(room,token_hash));
            """)
            for row in db.execute("SELECT m.song,m.version,m.lyrics,m.timeline FROM coordinator_manifests m "
                                  "LEFT JOIN coordinator_lyric_status ls ON ls.song=m.song AND ls.version=m.version "
                                  "WHERE ls.song IS NULL").fetchall():
                self.index_lyrics(db, row["song"], row["version"], json.loads(row["lyrics"]), json.loads(row["timeline"]))
            # This household-scoped marker outlives all scene retention. If an
            # older coordinator already left a household identity but no marker,
            # conservatively avoid adopting another legacy scene on upgrade.
            db.execute("INSERT OR IGNORE INTO coordinator_household VALUES(1,?)", (int(household_existed),))
            db.execute("UPDATE coordinator_household SET initialized=1 WHERE EXISTS(SELECT 1 FROM coordinator_session)")
            # A process restart is not proof that the old TV or worker is alive.
            db.execute("UPDATE coordinator_session SET tv_at=0,readiness='{}'")
            db.execute("UPDATE rooms SET playback_at=0 WHERE id=(SELECT room FROM coordinator_session WHERE id=1)")
            if "request_id" not in {row[1] for row in db.execute("PRAGMA table_info(coordinator_session)")}:
                db.execute("ALTER TABLE coordinator_session ADD COLUMN request_id TEXT NOT NULL DEFAULT ''")
            if "last_activity" not in {row[1] for row in db.execute("PRAGMA table_info(coordinator_session)")}:
                db.execute("ALTER TABLE coordinator_session ADD COLUMN last_activity INTEGER NOT NULL DEFAULT 0")
            cache_columns = {row[1] for row in db.execute("PRAGMA table_info(coordinator_cache)")}
            for field in ("device", "inode", "mtime_ns", "ctime_ns"):
                if field not in cache_columns:
                    db.execute("ALTER TABLE coordinator_cache ADD COLUMN " + field + " INTEGER NOT NULL DEFAULT 0")

    def changed(self):
        with self.condition:
            self.cursor += 1
            self.condition.notify_all()

    def authenticate(self, value):
        return isinstance(value, str) and len(value) <= 128 and hmac.compare_digest(value, self.tv_key)

    def active(self, db=None):
        with (self.store.connection() if db is None else nullcontext(db)) as connection:
            row = connection.execute(
                "SELECT s.*,r.closed,r.playback_state,r.playback_at FROM coordinator_session s "
                "JOIN rooms r ON r.id=s.room WHERE s.id=1"
            ).fetchone()
        return dict(row) if row and not row["closed"] else None

    def renew_inactive_room(self):
        now = int(time.time())
        with self.store.lock, self.store.connection() as db:
            active = self.active(db)
            if not active or time.localtime(active["opened"])[:3] >= time.localtime(now)[:3]:
                return False
            last_add = db.execute("SELECT max(created) FROM songs WHERE room=?", (active["room"],)).fetchone()[0] or 0
            last_use = max(active["opened"], active["last_activity"], last_add)
            if now - last_use < 3 * 86400:
                return False
            # 连续多天没有演唱和点歌才换场，午夜和普通重启会接续原歌单。
            if active["playback_state"] == "playing" and active["playback_at"] >= now - 12:
                return False
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE rooms SET closed=1,expires=? WHERE id=?", (now, active["room"]))
            room, secret = self.store.insert_room(db)
            db.execute("UPDATE coordinator_session SET room=?,secret=?,opened=?,last_activity=0,tv_at=0,readiness='{}',request_id='' WHERE id=1",
                       (room, secret, now))
            self.store._expire(db)
        return True

    def run_room_lifecycle(self, stop):
        while not stop.is_set():
            try:
                self.renew_inactive_room()
            except (OSError, sqlite3.Error, ValueError) as error:
                self.store.record_failure("room_lifecycle", error)
            stop.wait(5)

    def direct_room(self, room_id):
        active = self.active()
        return bool(active and active["room"] == room_id)

    def direct_live(self, room_id):
        active = self.active()
        return bool(active and active["room"] == room_id and 0 <= time.time() - active["tv_at"] <= 12)

    def worker_healthy(self):
        with self.lock:
            return bool(self.worker and 0 <= time.time() - self.worker["at"] <= 20
                        and all(self.worker["capabilities"].values()))

    def worker_media_ready(self):
        # A source-account/model fault degrades preparation readiness without
        # withholding an already verified finished song from a live file API.
        with self.lock:
            return bool(self.worker and 0 <= time.time() - self.worker["at"] <= 20
                        and self.worker["capabilities"]["media"])

    def register_worker(self, payload):
        fields = {"instance", "origin", "capabilities"}
        if (not fields <= set(payload) or set(payload) - fields - {"backgrounds_enabled"}
                or not isinstance(payload["instance"], str) or not UUID.fullmatch(payload["instance"])
                or not isinstance(payload["capabilities"], dict) or set(payload["capabilities"]) != CAPABILITIES
                or any(type(v) is not bool for v in payload["capabilities"].values())
                or "backgrounds_enabled" in payload and type(payload["backgrounds_enabled"]) is not bool):
            raise self.error(400, "invalid_worker")
        try:
            origin = private_origin(payload["origin"], self.allow_loopback)
        except (ValueError, TypeError):
            raise self.error(400, "invalid_worker_origin") from None
        with self.lock:
            if self.worker_origin and origin != self.worker_origin:
                raise self.error(409, "worker_origin_changed")
            if not self.worker_origin:
                self.worker_origin = origin
                atomic_private(self.store.root / "worker-origin.json", {"origin": origin})
            enabled = payload.get("backgrounds_enabled", self.backgrounds_enabled)
            if enabled != self.backgrounds_enabled:
                atomic_private(self.background_policy, {"enabled": enabled})
                self.backgrounds_enabled = enabled
            self.worker = {"instance": payload["instance"], "origin": origin,
                           "capabilities": dict(payload["capabilities"]), "at": time.time()}
            if all(self.worker["capabilities"].values()):
                self.recovery_cancel.set()
                self.recovery_identity = None
                self.recovery_started_at = None
                self.recovery_result = "worker_ready"
        self.changed()
        return {"accepted": True, "instance": payload["instance"], "expires_in": 20,
                "preparation_requested": bool(self.active()), "recovery": self.recovery(), "party": self.party()}

    def recovery(self):
        return self._recovery(self.active())

    def _recovery(self, active):
        # This is a request for the already configured local supervisor. There
        # are no shell commands, arbitrary launch targets or wake actions here.
        if not active:
            return {"requested": False, "attempt": 0, "reason": "tv_not_opened"}
        with self.lock:
            elapsed = (time.monotonic() - self.recovery_started_at if self.recovery_started_at is not None
                       else max(0, int(time.time()) - active["opened"]))
            return {"requested": not self.worker_healthy() and elapsed < RECOVERY_WINDOW,
                    "attempt": self.recovery_attempts, "reason": "readiness" if elapsed < RECOVERY_WINDOW else "timed_out",
                    "configured": bool(self.recovery_enabled and self.worker_origin), "cannot_wake": True,
                    "last_result": self.recovery_result}

    def _schedule_recovery(self, active):
        if not active or not self.recovery_enabled or not self.worker_origin:
            return
        identity = (active["instance"], active["request_id"])
        with self.lock:
            if self.closed or self.worker_healthy():
                return
            now = time.monotonic()
            same_request = self.recovery_identity == identity
            if same_request:
                worker_present = self.worker and 0 <= time.time() - self.worker["at"] <= 20
                if now - self.recovery_started_at < RECOVERY_RETRY_INTERVAL or worker_present:
                    return
            # 同一台仍在上报的电视可等到 Mac 重新可达，每分钟最多补一次探测。
            delays = (0,) if same_request else (0, 2, 6, 14)
            self.recovery_identity = identity
            self.recovery_cancel.set()
            self.recovery_cancel = threading.Event()
            self.recovery_attempts = 0
            self.recovery_result = "scheduled"
            self.recovery_started_at = now
            self.recovery_pool.submit(self._recover_worker, identity, self.recovery_cancel, delays)

    def _recover_worker(self, identity, cancel, delays):
        started = time.monotonic()
        for delay in delays:
            if cancel.wait(max(0, started + delay - time.monotonic())) or self.closed:
                return
            active = self.active()
            if not active or (active["instance"], active["request_id"]) != identity:
                return
            # Contact only the operator-pinned existing listener. Its response
            # can activate an already configured launchd socket, but neither
            # 200 nor 401 is readiness, and there is no device wake primitive.
            with self.lock:
                if (cancel is not self.recovery_cancel or cancel.is_set()
                        or time.monotonic() - self.recovery_started_at >= RECOVERY_WINDOW or self.worker_healthy()):
                    return
                self.recovery_attempts += 1
            try:
                request = urllib.request.Request(self.worker_origin + "/v1/status", method="GET")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                with opener.open(request, timeout=1) as response:
                    result = "listener_response" if response.status in {200, 401} else "listener_unready"
            except urllib.error.HTTPError as error:
                result = "listener_response" if error.code == 401 else "listener_unready"
                error.close()
            except (OSError, ValueError):
                result = "unreachable"
            with self.lock:
                if cancel is not self.recovery_cancel or cancel.is_set():
                    return
                self.recovery_result = result
            self.changed()

    def _nas_media_probe(self):
        # Only refresh_health publishes the completed file/DB snapshot. A
        # pending fsync must not invalidate its still-fresh predecessor.
        measured = False
        name = None
        try:
            fd, name = tempfile.mkstemp(prefix=".readiness-", dir=self.media_root)
            data = secrets.token_bytes(32)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            measured = Path(name).read_bytes() == data
        except OSError:
            pass
        finally:
            if name:
                try:
                    Path(name).unlink(missing_ok=True)
                except OSError:
                    measured = False
        return measured

    def refresh_health(self):
        """Local file/DB evidence on a separate lane; no devices or upstreams."""
        data = False
        try:
            with sqlite3.connect(str(self.store.db), timeout=.25) as db:
                # Exercise the actual database's writer/rollback and readback
                # path. Full integrity scans are a migration/review operation,
                # never a phone status-request dependency.
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE coordinator_session SET opened=opened WHERE id=1")
                schema = db.execute("PRAGMA schema_version").fetchone()[0]
                count = db.execute("SELECT count(*) FROM rooms").fetchone()[0]
                data = type(schema) is int and schema > 0 and type(count) is int
                db.rollback()
        except (OSError, sqlite3.Error):
            pass
        media = self._nas_media_probe()
        with self.lock:
            self.data_probe, self.probe = data, media
            self.health_at = time.monotonic()
        self.changed()

    def _schedule_health(self):
        with self.lock:
            if self.closed or not self.background_health or self.health_pending or 0 <= time.monotonic() - self.health_at < 3:
                return
            self.health_pending = True
        def refresh():
            try:
                self.refresh_health()
            finally:
                with self.lock:
                    self.health_pending = False
        self.health_pool.submit(refresh)

    def readiness(self):
        return self._readiness(self.active())

    def _readiness(self, active):
        self._schedule_health()
        with self.lock:
            worker = copy.deepcopy(self.worker)
        live_worker = bool(worker and 0 <= time.time() - worker["at"] <= 20)
        tv = json.loads(active["readiness"]) if active else {}
        live_tv = bool(active and 0 <= time.time() - active["tv_at"] <= 12)
        with self.lock:
            health_fresh = self.health_at > 0 and 0 <= time.monotonic() - self.health_at <= 15
            data, media = self.data_probe, self.probe
        gates = {"nas_api": True, "nas_data": health_fresh and data, "nas_media": health_fresh and media,
                 "link_worker": live_worker, "link_tv": live_tv, "qr": bool(active and self.qr_ready(active))}
        gates.update({"worker_" + k: bool(live_worker and worker["capabilities"][k]) for k in sorted(CAPABILITIES)})
        gates.update({"tv_" + k: bool(live_tv and tv.get(k)) for k in ("playback", "audio", "media")})
        ready = bool(active and all(value for name, value in gates.items() if name != "tv_audio"))
        return {"state": "ready" if ready else "degraded" if active else "closed", "ready": ready,
                "gates": gates, "recovery": self._recovery(active)}

    def start(self, payload):
        if (set(payload) != {"instance", "request_id"} or any(not isinstance(v, str) or not UUID.fullmatch(v)
                                                             for v in payload.values())):
            raise self.error(400, "invalid_start")
        with self.store.lock:
            self.renew_inactive_room()
            active = self.active()
            with self.store.connection() as db:
                previous = db.execute("SELECT * FROM coordinator_start_requests WHERE request_id=?", (payload["request_id"],)).fetchone()
            if previous:
                if previous["instance"] != payload["instance"]:
                    raise self.error(409, "idempotency_conflict")
                if not active or active["room"] != previous["room"]:
                    raise self.error(410, "room_closed")
                if active["instance"] != payload["instance"]:
                    raise self.error(409, "tv_instance_changed")
                return self.party()
            if active:
                with self.store.connection() as db:
                    # Reopening a TV preserves room, guests, queue and mute.
                    db.execute("UPDATE coordinator_session SET instance=?,tv_at=0,readiness='{}' WHERE id=1 AND instance!=?",
                               (payload["instance"], payload["instance"]))
                    if payload["instance"] != active["instance"]:
                        db.execute("UPDATE rooms SET playback_at=0,mic_muted=1,mic_request_at=0,control_revision=control_revision+1,mic_revision=mic_revision+1,last_command=max(last_command,coalesce((SELECT max(seq) FROM commands WHERE room=?),0)) WHERE id=?",
                                   (active["room"], active["room"]))
                    if payload["request_id"] != active["request_id"]:
                        db.execute("UPDATE coordinator_session SET request_id=? WHERE id=1", (payload["request_id"],))
            else:
                # Only the first coordinator opening can adopt a legacy scene.
                # Later explicit ends always lead to an empty new scene; other
                # unselected legacy rooms remain intact as retained history.
                with self.store.connection() as db:
                    initialized = db.execute("SELECT initialized FROM coordinator_household WHERE id=1").fetchone()[0]
                    row = db.execute("SELECT * FROM rooms WHERE closed=0 ORDER BY rowid DESC LIMIT 1").fetchone() if not initialized else None
                if row:
                    room, secret = row["id"], secrets.token_urlsafe(32)
                    with self.store.connection() as db:
                        db.execute("INSERT OR IGNORE INTO room_tokens VALUES(?,?,?)", (room, row["token_hash"], PERSISTENT_EXPIRY))
                        db.execute("UPDATE rooms SET token_hash=?,expires=? WHERE id=?", (hashlib.sha256(secret.encode()).hexdigest(), PERSISTENT_EXPIRY, room))
                else:
                    created = self.store.create()
                    room = created["room_id"]
                    secret = urllib.parse.parse_qs(urllib.parse.urlsplit(created["join_url"]).fragment)["k"][0]
                with self.store.connection() as db:
                    # Commands older than this TV-controlled adoption are not
                    # replayed. Desired microphone mute is preserved and every
                    # new TV session still starts locally muted.
                    db.execute("INSERT OR REPLACE INTO coordinator_session(id,room,secret,instance,opened,tv_at,readiness,request_id) VALUES(1,?,?,?,?,0,'{}',?)",
                               (room, secret, payload["instance"], int(time.time()), payload["request_id"]))
                    db.execute("UPDATE coordinator_household SET initialized=1 WHERE id=1")
                    db.execute("UPDATE rooms SET last_command=max(last_command,coalesce((SELECT max(seq) FROM commands WHERE room=?),0)) WHERE id=?", (room, room))
                    db.execute("UPDATE rooms SET playback_at=0,mic_muted=1,mic_request_at=0,control_revision=control_revision+1,mic_revision=mic_revision+1 WHERE id=?", (room,))
            active = self.active()
            with self.store.connection() as db:
                db.execute("UPDATE coordinator_household SET initialized=1 WHERE id=1")
                db.execute("INSERT INTO coordinator_start_requests VALUES(?,?,?,?)", (payload["request_id"], payload["instance"], active["room"], int(time.time())))
        self.changed()
        self._schedule_recovery(self.active())
        return self.party()

    def _instance(self, instance):
        active = self.active()
        if not active or instance != active["instance"]:
            raise self.error(409, "tv_instance_changed")
        return active

    def tv_readiness(self, payload):
        if (set(payload) - {"audio_formats", "queue_claims"} != {"instance", "playback", "audio", "media", "protocol"}
                or type(payload["protocol"]) is not int or payload["protocol"] != 1
                or type(payload.get("queue_claims", False)) is not bool
                or any(type(payload[k]) is not bool for k in ("playback", "audio", "media"))):
            raise self.error(400, "invalid_tv_readiness")
        formats = payload.get("audio_formats", ["pcm_s16le_wav"])
        if (not isinstance(formats, list) or not 1 <= len(formats) <= 2
                or any(not isinstance(name, str) or name not in {"pcm_s16le_wav", "aac_lc_m4a"} for name in formats)):
            raise self.error(400, "invalid_tv_readiness")
        with self.store.lock, self.store.connection() as db:
            active = self._instance(payload["instance"])
            readiness = {**{k: payload[k] for k in ("playback", "audio", "media")},
                         "audio_formats": formats, "queue_claims": payload.get("queue_claims", False)}
            db.execute("UPDATE coordinator_session SET tv_at=?,readiness=? WHERE id=1 AND instance=? AND room=?",
                       (int(time.time()), json.dumps(readiness),
                        active["instance"], active["room"]))
        self._schedule_recovery(self.active())
        self.changed()
        return self.readiness()

    def playback_encoding(self, active=None):
        active = active or self.active()
        return ("aac_lc_m4a" if active and active["tv_at"] >= int(time.time()) - 12
                and "aac_lc_m4a" in json.loads(active["readiness"]).get("audio_formats", [])
                else "pcm_s16le_wav")

    def queue_claims_ready(self, db, room_id):
        active = self.active(db)
        return bool(active and active["room"] == room_id and json.loads(active["readiness"]).get("queue_claims") is True)

    @staticmethod
    def playback_claimed(db, room_id, song, instance=None):
        now = int(time.time() * 1000)
        # 当前电视可能已经开唱，只是播放回报受阻。开播时限到期仍保留其占用。
        query = ("SELECT 1 FROM coordinator_playback_starts AS p WHERE p.room=? AND p.song=? AND p.released=0 "
                 "AND (p.expires>? OR EXISTS (SELECT 1 FROM coordinator_session AS s WHERE s.id=1 AND s.room=p.room "
                 "AND s.instance=p.instance))")
        return bool(db.execute(query + (" AND p.instance=?" if instance else ""),
                               (room_id, song, now, instance) if instance else (room_id, song, now)).fetchone())

    def playback_start(self, payload, instance):
        fields = {"room_id", "song", "request_id"}
        if (set(payload) - {"release"} != fields or type(payload.get("release", False)) is not bool
                or any(not isinstance(payload[k], str) or not UUID.fullmatch(payload[k]) for k in fields)):
            raise self.error(400, "invalid_playback_start")
        room_id, song, request_id = (payload[k] for k in ("room_id", "song", "request_id"))
        with self.store.lock, self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = int(time.time() * 1000)
            active = self.active(db)
            if not active or active["instance"] != instance:
                raise self.error(409, "tv_instance_changed")
            if active["room"] != room_id:
                raise self.error(409, "room_changed")
            db.execute("DELETE FROM coordinator_playback_starts WHERE expires<=? AND (released=1 OR room!=? OR instance!=?)",
                       (now, room_id, instance))
            previous = db.execute(
                "SELECT song,expires,released FROM coordinator_playback_starts WHERE room=? AND instance=? AND request_id=?",
                (room_id, instance, request_id),
            ).fetchone()
            if previous and previous["song"] != song:
                raise self.error(409, "idempotency_conflict")
            if payload.get("release", False):
                # 撤销先到时也落标记，防止迟到的同一请求重新占用曲目。
                db.execute("INSERT OR REPLACE INTO coordinator_playback_starts VALUES(?,?,?,?,?,1)",
                           (room_id, instance, request_id, song, now + 30000))
                return {"accepted": True}
            if previous and previous["released"]:
                raise self.error(409, "playback_start_released")
            if previous and previous["expires"] <= now:
                raise self.error(409, "playback_start_expired")
            if not db.execute("SELECT 1 FROM songs WHERE id=? AND room=? AND removed=0 AND state='ready'",
                              (song, room_id)).fetchone():
                raise self.error(409, "song_unavailable")
            expires = previous["expires"] if previous else now + 30000
            if not previous:
                db.execute("INSERT INTO coordinator_playback_starts VALUES(?,?,?,?,?,0)",
                           (room_id, instance, request_id, song, expires))
            return {"accepted": True, "lease_ms": expires - now}

    def party(self):
        def snapshot():
            # 电视高频取控制状态时，在同一读事务取房间、歌单和命令。
            with self.store.connection() as db:
                db.execute("BEGIN")
                active = self.active(db)
                if not active:
                    return None
                room = dict(db.execute("SELECT * FROM rooms WHERE id=?", (active["room"],)).fetchone())
                ordered = db.execute("SELECT s.id,s.state,ls.ready=1 AS synced_lyrics FROM songs s "
                                     "LEFT JOIN coordinator_lyric_status ls ON ls.song=s.id "
                                     "WHERE s.room=? AND s.removed=0 ORDER BY s.position,s.rowid LIMIT 80", (active["room"],)).fetchall()
                now = int(time.time())
                commands = [dict(r) for r in db.execute("SELECT seq,action,song,created FROM commands WHERE room=? AND seq>? AND created>? AND created<=? ORDER BY seq LIMIT 20",
                                                       (active["room"], room["last_command"], now - 20, now))]
                return active, room, ordered, commands, now, self.playable_ids(db, ordered, room)

        value = snapshot()
        if value and not value[1]["mic_muted"] and 0 < value[1]["mic_request_at"] <= value[4] - 5:
            self.store.expire_unmute(value[0]["room"])
            value = snapshot()
        if value is None:
            return {"active": False, "readiness": self._readiness(None)}
        active, room, ordered, commands, now, playable = value
        ids = [row["id"] for row in ordered]
        commands = [dict(r, **({"expires": r["created"] + 5} if r["action"].startswith("sfx_") else {})) for r in commands
                    if not r["action"].startswith("sfx_") or r["created"] + 5 > now]
        return {"active": True, "persistent": True, "household_id": self.household_id, "room_id": active["room"], "expires": room["expires"],
                "join_url": self.store.join_url(active["room"], active["secret"]),
                "backing_receipts": True, "queue_claims": True,
                "theme": room["theme"], "theme_revision": room["theme_revision"],
                "controls": {k: room[k] for k in ("music_gain", "mic_gain", "mic_muted", "original", "control_revision", "mic_revision", "vocal_preset", "sfx_gain")},
                "commands": commands, "order": ids, "upcoming": self.store.upcoming_ids(ordered, room),
                "playable": playable,
                "queue_revision": room["queue_revision"], "readiness": self._readiness(active)}

    def playable_ids(self, db, rows, room):
        ordered = self.store.upcoming_ids(rows, room, playable_only=True)
        available = self.available_track_ids(db, ordered, room["original"])
        return [song for song in ordered if song in available]

    def available_track_ids(self, db, song_ids, original):
        if not song_ids:
            return set()
        backing = "original" if original else "instrumental"
        now, worker_media = time.time(), self.worker_media_ready()
        placeholders = ",".join("?" for _ in song_ids)
        manifests = db.execute("SELECT song,job,origin,expires FROM coordinator_manifests WHERE song IN (" + placeholders + ")", song_ids)
        available = set()
        for row in manifests:
            if self._worker_delivery(row, now, worker_media):
                available.add(row["song"])
                continue
            media = json.loads(row["job"]).get("media", {}).get(backing)
            if media and self.store.management._verified_asset(media, db):
                available.add(row["song"])
        return available

    def _worker_delivery(self, row, now, media_ready):
        return media_ready and row["origin"] == self.worker_origin and row["expires"] > now + 5

    def playback(self, payload, instance):
        value = dict(payload)
        # 检查当前电视与写入回报共用开房锁，防止接管后写入上一连接的状态。
        with self.store.lock:
            active = self._instance(instance)
            if value.pop("room_id", None) != active["room"]:
                raise self.error(409, "room_changed")
            song = value.get("current")
            if (isinstance(song, str) and UUID.fullmatch(song)
                    and json.loads(active["readiness"]).get("queue_claims") is True):
                with self.store.connection() as db:
                    current = db.execute("SELECT current_song FROM rooms WHERE id=?", (active["room"],)).fetchone()[0]
                    if song != current and not self.playback_claimed(db, active["room"], song, instance):
                        raise self.error(409, "playback_start_unclaimed")
            result = self.store.playback(active["room"], value)
            now = int(time.time())
            singing = value.get("state") == "playing" and bool(value.get("current"))
            if active["tv_at"] < now or singing and active["last_activity"] < now - 30:
                with self.store.connection() as db:
                    db.execute("UPDATE coordinator_session SET tv_at=? WHERE id=1 AND instance=? AND room=? AND tv_at<?",
                               (now, instance, active["room"], now))
                    if singing:
                        db.execute("UPDATE coordinator_session SET last_activity=? WHERE id=1 AND instance=? AND room=? AND last_activity<?",
                                   (now, instance, active["room"], now - 30))
        return result

    def tv_controls(self, payload, instance):
        value = dict(payload)
        with self.store.lock:
            active = self._instance(instance)
            if value.pop("room_id", None) != active["room"]:
                raise self.error(409, "room_changed")
            return self.store.tv_controls(active["room"], value)

    def close_party(self, instance, payload):
        with self.store.lock:
            active = self._instance(instance)
            if set(payload) != {"room_id"} or payload["room_id"] != active["room"]:
                raise self.error(409, "room_changed")
            return self.store.close(active["room"])

    def qr_ready(self, active=None):
        active = active or self.active()
        if not active:
            return False
        path = self.store.root / ("qr-" + active["room"] + ".png")
        metadata = self.qr_metadata.get(active["room"])
        if metadata is None:
            saved = path.with_suffix(".json")
            try:
                if saved.is_symlink() or saved.stat().st_size > 2048:
                    return False
                metadata = json.loads(saved.read_text())
                self.qr_metadata[active["room"]] = metadata
            except (OSError, ValueError):
                return False
        identity = self._identity(path)
        return bool(identity and isinstance(metadata, dict) and metadata.get("identity") == list(identity)
                    and isinstance(metadata.get("sha256"), str) and SHA.fullmatch(metadata["sha256"])
                    and metadata.get("join_sha256") == self._qr_join_digest(active)
                    and 32 <= identity[2] <= 131072)

    def _qr_join_digest(self, active):
        return hashlib.sha256(self.store.join_url(active["room"], active["secret"]).encode()).hexdigest()

    def qr(self, payload):
        fields = {"room_id", "instance", "join_url", "sha256", "png"}
        active = self.active()
        if (set(payload) != fields or not active or payload["instance"] != active["instance"]
                or payload["room_id"] != active["room"] or not isinstance(payload["png"], str)
                or len(payload["png"]) > 180000 or payload["join_url"] != self.store.join_url(active["room"], active["secret"])
                or not isinstance(payload["sha256"], str) or not SHA.fullmatch(payload["sha256"])):
            raise self.error(400, "invalid_qr")
        try:
            data = base64.b64decode(payload["png"], validate=True)
            if not 32 <= len(data) <= 131072 or hashlib.sha256(data).hexdigest() != payload["sha256"] or data[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError()
            cursor, count, ended = 8, 0, False
            while cursor < len(data):
                size = struct.unpack(">I", data[cursor:cursor + 4])[0]
                kind = data[cursor + 4:cursor + 8]
                body = data[cursor + 8:cursor + 8 + size]
                crc = struct.unpack(">I", data[cursor + 8 + size:cursor + 12 + size])[0]
                if size > 131072 or crc != zlib.crc32(kind + body) or kind not in {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}:
                    raise ValueError()
                if count == 0:
                    width, height = struct.unpack(">II", body[:8])
                    if kind != b"IHDR" or size != 13 or width != height or not 64 <= width <= 1024:
                        raise ValueError()
                if kind == b"IEND":
                    if size != 0 or cursor + 12 != len(data):
                        raise ValueError()
                    ended = True
                cursor += size + 12
                count += 1
            if not ended or cursor != len(data):
                raise ValueError()
        except (ValueError, binascii.Error, struct.error):
            raise self.error(400, "invalid_qr") from None
        target = self.store.root / ("qr-" + active["room"] + ".png")
        fd, temporary = tempfile.mkstemp(prefix=".qr-", dir=self.store.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            current = self.active()
            if not current or current["instance"] != active["instance"] or current["room"] != active["room"]:
                raise self.error(409, "tv_instance_changed")
            if payload["join_url"] != self.store.join_url(current["room"], current["secret"]):
                raise self.error(409, "qr_invitation_changed")
            os.replace(temporary, target)
            metadata = {"sha256": payload["sha256"], "identity": list(self._identity(target)),
                        "join_sha256": self._qr_join_digest(current)}
            atomic_private(target.with_suffix(".json"), metadata)
            self.qr_metadata[active["room"]] = metadata
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.changed()
        return {"accepted": True, "room_id": active["room"]}

    def qr_file(self):
        active = self.active()
        if not self.qr_ready(active):
            raise self.error(503, "qr_pending")
        return self.store.root / ("qr-" + active["room"] + ".png")

    def status(self):
        party = self.party()
        return {"service": "friday-ktv-coordinator", "protocol": 2, "coordinator": True,
                "audio_formats": ["pcm_s16le_wav", "aac_lc_m4a"],
                "party": party, "readiness": party["readiness"], "cursor": self.cursor}

    def events(self, room, after, wait=5):
        if type(after) is not int or after < 0 or type(wait) not in {int, float} or not 0 <= wait <= 5:
            raise self.error(400, "invalid_events")
        if not self.waiters.acquire(blocking=False):
            raise self.error(429, "events_capacity")
        try:
            with self.condition:
                if after == self.cursor and wait:
                    self.condition.wait_for(lambda: self.cursor != after or self.closed, timeout=wait)
                cursor = self.cursor
            return {"cursor": cursor, "status": self.store.status(room)}
        finally:
            self.waiters.release()

    def _media_spec(self, value, kind, duration_ms):
        allowed = {"url", "sha256", "bytes", "duration_seconds", "sample_rate", "channels", "encoding"} | AAC_FIELDS
        if not isinstance(value, dict) or set(value) - allowed:
            raise self.error(400, "invalid_manifest_media")
        if (not isinstance(value.get("sha256"), str) or not SHA.fullmatch(value["sha256"])
                or type(value.get("bytes")) is not int or not 48 <= value["bytes"] <= (134217728 if kind == "video" else MEDIA_LIMIT)
                or not fixed_media_path(value.get("url"), kind)):
            raise self.error(400, "invalid_manifest_media")
        if kind == "video":
            if (value.get("encoding") not in VIDEO_ENCODINGS
                    or type(value.get("duration_seconds")) not in {int, float}
                    or not math.isfinite(value["duration_seconds"]) or not 0 < value["duration_seconds"] <= 1800):
                raise self.error(400, "invalid_manifest_media")
        elif (value.get("sample_rate") != 48000 or value.get("channels") != 2
              or not (value.get("encoding") == "pcm_s16le_wav" and not AAC_FIELDS.intersection(value) or compact_spec(value))
              or type(value.get("duration_seconds")) not in {int, float}
              or not math.isfinite(value["duration_seconds"]) or abs(value["duration_seconds"] * 1000 - duration_ms) > 150):
            raise self.error(400, "invalid_manifest_media")
        return dict(value)

    def _video_metadata(self, video, source_sha):
        if (video.get("source_audio_sha256") != source_sha
                or type(video.get("video_offset_ms")) not in {int, float}
                or not math.isfinite(video["video_offset_ms"]) or not -12000 <= video["video_offset_ms"] <= 12000):
            raise self.error(400, "invalid_manifest_video")
        result = {"source_audio_sha256": source_sha, "video_offset_ms": video["video_offset_ms"]}
        if video.get("method") == "same_mv_audio":
            if video["video_offset_ms"] != 0 or not isinstance(video.get("derived_from_video_sha256"), str) or not SHA.fullmatch(video["derived_from_video_sha256"]):
                raise self.error(400, "invalid_manifest_video")
            result.update(method="same_mv_audio", derived_from_video_sha256=video["derived_from_video_sha256"])
        else:
            if (any(type(video.get(k)) not in {int, float} or not math.isfinite(video[k]) for k in ("score_min", "offset_drift_ms"))
                    or not .94 <= video["score_min"] <= 1.01 or not 0 <= video["offset_drift_ms"] <= 25
                    or type(video.get("window_count")) is not int or not 5 <= video["window_count"] <= 7
                    or video.get("method", "multi_window_waveform_correlation") != "multi_window_waveform_correlation"):
                raise self.error(400, "invalid_manifest_video")
            result.update({k: video[k] for k in ("score_min", "offset_drift_ms", "window_count")})
            if "method" in video:
                result["method"] = video["method"]
        return result

    def _ready_video_spec(self, video, source_sha):
        if not isinstance(video, dict) or video.get("status") != "ready":
            raise self.error(400, "invalid_manifest_video")
        media_fields = {"url", "sha256", "bytes", "duration_seconds", "sample_rate", "channels", "encoding"}
        media = self._media_spec({k: v for k, v in video.items() if k in media_fields}, "video", None)
        return {**media, **self._video_metadata(video, source_sha), "status": "ready"}

    def manifest(self, payload, *, library=False):
        fields = {"id", "instance", "version", "timeline", "job", "lyrics", "media_ticket", "media_expires"}
        now = int(time.time())
        if (not fields <= set(payload) or set(payload) - fields - {"guide"} or not isinstance(payload["id"], str) or not UUID.fullmatch(payload["id"])
                or not isinstance(payload["version"], str) or not SHA.fullmatch(payload["version"])
                or not isinstance(payload["media_ticket"], str) or not TICKET.fullmatch(payload["media_ticket"])
                or type(payload["media_expires"]) is not int or not now + 10 <= payload["media_expires"] <= now + 900):
            raise self.error(400, "invalid_manifest")
        with self.lock:
            worker = copy.deepcopy(self.worker)
        if not worker or worker["instance"] != payload["instance"] or time.time() - worker["at"] > 20:
            raise self.error(409, "worker_instance_changed")
        timeline, job, lyrics = payload["timeline"], payload["job"], payload["lyrics"]
        if (not isinstance(timeline, dict) or set(timeline) != {"sample_rate", "frames", "duration_ms"}
                or timeline["sample_rate"] != 48000 or type(timeline["frames"]) is not int
                or not 1 <= timeline["frames"] <= 86400000 or type(timeline["duration_ms"]) is not int
                or abs(timeline["frames"] / 48 - timeline["duration_ms"]) > 1
                or not isinstance(job, dict) or job.get("id") != payload["id"] or job.get("state") != "ready"
                or not isinstance(job.get("media"), dict) or set(job["media"]) != {"original", "instrumental"}
                or not isinstance(lyrics, dict) or len(json.dumps(lyrics, allow_nan=False).encode()) > 131072):
            raise self.error(400, "invalid_manifest")
        media = {kind: self._media_spec(spec, kind, timeline["duration_ms"]) for kind, spec in job["media"].items()}
        if any(spec["encoding"] == "aac_lc_m4a" for spec in media.values()):
            if (not all(compact_spec(spec, timeline["frames"]) for spec in media.values())
                    or media["original"]["bitrate"] != media["instrumental"]["bitrate"]):
                raise self.error(400, "invalid_manifest_media")
        # Only fields consumed by the TV cross this boundary. No source account,
        # filesystem paths, arbitrary URLs, worker internals or exception strings.
        clean = {k: copy.deepcopy(job[k]) for k in ("id", "song_id", "title", "state", "code", "created", "progress", "requester", "audio_origin", "simulated", "source") if k in job}
        if isinstance(job.get("artist"), str) and 0 < len(job["artist"]) <= 256:
            clean["artist"] = job["artist"]
        clean["media"] = media
        background = job.get('background',{})
        if (isinstance(background,dict) and background.get('status') == 'ready' and background.get('encoding') in ('png', 'jpeg')
                and isinstance(background.get('sha256'),str) and SHA.fullmatch(background['sha256'])
                and type(background.get('bytes')) is int and 33 <= background['bytes'] <= 12*1024*1024
                and fixed_media_path(background.get('url'),'background')):
            clean['background'] = {k:background[k] for k in ('status','encoding','sha256','bytes','url')}
        video = job.get("video", {})
        if isinstance(video, dict) and video.get("status") == "ready":
            clean["video"] = self._ready_video_spec(video, media["original"]["sha256"])
        else:
            clean["video"] = {"status": "unavailable"}
        if "guide" in payload:
            guide = payload["guide"]
            if not isinstance(guide, dict) or set(guide) != {"sha256", "bytes", "duration_ms", "url", "reference"}:
                raise self.error(400, "invalid_manifest_guide")
            original = self._guide_spec({k: v for k, v in guide.items() if k != "reference"}, "guide_original", timeline["duration_ms"])
            reference = guide["reference"]
            if not isinstance(reference, dict) or reference.get("kind") != "accompaniment":
                raise self.error(400, "invalid_manifest_guide")
            reference = self._guide_spec({k: v for k, v in reference.items() if k != "kind"}, "guide_reference", timeline["duration_ms"])
            clean["guide"] = dict(original, reference=dict(reference, kind="accompaniment"))
        canonical = json.dumps({"timeline": timeline, "job": clean, "lyrics": lyrics}, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(canonical.encode()) > 196608:
            raise self.error(400, "invalid_manifest")
        if library:
            return self._library_manifest(payload, clean, timeline, lyrics, worker, now)
        with self.store.lock, self.store.connection() as db:
            row = db.execute("SELECT room,state FROM songs WHERE id=?", (payload["id"],)).fetchone()
            if not row or row["state"] == "cancelled" or db.execute("SELECT closed FROM rooms WHERE id=?", (row["room"],)).fetchone()[0]:
                raise self.error(409, "song_cancelled")
            old = db.execute("SELECT * FROM coordinator_manifests WHERE song=?", (payload["id"],)).fetchone()
            if old and old["version"] != payload["version"] and db.execute("SELECT 1 FROM coordinator_manifest_versions WHERE song=? AND version=?", (payload["id"], payload["version"])).fetchone():
                raise self.error(409, "manifest_superseded")
            if old and old["version"] == payload["version"] and (old["timeline"] != json.dumps(timeline, sort_keys=True)
                    or old["job"] != json.dumps(clean, sort_keys=True) or old["lyrics"] != json.dumps(lyrics, sort_keys=True)):
                raise self.error(409, "manifest_version_conflict")
            db.execute("INSERT OR REPLACE INTO coordinator_manifests VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (payload["id"], payload["version"], payload["instance"], json.dumps(timeline, sort_keys=True),
                        json.dumps(clean, sort_keys=True), json.dumps(lyrics, sort_keys=True), worker["origin"], payload["media_ticket"], payload["media_expires"], now))
            db.execute("INSERT OR IGNORE INTO coordinator_manifest_versions VALUES(?,?)", (payload["id"], payload["version"]))
            self.index_lyrics(db, payload["id"], payload["version"], lyrics, timeline)
            db.execute("UPDATE songs SET state='ready',code='none' WHERE id=? AND state!='cancelled'", (payload["id"],))
            if getattr(self.store, "management", None):
                published = db.execute("SELECT * FROM coordinator_manifests WHERE song=?", (payload["id"],)).fetchone()
                self.store.management.remember(published, db)
        self.changed()
        self._queue_cache(payload["id"])
        return {"accepted": True, "version": payload["version"], "archive": self.archive_state(payload["id"])}

    def _library_manifest(self, payload, job, timeline, lyrics, worker, now):
        """发布到歌曲库，可在任何房间点用，和当前场次没有关联。"""
        manager = self.store.management
        source, song_id, title = job.get("audio_origin"), job.get("song_id"), job.get("title")
        if (source not in {"song", "cloud"} or job.get("simulated") is not False
                or not isinstance(song_id, str) or not re.fullmatch(r"[1-9][0-9]{0,17}", song_id)
                or not isinstance(title, str) or not 0 < len(title) <= 120 or any(ord(c) < 32 for c in title)):
            raise self.error(400, "invalid_manifest")
        job.pop("requester", None)
        job.pop("progress", None)
        row = {"song": payload["id"], "version": payload["version"],
               "job": json.dumps(job, sort_keys=True), "timeline": json.dumps(timeline, sort_keys=True),
               "lyrics": json.dumps(lyrics, sort_keys=True), "updated": now,
               "origin": worker["origin"], "ticket": payload["media_ticket"], "expires": payload["media_expires"],
               "library_song_id": song_id, "library_source": source}
        with manager.lock:
            with self.store.connection() as db:
                if source == "cloud" and not db.execute("SELECT 1 FROM cloud_catalog WHERE song_id=?", (song_id,)).fetchone():
                    raise self.error(409, "cloud_song_unavailable")
                existing = db.execute(
                    "SELECT timeline,job,lyrics FROM managed_editions WHERE song_id=? AND source=? AND version=?",
                    (song_id, source, row["version"]),
                ).fetchone()
                if existing and (existing["timeline"] != row["timeline"] or existing["lyrics"] != row["lyrics"]
                                 or json.loads(existing["job"])["media"] != job["media"]):
                    raise self.error(409, "manifest_version_conflict")
                manager.remember(row, db, source=source)
            specs = self._specs(row)
            states = {}
            for kind, spec in specs.items():
                states[kind] = self.cached_file(spec) is not None
                if not states[kind]:
                    self._queue_asset_cache(row, kind, spec)
        return {"accepted": True, "version": row["version"],
                "archive": "ready" if all(states.values()) else "pending",
                "lyrics_ready": self.lyric_state(lyrics, timeline) == 1,
                "background_ready": states.get("background", False)}

    @staticmethod
    def lyric_state(lyrics, timeline):
        # 新歌先准备歌词。缺失或有冲突的时间轴留给后台恢复，不算可唱。
        lines = lyrics.get("lines") if isinstance(lyrics, dict) else None
        duration = timeline.get("duration_ms") if isinstance(timeline, dict) else None
        ready = (type(duration) is int and isinstance(lines, list) and 3 <= len(lines) <= 1000
                 and lyrics.get("status", "ready") == "ready")
        previous = -1
        if ready:
            for line in lines:
                stamp = line.get("time_ms", line.get("at_ms")) if isinstance(line, dict) else None
                text = line.get("text") if isinstance(line, dict) else None
                if type(stamp) is not int or not previous <= stamp <= duration or stamp < 0 or not isinstance(text, str) or not text.strip():
                    ready = False
                    break
                previous = stamp
        return 1 if ready else -1 if isinstance(lyrics, dict) and lyrics.get("retry_exhausted") is True else 0

    @classmethod
    def index_lyrics(cls, db, song, version, lyrics, timeline):
        db.execute("INSERT OR REPLACE INTO coordinator_lyric_status VALUES(?,?,?)",
                   (song, version, cls.lyric_state(lyrics, timeline)))

    def lyric_tasks(self, room_id, db):
        rows = db.execute("SELECT s.id,s.song_id,s.source,s.title,m.timeline,m.version,m.lyrics,ls.ready "
                          "FROM songs s JOIN coordinator_manifests m ON m.song=s.id "
                          "JOIN coordinator_lyric_status ls ON ls.song=s.id "
                          "WHERE s.room=? AND s.removed=0 AND s.state='ready' AND m.instance='' "
                          "AND (ls.ready=0 OR (ls.ready=-1 AND s.source IN ('song','cloud'))) "
                          "ORDER BY s.position,s.rowid", (room_id,))
        tasks = []
        for row in rows:
            if row["ready"] == -1:
                revision = json.loads(row["lyrics"]).get("lookup_revision", 0)
                if type(revision) is int and revision >= LYRIC_LOOKUP_REVISION:
                    continue
            tasks.append({**{k: row[k] for k in ("id", "song_id", "source", "title", "version")},
                          "duration_ms": json.loads(row["timeline"])["duration_ms"]})
            if len(tasks) == 8:
                break
        return tasks

    def repair_lyrics(self, payload):
        if (set(payload) != {"id", "instance", "version", "lyrics"}
                or any(not isinstance(payload[k], str) or not UUID.fullmatch(payload[k]) for k in ("id", "instance"))
                or not isinstance(payload["version"], str) or not SHA.fullmatch(payload["version"])
                or not isinstance(payload["lyrics"], dict)):
            raise self.error(400, "invalid_lyrics")
        with self.lock:
            worker = self.worker
            if not worker or payload["instance"] != worker["instance"] or not 0 <= time.time() - worker["at"] <= 20:
                raise self.error(409, "worker_unavailable")
        lyrics = payload["lyrics"]
        encoded = json.dumps(lyrics, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 131072:
            raise self.error(400, "invalid_lyrics")
        with self.store.lock, self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT m.* FROM coordinator_manifests m JOIN songs s ON s.id=m.song "
                             "JOIN coordinator_session c ON c.room=s.room JOIN rooms r ON r.id=s.room "
                             "WHERE m.song=? AND m.instance='' AND s.state='ready' AND s.removed=0 AND r.closed=0",
                             (payload["id"],)).fetchone()
            if not row:
                raise self.error(409, "lyrics_edition_changed")
            if row["version"] != payload["version"]:
                expected = hashlib.sha256((payload["version"] + ":" + encoded).encode()).hexdigest()
                if row["version"] == expected:
                    return {"accepted": True, "version": expected}
                raise self.error(409, "lyrics_edition_changed")
            timeline = json.loads(row["timeline"])
            if self.lyric_state(lyrics, timeline) == 0:
                raise self.error(400, "invalid_lyrics")
            version = hashlib.sha256((row["version"] + ":" + encoded).encode()).hexdigest()
            db.execute("UPDATE coordinator_manifests SET lyrics=?,version=?,updated=? WHERE song=?",
                       (encoded, version, int(time.time()), payload["id"]))
            self.index_lyrics(db, payload["id"], version, lyrics, timeline)
            db.execute("INSERT OR IGNORE INTO coordinator_manifest_versions VALUES(?,?)", (payload["id"], version))
            if getattr(self.store, "management", None):
                self.store.management.remember(db.execute("SELECT * FROM coordinator_manifests WHERE song=?",
                                                          (payload["id"],)).fetchone(), db)
        return {"accepted": True, "version": version}

    def _row(self, song):
        with self.store.connection() as db:
            row = db.execute("SELECT m.* FROM coordinator_manifests m JOIN songs s ON s.id=m.song JOIN rooms r ON r.id=s.room WHERE m.song=? AND s.state='ready' AND s.removed=0 AND r.closed=0", (song,)).fetchone()
        return dict(row) if row else None

    def _guide_spec(self, spec, kind, duration_ms):
        if (set(spec) != {"sha256", "bytes", "duration_ms", "url"}
                or not isinstance(spec["sha256"], str) or not SHA.fullmatch(spec["sha256"])
                or type(spec["bytes"]) is not int or not 1024 < spec["bytes"] <= 32 * 1024 * 1024
                or type(spec["duration_ms"]) is not int or not 1000 < spec["duration_ms"] <= 1800000
                or abs(spec["duration_ms"] - duration_ms) > 150
                or not fixed_media_path(spec["url"], kind)):
            raise self.error(400, "invalid_manifest_guide")
        return dict(spec)

    def _specs(self, row):
        job = json.loads(row["job"])
        result = dict(job["media"])
        if job.get('background',{}).get('status') == 'ready':
            result['background'] = job['background']
        if job.get("video", {}).get("status") == "ready":
            result["video"] = job["video"]
        if "guide" in job:
            result["guide_original"] = {k: v for k, v in job["guide"].items() if k != "reference"}
            result["guide_reference"] = {k: v for k, v in job["guide"]["reference"].items() if k != "kind"}
        return result

    @staticmethod
    def _identity(path):
        try:
            value = path.lstat()
            if not stat.S_ISREG(value.st_mode):
                return None
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        except OSError:
            return None

    @staticmethod
    def _verified_identity(row):
        return (row["device"], row["inode"], row["bytes"], row["mtime_ns"], row["ctime_ns"])

    def cached_file(self, spec, *, repair=True):
        path = self.media_root / spec["sha256"]
        identity = self._identity(path)
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM coordinator_cache WHERE sha=?", (spec["sha256"],)).fetchone()
        if row and row["bytes"] != spec["bytes"]:
            return None
        if row and row["state"] == "ready" and identity == self._verified_identity(row) and row["bytes"] == spec["bytes"]:
            return path
        if row and row["state"] == "ready":
            self._queue_verify(spec, repair=repair)
        return None

    def _queue_verify(self, spec, *, repair=True):
        # Queue only; large hashes run in the background, never on status,
        # controls, event delivery or TV job-description requests.
        with self.lock:
            if self.closed or spec["sha256"] in self.verify_pending or len(self.verify_pending) >= 32:
                return
            self.verify_pending.add(spec["sha256"])
        pool = self.video_cache_pool if spec.get("encoding") in VIDEO_ENCODINGS else self.cache_pool
        pool.submit(self._verify_cached, dict(spec), repair)

    def _verify_cached(self, spec, repair=True):
        path = self.media_root / spec["sha256"]
        state, identity = "failed", self._identity(path)
        try:
            with self.store.connection() as db:
                db.execute("UPDATE coordinator_cache SET state='pending' WHERE sha=?", (spec["sha256"],))
            if identity is not None and identity[2] == spec["bytes"]:
                digest, total, deadline = hashlib.sha256(), 0, time.monotonic() + 60
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                    for block in iter(lambda: stream.read(65536), b""):
                        if self.closed or time.monotonic() > deadline:
                            raise ValueError("cache_verification_cancelled")
                        digest.update(block)
                        total += len(block)
                if total == spec["bytes"] and digest.hexdigest() == spec["sha256"] and self._identity(path) == identity:
                    state = "ready"
        except (OSError, ValueError):
            pass
        finally:
            with self.store.connection() as db:
                if state == "ready":
                    db.execute("UPDATE coordinator_cache SET state='ready',attempts=0,device=?,inode=?,mtime_ns=?,ctime_ns=? WHERE sha=?",
                               (identity[0], identity[1], identity[3], identity[4], spec["sha256"]))
                else:
                    db.execute("UPDATE coordinator_cache SET state='failed',updated=0 WHERE sha=?", (spec["sha256"],))
            with self.lock:
                self.verify_pending.discard(spec["sha256"])
            self.changed()
            if repair and state != "ready" and not self.closed:
                # Reuse a still-valid immutable manifest for a bounded retry;
                # failed verification never changes an archive to ready.
                with self.store.connection() as db:
                    rows = db.execute("SELECT song FROM coordinator_manifests WHERE job LIKE ? LIMIT 80", ("%" + spec["sha256"] + "%",)).fetchall()
                for row in rows:
                    self._queue_cache(row["song"])

    def archive_check(self, payload):
        """Content proof after a session ends; no room/user association stored."""
        if set(payload) != {"items"} or not isinstance(payload["items"], list) or len(payload["items"]) > 16:
            raise self.error(400, "invalid_archive_check")
        seen = set()
        for item in payload["items"]:
            if (not isinstance(item, dict) or set(item) != {"id", "files"}
                    or not isinstance(item["id"], str) or not UUID.fullmatch(item["id"]) or item["id"] in seen
                    or not isinstance(item["files"], list) or not 1 <= len(item["files"]) <= 5):
                raise self.error(400, "invalid_archive_check")
            seen.add(item["id"])
            for spec in item["files"]:
                if (not isinstance(spec, dict) or set(spec) != {"sha256", "bytes"}
                        or not isinstance(spec["sha256"], str) or not SHA.fullmatch(spec["sha256"])
                        or type(spec["bytes"]) is not int or not 48 <= spec["bytes"] <= MEDIA_LIMIT):
                    raise self.error(400, "invalid_archive_check")
        result = []
        for item in payload["items"]:
            ready, failed = True, False
            for spec in item["files"]:
                # A content lookup must never cause an implicit media transfer.
                # Changed files can only enter the existing background hash lane.
                ready &= self.cached_file(spec, repair=False) is not None
                with self.store.connection() as db:
                    row = db.execute("SELECT bytes,state FROM coordinator_cache WHERE sha=?", (spec["sha256"],)).fetchone()
                failed |= bool(row and (row["state"] == "failed" or row["bytes"] != spec["bytes"]))
            result.append({"id": item["id"], "state": "ready" if ready else "failed" if failed else "pending"})
        return {"items": result}

    def archive_videos(self, payload):
        now = int(time.time())
        instance, items = payload.get("instance"), payload.get("items")
        if (set(payload) != {"instance", "items"} or not isinstance(instance, str) or not UUID.fullmatch(instance)
                or not isinstance(items, list) or not 1 <= len(items) <= 16):
            raise self.error(400, "invalid_archive_videos")
        with self.lock:
            worker = self.worker
            if not worker or instance != worker["instance"] or not 0 <= time.time() - worker["at"] <= 20:
                raise self.error(409, "worker_unavailable")
        seen, videos = set(), []
        for item in items:
            if (not isinstance(item, dict) or set(item) != {"id", "audio", "video", "media_ticket", "media_expires"}
                    or not isinstance(item["id"], str) or not UUID.fullmatch(item["id"]) or item["id"] in seen
                    or not isinstance(item["audio"], dict) or set(item["audio"]) != {"original", "instrumental"}
                    or any(not isinstance(sha, str) or not SHA.fullmatch(sha) for sha in item["audio"].values())
                    or not isinstance(item["media_ticket"], str) or not TICKET.fullmatch(item["media_ticket"])
                    or type(item["media_expires"]) is not int or not now + 10 <= item["media_expires"] <= now + 900):
                raise self.error(400, "invalid_archive_videos")
            seen.add(item["id"])
            videos.append((item, self._ready_video_spec(item["video"], item["audio"]["original"])))
        return {"items": [{"id": item["id"], "state": self._archive_video(worker, item, spec)} for item, spec in videos]}

    def _archive_video(self, worker, item, spec):
        with self.store.connection() as db:
            row = self._archived_asset_row(db, item["id"], "video")
        if not row:
            return "obsolete"
        job = json.loads(row["job"])
        prefix = spec["url"].rsplit("/", 1)[0]
        if any(job["media"][kind]["sha256"] != sha or job["media"][kind]["url"] != prefix + "/" + kind
               for kind, sha in item["audio"].items()):
            return "obsolete"
        published = job.get("video", {})
        if published.get("status") == "ready" and published != spec:
            return "obsolete"
        manager = getattr(self.store, "management", None)
        with manager.lock if manager else self.lock:
            if self.cached_file(spec):
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    attached = self._attach_archive_asset(db, row, "video", spec)
                return "ready" if attached else "obsolete"
            download = dict(row, origin=worker["origin"], ticket=item["media_ticket"], expires=item["media_expires"])
            self._queue_asset_cache(download, "video", spec, archived=True)
        return "pending"

    def archive_state(self, song):
        row = self._row(song)
        if not row:
            return "unavailable"
        states = []
        valid = True
        specs = [spec for kind, spec in self._specs(row).items() if kind != "background"]
        with self.store.connection() as db:
            for spec in specs:
                found = db.execute("SELECT state FROM coordinator_cache WHERE sha=?", (spec["sha256"],)).fetchone()
                states.append(found[0] if found else "pending")
        for spec in specs:
            valid &= self.cached_file(spec) is not None
        return "ready" if states and all(s == "ready" for s in states) and valid else "failed" if "failed" in states else "pending"

    def archives(self, room_id, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.archives(room_id, connection)
        rows = db.execute("SELECT m.song,m.version,m.job FROM coordinator_manifests m JOIN songs s ON s.id=m.song WHERE s.room=? AND s.state='ready' LIMIT 80", (room_id,)).fetchall()
        cache = {r["sha"]: dict(r) for r in db.execute("SELECT * FROM coordinator_cache")}
        result = []
        for row in rows:
            job = json.loads(row["job"])
            specs = list(job["media"].values())
            if job.get("video", {}).get("status") == "ready":
                specs.append(job["video"])
            if job.get("guide"):
                specs.extend((job["guide"], job["guide"]["reference"]))
            states = []
            present = True
            for spec in specs:
                record = cache.get(spec["sha256"])
                valid = bool(record and record["state"] == "ready" and record["bytes"] == spec["bytes"] and self._identity(self.media_root / spec["sha256"]) == self._verified_identity(record))
                states.append(record["state"] if record else "pending")
                present &= valid
                if record and record["state"] == "ready" and not valid:
                    self._queue_verify(spec)
            state = "ready" if states and all(s == "ready" for s in states) and present else "failed" if "failed" in states else "pending"
            result.append({"id": row["song"], "version": row["version"], "state": state})
        return result

    def jobs(self):
        active = self.active()
        backgrounds_enabled = self.backgrounds_enabled
        if not active:
            return {"jobs": [], "backgrounds_enabled": backgrounds_enabled}
        with self.store.connection() as db:
            db.execute("BEGIN")
            songs = [dict(r) for r in db.execute(
                "SELECT s.id,s.song_id,s.title,s.state,s.code,s.created,s.source,m.version,m.job,m.timeline,m.origin,m.ticket,m.expires,"
                "coalesce(ls.ready,0) AS lyric_state "
                "FROM songs s LEFT JOIN coordinator_manifests m ON m.song=s.id AND s.state='ready' "
                "LEFT JOIN coordinator_lyric_status ls ON ls.song=m.song AND ls.version=m.version "
                "WHERE s.room=? AND s.removed=0 ORDER BY s.position,s.rowid LIMIT 80", (active["room"],))]
            rows = [(row, self._specs(row)) for row in songs if row["version"]]
            specs_by_song = {row["id"]: specs for row, specs in rows}
            hashes = sorted({spec["sha256"] for specs in specs_by_song.values() for spec in specs.values()})
            cache = {}
            for i in range(0, len(hashes), 200):
                batch = hashes[i:i + 200]
                placeholders = ",".join("?" for _ in batch)
                cache.update((r["sha"], dict(r)) for r in db.execute(
                    "SELECT * FROM coordinator_cache WHERE sha IN (" + placeholders + ")", batch))
        # 同一文件只查一次；验证通过才对电视公开本地来源。
        present = {}
        for specs in specs_by_song.values():
            for spec in specs.values():
                key = spec["sha256"], spec["bytes"]
                if key in present:
                    continue
                record = cache.get(key[0])
                valid = bool(record and record["state"] == "ready" and record["bytes"] == key[1]
                             and self._identity(self.media_root / key[0]) == self._verified_identity(record))
                present[key] = valid
                if not valid and record and record["state"] == "ready" and record["bytes"] == key[1]:
                    self._queue_verify(spec)
        now, worker_media = time.time(), self.worker_media_ready()
        jobs = []
        for row in songs:
            song = row
            if not row["version"]:
                pending = {k: song[k] for k in ("id", "song_id", "title", "state", "code", "created", "source")}
                if pending["state"] == "ready":
                    pending.update(state="transcoding", code="none")
                jobs.append(pending)
                continue
            job = json.loads(row["job"])
            specs = specs_by_song[row["id"]]
            required = [spec for kind, spec in specs.items() if kind != "background"]
            states = [cache.get(spec["sha256"], {}).get("state", "pending") for spec in required]
            all_local = all(present[(spec["sha256"], spec["bytes"])] for spec in required)
            archive = "ready" if states and all(s == "ready" for s in states) and all_local else "failed" if "failed" in states else "pending"
            job.update(version=row["version"], timeline=json.loads(row["timeline"]), archive=archive,
                       lyric_state=row["lyric_state"], synced_lyrics=row["lyric_state"] == 1)
            for kind, spec in specs.items():
                if kind.startswith("guide_"):
                    continue
                if kind == "background" and not backgrounds_enabled:
                    job.pop("background", None)
                    continue
                sources = []
                local_url = "/v1/jobs/" + song["id"] + "/media/" + kind
                if present[(spec["sha256"], spec["bytes"])]:
                    sources.append({"url": local_url})
                if self._worker_delivery(row, now, worker_media) and not (kind == "background" and spec["url"] == local_url):
                    sources.append({"origin": row["origin"], "url": spec["url"], "header": "X-KTV-Media",
                                    "credential": row["ticket"], "expires": row["expires"]})
                public = dict(spec, sources=sources)
                # Relative url remains the local cache fallback for old clients;
                # clients using sources never wait for it to become ready.
                public["url"] = local_url
                if kind == "video":
                    job["video"] = public
                elif kind == 'background':
                    job['background'] = public
                else:
                    job["media"][kind] = public
            if any(not spec.get("sources") for spec in job["media"].values()):
                job.update(state="transcoding", code="none")
            jobs.append(job)
        return {"jobs": jobs, "backgrounds_enabled": backgrounds_enabled}

    def lyrics(self, song):
        row = self._row(song)
        if not row:
            raise self.error(404, "not_found")
        return json.loads(row["lyrics"])

    def media_file(self, song, kind):
        if kind == "background" and not self.backgrounds_enabled:
            raise self.error(404, "not_found")
        row = self._row(song)
        if not row or kind not in self._specs(row):
            raise self.error(404, "not_found")
        spec = self._specs(row)[kind]
        path = self.cached_file(spec)
        if path is None:
            raise self.error(503, "media_cache_pending")
        return path, spec

    def background_tasks(self):
        if not self.backgrounds_enabled:
            return {"tasks": []}
        active = self.active()
        if not active:
            return {"tasks": []}
        with self.lock:
            worker_instance = self.worker["instance"] if self.worker else ""
        with self.store.connection() as db:
            room = db.execute("SELECT current_song, played FROM rooms WHERE id=?", (active["room"],)).fetchone()
            current, played = room["current_song"], set(json.loads(room["played"]))
            rows = db.execute(
                "SELECT s.id,s.song_id,s.title,s.source,m.job,m.version,m.instance "
                "FROM songs s JOIN coordinator_manifests m ON m.song=s.id "
                "WHERE s.room=? AND s.removed=0 AND s.state='ready' "
                "ORDER BY s.id=? DESC,s.position,s.rowid LIMIT 80", (active["room"], current))
            tasks = []
            for row in rows:
                # 现任 Worker 会为待唱及正在唱的本机任务发布背景。历史记录可直接补图。
                if row["instance"] and row["instance"] == worker_instance and row["id"] not in played:
                    continue
                job = json.loads(row["job"])
                if job.get("simulated") is True:
                    continue
                spec = job.get("background")
                storage = getattr(self.store, "management", None)
                ready = spec and storage and storage._verified_asset(spec, db)
                tasks.append({"id": row["id"], "song_id": row["song_id"], "title": job.get("title", row["title"]),
                              "artist": job.get("artist", ""),
                              "audio_origin": job.get("audio_origin") or row["source"], "version": row["version"],
                              "current": row["id"] == current, "played": row["id"] in played,
                              "background_sha": spec["sha256"] if ready else ""})
        return {"tasks": tasks}

    def repair_background(self, payload):
        now = int(time.time())
        spec = payload.get("background")
        if (set(payload) != {"id", "instance", "version", "background", "media_ticket", "media_expires"}
                or any(not isinstance(payload[key], str) or not UUID.fullmatch(payload[key]) for key in ("id", "instance"))
                or not isinstance(payload["version"], str) or not SHA.fullmatch(payload["version"])
                or not isinstance(payload["media_ticket"], str) or not TICKET.fullmatch(payload["media_ticket"])
                or type(payload["media_expires"]) is not int or not now + 10 <= payload["media_expires"] <= now + 900
                or not isinstance(spec, dict) or set(spec) != {"status", "sha256", "bytes", "encoding", "url"}
                or spec["status"] != "ready" or spec["encoding"] not in ("jpeg", "png")
                or not isinstance(spec["sha256"], str) or not SHA.fullmatch(spec["sha256"])
                or type(spec["bytes"]) is not int or not 33 <= spec["bytes"] <= 12 * 1024 * 1024
                or not isinstance(spec["url"], str) or not re.fullmatch(r"/v1/background-assets/[a-f0-9]{64}/" + spec["sha256"], spec["url"])):
            raise self.error(400, "invalid_background")
        with self.lock:
            worker = self.worker
            if (not self.backgrounds_enabled or not worker or payload["instance"] != worker["instance"]
                    or not 0 <= time.time() - worker["at"] <= 20):
                raise self.error(409, "worker_unavailable")
        row = self._row(payload["id"])
        if not row or payload["version"] != row["version"]:
            raise self.error(409, "background_edition_changed")
        manager = getattr(self.store, "management", None)
        with manager.lock if manager else self.lock:
            if self.cached_file(spec):
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self._attach_archive_asset(db, row, "background", spec)
            else:
                download = dict(row, origin=worker["origin"], ticket=payload["media_ticket"], expires=payload["media_expires"])
                self._queue_asset_cache(download, "background", spec, archived=True)
        return {"accepted": True}

    @staticmethod
    def _archived_asset_row(db, song, kind):
        query = ("SELECT m.* FROM coordinator_manifests m JOIN songs s ON s.id=m.song JOIN rooms r ON r.id=s.room "
                 "WHERE m.song=? AND s.state='ready' AND s.removed=0")
        if kind == "background":
            query += " AND r.closed=0 AND EXISTS (SELECT 1 FROM coordinator_session c WHERE c.room=s.room)"
        return db.execute(query, (song,)).fetchone()

    def _attach_archive_asset(self, db, row, kind, spec):
        current = self._archived_asset_row(db, row["song"], kind)
        if not current:
            return False
        job, snapshot = json.loads(current["job"]), json.loads(row["job"])
        if (current["instance"] != row["instance"] or current["timeline"] != row["timeline"]
                or job["media"] != snapshot["media"]):
            return False
        descriptor = dict(spec, url=f'/v1/jobs/{row["song"]}/media/{kind}') if kind == "background" else spec
        if job.get(kind) == descriptor:
            return True
        if job.get(kind) != snapshot.get(kind):
            return False
        job[kind] = descriptor
        version = hashlib.sha256(f'{current["version"]}:{kind}:{spec["sha256"]}'.encode()).hexdigest()
        db.execute("UPDATE coordinator_manifests SET job=?,version=?,updated=? WHERE song=?",
                   (json.dumps(job, sort_keys=True), version, int(time.time()), row["song"]))
        db.execute("INSERT OR IGNORE INTO coordinator_manifest_versions VALUES(?,?)", (row["song"], version))
        self.index_lyrics(db, row["song"], version, json.loads(current["lyrics"]), json.loads(current["timeline"]))
        if getattr(self.store, "management", None):
            self.store.management.remember(db.execute("SELECT * FROM coordinator_manifests WHERE song=?", (row["song"],)).fetchone(), db)
        return True

    def _queue_cache(self, song):
        manager = getattr(self.store, "management", None)
        if manager:
            with manager.lock:
                return self._queue_cache_impl(song)
        return self._queue_cache_impl(song)

    def _queue_cache_impl(self, song):
        row = self._row(song)
        if not row or self.closed:
            return
        for kind, spec in self._specs(row).items():
            if self.cached_file(spec):
                continue
            self._queue_asset_cache(row, kind, spec)

    def _queue_asset_cache(self, row, kind, spec, *, archived=False):
        now = int(time.time())
        if row["origin"] != self.worker_origin or row["expires"] <= now:
            return
        with self.lock:
            if self.closed or spec["sha256"] in self.cache_pending or spec["sha256"] in self.verify_pending:
                return
            self.cache_pending.add(spec["sha256"])
        try:
            with self.store.connection() as db:
                old = db.execute("SELECT updated,attempts FROM coordinator_cache WHERE sha=?", (spec["sha256"],)).fetchone()
                # 临时断线或过期票据不会把同一份已校验摘要永久锁死。
                wait = min(300, 30 * 2 ** min(4, max(0, old["attempts"] - 1))) if old else 0
                if old and now - old["updated"] < wait:
                    with self.lock:
                        self.cache_pending.discard(spec["sha256"])
                    return
                db.execute("INSERT OR REPLACE INTO coordinator_cache(sha,bytes,state,updated,attempts) VALUES(?,?,'pending',?,?)",
                           (spec["sha256"], spec["bytes"], now, (old["attempts"] if old else 0) + 1))
            pool = self.video_cache_pool if kind == "video" else self.cache_pool
            if archived:
                pool.submit(self._cache_one, row, kind, spec, True)
            else:
                pool.submit(self._cache_one, row, kind, spec)
        except BaseException:
            with self.lock:
                self.cache_pending.discard(spec["sha256"])
            raise

    def _cache_one(self, row, kind, spec, archived=False):
        temporary = None
        state = "failed"
        try:
            if row["origin"] != self.worker_origin or row["expires"] <= time.time():
                return
            manager = getattr(self.store, "management", None)
            if manager and not manager.reserve(spec):
                state = "blocked"
                return
            request = urllib.request.Request(row["origin"] + spec["url"], headers={"X-KTV-Media": row["ticket"]})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            fd, temporary = tempfile.mkstemp(prefix=".download-", dir=self.media_root)
            total, digest, deadline = 0, hashlib.sha256(), time.monotonic() + 180
            with os.fdopen(fd, "wb") as out, opener.open(request, timeout=10) as response:
                if response.status != 200 or int(response.headers.get("Content-Length", "-1")) != spec["bytes"]:
                    raise ValueError("media_length_mismatch")
                while True:
                    block = response.read(65536)
                    if not block:
                        break
                    total += len(block)
                    if self.closed or time.monotonic() > deadline or total > spec["bytes"]:
                        raise ValueError("media_budget_exceeded")
                    digest.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            if total != spec["bytes"] or digest.hexdigest() != spec["sha256"]:
                raise ValueError("media_integrity_failed")
            if kind in {"original", "instrumental"}:
                if spec.get("encoding") == "aac_lc_m4a":
                    verify_compact(temporary, spec)
                else:
                    with wave.open(temporary, "rb") as audio:
                        if audio.getnchannels() != 2 or audio.getframerate() != 48000 or audio.getsampwidth() != 2:
                            raise ValueError("media_contract_failed")
                        if abs(audio.getnframes() / 48 - json.loads(row["timeline"])["duration_ms"]) > 150:
                            raise ValueError("media_timeline_failed")
            with manager.lock if manager else self.lock:
                with self.store.connection() as db:
                    if archived:
                        db.execute("BEGIN IMMEDIATE")
                        if not self._attach_archive_asset(db, row, kind, spec):
                            return
                    else:
                        if "library_source" in row:
                            current = db.execute("SELECT job FROM managed_editions WHERE song_id=? AND source=? AND version=?",
                                                 (row["library_song_id"], row["library_source"], row["version"])).fetchone()
                        else:
                            current = db.execute("SELECT job FROM coordinator_manifests WHERE song=?", (row["song"],)).fetchone()
                        expected = self._specs(current).get(kind) if current else None
                        # 与删除和缓存淘汰串行确认当前文件仍被引用。
                        if not expected or expected["sha256"] != spec["sha256"] or expected["bytes"] != spec["bytes"]:
                            return
                    os.chmod(temporary, 0o400)
                    os.replace(temporary, self.media_root / spec["sha256"])
                    temporary = None
                    identity = self._identity(self.media_root / spec["sha256"])
                    if not identity:
                        raise ValueError("media_file_missing")
                    # 视频引用与已校验文件同时提交，下一次点播可立即复用。
                    db.execute("UPDATE coordinator_cache SET state='ready',updated=?,attempts=0,device=?,inode=?,mtime_ns=?,ctime_ns=? WHERE sha=?",
                               (int(time.time()), identity[0], identity[1], identity[3], identity[4], spec["sha256"]))
                state = "ready"
        except Exception:
            # Fixed state only; upstream URLs/tickets/errors are never logged.
            pass
        finally:
            try:
                if temporary:
                    Path(temporary).unlink(missing_ok=True)
                if state != "ready":
                    with self.store.connection() as db:
                        db.execute("UPDATE coordinator_cache SET state=?,updated=?,attempts=CASE WHEN ?='blocked' THEN 0 ELSE attempts END WHERE sha=?", (state, int(time.time()), state, spec["sha256"]))
            except (OSError, sqlite3.Error) as error:
                self.store.record_failure("archive_cleanup", error)
            finally:
                with self.lock:
                    self.cache_pending.discard(spec["sha256"])
                manager = getattr(self.store, "management", None)
                if manager:
                    with manager.lock:
                        manager.reserved.pop(spec["sha256"], None)
            if state == "ready" and kind.startswith("guide_"):
                self._publish_guide(row["song"])
            self.changed()

    def _publish_guide(self, song):
        row = self._row(song)
        if not row:
            return
        job = json.loads(row["job"])
        guide = job.get("guide")
        if not guide or not self.cached_file(guide) or not self.cached_file(guide["reference"]):
            return
        value = {k: guide[k] for k in ("sha256", "bytes", "duration_ms")}
        value.update(id=song, reference={k: guide["reference"][k] for k in ("sha256", "bytes", "duration_ms", "kind")})
        self.store.guide_update(value)

    def close(self):
        with self.lock:
            self.closed = True
            self.recovery_cancel.set()
        self.changed()
        self.cache_pool.shutdown(wait=False)
        self.video_cache_pool.shutdown(wait=False)
        self.recovery_pool.shutdown(wait=False)
        self.health_pool.shutdown(wait=False)
        if getattr(self.store, "management", None):
            self.store.management.close()
