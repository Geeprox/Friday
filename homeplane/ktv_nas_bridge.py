"""Outbound-only bridge from private Mac KTV jobs to the user's NAS guest station."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from .ktv_service import ERRORS, JobRequest, atomic_json, private_directory

VOCAL_PRESETS = {"dry", "pop", "ktv", "power", "opera", "studio", "echo"}
SFX_ACTIONS = {"sfx_applause", "sfx_cheer", "sfx_stop"}
SFX_TTL = 5
AUDIO_PLAYBACK_FIELDS = {"audio_capabilities", "vocal_preset", "vocal_preset_state",
                         "sfx_seq", "sfx_action", "sfx_state"}


# Kept aligned with the standalone NAS schema; neither side accepts vendor strings.
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


def command_is_fresh(command, now):
    if type(command.get("created")) is not int:
        return False
    created = command["created"]
    if command.get("action") in SFX_ACTIONS:
        return (command.get("song") == "" and type(command.get("expires")) is int
                and command["expires"] == created + SFX_TTL and created <= now < command["expires"])
    return created <= now < created + 20


class NasBridge:
    def __init__(self, queue, config_path: Path, *, allow_loopback=False, cloud_catalog=None, guide=None, cloud_lyrics=None):
        path = config_path.absolute()
        if path.resolve() != path or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError("private_public_config_required")
        value = json.loads(path.read_text())
        endpoint = value["endpoint"].rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or (
                parsed.scheme != "https"
                and not (allow_loopback and parsed.scheme == "http" and parsed.hostname == "127.0.0.1")
            )
            or not re.fullmatch(r"/[a-z0-9_-]+", parsed.path)
        ):
            raise ValueError("https_public_endpoint_required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,100}", value["agent_key"]):
            raise ValueError("invalid_agent_key")
        self.queue, self.endpoint, self.key = queue, endpoint, value["agent_key"]
        # Reuse TLS/HTTP connections across the short control polls. The pool
        # leaves capacity for the independent media and catalog workers.
        self.http = httpx.Client(timeout=8, follow_redirects=False,
                                 limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
        self.path = private_directory(path.parent) / "nas-bridge-state.json"
        self.mapping, self.party = {}, None
        self.cloud_lyrics = cloud_lyrics
        self.lyric_prepared = set()
        if self.path.is_file() and not self.path.is_symlink() and self.path.stat().st_size < 65536:
            state = json.loads(self.path.read_text())
            self.mapping, self.party = state.get("mapping", {}), state.get("party")
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.thread = None
        self.worker_thread = None
        self.poll_lock, self.work_run_lock = threading.Lock(), threading.Lock()
        self.persist_lock, self.room_operation_lock = threading.Lock(), threading.Lock()
        self.work_wake = threading.Event()
        self.background = False
        self.room_generation = 0
        self.pending_tasks, self.pending_cancelled = {}, set()
        self.touch_pending = ""
        self.remote_order = []
        self.online = False
        self._persisted = None
        self.playback = None
        self.playback_received = 0
        self.cloud_catalog = cloud_catalog
        self.catalog_thread = None
        self.catalog_revision = ""
        self.catalog_checked = 0
        self.guide = guide
        self.guide_acked = set()
        # Terminal reports already visible on NAS must not overwrite a user's
        # retry between task polls. A retry receives a different local job ID.
        self.terminal_acked = set()
        for local in self.mapping.values():
            try:
                if queue.get(local)["state"] in {"ready", "failed", "cancelled"}:
                    self.terminal_acked.add(local)
            except Exception:
                pass

    def request(self, path, body=None):
        if not re.fullmatch(r"/agent/(tasks|updates|cloud|guides|rooms|rooms/[a-f0-9-]{36}/(close|playback|controls))", path):
            raise ValueError("invalid_public_operation")
        with self.http.stream(
            "POST" if body is not None else "GET",
            self.endpoint + path,
            headers={"Authorization": "Bearer " + self.key},
            json=body,
            timeout=8,
            follow_redirects=False,
        ) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError("nas_unavailable")
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > 65536:
                    raise ValueError("public_response_exceeded")
                data.extend(chunk)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("invalid_public_response")
        return value

    def _save(self):
        # Serialize disk commits, but never hold the snapshot lock over file I/O.
        with self.persist_lock:
            with self.lock:
                state = copy.deepcopy({"mapping": self.mapping, "party": self.party})
            encoded = json.dumps(state, sort_keys=True, ensure_ascii=False)
            if encoded != self._persisted:
                atomic_json(self.path, state)
                self._persisted = encoded

    def _same_room(self, generation):
        with self.lock:
            return not self.stop.is_set() and generation == self.room_generation

    def tick(self):
        # The control lane does no queue, media, lyrics, or disk I/O. The public
        # method remains synchronous before start() for callers and contract tests.
        with self.poll_lock:
            with self.lock:
                generation = self.room_generation
            value = self.request("/agent/tasks")
            tasks, cancelled, rooms = value.get("tasks", []), value.get("cancelled", []), value.get("rooms", [])
            if (not isinstance(tasks, list) or len(tasks) > 8
                    or not isinstance(cancelled, list) or len(cancelled) > 80
                    or any(not isinstance(i, str) for i in cancelled)
                    or not isinstance(rooms, list) or len(rooms) > 8):
                raise ValueError("invalid_public_response")
            with self.lock:
                if not self._same_room(generation):
                    return
                self.online = True
                for room in rooms:
                    if not isinstance(room, dict) or not self.party or room.get("room_id") != self.party["room_id"]:
                        continue
                    if (type(room.get("expires")) is int and time.time() < room["expires"]
                            <= (10**12 if room.get("persistent") is True else time.time() + 28800)):
                        self.party["expires"] = room["expires"]
                        self.party["persistent"] = room.get("persistent") is True
                    if room.get("theme") in {"content", "festival", "live"}:
                        self.party.update(theme=room["theme"], theme_revision=room.get("theme_revision", 0))
                    controls = {k: room.get(k) for k in ("music_gain", "mic_gain", "mic_muted", "original", "control_revision", "mic_revision")}
                    if (all(type(v) is int and v >= 0 for v in controls.values())
                            and controls["music_gain"] <= 100 and controls["mic_gain"] <= 100
                            and controls["mic_muted"] <= 1 and controls["original"] <= 1):
                        preset, gain = room.get("vocal_preset", "dry"), room.get("sfx_gain", 60)
                        if not isinstance(preset, str) or preset not in VOCAL_PRESETS or type(gain) is not int or not 0 <= gain <= 100:
                            raise ValueError("invalid_audio_controls")
                        controls.update(vocal_preset=preset, sfx_gain=gain)
                        if controls["control_revision"] >= self.party.get("controls", {}).get("control_revision", 0):
                            self.party["controls"] = controls
                    order = room.get("order", [])
                    if isinstance(order, list) and len(order) <= 80 and all(isinstance(i, str) for i in order):
                        self.remote_order = list(order)
                        self.party["order"] = [self.mapping[i] for i in order if i in self.mapping]
                    commands = []
                    raw_commands = room.get("commands", [])
                    if not isinstance(raw_commands, list):
                        raise ValueError("invalid_public_response")
                    for command in raw_commands[:20]:
                        if (isinstance(command, dict) and type(command.get("seq")) is int
                                and 0 < command["seq"] <= 10**12
                                and isinstance(command.get("action"), str)
                                and command["action"] in {"next", "play", "pause", "resume"} | SFX_ACTIONS
                                and not set(command) - {"seq", "action", "song", "created", "expires"}
                                and isinstance(command.get("song"), str)
                                and command["song"] in (set(self.mapping) | {""})
                                and command_is_fresh(command, time.time())):
                            commands.append({**command, "song": self.mapping.get(command["song"], "")})
                    self.party["commands"] = commands
                # Leased tasks may be absent from subsequent polls: retain them
                # until the work lane consumes them, rather than replacing a batch.
                for item in tasks:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        self.pending_tasks[item["id"]] = item
                self.pending_cancelled.update(cancelled)
            if self.background:
                self.work_wake.set()
            else:
                self._work_once()
            self._push_playback(generation)

    def _push_playback(self, generation):
        with self.lock:
            if (not self._same_room(generation) or not self.party or not self.playback
                    or time.monotonic() - self.playback_received >= 6):
                return
            room_id = self.party["room_id"]
            inverse = {v: k for k, v in self.mapping.items()}
            report = copy.deepcopy(self.playback)
            report.update(current=inverse.get(report["current"], ""),
                          played=[inverse[i] for i in report["played"] if i in inverse][:80],
                          lyrics_waiting=inverse.get(report.get("lyrics_waiting", ""), ""))
            report["position_ms"] = min(1800000, report.get("position_ms", 0) +
                (round((time.monotonic() - self.playback_received) * 1000) if report["state"] == "playing" else 0))
        if self._same_room(generation):
            # The URL is bound to the captured room, including across close/create.
            self.request("/agent/rooms/" + room_id + "/playback", report)

    def _work_once(self):
        with self.work_run_lock:
            with self.lock:
                generation = self.room_generation
                tasks, cancelled = list(self.pending_tasks.values()), set(self.pending_cancelled)
                self.pending_tasks.clear()
                self.pending_cancelled.clear()
                touch, self.touch_pending = self.touch_pending, ""
            for item in tasks:
                if not self._same_room(generation):
                    return
                if (not {"id", "song_id", "title"} <= set(item)
                        or set(item) - {"id", "song_id", "title", "requester", "attempt", "source"}
                        or item.get("source", "song") not in {"song", "cloud", "mv"}):
                    continue
                try:
                    request = JobRequest(song_id=item["song_id"], title=item["title"],
                        request_id=item["id"] if not item.get("attempt") else str(uuid.uuid5(uuid.UUID(item["id"]), "retry:" + str(item["attempt"]))),
                        requester=item.get("requester"), source_preference={"cloud": "cloud", "mv": "mv_direct"}.get(item.get("source"), "song"))
                    job = self.queue.submit(request)
                    with self.lock:
                        if not self._same_room(generation):
                            return
                        self.mapping[item["id"]] = job["id"]
                        if self.party:
                            self.party["order"] = [self.mapping[i] for i in self.remote_order if i in self.mapping]
                except Exception:
                    if self._same_room(generation):
                        self.request("/agent/updates", {"updates": [{"id": item.get("id", ""), "state": "failed", "code": "internal_error"}]})
            with self.lock:
                if not self._same_room(generation):
                    return
                mapping = dict(self.mapping)
                order = list(self.party.get("order", [])) if self.party else []
                report = copy.deepcopy(self.playback or {})
            for remote in cancelled:
                if not self._same_room(generation):
                    return
                if remote in mapping:
                    try:
                        self.queue.cancel(mapping[remote])
                    except Exception:
                        pass
            if touch and hasattr(self.queue, "touch"):
                self.queue.touch(touch)
            if hasattr(self.queue, "prioritize") and self._same_room(generation):
                self.queue.prioritize(order)
            guide_targets = [report["current"]] if report.get("current") else []
            guide_targets += [i for i in order if i not in set(report.get("played", [])) and i not in guide_targets]
            guide_targets = set(guide_targets[:2])
            if self.guide is not None:
                self.guide.protect(guide_targets)
            updates = []
            for remote, local in mapping.items():
                if not self._same_room(generation):
                    return
                try:
                    state = self.queue.get(local)
                except Exception:
                    state = {"state": "cancelled", "code": "cancelled"}
                # A guide/network failure must not cancel a successfully prepared song.
                try:
                    if (self.cloud_lyrics is not None and local in guide_targets and state["state"] == "ready"
                            and state.get("audio_origin") == "cloud" and local not in self.lyric_prepared):
                        self.cloud_lyrics(state["song_id"])
                        self.lyric_prepared.add(local)
                    if self.guide is not None and local in guide_targets and state["state"] == "ready" and remote not in self.guide_acked:
                        media = self.guide.get(local)
                        if media and self._same_room(generation):
                            self._publish_guide(remote, media, generation)
                except Exception:
                    pass
                if state["state"] in {"ready", "failed", "cancelled"} and local in self.terminal_acked:
                    continue
                updates.append({"id": remote, "state": state["state"],
                    "code": state["code"] if state["code"] in ERRORS | {"none"} else "internal_error",
                    "progress": state.get("progress", {}), "cache_hit": state.get("cache_hit", False)})
                if len(updates) == 32:
                    self._send_updates(updates, mapping, generation)
                    updates = []
            if updates:
                self._send_updates(updates, mapping, generation)
            with self.lock:
                if not self._same_room(generation):
                    return
                if len(self.mapping) > 128:
                    self.mapping = dict(list(self.mapping.items())[-128:])
                self.terminal_acked.intersection_update(self.mapping.values())
            self._save()

    def _publish_guide(self, remote, media, generation):
        if self._same_room(generation):
            self.request("/agent/guides", {"id": remote, **media})
            self.guide_acked.add(remote)

    def _send_updates(self, updates, mapping, generation):
        if not self._same_room(generation):
            return
        self.request("/agent/updates", {"updates": updates})
        with self.lock:
            if self._same_room(generation):
                self.terminal_acked.update(mapping[u["id"]] for u in updates
                    if u["state"] in {"ready", "failed", "cancelled"} and self.mapping.get(u["id"]) == mapping[u["id"]])

    def start(self):
        if self.thread is not None:
            return
        self.background = True

        def controls():
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    self.tick()
                    self.refresh_cloud()
                except Exception:
                    with self.lock:
                        self.online = False
                self.stop.wait(max(0.01, 0.15 - (time.monotonic() - started)))

        def work():
            while not self.stop.is_set():
                self.work_wake.wait(0.5)
                self.work_wake.clear()
                started = time.monotonic()
                try:
                    self._work_once()
                except Exception:
                    pass
                self.stop.wait(max(0, 0.5 - (time.monotonic() - started)))

        self.worker_thread = threading.Thread(target=work, name="ktv-nas-work", daemon=True)
        self.thread = threading.Thread(target=controls, name="ktv-nas-control", daemon=True)
        self.worker_thread.start()
        self.thread.start()

    def refresh_cloud(self):
        # Catalog network I/O never holds the control/queue polling thread.
        if (self.cloud_catalog is None or time.monotonic() - self.catalog_checked < 300
                or (self.catalog_thread is not None and self.catalog_thread.is_alive())):
            return
        self.catalog_checked = time.monotonic()
        def sync():
            try:
                rows = self.cloud_catalog()
                revision = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                for offset in range(0, max(1, len(rows)), 30):
                    if self.stop.is_set():
                        return
                    self.request("/agent/cloud", {"revision": revision, "total": len(rows),
                        "offset": offset, "songs": rows[offset:offset + 30]})
                self.catalog_revision = revision
            except Exception:
                pass  # Keep the last complete NAS index; never expose credentials/errors.
        self.catalog_thread = threading.Thread(target=sync, name="ktv-cloud-index", daemon=True)
        self.catalog_thread.start()

    def close(self):
        self.stop.set()
        self.work_wake.set()
        if self.thread is not None:
            self.thread.join(timeout=12)
        if self.worker_thread is not None:
            self.worker_thread.join(timeout=12)
        if self.catalog_thread is not None:
            self.catalog_thread.join(timeout=12)
        if self.guide is not None:
            self.guide.close()
        self.http.close()

    def current(self):
        with self.lock:
            if self.party and self.party.get("expires", 0) > time.time():
                # Filter again at delivery: an old in-memory/disk room snapshot must not replay SFX.
                commands = [c for c in self.party.get("commands", []) if isinstance(c, dict)
                            and isinstance(c.get("action"), str) and c["action"] in {"next", "play", "pause", "resume"} | SFX_ACTIONS
                            and command_is_fresh(c, time.time())]
                return {**copy.deepcopy(self.party), "commands": copy.deepcopy(commands), "active": True, "nas_online": self.online}
            return {"active": False, "nas_online": self.online}

    def report_playback(self, value):
        required = {
            "current",
            "played",
            "state",
            "last_command",
            "safety_latched",
            "applied_revision",
            "room_id",
            "mic_muted",
        }
        if (
            not required <= set(value) or set(value) - required - {"position_ms", "lyrics_waiting", "playback_epoch"} - AUDIO_PLAYBACK_FIELDS
            or ("playback_epoch" in value and (not isinstance(value["playback_epoch"], str)
                    or not re.fullmatch(r"[a-f0-9-]{36}", value["playback_epoch"])))
            or not isinstance(value.get("lyrics_waiting", ""), str)
            or (value.get("lyrics_waiting") and not re.fullmatch(r"[a-f0-9-]{36}", value["lyrics_waiting"]))
            or not isinstance(value["current"], str)
            or value["state"] not in ("idle", "playing", "paused", "preparing")
            or not isinstance(value["played"], list)
            or len(value["played"]) > 80
            or any(not isinstance(i, str) or not re.fullmatch(r"[a-f0-9-]{36}", i) for i in value["played"])
            or (value["current"] and not re.fullmatch(r"[a-f0-9-]{36}", value["current"]))
            or any(
                type(value[k]) is not int or not 0 <= value[k] <= 10**12
                for k in ("last_command", "applied_revision")
            )
            or type(value["safety_latched"]) is not bool
            or type(value["mic_muted"]) is not bool
            or type(value.get("position_ms", 0)) is not int or not 0 <= value.get("position_ms", 0) <= 1800000
        ):
            raise ValueError("invalid_playback")
        audio = audio_playback(value)
        with self.lock:
            if not self.party or value["room_id"] != self.party["room_id"]:
                raise ValueError("room_changed")
            self.playback = {k: v for k, v in value.items() if k != "room_id"}
            self.playback.update({k: audio[k] for k in AUDIO_PLAYBACK_FIELDS if k in value})
            self.playback_received = time.monotonic()
            self.touch_pending = value["current"]
        return {"accepted": True}

    def control_from_tv(self, value):
        if (not isinstance(value, dict) or "room_id" not in value
                or not 2 <= len(value) <= 4
                or set(value) - {"room_id", "original", "mic_muted", "vocal_preset"}
                or any(type(v) is not int or v not in (0, 1)
                       for k, v in value.items() if k in {"original", "mic_muted"})
                or ("vocal_preset" in value and (not isinstance(value["vocal_preset"], str)
                                                  or value["vocal_preset"] not in VOCAL_PRESETS))):
            raise ValueError("invalid_control")
        with self.lock:
            if not self.current()["active"] or value["room_id"] != self.party["room_id"]:
                raise ValueError("room_changed")
            room_id = self.party["room_id"]
            generation = self.room_generation
        if value.get("vocal_preset", "dry") != "dry":
            # A local audition first reports native readiness. Forward that fresh
            # report before the selection so the NAS checks the same capability
            # snapshot instead of waiting for a later background heartbeat.
            self._push_playback(generation)
        return self.request("/agent/rooms/" + room_id + "/controls",
                            {k: v for k, v in value.items() if k != "room_id"})

    def create_party(self):
        with self.room_operation_lock:
            current = self.current()
            if current["active"]:
                return current
            value = self.request("/agent/rooms", {"ttl": 14400})
            if (not re.fullmatch(r"[a-f0-9-]{36}", value.get("room_id", ""))
                    or type(value.get("expires")) is not int
                    or not time.time() < value["expires"] <= (10**12 if value.get("persistent") is True else time.time() + 28800)
                    or not value.get("join_url", "").startswith(self.endpoint + "/#")):
                raise ValueError("invalid_party")
            fragment = parse_qs(urlsplit(value["join_url"]).fragment)
            if fragment.get("r") != [value["room_id"]] or not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", fragment.get("k", [""])[0]):
                raise ValueError("invalid_party")
            with self.lock:
                self.room_generation += 1
                self.party = {k: value[k] for k in ("room_id", "join_url", "expires")}
                self.party["persistent"] = value.get("persistent") is True
                self.party.update(theme=value.get("theme", "content"), theme_revision=value.get("theme_revision", 0), order=[], commands=[])
                self.playback = None
                self.remote_order = []
                self.pending_tasks.clear()
                self.pending_cancelled.clear()
                self.touch_pending = ""
            self._save()
            return self.current()

    def close_party(self):
        with self.room_operation_lock:
            with self.lock:
                room_id = self.party["room_id"] if self.party else ""
            if room_id:
                self.request("/agent/rooms/" + room_id + "/close", {})
            with self.lock:
                self.room_generation += 1
                self.party = None
                self.playback = None
                self.remote_order = []
                self.pending_tasks.clear()
                self.pending_cancelled.clear()
                self.touch_pending = ""
            self._save()
            return {"closed": True}

    def qr(self):
        party = self.current()
        if not party["active"]:
            raise ValueError("party_unavailable")
        import qrcode
        from qrcode.image.pure import PyPNGImage

        image = qrcode.make(party["join_url"], image_factory=PyPNGImage, box_size=8, border=4)
        result = io.BytesIO()
        image.save(result)
        return result.getvalue()
