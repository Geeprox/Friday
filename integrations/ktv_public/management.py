"""Owner management for managed media only. Stdlib / Python 3.8 NAS compatible.

The authenticated worker is the only caller. Room/guest and TV credentials never
authorize storage changes. This module cannot start a TV or an audio device.
"""
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid

ID = re.compile(r"[1-9][0-9]{0,17}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
DEFAULT_POLICY = {"enabled": False, "limit_bytes": 100 * 1024**3}
DEFAULTS = {"theme": "content", "music_gain": 70, "mic_gain": 50, "sfx_gain": 60, "original": 0, "vocal_preset": "dry"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Management:
    def __init__(self, store):
        self.store, self.coordinator, self.error = store, store.coordinator, store.coordinator.error
        self.stop = threading.Event()
        self.thread = None
        self.reserved = {}
        self.lock = threading.RLock()
        self.deleting = set()
        self.last_cleanup = {"state": "not_run", "removed_bytes": 0, "removed": 0}
        with store.connection() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS managed_media(song_id TEXT NOT NULL,sha TEXT NOT NULL,
                title TEXT NOT NULL,bytes INTEGER NOT NULL,last_used INTEGER NOT NULL,PRIMARY KEY(song_id,sha));
              CREATE TABLE IF NOT EXISTS owner_settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS owner_receipts(request_id TEXT PRIMARY KEY,body_sha TEXT NOT NULL,
                result TEXT NOT NULL,created INTEGER NOT NULL);
            """)
            # Retain song/content associations separately from expiring scene metadata.
            for row in db.execute("SELECT * FROM coordinator_manifests").fetchall():
                self.remember(row, db)

    def remember(self, row, db):
        job = json.loads(row["job"])
        for spec in self.coordinator._specs(row).values():
            db.execute("INSERT INTO managed_media VALUES(?,?,?,?,?) ON CONFLICT(song_id,sha) DO UPDATE SET title=excluded.title,bytes=excluded.bytes,last_used=max(last_used,excluded.last_used)",
                       (job["song_id"], spec["sha256"], job["title"], spec["bytes"], row["updated"]))

    def touch(self, song_id):
        with self.store.connection() as db:
            db.execute("UPDATE managed_media SET last_used=? WHERE song_id=? AND last_used<?",
                       (int(time.time()), song_id, int(time.time()) - 60))

    def settings(self, name, default):
        with self.store.connection() as db:
            row = db.execute("SELECT value FROM owner_settings WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else dict(default)

    def save(self, name, value):
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO owner_settings VALUES(?,?)", (name, json.dumps(value)))

    def policy(self, value=None):
        if value is not None:
            if (set(value) != {"enabled", "limit_bytes"} or type(value["enabled"]) is not bool
                    or type(value["limit_bytes"]) is not int or not 2 * 1024**3 <= value["limit_bytes"] <= 100 * 1024**4):
                raise self.error(400, "invalid_policy")
            self.save("storage", value)
        return self.settings("storage", DEFAULT_POLICY)

    def preferences(self, value=None):
        current = self.settings("room", DEFAULTS)
        if value is not None:
            if (not value or set(value) - set(DEFAULTS)
                    or any(type(v) is not int or not 0 <= v <= (1 if k == "original" else 100)
                           for k, v in value.items() if k not in {"theme", "vocal_preset"})
                    or "theme" in value and (not isinstance(value["theme"], str) or value["theme"] not in {"content", "festival", "live"})
                    or "vocal_preset" in value and (not isinstance(value["vocal_preset"], str) or value["vocal_preset"] not in {"dry", "pop", "ktv", "power", "opera", "studio", "echo"})):
                raise self.error(400, "invalid_preferences")
            current.update(value)
            self.save("room", current)
        return current

    def protected(self):
        result = set()
        with self.store.lock, self.store.connection() as db:
            for room in db.execute("SELECT id,current_song,played FROM rooms WHERE closed=0"):
                played = set(json.loads(room["played"]))
                for row in db.execute("SELECT id,song_id,state FROM songs WHERE room=? AND removed=0", (room["id"],)):
                    if row["id"] == room["current_song"] or (row["id"] not in played and row["state"] not in {"failed", "cancelled"}):
                        result.add(row["song_id"])
            for row in db.execute("SELECT s.song_id FROM commands c JOIN songs s ON s.id=c.song JOIN rooms r ON r.id=c.room WHERE r.closed=0 AND c.action='play' AND c.seq>r.last_command AND c.created>?", (int(time.time()) - 20,)):
                result.add(row[0])
        return result

    def inventory(self):
        with self.lock:
            protected = self.protected()
            with self.store.connection() as db:
                links = [dict(r) for r in db.execute("SELECT * FROM managed_media")]
                records = {r["sha"]: dict(r) for r in db.execute("SELECT * FROM coordinator_cache")}
            groups, identities, unknown = {}, {}, 0
            for path in self.coordinator.media_root.iterdir():
                if not SHA.fullmatch(path.name):
                    # In-flight transfers / health probes are not eviction targets.
                    continue
                info = path.lstat()
                identities[path.name] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
                if not stat.S_ISREG(info.st_mode):
                    unknown += 1
            linked = set()
            for item in links:
                key = "song:" + item["song_id"]
                group = groups.setdefault(key, {"id": key, "title": item["title"], "last_used": 0, "hashes": set(), "kind": "song"})
                group["hashes"].add(item["sha"])
                group["last_used"] = max(group["last_used"], item["last_used"])
                linked.add(item["sha"])
            # Legacy orphan content remains visible after old scene metadata expired.
            for sha in identities:
                if sha not in linked:
                    item = records.get(sha)
                    groups["blob:" + sha] = {"id": "blob:" + sha, "title": "未关联歌曲的播放缓存", "last_used": item["updated"] if item else 0,
                                             "hashes": {sha}, "kind": "orphan", "unknown": item is None}
            entries = []
            for key, group in groups.items():
                files = {sha: identities[sha] for sha in group["hashes"] if sha in identities}
                unsafe = group.get("unknown", False) or any(not stat.S_ISREG((self.coordinator.media_root / sha).lstat().st_mode) for sha in files)
                with self.coordinator.lock:
                    transferring = bool(group["hashes"] & (self.coordinator.cache_pending | self.coordinator.verify_pending))
                in_queue = key[5:] in protected if key.startswith("song:") else False
                entries.append({"id": key, "title": group["title"], "last_used": group["last_used"], "kind": group["kind"],
                                "bytes": sum(v[2] for v in files.values()), "revision": fingerprint([sorted(group["hashes"]), files]),
                                "can_delete": not (in_queue or transferring or unsafe),
                                "reason": "in_queue" if in_queue else "transferring" if transferring else "unsafe" if unsafe else ""})
            known = {sha for group in groups.values() for sha in group["hashes"]}
            used = sum(info[2] for sha, info in identities.items() if sha in known)
            return {"available": True, "entries": sorted(entries, key=lambda r: r["last_used"], reverse=True), "bytes": used,
                    "policy": self.policy(), "protected_song_ids": sorted(protected), "unmanaged_entries": unknown,
                    "last_cleanup": dict(self.last_cleanup)}

    def _validate_items(self, value):
        if (not isinstance(value, list) or not 1 <= len(value) <= 128
                or any(not isinstance(v, dict) or set(v) != {"id", "revision"}
                       or not isinstance(v["id"], str) or not re.fullmatch(r"song:[1-9][0-9]{0,17}|blob:[a-f0-9]{64}", v["id"])
                       or not isinstance(v["revision"], str) or not SHA.fullmatch(v["revision"]) for v in value)
                or len({v["id"] for v in value}) != len(value)):
            raise self.error(400, "invalid_selection")

    def delete(self, payload):
        if (set(payload) != {"items", "request_id"} or not isinstance(payload["request_id"], str)
                or not UUID.fullmatch(payload["request_id"])):
            raise self.error(400, "invalid_selection")
        self._validate_items(payload["items"])
        body_sha = fingerprint(payload)
        with self.lock:
            with self.store.connection() as db:
                old = db.execute("SELECT * FROM owner_receipts WHERE request_id=?", (payload["request_id"],)).fetchone()
            if old:
                if old["body_sha"] != body_sha:
                    raise self.error(409, "idempotency_conflict")
                return json.loads(old["result"])
            rows = {r["id"]: r for r in self.inventory()["entries"]}
            for item in payload["items"]:
                current = rows.get(item["id"])
                if not current or current["revision"] != item["revision"]:
                    raise self.error(409, "cache_changed")
                if not current["can_delete"]:
                    raise self.error(409, "cache_in_use")
            selected, candidates = {v["id"] for v in payload["items"]}, set()
            with self.store.connection() as db:
                for key in selected:
                    if key.startswith("song:"):
                        candidates.update(r[0] for r in db.execute("SELECT sha FROM managed_media WHERE song_id=?", (key[5:],)))
                    else:
                        candidates.add(key[5:])
                keep = {r["sha"] for r in db.execute("SELECT song_id,sha FROM managed_media") if "song:" + r["song_id"] not in selected}
            selected_songs = {key[5:] for key in selected if key.startswith("song:")}
            with self.store.lock:
                if selected_songs & self.protected():
                    raise self.error(409, "cache_in_use")
                self.deleting.update(selected_songs)
            try:
                # No SQLite transaction or room/control lock spans filesystem I/O.
                removed_bytes = 0
                for sha in candidates - keep:
                    path = self.coordinator.media_root / sha
                    if path.exists() or path.is_symlink():
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode):
                            raise self.error(409, "unsafe_cache")
                        path.unlink()
                        removed_bytes += info.st_size
                with self.store.connection() as db:
                    for sha in candidates - keep:
                        db.execute("DELETE FROM coordinator_cache WHERE sha=?", (sha,))
                    for sid in selected_songs:
                        db.execute("DELETE FROM coordinator_manifests WHERE song IN (SELECT id FROM songs WHERE song_id=?)", (sid,))
                        db.execute("DELETE FROM coordinator_manifest_versions WHERE song IN (SELECT id FROM songs WHERE song_id=?)", (sid,))
                        db.execute("DELETE FROM managed_media WHERE song_id=?", (sid,))
                    result = {"removed": len(selected), "removed_bytes": removed_bytes, "models_deleted": False}
                    db.execute("INSERT INTO owner_receipts VALUES(?,?,?,?)", (payload["request_id"], body_sha, json.dumps(result), int(time.time())))
                    db.execute("DELETE FROM owner_receipts WHERE request_id NOT IN (SELECT request_id FROM owner_receipts ORDER BY created DESC,rowid DESC LIMIT 128)")
            finally:
                with self.store.lock:
                    self.deleting.difference_update(selected_songs)
            self.coordinator.changed()
            return result

    def enforce(self, reserve=0):
        with self.lock:
            data, removed, total = self.inventory(), 0, 0
            if not data["policy"]["enabled"]:
                return {"state": "disabled", "removed_bytes": 0, "removed": 0}
            external = self.settings("external_storage", {"bytes": 0, "delegated": False})
            reserve += external["bytes"]
            limit = data["policy"]["limit_bytes"]
            if external["delegated"]:
                return {"state": "within_limit" if data["bytes"] + reserve <= limit else "protected_over_limit", "removed_bytes": 0, "removed": 0}
            for row in sorted(data["entries"], key=lambda r: (r["last_used"], r["id"])):
                if data["bytes"] + reserve <= limit:
                    break
                if not row["can_delete"]:
                    continue
                # Earlier deletion can change shared-content revisions. Re-inventory.
                current = next((r for r in self.inventory()["entries"] if r["id"] == row["id"]), None)
                if current and current["can_delete"]:
                    result = self.delete({"items": [{"id": current["id"], "revision": current["revision"]}], "request_id": str(uuid.uuid4())})
                    removed += result["removed"]
                    total += result["removed_bytes"]
                    data = self.inventory()
            state = "within_limit" if data["bytes"] + reserve <= limit else "protected_over_limit"
            self.last_cleanup = {"state": state, "removed_bytes": total, "removed": removed, "at": int(time.time())}
            return dict(self.last_cleanup)

    def reserve(self, spec):
        # Two download workers must account for one another's unfinished bytes.
        with self.lock:
            extra = sum(self.reserved.values()) + spec["bytes"]
            result = self.enforce(reserve=extra)
            if result["state"] == "protected_over_limit":
                return False
            free = os.statvfs(self.coordinator.media_root)
            if free.f_bavail * free.f_frsize < extra + 64 * 1024**2:
                return False
            self.reserved[spec["sha256"]] = spec["bytes"]
            return True

    def _room(self):
        active = self.coordinator.active()
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM rooms WHERE closed=0 " + ("AND id=?" if active else "") + " ORDER BY rowid DESC LIMIT 2", (active["room"],) if active else ()).fetchall()
        if len(rows) > 1:
            raise self.error(409, "ambiguous_room")
        return dict(rows[0]) if rows else None

    def room(self):
        room = self._room()
        return {"active": bool(room), "room_id": room["id"] if room else "", "defaults": self.preferences(),
                "status": self.store.status(room) if room else None}

    def room_action(self, operation, payload):
        if operation == "preferences":
            return self.preferences(payload)
        if operation == "search":
            if set(payload) != {"q", "source", "offset"}:
                raise self.error(400, "invalid_query")
            return self.store.search(payload["q"], payload["source"], payload["offset"])
        if set(payload) != {"room_id", "value"} or not isinstance(payload["value"], dict):
            raise self.error(400, "invalid_request")
        room = self._room()
        if not room or payload["room_id"] != room["id"]:
            raise self.error(409, "room_changed")
        value = payload["value"]
        if operation == "queue-remove":
            if (set(value) != {"ids", "revision"} or type(value["revision"]) is not int
                    or not isinstance(value["ids"], list) or not 1 <= len(value["ids"]) <= 80
                    or any(not isinstance(i, str) or not UUID.fullmatch(i) for i in value["ids"])
                    or len(set(value["ids"])) != len(value["ids"])):
                raise self.error(400, "invalid_selection")
            with self.store.lock, self.store.connection() as db:
                state = db.execute("SELECT current_song,queue_revision FROM rooms WHERE id=? AND closed=0", (room["id"],)).fetchone()
                ids = {r[0] for r in db.execute("SELECT id FROM songs WHERE room=? AND removed=0", (room["id"],))}
                if not state or state["queue_revision"] != value["revision"] or not set(value["ids"]) <= ids:
                    raise self.error(409, "queue_changed")
                if state["current_song"] in value["ids"]:
                    raise self.error(409, "song_playing")
                for song in value["ids"]:
                    db.execute("UPDATE songs SET removed=1,state='cancelled',code='cancelled' WHERE id=? AND room=?", (song, room["id"]))
                db.execute("UPDATE rooms SET queue_revision=queue_revision+1 WHERE id=?", (room["id"],))
            result = {"removed": len(value["ids"])}
        elif operation in {"theme", "controls", "command", "retry", "order", "songs"}:
            method = {"order": "reorder", "songs": "add"}.get(operation, operation)
            result = getattr(self.store, method)(room, value)
        else:
            raise self.error(404, "not_found")
        self.coordinator.changed()
        return result

    def dispatch(self, payload):
        if set(payload) != {"operation", "value"} or not isinstance(payload["operation"], str) or not isinstance(payload["value"], dict):
            raise self.error(400, "invalid_request")
        op, value = payload["operation"], payload["value"]
        if op == "library" and not value:
            return self.inventory()
        if op == "delete":
            return self.delete(value)
        if op == "policy":
            return self.policy(value or None)
        if op == "room" and not value:
            return self.room()
        if op == "external-usage":
            if (set(value) != {"bytes", "delegated"} or type(value["bytes"]) is not int or value["bytes"] < 0
                    or type(value["delegated"]) is not bool):
                raise self.error(400, "invalid_policy")
            self.save("external_storage", value)
            return {"accepted": True}
        return self.room_action(op, value)

    def start(self):
        def run():
            while not self.stop.wait(30):
                try:
                    self.enforce()
                except Exception:
                    self.last_cleanup = {"state": "unavailable", "removed_bytes": 0, "removed": 0}
        self.thread = threading.Thread(target=run, name="ktv-storage", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)
