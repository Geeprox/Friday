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
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .ktv_nas_bridge import NasBridge
from .ktv_service import atomic_json, private_directory

UUID = re.compile(r"[a-f0-9-]{36}")
TICKET = re.compile(r"([0-9]{10})\.([A-Za-z0-9_-]{43})")
CAPABILITIES = ("source", "model", "download", "lyrics", "media")
LYRIC_RETRY_SECONDS = (15, 60, 300)
LYRIC_PREFETCH_COUNT = 4
LYRIC_LOOKUP_WORKERS = 2
BACKGROUND_PREFETCH_COUNT = 10


@dataclass
class LyricWork:
    future: Future | None = None
    attempts: int = 0
    retry_at: float = 0
    recovering: bool = False


def has_lyrics(value):
    if not isinstance(value, dict) or value.get("status") != "ready":
        return False
    lines = value.get("lines")
    return (isinstance(lines, list) and 3 <= len(lines) <= 1000
            and all(isinstance(line, dict) and type(line.get("time_ms")) is int
                    and 0 <= line["time_ms"] <= 1800000
                    and isinstance(line.get("text"), str) and 0 < len(line["text"]) <= 200 for line in lines))


def archive_lyric_tasks(value):
    if not isinstance(value, list) or len(value) > 8:
        return {}
    tasks = {}
    for task in value:
        if (not isinstance(task, dict) or not isinstance(task.get("id"), str) or not UUID.fullmatch(task["id"])
                or not isinstance(task.get("song_id"), str) or not re.fullmatch(r"[1-9][0-9]{0,17}", task["song_id"])
                or task.get("source") not in ("song", "cloud", "mv", "mv_direct")
                or type(task.get("duration_ms")) is not int or not 1 <= task["duration_ms"] <= 1800000
                or not isinstance(task.get("version"), str) or not re.fullmatch(r"[0-9a-f]{64}", task["version"])):
            continue
        tasks[task["id"]] = {field: task[field] for field in ("id", "song_id", "source", "duration_ms", "version")}
    return tasks


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


