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
from pathlib import Path

UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
TICKET = re.compile(r"[A-Za-z0-9_.-]{32,128}\Z")
CAPABILITIES = frozenset({"source", "model", "download", "lyrics", "media"})
PERSISTENT_EXPIRY = 2147483647
MEDIA_LIMIT = 345600044


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
        self.cache_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ktv-nas-cache")
        self.cache_pending = set()
        self.verify_pending = set()
        self.closed = False
        self.recovery_enabled = recovery_enabled
        self.recovery_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ktv-worker-recovery")
        self.recovery_cancel = threading.Event()
        self.recovery_identity = None
        self.recovery_attempts = 0
        self.recovery_result = "not_requested"
        self.recovery_started_at = 0
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
              CREATE TABLE IF NOT EXISTS coordinator_start_requests(request_id TEXT PRIMARY KEY,
                instance TEXT NOT NULL, room TEXT NOT NULL, created INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS coordinator_household(id INTEGER PRIMARY KEY CHECK(id=1),
                initialized INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS room_tokens(room TEXT NOT NULL, token_hash TEXT NOT NULL,
                expires INTEGER NOT NULL, PRIMARY KEY(room,token_hash));
            """)
            # This household-scoped marker outlives all scene retention. If an
            # older coordinator already left a household identity but no marker,
            # conservatively avoid adopting another legacy scene on upgrade.
            db.execute("INSERT OR IGNORE INTO coordinator_household VALUES(1,?)", (int(household_existed),))
            db.execute("UPDATE coordinator_household SET initialized=1 WHERE EXISTS(SELECT 1 FROM coordinator_session)")
            # A process restart is not proof that the old TV or worker is alive.
            db.execute("UPDATE coordinator_session SET tv_at=0,readiness='{}'")
            if "request_id" not in {row[1] for row in db.execute("PRAGMA table_info(coordinator_session)")}:
                db.execute("ALTER TABLE coordinator_session ADD COLUMN request_id TEXT NOT NULL DEFAULT ''")
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

    def active(self):
        with self.store.connection() as db:
            row = db.execute("SELECT s.*,r.closed FROM coordinator_session s JOIN rooms r ON r.id=s.room WHERE s.id=1").fetchone()
        return dict(row) if row and not row["closed"] else None

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
        if (set(payload) != {"instance", "origin", "capabilities"}
                or not isinstance(payload["instance"], str) or not UUID.fullmatch(payload["instance"])
                or not isinstance(payload["capabilities"], dict) or set(payload["capabilities"]) != CAPABILITIES
                or any(type(v) is not bool for v in payload["capabilities"].values())):
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
            self.worker = {"instance": payload["instance"], "origin": origin,
                           "capabilities": dict(payload["capabilities"]), "at": time.time()}
            if all(self.worker["capabilities"].values()):
                self.recovery_cancel.set()
                self.recovery_identity = None
                self.recovery_started_at = 0
                self.recovery_result = "worker_ready"
        self.changed()
        return {"accepted": True, "instance": payload["instance"], "expires_in": 20,
                "preparation_requested": bool(self.active()), "recovery": self.recovery(), "party": self.party()}

    def recovery(self):
        active = self.active()
        # This is a request for the already configured local supervisor. There
        # are no shell commands, arbitrary launch targets or wake actions here.
        if not active:
            return {"requested": False, "attempt": 0, "reason": "tv_not_opened"}
        elapsed = max(0, int(time.time()) - (self.recovery_started_at or active["opened"]))
        return {"requested": not self.worker_healthy() and elapsed < 30,
                "attempt": self.recovery_attempts, "reason": "readiness" if elapsed < 30 else "timed_out",
                "configured": bool(self.recovery_enabled and self.worker_origin), "cannot_wake": True,
                "last_result": self.recovery_result}

    def _schedule_recovery(self, active):
        if not active or not self.recovery_enabled or not self.worker_origin or self.worker_healthy():
            return
        identity = (active["instance"], active["request_id"])
        with self.lock:
            if self.recovery_identity == identity:
                return
            self.recovery_identity = identity
            self.recovery_cancel.set()
            self.recovery_cancel = threading.Event()
            self.recovery_attempts = 0
            self.recovery_result = "scheduled"
            self.recovery_started_at = time.time()
            self.recovery_pool.submit(self._recover_worker, identity, self.recovery_cancel)

    def _recover_worker(self, identity, cancel):
        started = time.monotonic()
        for delay in (0, 2, 6, 14):
            if cancel.wait(max(0, started + delay - time.monotonic())) or self.closed:
                return
            active = self.active()
            if (not active or (active["instance"], active["request_id"]) != identity
                    or time.time() - self.recovery_started_at >= 30 or self.worker_healthy()):
                return
            # Contact only the operator-pinned existing listener. Its response
            # can activate an already configured launchd socket, but neither
            # 200 nor 401 is readiness, and there is no device wake primitive.
            with self.lock:
                self.recovery_attempts += 1
            try:
                request = urllib.request.Request(self.worker_origin + "/v1/status", method="GET")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                with opener.open(request, timeout=1) as response:
                    self.recovery_result = "listener_response" if response.status in {200, 401} else "listener_unready"
            except urllib.error.HTTPError as error:
                self.recovery_result = "listener_response" if error.code == 401 else "listener_unready"
            except (OSError, ValueError):
                self.recovery_result = "unreachable"
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
        self._schedule_health()
        active = self.active()
        with self.lock:
            worker = copy.deepcopy(self.worker)
        live_worker = bool(worker and 0 <= time.time() - worker["at"] <= 20)
        tv = json.loads(active["readiness"]) if active else {}
        live_tv = bool(active and 0 <= time.time() - active["tv_at"] <= 12)
        with self.lock:
            health_fresh = self.health_at > 0 and 0 <= time.monotonic() - self.health_at <= 15
            data, media = self.data_probe, self.probe
        gates = {"nas_api": True, "nas_data": health_fresh and data, "nas_media": health_fresh and media,
                 "link_worker": live_worker, "link_tv": live_tv, "qr": self.qr_ready(active)}
        gates.update({"worker_" + k: bool(live_worker and worker["capabilities"][k]) for k in sorted(CAPABILITIES)})
        gates.update({"tv_" + k: bool(live_tv and tv.get(k)) for k in ("playback", "audio", "media")})
        ready = bool(active and all(gates.values()))
        return {"state": "ready" if ready else "degraded" if active else "closed", "ready": ready,
                "gates": gates, "recovery": self.recovery()}

    def start(self, payload):
        if (set(payload) != {"instance", "request_id"} or any(not isinstance(v, str) or not UUID.fullmatch(v)
                                                             for v in payload.values())):
            raise self.error(400, "invalid_start")
        with self.store.lock:
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
                    db.execute("UPDATE coordinator_session SET instance=?,opened=?,tv_at=0,readiness='{}' WHERE id=1 AND instance!=?",
                               (payload["instance"], int(time.time()), payload["instance"]))
                    if payload["instance"] != active["instance"]:
                        db.execute("UPDATE rooms SET mic_muted=1,mic_request_at=0,control_revision=control_revision+1,mic_revision=mic_revision+1,last_command=max(last_command,coalesce((SELECT max(seq) FROM commands WHERE room=?),0)) WHERE id=?",
                                   (active["room"], active["room"]))
                    if payload["request_id"] != active["request_id"]:
                        db.execute("UPDATE coordinator_session SET opened=?,request_id=? WHERE id=1", (int(time.time()), payload["request_id"]))
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
                    db.execute("UPDATE rooms SET mic_muted=1,mic_request_at=0,control_revision=control_revision+1,mic_revision=mic_revision+1 WHERE id=?", (room,))
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
        if (set(payload) != {"instance", "playback", "audio", "media", "protocol"}
                or type(payload["protocol"]) is not int or payload["protocol"] != 1
                or any(type(payload[k]) is not bool for k in ("playback", "audio", "media"))):
            raise self.error(400, "invalid_tv_readiness")
        active = self._instance(payload["instance"])
        with self.store.connection() as db:
            db.execute("UPDATE coordinator_session SET tv_at=?,readiness=? WHERE id=1 AND instance=?",
                       (int(time.time()), json.dumps({k: payload[k] for k in ("playback", "audio", "media")}), active["instance"]))
        self._schedule_recovery(self.active())
        self.changed()
        return self.readiness()

    def party(self):
        active = self.active()
        if not active:
            return {"active": False, "readiness": self.readiness()}
        self.store.expire_unmute(active["room"])
        with self.store.connection() as db:
            room = dict(db.execute("SELECT * FROM rooms WHERE id=?", (active["room"],)).fetchone())
            ids = [r[0] for r in db.execute("SELECT id FROM songs WHERE room=? ORDER BY position,rowid LIMIT 80", (active["room"],))]
            now = int(time.time())
            commands = [dict(r) for r in db.execute("SELECT seq,action,song,created FROM commands WHERE room=? AND seq>? AND created>? AND created<=? ORDER BY seq LIMIT 20",
                                                   (active["room"], room["last_command"], now - 20, now))]
        commands = [dict(r, **({"expires": r["created"] + 5} if r["action"].startswith("sfx_") else {})) for r in commands
                    if not r["action"].startswith("sfx_") or r["created"] + 5 > now]
        return {"active": True, "persistent": True, "household_id": self.household_id, "room_id": active["room"], "expires": room["expires"],
                "join_url": self.store.origin + self.store.base_path + "/#" + urllib.parse.urlencode({"r": active["room"], "k": active["secret"]}),
                "theme": room["theme"], "theme_revision": room["theme_revision"],
                "controls": {k: room[k] for k in ("music_gain", "mic_gain", "mic_muted", "original", "control_revision", "mic_revision", "vocal_preset", "sfx_gain")},
                "commands": commands, "order": ids, "readiness": self.readiness()}

    def playback(self, payload, instance):
        active = self._instance(instance)
        value = dict(payload)
        if value.pop("room_id", None) != active["room"]:
            raise self.error(409, "room_changed")
        result = self.store.playback(active["room"], value)
        with self.store.connection() as db:
            db.execute("UPDATE coordinator_session SET tv_at=? WHERE id=1 AND instance=?", (int(time.time()), instance))
        self.changed()
        return result

    def tv_controls(self, payload, instance):
        active = self._instance(instance)
        value = dict(payload)
        if value.pop("room_id", None) != active["room"]:
            raise self.error(409, "room_changed")
        return self.store.tv_controls(active["room"], value)

    def close_party(self, instance, payload):
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
                    and 32 <= identity[2] <= 131072)

    def qr(self, payload):
        fields = {"room_id", "instance", "join_url", "sha256", "png"}
        active = self.active()
        if (set(payload) != fields or not active or payload["instance"] != active["instance"]
                or payload["room_id"] != active["room"] or not isinstance(payload["png"], str)
                or len(payload["png"]) > 180000 or payload["join_url"] != self.party()["join_url"]
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
            os.replace(temporary, target)
            metadata = {"sha256": payload["sha256"], "identity": list(self._identity(target))}
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
        return {"service": "friday-ktv-coordinator", "protocol": 2, "coordinator": True,
                "party": self.party(), "readiness": self.readiness(), "cursor": self.cursor}

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
        allowed = {"url", "sha256", "bytes", "duration_seconds", "sample_rate", "channels", "encoding"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise self.error(400, "invalid_manifest_media")
        if (not isinstance(value.get("sha256"), str) or not SHA.fullmatch(value["sha256"])
                or type(value.get("bytes")) is not int or not 48 <= value["bytes"] <= (134217728 if kind == "video" else MEDIA_LIMIT)
                or not fixed_media_path(value.get("url"), kind)):
            raise self.error(400, "invalid_manifest_media")
        if kind == "video":
            if (value.get("encoding") not in {"h264_mp4_video_only", "h264_mp4_silent_clock"}
                    or type(value.get("duration_seconds")) not in {int, float}
                    or not math.isfinite(value["duration_seconds"]) or not 0 < value["duration_seconds"] <= 1800):
                raise self.error(400, "invalid_manifest_media")
        elif (value.get("sample_rate") != 48000 or value.get("channels") != 2 or value.get("encoding") != "pcm_s16le_wav"
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

    def manifest(self, payload):
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
        # Only fields consumed by the TV cross this boundary. No source account,
        # filesystem paths, arbitrary URLs, worker internals or exception strings.
        clean = {k: copy.deepcopy(job[k]) for k in ("id", "song_id", "title", "state", "code", "created", "progress", "requester", "audio_origin", "simulated", "source") if k in job}
        clean["media"] = media
        background = job.get('background',{})
        if (isinstance(background,dict) and background.get('status') == 'ready' and background.get('encoding') == 'png'
                and isinstance(background.get('sha256'),str) and SHA.fullmatch(background['sha256'])
                and type(background.get('bytes')) is int and 33 <= background['bytes'] <= 12*1024*1024
                and fixed_media_path(background.get('url'),'background')):
            clean['background'] = {k:background[k] for k in ('status','encoding','sha256','bytes','url')}
        video = job.get("video", {})
        if isinstance(video, dict) and video.get("status") == "ready":
            spec = self._media_spec({k: v for k, v in video.items() if k in {"url", "sha256", "bytes", "duration_seconds", "sample_rate", "channels", "encoding"}}, "video", timeline["duration_ms"])
            clean["video"] = {**spec, **self._video_metadata(video, media["original"]["sha256"]), "status": "ready"}
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
            db.execute("UPDATE songs SET state='ready',code='none' WHERE id=? AND state!='cancelled'", (payload["id"],))
            if getattr(self.store, "management", None):
                published = db.execute("SELECT * FROM coordinator_manifests WHERE song=?", (payload["id"],)).fetchone()
                self.store.management.remember(published, db)
        self.changed()
        self._queue_cache(payload["id"])
        return {"accepted": True, "version": payload["version"], "archive": self.archive_state(payload["id"])}

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
        self.cache_pool.submit(self._verify_cached, dict(spec), repair)

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
                    db.execute("UPDATE coordinator_cache SET state='ready',device=?,inode=?,mtime_ns=?,ctime_ns=? WHERE sha=?",
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

    def archive_state(self, song):
        row = self._row(song)
        if not row:
            return "unavailable"
        states = []
        valid = True
        with self.store.connection() as db:
            for spec in self._specs(row).values():
                found = db.execute("SELECT state FROM coordinator_cache WHERE sha=?", (spec["sha256"],)).fetchone()
                states.append(found[0] if found else "pending")
        for spec in self._specs(row).values():
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
        if not active:
            return {"jobs": []}
        with self.store.connection() as db:
            songs = [dict(r) for r in db.execute("SELECT * FROM songs WHERE room=? AND removed=0 ORDER BY position,rowid LIMIT 80", (active["room"],))]
        jobs = []
        for song in songs:
            row = self._row(song["id"])
            if not row:
                pending = {k: song[k] for k in ("id", "song_id", "title", "state", "code", "created", "source")}
                if pending["state"] == "ready":
                    pending.update(state="transcoding", code="none")
                jobs.append(pending)
                continue
            job = json.loads(row["job"])
            job.update(version=row["version"], timeline=json.loads(row["timeline"]), archive=self.archive_state(song["id"]))
            for kind, spec in self._specs(row).items():
                if kind.startswith("guide_"):
                    continue
                sources = []
                if self.cached_file(spec):
                    sources.append({"url": "/v1/jobs/" + song["id"] + "/media/" + kind})
                if row["expires"] > time.time() + 5 and self.worker_media_ready():
                    sources.append({"origin": row["origin"], "url": spec["url"], "header": "X-KTV-Media",
                                    "credential": row["ticket"], "expires": row["expires"]})
                public = dict(spec, sources=sources)
                # Relative url remains the local cache fallback for old clients;
                # clients using sources never wait for it to become ready.
                public["url"] = "/v1/jobs/" + song["id"] + "/media/" + kind
                if kind == "video":
                    job["video"] = public
                elif kind == 'background':
                    job['background'] = public
                else:
                    job["media"][kind] = public
            if any(not spec.get("sources") for spec in job["media"].values()):
                job.update(state="transcoding", code="none")
            jobs.append(job)
        return {"jobs": jobs}

    def lyrics(self, song):
        row = self._row(song)
        if not row:
            raise self.error(404, "not_found")
        return json.loads(row["lyrics"])

    def media_file(self, song, kind):
        row = self._row(song)
        if not row or kind not in self._specs(row):
            raise self.error(404, "not_found")
        spec = self._specs(row)[kind]
        path = self.cached_file(spec)
        if path is None:
            raise self.error(503, "media_cache_pending")
        return path, spec

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
            with self.lock:
                if spec["sha256"] in self.cache_pending or spec["sha256"] in self.verify_pending:
                    continue
                self.cache_pending.add(spec["sha256"])
            with self.store.connection() as db:
                old = db.execute("SELECT updated,attempts FROM coordinator_cache WHERE sha=?", (spec["sha256"],)).fetchone()
                if old and (old["attempts"] >= 4 or time.time() - old["updated"] < 30):
                    with self.lock:
                        self.cache_pending.discard(spec["sha256"])
                    continue
                db.execute("INSERT OR REPLACE INTO coordinator_cache(sha,bytes,state,updated,attempts) VALUES(?,?,'pending',?,?)", (spec["sha256"], spec["bytes"], int(time.time()), (old["attempts"] if old else 0) + 1))
            self.cache_pool.submit(self._cache_one, row, kind, spec)

    def _cache_one(self, row, kind, spec):
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
                with wave.open(temporary, "rb") as audio:
                    if audio.getnchannels() != 2 or audio.getframerate() != 48000 or audio.getsampwidth() != 2:
                        raise ValueError("media_contract_failed")
                    if abs(audio.getnframes() / 48 - json.loads(row["timeline"])["duration_ms"]) > 150:
                        raise ValueError("media_timeline_failed")
            with manager.lock if manager else self.lock:
                with self.store.connection() as db:
                    current = db.execute("SELECT job FROM coordinator_manifests WHERE song=?", (row["song"],)).fetchone()
                expected = self._specs(current).get(kind) if current else None
                # Serialize identity commits with owner deletion and eviction.
                if not expected or expected["sha256"] != spec["sha256"] or expected["bytes"] != spec["bytes"]:
                    return
                os.chmod(temporary, 0o400)
                os.replace(temporary, self.media_root / spec["sha256"])
                temporary = None
                state = "ready"
        except Exception:
            # Fixed state only; upstream URLs/tickets/errors are never logged.
            pass
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
            with self.store.connection() as db:
                identity = self._identity(self.media_root / spec["sha256"]) if state == "ready" else None
                if identity is None:
                    state = "blocked" if state == "blocked" else "failed"
                    db.execute("UPDATE coordinator_cache SET state=?,updated=?,attempts=CASE WHEN ?='blocked' THEN 0 ELSE attempts END WHERE sha=?", (state, int(time.time()), state, spec["sha256"]))
                else:
                    db.execute("UPDATE coordinator_cache SET state='ready',updated=?,device=?,inode=?,mtime_ns=?,ctime_ns=? WHERE sha=?",
                               (int(time.time()), identity[0], identity[1], identity[3], identity[4], spec["sha256"]))
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
        self.closed = True
        self.recovery_cancel.set()
        self.changed()
        self.cache_pool.shutdown(wait=False)
        self.recovery_pool.shutdown(wait=False)
        self.health_pool.shutdown(wait=False)
        if getattr(self.store, "management", None):
            self.store.management.close()
