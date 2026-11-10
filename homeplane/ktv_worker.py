"""NAS-coordinated preparation worker and narrowly scoped LAN media tickets.

No controller command is relayed through this worker. Credentials remain local;
the NAS/TV receives only expiring read access to one immutable prepared job.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import io
import ipaddress
import json
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

from .ktv_nas_bridge import NasBridge
from .ktv_service import atomic_json, private_directory

UUID = re.compile(r"[a-f0-9-]{36}")
TICKET = re.compile(r"([0-9]{10})\.([A-Za-z0-9_-]{43})")
CAPABILITIES = ("source", "model", "download", "lyrics", "media")


def media_origin(value, *, allow_loopback=False):
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        private = address.version == 4 and any(address in network for network in (
            ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16")))
        valid = private or allow_loopback and address.is_loopback
        port = parsed.port
    except ValueError:
        raise ValueError("private_worker_origin_required") from None
    if (not valid or parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or not port or not 1024 <= port <= 65535 or address.is_unspecified or address.is_link_local):
        raise ValueError("private_worker_origin_required")
    return "http://" + str(address) + ":" + str(port)


def manifest_version(job):
    if (job.get("state") != "ready" or not UUID.fullmatch(job.get("id", ""))
            or set(job.get("media", {})) != {"original", "instrumental"}):
        raise ValueError("ready_job_required")
    bound = {"id": job["id"], "song_id": job["song_id"], "media": job["media"],
             "video": job.get("video", {"status": "unavailable"})}
    return hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MediaTickets:
    def __init__(self, secret):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
            raise ValueError("strong_media_signer_required")
        self.secret = hmac.digest(secret.encode(), b"Friday-KTV-prepared-media-v1", "sha256")

    def _signature(self, job, expires):
        payload = (job["id"] + ":" + manifest_version(job) + ":" + str(expires)).encode()
        return base64.urlsafe_b64encode(hmac.digest(self.secret, payload, "sha256")).decode().rstrip("=")

    def issue(self, job, *, now=None, ttl=600):
        if not 1 <= ttl <= 900:
            raise ValueError("bounded_media_ttl_required")
        expires = int(time.time() if now is None else now) + ttl
        return str(expires) + "." + self._signature(job, expires), expires

    def permits(self, ticket, job, *, now=None):
        match = TICKET.fullmatch(ticket) if isinstance(ticket, str) else None
        if not match:
            return False
        current, expires = int(time.time() if now is None else now), int(match[1])
        if not current < expires <= current + 900:
            return False
        try:
            return hmac.compare_digest(match[2], self._signature(job, expires))
        except (ValueError, KeyError, TypeError):
            return False


class WorkerCapabilities:
    """Runs on the worker health lane, never the control/TV request lane.

    Checks authenticated source evidence, pinned bytes and isolated runtime
    imports without loading a model, separating a song or opening audio.
    """
    def __init__(self, source, separation_cache, *, download_enabled=False, runtime=None):
        from .ktv_accompaniment import RUNTIME
        self.source, self.cache = source, Path(separation_cache)
        self.download_enabled, self.runtime = download_enabled, runtime or RUNTIME
        self.binding = None
        self.model_ready = False

    def __call__(self):
        result = dict.fromkeys(CAPABILITIES, False)
        try:
            cookie = self.source._session()
            body = self.source.rpc("status", cookie=cookie)["body"] if cookie else {}
            profile = body.get("data", body).get("profile") or {}
            result["source"] = type(profile.get("userId")) is int and profile["userId"] > 0
        except Exception:
            pass
        try:
            from .ktv_accompaniment import MODEL, PACKAGE, PACKAGE_VERSION, PINNED_MODEL_ASSETS, _model_files
            files = [self.cache / "models" / MODEL / name for name in PINNED_MODEL_ASSETS]
            if any(p.is_symlink() or p.resolve() != p for p in files):
                raise ValueError("model_identity_mismatch")
            binding = tuple((p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in files)
            if binding != self.binding:
                self.model_ready = False
                _model_files(self.cache / "models")
                self.binding, self.model_ready = binding, True
            # Isolate native import failure in a short-lived child. It loads
            # dependencies only, never a model or an audio input/output.
            subprocess.run([str(self.runtime), "-c", "import importlib.metadata as m; assert m.version("
                            + repr(PACKAGE) + ")==" + repr(PACKAGE_VERSION)
                            + "; from homeplane.ktv_accompaniment import _load_runtime; _load_runtime()"],
                           cwd=Path(__file__).resolve().parents[1], check=True, timeout=8,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            result["model"] = self.model_ready
        except Exception:
            self.binding, self.model_ready = None, False
        result["download"] = bool(self.download_enabled and result["source"] and shutil.which("ffmpeg"))
        result["lyrics"] = bool(result["source"] and self.source.node and self.source.bridge.is_file())
        try:
            probe = self.cache / ".worker-capability-check"
            probe.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            part = probe.with_suffix(".part")
            value = b"Friday KTV media capability 1\n"
            part.write_bytes(value)
            part.chmod(0o600)
            part.replace(probe)
            result["media"] = probe.read_bytes() == value
            probe.unlink()
        except OSError:
            pass
        return result


class PreparationWorker(NasBridge):
    """Preparation and archive publication; the NAS owns the room/control lane."""
    def __init__(self, queue, config_path, *, signing_key, health, lyrics=None, **kwargs):
        super().__init__(queue, config_path, **kwargs)
        config = json.loads(Path(config_path).read_text())
        self.media_origin = media_origin(config["media_origin"], allow_loopback=kwargs.get("allow_loopback", False))
        self.instance = str(uuid.uuid4())
        self.tickets = MediaTickets(signing_key)
        self.health_check, self.lyrics_provider = health, lyrics
        self.capabilities = dict.fromkeys(CAPABILITIES, False)
        self.capabilities_at = 0
        self.health_thread = None
        self.worker_registered = 0
        self.manifest_acked, self.lyrics_cache, self.qr_acked = {}, {}, set()
        self.archive_pending = set(self.mapping.values())
        self.metadata_root = private_directory(Path(config_path).absolute().parent / "worker-publications")
        self.published_metadata = {}
        self.backgrounds = None
        self.archive_checked, self.archive_offset = 0, 0

    def _metadata(self, audio_version):
        if audio_version in self.published_metadata:
            return self.published_metadata[audio_version]
        value = {}
        path = self.metadata_root / (audio_version + ".json")
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 196608:
                raise ValueError("untrusted_publication_metadata")
            candidate = json.loads(path.read_text())
            if (set(candidate) != {"audio_version", "lyrics", "guide"} or candidate["audio_version"] != audio_version
                    or not isinstance(candidate["lyrics"], dict) or candidate["guide"] is not None and not isinstance(candidate["guide"], dict)):
                raise ValueError("untrusted_publication_metadata")
            value = candidate
        except (OSError, ValueError, TypeError):
            pass
        if len(self.published_metadata) >= 256:
            self.published_metadata.pop(next(iter(self.published_metadata)))
        self.published_metadata[audio_version] = value
        return value

    def _remember_metadata(self, audio_version, lyrics, guide):
        value = {"audio_version": audio_version, "lyrics": lyrics, "guide": guide}
        if self._metadata(audio_version) != value:
            # Persist stable edition metadata before the NAS commit. A restart
            # cannot roll a with-guide edition back to a retired no-guide one.
            atomic_json(self.metadata_root / (audio_version + ".json"), value)
            self.published_metadata[audio_version] = copy.deepcopy(value)

    def _publish_guide(self, remote, media, generation):
        # Direct coordination publishes guide metadata once through immutable
        # manifests. The legacy SMB guide endpoint has a different schema.
        return

    def _send_updates(self, updates, mapping, generation):
        # A successful manifest is the only direct-mode ready commit. If the
        # process exits before that point, the NAS can lease the task again.
        progress = [row for row in updates if row["state"] != "ready"]
        if progress:
            super()._send_updates(progress, mapping, generation)

    def request(self, path, body=None):
        if path in {"/agent/worker", "/agent/manifests", "/agent/qr", "/agent/archive-check"}:
            # The fixed extra endpoints carry no arbitrary path or proxy target.
            with self.http.stream("POST", self.endpoint + path,
                                  headers={"Authorization": "Bearer " + self.key}, json=body,
                                  timeout=8, follow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    raise ValueError("nas_unavailable")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > 65536:
                        raise ValueError("public_response_exceeded")
                    raw.extend(chunk)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("invalid_public_response")
            return result
        return super().request(path, body)

    def manage(self, operation, value):
        """Owner-only server-side relay. Never send worker credentials to a browser."""
        try:
            with self.http.stream("POST", self.endpoint + "/agent/manage", headers={"Authorization": "Bearer " + self.key},
                                  json={"operation": operation, "value": value}, timeout=12, follow_redirects=False) as reply:
                raw = bytearray()
                for chunk in reply.iter_bytes():
                    if len(raw) + len(chunk) > 2 * 1024**2:
                        raise ValueError("response_exceeded")
                    raw.extend(chunk)
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("invalid_response")
                if not 200 <= reply.status_code < 300:
                    allowed = {"cache_changed", "cache_in_use", "invalid_policy", "invalid_selection", "invalid_preferences", "invalid_query",
                               "room_changed", "ambiguous_room", "tv_offline", "queue_changed", "song_playing", "song_not_ready", "playback_changed",
                               "vocal_preset_unavailable", "sfx_unavailable", "microphone_update_required", "cloud_song_unavailable", "mac_offline", "rate_limited",
                               "invalid_control", "invalid_command", "search_unavailable", "queue_full", "idempotency_conflict", "unsafe_cache"}
                    code = data.get("code")
                    raise HTTPException(reply.status_code if reply.status_code in {400, 409, 429} else 503,
                                        code if code in allowed else "nas_management_unavailable")
                return data
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "nas_management_unavailable") from None

    def tick(self):
        with self.poll_lock:
            current = time.monotonic()
            if current - self.worker_registered >= 3:
                capabilities = dict(self.capabilities) if current - self.capabilities_at <= 15 else dict.fromkeys(CAPABILITIES, False)
                self.request("/agent/worker", {"instance": self.instance, "origin": self.media_origin,
                                              "capabilities": capabilities})
                self.worker_registered = current
            value = self.request("/agent/tasks")
            tasks, rooms, cancelled = value.get("tasks", []), value.get("rooms", []), value.get("cancelled", [])
            if (not isinstance(tasks, list) or len(tasks) > 8 or not isinstance(rooms, list) or len(rooms) > 8
                    or not isinstance(cancelled, list) or len(cancelled) > 80
                    or any(not isinstance(i, str) or not UUID.fullmatch(i) for i in cancelled)):
                raise ValueError("invalid_public_response")
            with self.lock:
                self.online = True
                # The direct coordinator returns the household's current scene.
                # Never create/close a room here or relay playback/control state.
                direct = [r for r in rooms if isinstance(r, dict) and r.get("direct") is True
                          and isinstance(r.get("room_id"), str) and UUID.fullmatch(r["room_id"])]
                if len(direct) > 1:
                    raise ValueError("ambiguous_direct_room")
                active = direct[0] if direct else None
                if (active or {}).get("room_id") != (self.party or {}).get("room_id"):
                    self.room_generation += 1
                    self.pending_tasks.clear()
                    self.pending_cancelled.clear()
                self.party = copy.deepcopy(active)
                self.remote_order = list(active.get("order", [])) if active else []
                if self.party:
                    self.party["order"] = [self.mapping[i] for i in self.remote_order if i in self.mapping]
                    report = self.party.get("playback", {})
                    self.playback = {"current": self.mapping.get(report.get("current"), ""),
                                     "played": [self.mapping[i] for i in report.get("played", []) if i in self.mapping]}
                else:
                    self.playback = None
                if not active:
                    self.pending_tasks.clear()
                for item in tasks if active else []:
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and UUID.fullmatch(item["id"]):
                        self.pending_tasks[item["id"]] = item
                self.pending_cancelled.update(cancelled)
                for row in value.get("archives", [])[:80]:
                    if isinstance(row, dict) and row.get("id") in self.mapping:
                        local = self.mapping[row["id"]]
                        ack = self.manifest_acked.get(row["id"])
                        if ack and ack[0] == row.get("version"):
                            if row.get("state") == "ready":
                                self.archive_pending.discard(local)
                            else:
                                self.archive_pending.add(local)
            self.work_wake.set()
        if not self.background:
            self._work_once()

    def _work_once(self):
        super()._work_once()
        self._check_archives()
        with self.lock:
            party, mapping, generation = copy.deepcopy(self.party), dict(self.mapping), self.room_generation
        if not party:
            return
        report = self.playback or {}
        if report.get("current") and hasattr(self.queue, "touch"):
            self.queue.touch(report["current"])
        url = party.get("join_url", "")
        tv_instance = party.get("tv_instance", "")
        if (url.startswith(self.endpoint + "/#") and (url not in self.qr_acked or party.get("qr_ready") is False)
                and UUID.fullmatch(tv_instance)):
            import qrcode
            from qrcode.image.pure import PyPNGImage
            output = io.BytesIO()
            qrcode.make(url, image_factory=PyPNGImage, box_size=8, border=4).save(output)
            png = output.getvalue()
            self.request("/agent/qr", {"room_id": party["room_id"], "instance": tv_instance, "join_url": url,
                "sha256": hashlib.sha256(png).hexdigest(), "png": base64.b64encode(png).decode()})
            self.qr_acked.add(url)
        for remote in party.get("order", []):
            if remote in report.get("played", []) and remote != report.get("current"):
                continue
            # party.order is local after synchronization, mapping is remote→local.
            if remote in mapping.values():
                remote = next(k for k, v in mapping.items() if v == remote)
            local = mapping.get(remote)
            if local is None or not self._same_room(generation):
                continue
            try:
                job = self.queue.get(local)
                if job["state"] != "ready":
                    continue
                audio_version = manifest_version(job)
                background = None
                if self.backgrounds is not None:
                    try:
                        candidate = self.backgrounds.request_from_thread(job)
                        if candidate.get('status') == 'ready':
                            background = {k:candidate[k] for k in ('status','sha256','bytes')}
                            background.update(encoding='png',url=f'/v1/jobs/{local}/media/background')
                    except (ValueError,OSError,RuntimeError,TypeError,KeyError):
                        pass
                previous_metadata = self._metadata(audio_version)
                guide = previous_metadata.get("guide")
                if self.guide is not None and local in self.guide.protected:
                    guide = self.guide.get(local) or guide
                cached_lyrics = self.lyrics_cache.get(audio_version)
                if cached_lyrics is None and previous_metadata.get("lyrics", {}).get("status") == "ready":
                    cached_lyrics = self.lyrics_cache[audio_version] = (previous_metadata["lyrics"], time.monotonic())
                if (cached_lyrics is None or (cached_lyrics[0].get("status") != "ready"
                                             and time.monotonic() - cached_lyrics[1] >= 30)):
                    try:
                        value = self.lyrics_provider(local) if self.lyrics_provider else None
                        if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False).encode()) > 131072:
                            raise ValueError("lyrics_unavailable")
                    except Exception:
                        value = {"status": "unavailable", "lines": []}
                    self.lyrics_cache[audio_version] = (value, time.monotonic())
                lyrics_value = self.lyrics_cache[audio_version][0]
                version = hashlib.sha256(json.dumps({"audio": audio_version, "job": job, "lyrics": lyrics_value, "guide": guide, "background":background},
                    sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                ack = self.manifest_acked.get(remote)
                if ack and ack[0] == version and time.monotonic() - ack[1] < 240:
                    continue
                duration = job["media"]["original"]["duration_seconds"]
                if (not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 1800
                        or abs(duration - job["media"]["instrumental"]["duration_seconds"]) > .1):
                    raise ValueError("media_timeline_mismatch")
                ticket, expires = self.tickets.issue(job)
                published = copy.deepcopy(job)
                published["id"] = remote
                if background is not None:
                    published['background'] = background
                payload = {"id": remote, "instance": self.instance, "version": version,
                    "timeline": {"sample_rate": 48000, "frames": round(duration * 48000), "duration_ms": round(duration * 1000)},
                    "job": published, "lyrics": lyrics_value, "media_ticket": ticket, "media_expires": expires}
                if guide and guide.get("url"):
                    payload["guide"] = guide
                self._remember_metadata(audio_version, lyrics_value, guide)
                self.request("/agent/manifests", payload)
                with self.lock:
                    self.manifest_acked[remote] = (version, time.monotonic())
                    self.archive_pending.add(local)
                    self.terminal_acked.add(local)
                if len(self.lyrics_cache) > 256:
                    self.lyrics_cache.pop(next(iter(self.lyrics_cache)))
            except Exception:
                # A transient publish/cache fault cannot mark a ready local job failed.
                continue

    def _check_archives(self):
        now = time.monotonic()
        if now - self.archive_checked < 3:
            return
        self.archive_checked = now
        with self.lock:
            pending = sorted(self.archive_pending)
        if not pending:
            return
        start = self.archive_offset % len(pending)
        selected = (pending + pending)[start:start + min(16, len(pending))]
        self.archive_offset = start + len(selected)
        items, versions = [], {}
        for local in selected:
            try:
                job = self.queue.get(local)
                version = manifest_version(job)
                specs = list(job["media"].values())
                if job.get("video", {}).get("status") == "ready":
                    specs.append(job["video"])
                guide = self._metadata(version).get("guide")
                if guide:
                    specs.extend((guide, guide["reference"]))
                items.append({"id": local, "files": [{"sha256": spec["sha256"], "bytes": spec["bytes"]} for spec in specs]})
                versions[local] = version
            except Exception:
                continue
        if not items:
            return
        try:
            response = self.request("/agent/archive-check", {"items": items})
            rows = response.get("items")
            if not isinstance(rows, list) or len(rows) > len(items):
                return
            for row in rows:
                local = row.get("id") if isinstance(row, dict) else None
                if local not in versions or row.get("state") not in {"ready", "pending", "failed"}:
                    continue
                if manifest_version(self.queue.get(local)) != versions[local]:
                    continue
                with self.lock:
                    if row["state"] == "ready":
                        self.archive_pending.discard(local)
                    else:
                        self.archive_pending.add(local)
        except Exception:
            return

    def start(self):
        if self.thread is not None:
            return
        def check():
            while not self.stop.is_set():
                try:
                    value = self.health_check()
                    self.capabilities = {name: value.get(name) is True for name in CAPABILITIES}
                except Exception:
                    self.capabilities = dict.fromkeys(CAPABILITIES, False)
                self.capabilities_at = time.monotonic()
                self.stop.wait(5)
        self.health_thread = threading.Thread(target=check, name="ktv-worker-health", daemon=True)
        self.health_thread.start()
        super().start()

    def current(self):
        return {"active": False, "nas_online": self.online, "role": "preparation_worker"}

    def protected_jobs(self):
        with self.lock:
            order = set((self.party or {}).get("order", []))
            report = self.playback or {}
            order -= set(report.get("played", []))
            if report.get("current"):
                order.add(report["current"])
            return order | self.archive_pending

    def create_party(self):
        raise ValueError("nas_coordinates_room")

    def close_party(self):
        raise ValueError("nas_coordinates_room")

    def report_playback(self, value):
        raise ValueError("nas_coordinates_playback")

    def control_from_tv(self, value):
        raise ValueError("nas_coordinates_controls")

    def close(self):
        super().close()
        if self.health_thread is not None:
            self.health_thread.join(timeout=1)