def _public_join_endpoint(value, worker_endpoint):
    if value is None:
        current = urlsplit(worker_endpoint)
        if current.scheme == "https" and current.netloc.endswith(":443"):
            return current._replace(netloc=current.netloc[:-4]).geturl()
        return worker_endpoint
    if not isinstance(value, str) or not re.fullmatch(
        r"https://[a-z0-9.-]+(?::[1-9][0-9]{0,4})?/[a-z0-9_-]+/?", value
    ):
        raise ValueError("https_public_join_endpoint_required")
    candidate = urlsplit(value.rstrip("/"))
    try:
        port = candidate.port
    except ValueError:
        raise ValueError("https_public_join_endpoint_required") from None
    if (candidate.path != urlsplit(worker_endpoint).path or not candidate.hostname
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                       for part in candidate.hostname.split("."))):
        raise ValueError("https_public_join_endpoint_required")
    authority = candidate.hostname if port in (None, 443) else f"{candidate.hostname}:{port}"
    return f"https://{authority}{candidate.path}"


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
        self.public_join_endpoint = _public_join_endpoint(config.get("public_join_endpoint"), self.endpoint)
        self.instance = str(uuid.uuid4())
        self.tickets = MediaTickets(signing_key)
        self.health_check, self.lyrics_provider = health, lyrics
        self.capabilities = dict.fromkeys(CAPABILITIES, False)
        self.capabilities_at = 0
        self.health_thread = None
        self.worker_registered = 0
        self.manifest_acked = {}
        self.lyric_pool = ThreadPoolExecutor(max_workers=LYRIC_LOOKUP_WORKERS, thread_name_prefix="ktv-timed-lyrics")
        self.lyric_recovery_pool = ThreadPoolExecutor(max_workers=LYRIC_LOOKUP_WORKERS, thread_name_prefix="ktv-lyric-recovery")
        self.lyric_work = OrderedDict()
        self.lyric_prefetch = OrderedDict()
        self.archive_lyric_tasks = {}
        self.archive_lyric_acks = set()
        self.qr_acked = ""
        self.next_qr_attempt = 0
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
        if not has_lyrics(lyrics) and guide is None:
            return
        value = {"audio_version": audio_version, "lyrics": lyrics, "guide": guide}
        if self._metadata(audio_version) != value:
            # Persist stable edition metadata before the NAS commit. A restart
            # cannot roll a with-guide edition back to a retired no-guide one.
            try:
                atomic_json(self.metadata_root / (audio_version + ".json"), value)
            except OSError:
                # 持久化失败时仍发布可用音轨，后续进程会重新校验补全结果。
                pass
            self.published_metadata[audio_version] = copy.deepcopy(value)

    def _matches_lyric_target(self, target, edition):
        if isinstance(target, dict):
            with self.lock:
                return self.archive_lyric_tasks.get(target["id"]) == target
        return manifest_version(self.queue.get(target)) == edition

    def _candidate_lyrics(self, resolve, target, edition):
        if not self._matches_lyric_target(target, edition):
            return None
        value = resolve(target)
        if (isinstance(value, dict) and (has_lyrics(value) or value.get("status") == "instrumental")
                and len(json.dumps(value, allow_nan=False).encode()) <= 131072
                and self._matches_lyric_target(target, edition)):
            return value
        return None

    def _active_lyric_lookups(self, recovering=False):
        return sum(work.recovering == recovering and work.future is not None and not work.future.done()
                   for work in self.lyric_work.values())

    def _prime_pending_lyrics(self):
        prime = getattr(self.lyrics_provider, "prefetch_public", None)
        if not callable(prime) or self.stop.is_set():
            return
        if any(not future.done() for future in self.lyric_prefetch.values()):
            return
        if self._active_lyric_lookups() >= LYRIC_LOOKUP_WORKERS:
            return
        with self.lock:
            played = set(((self.party or {}).get("playback") or {}).get("played", []))
            waiting = [remote for remote in self.remote_order if remote not in played][:LYRIC_PREFETCH_COUNT]
            tasks = [self.pending_tasks[remote] for remote in waiting if remote in self.pending_tasks]
        for task in tasks:
            song_id = task.get("song_id")
            if (task.get("source", "song") != "song" or not isinstance(song_id, str)
                    or not re.fullmatch(r"[1-9][0-9]{0,17}", song_id) or song_id in self.lyric_prefetch):
                continue
            if len(self.lyric_prefetch) >= 128:
                self.lyric_prefetch.popitem(last=False)
            future = self.lyric_pool.submit(prime, song_id)
            self.lyric_prefetch[song_id] = future
            future.add_done_callback(lambda _: self.work_wake.set())
            return

    def _lyrics_for(self, target, song_id, edition, previous=None):
        if has_lyrics(previous):
            return previous
        unavailable = {"status": "unavailable", "lines": []}
        work = self.lyric_work.get(edition)
        now = time.monotonic()
        recovery = getattr(self.lyrics_provider, "recover", None)
        if work and work.future and work.future.done():
            try:
                value = work.future.result()
            except Exception:
                value = None
            if has_lyrics(value):
                # 先保存已验证歌词，重试 NAS 发布时继续使用同一份。
                return value
            work.future = None
            if value and value.get("status") == "instrumental":
                work.attempts = len(LYRIC_RETRY_SECONDS) + 1
                work.recovering = False
            elif not work.recovering and callable(recovery):
                work.recovering = True
            else:
                work.recovering = False
                if work.attempts <= len(LYRIC_RETRY_SECONDS):
                    work.retry_at = now + LYRIC_RETRY_SECONDS[work.attempts - 1]
        if not self.lyrics_provider or self.stop.is_set():
            return unavailable
        primer = self.lyric_prefetch.get(song_id)
        if primer is not None and not primer.done():
            return unavailable
        if work and (work.future or now < work.retry_at):
            return unavailable
        recovering = bool(work and work.recovering)
        if work and not recovering and work.attempts > len(LYRIC_RETRY_SECONDS):
            return {**unavailable, "retry_exhausted": True}
        active = self._active_lyric_lookups(recovering)
        if not recovering:
            active += sum(not future.done() for future in self.lyric_prefetch.values())
        if active >= LYRIC_LOOKUP_WORKERS:
            return unavailable
        if work is None:
            if len(self.lyric_work) >= 256:
                oldest = next((key for key, item in self.lyric_work.items() if item.future is None or item.future.done()), None)
                if oldest is None:
                    return unavailable
                self.lyric_work.pop(oldest)
            work = self.lyric_work[edition] = LyricWork()
        self.lyric_work.move_to_end(edition)
        if recovering:
            work.future = self.lyric_recovery_pool.submit(self._candidate_lyrics, recovery, target, edition)
        else:
            work.attempts += 1
            primary = getattr(self.lyrics_provider, "primary_for", self.lyrics_provider)
            work.future = self.lyric_pool.submit(self._candidate_lyrics, primary, target, edition)
        work.future.add_done_callback(lambda _: self.work_wake.set())
        return unavailable

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
        if path in {"/agent/worker", "/agent/manifests", "/agent/lyrics", "/agent/qr", "/agent/archive-check"}:
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
                               "invalid_control", "invalid_command", "search_unavailable", "queue_full", "idempotency_conflict", "unsafe_cache",
                               "retry_unavailable", "retry_limit"}
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
                    self.next_qr_attempt = 0
                    self.pending_tasks.clear()
                    self.pending_cancelled.clear()
                self.party = copy.deepcopy(active)
                self.remote_order = list(active.get("order", [])) if active else []
                self.archive_lyric_tasks = archive_lyric_tasks(value.get("lyric_tasks")) if active else {}
                self.archive_lyric_acks.intersection_update((task["id"], task["version"])
                                                            for task in self.archive_lyric_tasks.values())
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
            self._publish_room_qr(active)
        if not self.background:
            self._work_once()

    def _publish_room_qr(self, party):
        # 房间二维码随状态轮询交付，避免被上一首歌的归档或媒体制作拖住。
        if not party:
            return
        url = party.get("join_url", "")
        tv_instance = party.get("tv_instance", "")
        if (time.monotonic() < self.next_qr_attempt or not isinstance(url, str)
                or not url.startswith(self.public_join_endpoint + "/#")
                or (url == self.qr_acked and party.get("qr_ready") is not False)
                or not isinstance(tv_instance, str) or not UUID.fullmatch(tv_instance)):
            return
        try:
            import qrcode
            from qrcode.image.pure import PyPNGImage

            output = io.BytesIO()
            qrcode.make(url, image_factory=PyPNGImage, box_size=8, border=4).save(output)
            png = output.getvalue()
            self.request("/agent/qr", {"room_id": party["room_id"], "instance": tv_instance, "join_url": url,
                "sha256": hashlib.sha256(png).hexdigest(), "png": base64.b64encode(png).decode()})
            self.qr_acked = url
        except (ImportError, OSError, ValueError, httpx.HTTPError):
            self.next_qr_attempt = time.monotonic() + 2

    def _work_once(self):
        self._prime_pending_lyrics()
        super()._work_once()
        self._check_archives()
        with self.lock:
            party, mapping, generation = copy.deepcopy(self.party), dict(self.mapping), self.room_generation
            report = copy.deepcopy(self.playback or {})
        if not party:
            return
        current = report.get("current")
        if current and hasattr(self.queue, "touch"):
            self.queue.touch(current)
        played = set(report.get("played", []))
        inverse = {local: remote for remote, local in mapping.items()}
        order = [local for local in party.get("order", []) if local not in played or local == current]
        if current in inverse:
            order = [current] + [local for local in order if local != current]
        background_count = 0
        for local in order:
            remote = inverse.get(local)
            if remote is None or not self._same_room(generation):
                continue
            try:
                job = self.queue.get(local)
                if job["state"] in {"failed", "cancelled"}:
                    continue
                if job["state"] != "ready":
                    continue
                audio_version = manifest_version(job)
                background = None
                if self.backgrounds is not None:
                    try:
                        if background_count < BACKGROUND_PREFETCH_COUNT and self.backgrounds.eligible_for_generation(job):
                            background_count += 1
                            candidate = self.backgrounds.request_from_thread(job)
                        else:
                            candidate = self.backgrounds.cached_for(job)
                        if candidate and candidate.get('status') == 'ready':
                            background = {k:candidate[k] for k in ('status','sha256','bytes')}
                            background.update(encoding='png',url=f'/v1/jobs/{local}/media/background')
                    except (ValueError,OSError,RuntimeError,TypeError,KeyError):
                        pass
                previous_metadata = self._metadata(audio_version)
                guide = previous_metadata.get("guide")
                if self.guide is not None and local in self.guide.protected:
                    guide = self.guide.get(local) or guide
                lyrics_value = self._lyrics_for(local, job["song_id"], audio_version, previous_metadata.get("lyrics"))
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
                if has_lyrics(lyrics_value):
                    self.lyric_work.pop(audio_version, None)
                self.request("/agent/manifests", payload)
                with self.lock:
                    self.manifest_acked[remote] = (version, time.monotonic())
                    self.archive_pending.add(local)
                    self.terminal_acked.add(local)
            except Exception:
                # A transient publish/cache fault cannot mark a ready local job failed.
                continue
        self._publish_archive_lyrics(generation)

    def _publish_archive_lyrics(self, generation):
        with self.lock:
            tasks = list(self.archive_lyric_tasks.values())
        for task in tasks:
            edition = task["id"], task["version"]
            with self.lock:
                if not self._same_room(generation) or edition in self.archive_lyric_acks:
                    continue
            lyrics = self._lyrics_for(task, task["song_id"], edition)
            if (not has_lyrics(lyrics) and not lyrics.get("retry_exhausted")) or not self._matches_lyric_target(task, edition):
                continue
            try:
                self.request("/agent/lyrics", {"id": task["id"], "instance": self.instance,
                                          "version": task["version"], "lyrics": lyrics})
            except (OSError, httpx.HTTPError, ValueError):
                return
            with self.lock:
                if self._same_room(generation):
                    self.archive_lyric_acks.add(edition)
            self.lyric_work.pop(edition, None)

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
        self.lyric_pool.shutdown(wait=False, cancel_futures=True)
        self.lyric_recovery_pool.shutdown(wait=False, cancel_futures=True)
        if self.health_thread is not None:
            self.health_thread.join(timeout=1)
