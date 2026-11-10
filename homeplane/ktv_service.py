"""Private LAN KTV preparation queue. No device, model or downloader starts on import."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import wave
from concurrent.futures import Future
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .ktv_accompaniment import MAX_DURATION_SECONDS, MODEL_REVISION, prepare
from .ktv_scheduler import PriorityPool

SONG_ID = re.compile(r"[1-9][0-9]{0,17}\Z")
MAX_MEDIA = 512 * 1024 * 1024
MAX_STORAGE = 4 * 1024 * 1024 * 1024
ACTIVE = {"queued", "acquiring", "separating", "transcoding"}
ERRORS = {
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


def requested_audio_origin(preference: str) -> str:
    """Automatic MV matching must never change the selected song's audio edition."""
    return {"auto": "song", "song": "song", "mv": "mv", "cloud": "cloud", "mv_direct": "mv_direct"}[preference]


def valid_video(value, source_hash):
    if not isinstance(value, dict):
        return False
    common = (value.get("status") == "ready"
        and value.get("encoding") in {"h264_mp4_video_only", "h264_mp4_silent_clock"}
        and value.get("source_audio_sha256") == source_hash
        and isinstance(value.get("sha256"), str) and bool(re.fullmatch(r"[a-f0-9]{64}", value["sha256"]))
        and type(value.get("bytes")) is int and 0 < value["bytes"] <= 128*1024*1024
        and type(value.get("duration_seconds")) in {int,float} and math.isfinite(value["duration_seconds"])
        and 0 < value["duration_seconds"] <= MAX_DURATION_SECONDS
        and type(value.get("video_offset_ms")) in {int,float} and math.isfinite(value["video_offset_ms"])
        and -12000 <= value["video_offset_ms"] <= 12000)
    if not common:
        return False
    if value.get("method") == "same_mv_audio":
        return value["video_offset_ms"] == 0 and isinstance(value.get("derived_from_video_sha256"),str) and bool(re.fullmatch(r"[a-f0-9]{64}",value["derived_from_video_sha256"]))
    return (all(type(value.get(k)) in {int,float} and math.isfinite(value[k]) for k in ("score_min","offset_drift_ms"))
        and .94 <= value["score_min"] <= 1.01 and type(value.get("window_count")) is int
        and 5 <= value["window_count"] <= 7 and 0 <= value["offset_drift_ms"] <= 25)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(6))
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def safe_file(path: Path, root: Path | None = None) -> Path:
    path = path.absolute()
    if path.resolve() != path or not path.is_file() or not 0 < path.stat().st_size <= MAX_MEDIA:
        raise ValueError("source_unavailable")
    if root is not None and not path.is_relative_to(root):
        raise ValueError("source_unavailable")
    return path


def private_directory(path: Path) -> Path:
    path = path.absolute()
    if path.resolve() != path:
        raise ValueError("unsafe_directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("unsafe_directory")
    path.chmod(0o700)
    return path


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    song_id: str = Field(pattern=r"^[1-9][0-9]{0,17}$")
    title: str = Field(default="", max_length=120)
    request_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    requester: dict | None = None
    cache_only: bool = False
    source_preference: Literal["auto", "mv", "song", "cloud", "mv_direct"] = "auto"


class Queue:
    """Priority-aware stage pipeline: downloads overlap one GPU worker and two encoders."""

    def __init__(
        self,
        root: Path,
        *,
        acquire: Callable,
        separate: Callable,
        transcode: Callable,
        simulated: bool = False,
        capacity: int = 32,
        video: Callable | None = None,
        download_workers: int = 2,
        hardware=None,
        memory_available=None,
    ):
        self.root = private_directory(root)
        self.jobs_dir = private_directory(self.root / "jobs")
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.events: dict[str, threading.Event] = {}
        self.futures = {}
        self.acquire, self.separate, self.transcode = acquire, separate, transcode
        self.video = video
        self.simulated, self.capacity = simulated, capacity
        self.pools = {
            "acquire": PriorityPool(download_workers, "ktv-download"),
            "separate": PriorityPool(1, "ktv-model"),
            "finish": PriorityPool(2, "ktv-encode"),
        }
        self.hardware = hardware or {"cpu": "测试环境", "memory_bytes": 0, "cpu_cores": 1}
        self.memory_available = memory_available
        self.model_condition = threading.Condition()
        self.active_models = 0
        self.scheduler_config = {"mode": "manual", "download": download_workers, "model": 1, "encode": 2}
        self.stage_futures = {}
        self.priorities = []
        self.closed = False
        self._load()
        saved = self.root / "scheduler.json"
        if saved.is_file() and not saved.is_symlink() and saved.stat().st_size < 2048:
            try:
                self.configure(json.loads(saved.read_text()), persist=False)
            except (ValueError, HTTPException):
                pass

    def _load(self):
        for path in sorted(self.jobs_dir.glob("*/state.json"))[-128:]:
            try:
                if path.resolve() != path or path.stat().st_size > 32768:
                    continue
                value = json.loads(path.read_text())
                job_id = value["id"]
                if (
                    str(uuid.UUID(job_id)) != path.parent.name
                    or not SONG_ID.fullmatch(value["song_id"])
                    or value["state"] not in ACTIVE | {"ready", "failed", "cancelled"}
                    or not isinstance(value["title"], str)
                    or len(value["title"]) > 120
                    or not isinstance(value["created"], int)
                    or value["code"] not in ERRORS | {"none"}
                ):
                    continue
                JobRequest(song_id=value["song_id"], title=value["title"], request_id=value["request_id"])
                if value["state"] == "ready":
                    if set(value["media"]) != {"original", "instrumental"}:
                        continue
                    for item in value["media"].values():
                        if (
                            not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                            or not isinstance(item["bytes"], int)
                            or not 44 < item["bytes"] <= MAX_MEDIA
                            or not 0 < item["duration_seconds"] <= MAX_DURATION_SECONDS
                        ):
                            raise ValueError("invalid_state")
                    if not valid_video(value.get("video"), value["media"]["original"]["sha256"]):
                        value["video"] = {"status": "unavailable"}
                if value["state"] == "ready" and not value.get("binding"):
                    report = path.parent / "preparation.json"
                    if report.is_file() and not report.is_symlink() and report.stat().st_size < 65536:
                        binding = json.loads(report.read_text())
                        if (
                            binding.get("model_revision") == MODEL_REVISION
                            and binding.get("model_verified") is True
                        ):
                            value["binding"] = MODEL_REVISION
                if value.get("simulated") is not self.simulated:
                    continue
                if value["state"] in ACTIVE:
                    value.update(state="failed", code="interrupted")
                    atomic_json(path, value)
                self.jobs[job_id] = value
                self.events[job_id] = threading.Event()
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def snapshot(self, value: dict) -> dict:
        result = {k: value[k] for k in ("id", "song_id", "title", "state", "code", "created", "simulated")}
        result["media"] = (
            {
                kind: {
                    "url": f"/v1/jobs/{value['id']}/media/{kind}",
                    "sha256": item["sha256"],
                    "bytes": item["bytes"],
                    "duration_seconds": item["duration_seconds"],
                    "sample_rate": 48000,
                    "channels": 2,
                    "encoding": "pcm_s16le_wav",
                }
                for kind, item in value.get("media", {}).items()
            }
            if value["state"] == "ready"
            else {}
        )
        result["video"] = {"status": "unavailable"}
        stage = value.get("stage", value["state"])
        result["progress"] = {
            "stage": stage,
            "stage_started": value.get("stage_started", value["created"]),
            "updated": value.get("updated", value["created"]),
            "completed_steps": 4
            if value["state"] == "ready"
            else {
                "queued": 0,
                "acquiring": 0,
                "waiting_separation": 1,
                "waiting_memory": 1,
                "separating": 1,
                "waiting_transcode": 2,
                "transcoding": 2,
                "video": 3,
            }.get(stage, 0),
            "total_steps": 4,
            "fraction": None,
            "failed_stage": value.get(
                "failed_stage", "acquiring" if value.get("code") == "download_timeout" else ""
            ),
        }
        result["cache_hit"] = value.get("cache_hit", False)
        result["audio_origin"] = value.get("audio_origin", "song")
        result["progress"].update(
            source_phase=value.get("source_phase", ""), source_bytes=value.get("source_bytes", 0),
            source_total=value.get("source_total", 0),
        )
        result["requester"] = value.get("requester") or {"id": "", "nickname": "朋友", "avatar": 0}
        video = value.get("video") or {}
        if value["state"] == "ready" and video.get("status") == "ready":
            result["video"] = {
                k: video[k]
                for k in (
                    "status",
                    "encoding",
                    "bytes",
                    "sha256",
                    "duration_seconds",
                    "source_audio_sha256",
                    "video_offset_ms",
                    "score_min",
                    "window_count",
                    "offset_drift_ms",
                    "method",
                    "derived_from_video_sha256",
                ) if k in video
            }
            result["video"]["url"] = f"/v1/jobs/{value['id']}/media/video"
        return result

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise HTTPException(404, "not_found")
            return self.snapshot(self.jobs[job_id])

    def list(self, *, for_playback=False) -> list[dict]:
        with self.lock:
            return [self.snapshot(v) for v in self.jobs.values() if not (
                for_playback and v["state"] == "ready"
                and v.get("source_preference", "auto") in {"auto", "song"}
                and (v.get("audio_origin", "song") != "song"
                     or (v.get("video") or {}).get("method") == "same_mv_audio"))]

    def submit(self, request: JobRequest) -> dict:
        if any(ord(c) < 32 or ord(c) == 127 for c in request.title):
            raise HTTPException(400, "invalid_title")
        requester = request.requester or {"id": "", "nickname": "朋友", "avatar": 0}
        if (
            set(requester) != {"id", "nickname", "avatar"}
            or not isinstance(requester["id"], str)
            or (requester["id"] and not re.fullmatch(r"[a-f0-9-]{36}", requester["id"]))
            or not isinstance(requester["nickname"], str)
            or not 1 <= len(requester["nickname"]) <= 24
            or any(ord(c) < 32 or ord(c) == 127 for c in requester["nickname"])
            or type(requester["avatar"]) is not int
            or not 0 <= requester["avatar"] < 12
        ):
            raise HTTPException(400, "invalid_requester")
        with self.lock:
            if self.closed:
                raise HTTPException(503, "service_closed")
            for value in self.jobs.values():
                if value["request_id"] == str(request.request_id):
                    if value["song_id"] != request.song_id or value["title"] != request.title or value.get("source_preference", "auto") != request.source_preference:
                        raise HTTPException(409, "idempotency_conflict")
                    return self.snapshot(value)
            if (
                len(self.jobs) >= 128
                or sum(j["state"] in ACTIVE for j in self.jobs.values()) >= self.capacity
            ):
                raise HTTPException(429, "queue_full")
            job_id = str(uuid.uuid4())
            private_directory(self.jobs_dir / job_id)
            value = {
                "id": job_id,
                "song_id": request.song_id,
                "title": request.title,
                "request_id": str(request.request_id),
                "state": "queued",
                "code": "none",
                "created": int(time.time()),
                "simulated": self.simulated,
                "media": {},
                "requester": requester,
                "binding": MODEL_REVISION,
                "events": [],
                "stage": "queued",
                "cache_hit": False,
                "cache_only": request.cache_only,
                "source_preference": request.source_preference,
            }
            self.jobs[job_id] = value
            self.events[job_id] = threading.Event()
            self._save(value)
            self.futures[job_id] = Future()
            if not self._reuse(job_id):
                if request.cache_only and not getattr(self, "archive", None):
                    shutil.rmtree(self.jobs_dir / job_id)
                    self.jobs.pop(job_id)
                    self.events.pop(job_id)
                    self.futures.pop(job_id)
                    raise HTTPException(409, "cache_miss")
                self._schedule(job_id, "acquire", lambda: self._work(job_id))
            return self.snapshot(value)

    def _save(self, value):
        atomic_json(self.jobs_dir / value["id"] / "state.json", value)

    def report_source(self, job_id, progress):
        if progress.get("phase") not in {
            "source_cache_hit",
            "file_detected",
            "decoding",
            "official_request_sent",
            "source_authorizing",
            "source_downloading",
        }:
            return
        with self.lock:
            value = self.jobs.get(job_id)
            if not value or value["state"] not in ACTIVE:
                return
            previous = value.get("source_phase")
            value.update(source_phase=progress["phase"], source_bytes=max(0, int(progress.get("bytes", 0))),
                         source_total=max(0, int(progress.get("total", 0))), updated=int(time.time()))
            if previous != value["source_phase"]:
                value["events"] = (
                    value.get("events", [])
                    + [
                        {
                            "at": int(time.time()),
                            "stage": value["source_phase"],
                            "state": value["state"],
                            "code": "none",
                        }
                    ]
                )[-48:]
            self._save(value)

    def touch(self, job_id):
        with self.lock:
            value = self.jobs.get(job_id)
            if value and int(time.time()) - value.get("last_used", 0) >= 60:
                value["last_used"] = int(time.time())
                self._save(value)

    def journal(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise HTTPException(404, "not_found")
            return {
                "id": job_id,
                "events": list(self.jobs[job_id].get("events", [])),
                "raw_audio_saved": False,
            }

    def _state(self, job_id, state, code="none", stage=None):
        with self.lock:
            value = self.jobs[job_id]
            if state in {"failed", "cancelled"}:
                value["failed_stage"] = value.get("stage", value["state"])
            now = int(time.time())
            value.update(state=state, code=code, stage=stage or state, stage_started=now, updated=now)
            value["events"] = (
                value.get("events", []) + [{"at": now, "stage": stage or state, "state": state, "code": code}]
            )[-48:]
            self._save(value)

    def scheduler_status(self):
        return {
            "settings": dict(self.scheduler_config),
            "hardware": self.hardware,
            "active_models": self.active_models,
            "limits": {"download": 3, "model": 2, "encode": 4},
            "recommended": {"download": 2, "model": 1, "encode": 2},
            "optimal_measured": False,
        }

    def configure(self, value, persist=True):
        if (
            set(value) != {"mode", "download", "model", "encode"}
            or value["mode"] not in ("auto", "manual")
            or any(
                type(value[k]) is not int or not 1 <= value[k] <= limit
                for k, limit in (("download", 3), ("model", 2), ("encode", 4))
            )
        ):
            raise HTTPException(400, "invalid_scheduler")
        effective = {**value}
        if effective["mode"] == "auto":
            effective.update(download=2, model=1, encode=2)
        for stage, key in (("acquire", "download"), ("separate", "model"), ("finish", "encode")):
            self.pools[stage].resize(effective[key])
        self.scheduler_config = effective
        if persist:
            atomic_json(self.root / "scheduler.json", effective)
        return self.scheduler_status()

    def prioritize(self, ids):
        with self.lock:
            self.priorities = [i for i in ids if i in self.jobs][:128]
        for pool in self.pools.values():
            pool.prioritize(self.priorities)

    def _schedule(self, job_id, stage, work):
        if self.closed or self.events[job_id].is_set():
            raise ValueError("cancelled")
        self.stage_futures[job_id] = self.pools[stage].submit(job_id, work)

    def _reuse(self, job_id):
        value = self.jobs[job_id]
        origin = requested_audio_origin(value.get("source_preference", "auto"))
        for old in list(self.jobs.values()):
            if old.get("audio_origin", "song") != origin:
                continue
            # Legacy MV jobs without an origin tag still cannot become song audio.
            if origin == "song" and (old.get("video") or {}).get("method") == "same_mv_audio":
                continue
            if (
                old["id"] == job_id
                or old["song_id"] != value["song_id"]
                or old["state"] != "ready"
                or old.get("binding") != MODEL_REVISION
            ):
                continue
            folder = self.jobs_dir / job_id
            try:
                for kind, item in old["media"].items():
                    path = safe_file(self.jobs_dir / old["id"] / item["filename"], self.jobs_dir)
                    if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
                        raise ValueError("source_changed")
                    os.link(path, folder / item["filename"])
                video = dict(old.get("video", {"status": "unavailable"}))
                if video.get("status") == "ready":
                    path = safe_file(self.jobs_dir / old["id"] / "video.mp4", self.jobs_dir)
                    if digest(path) != video["sha256"]:
                        raise ValueError("source_changed")
                    os.link(path, folder / "video.mp4")
                value.update(media=old["media"], video=video, cache_hit=True, audio_origin=old.get("audio_origin", "song"))
                self._state(job_id, "ready")
                self.futures[job_id].set_result(self.snapshot(value))
                return True
            except (OSError, ValueError):
                for name in ("original.wav", "instrumental.wav", "video.mp4"):
                    (folder / name).unlink(missing_ok=True)
        return False

    def _fail(self, job_id, error):
        event = self.events[job_id]
        code = error.args[0] if isinstance(error, ValueError) and error.args else "internal_error"
        code = code if code in ERRORS else "internal_error"
        self._state(
            job_id, "cancelled" if event.is_set() else "failed", "cancelled" if event.is_set() else code
        )
        for name in ("original.wav", "instrumental.wav", "video.mp4"):
            (self.jobs_dir / job_id / name).unlink(missing_ok=True)
        future = self.futures[job_id]
        if not future.done():
            future.set_result(self.get(job_id))

    def _work(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                return
            event = self.events[job_id]
        folder = self.jobs_dir / job_id
        try:
            if event.is_set():
                raise ValueError("cancelled")
            storage_files = {}
            for path in self.root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    info = path.stat()
                    storage_files[(info.st_dev, info.st_ino)] = info.st_size
            storage = sum(storage_files.values())
            manager = getattr(self, "storage_manager", None)
            managed = manager.reserve(job_id, MAX_MEDIA * 4) if manager else False
            if (not managed and storage + MAX_MEDIA * 3 > MAX_STORAGE) or shutil.disk_usage(self.root).free < MAX_MEDIA * 4:
                raise ValueError("storage_full")
            self._state(job_id, "acquiring")
            archive = getattr(self, "archive", None)
            if archive:
                restored = archive.restore_job(self.jobs[job_id]["song_id"], folder, MODEL_REVISION,
                    audio_origin=requested_audio_origin(self.jobs[job_id].get("source_preference", "auto")))
                if restored:
                    if event.is_set():
                        raise ValueError("cancelled")
                    with self.lock:
                        self.jobs[job_id].update(restored)
                    self._state(job_id, "ready")
                    self.futures[job_id].set_result(self.get(job_id))
                    return
            if self.jobs[job_id].get("cache_only"):
                raise ValueError("source_unavailable")
            source = safe_file(self.acquire(self.jobs[job_id]["song_id"], folder, event))
            if event.is_set():
                raise ValueError("cancelled")
            self._state(job_id, "acquiring", stage="waiting_separation")
            self._schedule(job_id, "separate", lambda: self._separate_work(job_id, source))
        except Exception as error:
            self._fail(job_id, error)

    def _separate_work(self, job_id, source):
        event = self.events[job_id]
        folder = self.jobs_dir / job_id
        try:
            if event.is_set():
                raise ValueError("cancelled")
            admitted = False
            try:
                while not admitted:
                    if event.is_set():
                        raise ValueError("cancelled")
                    with self.model_condition:
                        # A second model is optional; retain at least 6 GiB available before admitting it.
                        if self.active_models == 0 or (
                            self.scheduler_config["model"] > 1
                            and self.memory_available is not None
                            and self.memory_available() >= 6 * 1024**3
                        ):
                            self.active_models += 1
                            admitted = True
                        else:
                            if self.jobs[job_id].get("stage") != "waiting_memory":
                                self._state(job_id, "acquiring", stage="waiting_memory")
                            self.model_condition.wait(0.5)
                self._state(job_id, "separating")
                instrumental = safe_file(self.separate(source, folder, event))
            finally:
                if admitted:
                    with self.model_condition:
                        self.active_models -= 1
                        self.model_condition.notify_all()
            if event.is_set():
                raise ValueError("cancelled")
            self._state(job_id, "separating", stage="waiting_transcode")
            self._schedule(job_id, "finish", lambda: self._finish_work(job_id, source, instrumental))
        except Exception as error:
            self._fail(job_id, error)

    def _finish_work(self, job_id, source, instrumental):
        event = self.events[job_id]
        folder = self.jobs_dir / job_id
        try:
            if event.is_set():
                raise ValueError("cancelled")
            self._state(job_id, "transcoding")
            items = {}
            for kind, input_path in (("original", source), ("instrumental", instrumental)):
                output = folder / (kind + ".wav")
                self.transcode(input_path, output, event)
                safe_file(output, folder)
                with wave.open(str(output), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
                    if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (48000, 2, 2):
                        raise ValueError("invalid_audio")
                    if not 0 < duration <= MAX_DURATION_SECONDS:
                        raise ValueError("invalid_audio")
                    if output.stat().st_size != 44 + audio.getnframes() * 4:
                        raise ValueError("invalid_audio")
                items[kind] = {
                    "filename": output.name,
                    "bytes": output.stat().st_size,
                    "sha256": digest(output),
                    "duration_seconds": duration,
                }
            if abs(items["original"]["duration_seconds"] - items["instrumental"]["duration_seconds"]) > 0.1:
                raise ValueError("invalid_audio")
            video = {"status": "unavailable"}
            self._state(job_id, "transcoding", stage="video")
            if self.video is not None and not event.is_set():
                try:
                    candidate = self.video(
                        self.jobs[job_id]["song_id"], folder / "original.wav", folder, event
                    )
                    path = safe_file(folder / "video.mp4", folder)
                    if (
                        not valid_video(candidate, items["original"]["sha256"])
                        or path.stat().st_size != candidate["bytes"]
                        or digest(path) != candidate.get("sha256")
                    ):
                        raise ValueError("invalid_video")
                    video = candidate
                except Exception:
                    (folder / "video.mp4").unlink(missing_ok=True)
            with self.lock:
                if event.is_set():
                    raise ValueError("cancelled")
                self.jobs[job_id].update(media=items, video=video, state="ready", code="none")
                self._save(self.jobs[job_id])
            self._state(job_id, "ready")
            if not self.futures[job_id].done():
                self.futures[job_id].set_result(self.get(job_id))
        except Exception as error:
            self._fail(job_id, error)

    def cancel(self, job_id):
        with self.lock:
            value = self.jobs.get(job_id)
            if value is None:
                raise HTTPException(404, "not_found")
            if value["state"] in ACTIVE:
                self.events[job_id].set()
                if value["state"] == "queued" or (
                    self.stage_futures.get(job_id) is not None and not self.stage_futures[job_id].running()
                ):
                    future = self.futures.get(job_id)
                    if future is not None:
                        future.cancel()
                    stage_future = self.stage_futures.get(job_id)
                    if stage_future is not None:
                        stage_future.cancel()
                    value.update(state="cancelled", code="cancelled")
                    self._save(value)
            return self.snapshot(value)

    def remove(self, job_id):
        with self.lock:
            value = self.jobs.get(job_id)
            if value is None:
                raise HTTPException(404, "not_found")
            if value["state"] in ACTIVE:
                raise HTTPException(409, "cancel_before_remove")
            future = self.futures.get(job_id)
            if future is not None and not future.done():
                raise HTTPException(409, "worker_stopping")
            folder = self.jobs_dir / job_id
            if folder.resolve() != folder:
                raise HTTPException(409, "unsafe_job")
            shutil.rmtree(folder)
            del self.jobs[job_id]
            del self.events[job_id]
            self.futures.pop(job_id, None)
            self.stage_futures.pop(job_id, None)
            return {"removed": True, "original_download_deleted": False}

    def close(self):
        with self.lock:
            self.closed = True
            for job_id, value in self.jobs.items():
                if value["state"] in ACTIVE:
                    self.events[job_id].set()
        for pool in self.pools.values():
            pool.shutdown(wait=False)
        for pool in self.pools.values():
            pool.shutdown(wait=True)

    def media(self, job_id, kind):
        with self.lock:
            value = self.jobs.get(job_id)
            if (
                value is None
                or value["state"] != "ready"
                or kind not in {"original", "instrumental", "video"}
            ):
                raise HTTPException(404, "media_not_ready")
            if kind == "video":
                if value.get("video", {}).get("status") != "ready":
                    raise HTTPException(404, "media_not_ready")
                item = dict(value["video"])
            else:
                item = dict(value["media"][kind])
        path = self.jobs_dir / job_id / ("video.mp4" if kind == "video" else kind + ".wav")
        try:
            safe_file(path, self.jobs_dir)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            stream = os.fdopen(fd, "rb")
            h = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
            if h.hexdigest() != item["sha256"] or os.fstat(stream.fileno()).st_size != item["bytes"]:
                stream.close()
                raise ValueError("source_changed")
            stream.seek(0)
            return stream, item
        except (OSError, ValueError):
            if kind != "video":
                self._state(job_id, "failed", "source_changed")
            raise HTTPException(409, "media_integrity_failed") from None


def run_bounded(command: list[str], event: threading.Event, *, timeout: float):
    from .ktv_accompaniment import _terminate

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if event.wait(0.05):
                raise ValueError("cancelled")
            if time.monotonic() > deadline:
                raise ValueError("transcode_failed")
        if process.returncode != 0:
            raise ValueError("transcode_failed")
    finally:
        _terminate(process)


def transcode_pcm(source: Path, output: Path, event: threading.Event, *, ffmpeg: str):
    # Rewrap with stdlib wave to eliminate extra RIFF chunks; Android consumes one exact PCM contract.
    intermediate = output.with_suffix(".partial.wav")
    try:
        run_bounded(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-t",
                str(MAX_DURATION_SECONDS + 1),
                "-map_metadata",
                "-1",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(intermediate),
            ],
            event,
            timeout=180,
        )
        safe_file(intermediate)
        with wave.open(str(intermediate), "rb") as reader, wave.open(str(output), "wb") as writer:
            writer.setparams(reader.getparams())
            while chunk := reader.readframes(8192):
                if event.is_set():
                    raise ValueError("cancelled")
                writer.writeframesraw(chunk)
        output.chmod(0o600)
    finally:
        intermediate.unlink(missing_ok=True)


def separator(cache: Path):
    def separate(source, folder, event):
        result = prepare(source, cache, folder / "preparation.json", cancel=event)
        if result.get("status") != "ready" or result.get("model_verified") is not True:
            raise ValueError("separation_failed")
        return Path(result["instrumental_path"])

    return separate


def prepared_lyrics(queue, job_id, lyrics=None, source_api=None):
    job = queue.get(job_id)
    try:
        from .ktv_cues import with_count_ins
        folder = queue.jobs_dir / job_id
        def annotate(value):
            return with_count_ins(value, folder / "preparation.json", original=folder / "original.wav", instrumental=folder / "instrumental.wav")
        if job.get("audio_origin") == "cloud" and source_api is not None:
            return annotate(source_api.cloud_lyrics(job["song_id"]))
        # Provider lyrics do not establish the timeline of an MV edit/live take.
        if lyrics is not None and job.get("audio_origin") == "song":
            return annotate(lyrics(job["song_id"]))
    except Exception:
        pass
    return {"status": "unavailable", "lines": []}


def create_app(
    queue: Queue,
    token: str,
    catalog: Callable[[], list[dict]] = lambda: [],
    search: Callable[[str], list[dict]] | None = None,
    lyrics: Callable[[str], dict] | None = None,
    bridge=None,
    cache_manager=None,
    console_origin=None,
    source_api=None,
    archive_manager=None,
    library=None,
) -> FastAPI:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("strong_token_required")

    @asynccontextmanager
    async def lifespan(app):
        if archive_manager is not None:
            archive_manager.start()
        if bridge is not None:
            bridge.start()
        if library is not None:
            library.start()
        yield
        if library is not None:
            library.close()
        if bridge is not None:
            bridge.close()
        if archive_manager is not None:
            archive_manager.close()
        queue.close()
        if source_api is not None and callable(getattr(source_api, "close", None)):
            source_api.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    from .ktv_worker import MediaTickets
    media_tickets = MediaTickets(token)
    tv_telemetry = {}
    console_sessions = {}
    console_host = console_origin.removeprefix("http://").split(":")[0] if console_origin else None

    @app.post("/v1/tv/diagnostics")
    def report_tv(value: dict):
        # Bounded aggregate counters and device capabilities only; never audio or device serials.
        allowed = {
            "direct_monitor_input_frames", "direct_monitor_output_frames", "direct_monitor_voice_frames",
            "direct_monitor_input_dropped", "direct_monitor_trimmed", "direct_monitor_missing",
            "direct_monitor_peak", "direct_monitor_native_error", "direct_monitor_output_performance",
            "direct_monitor_input_performance", "direct_monitor_output_buffer", "direct_monitor_input_buffer",
            "direct_monitor_result", "direct_monitor_finished_at", "direct_monitor_output_type", "direct_monitor_elapsed_ms", "direct_monitor_output_api",
            "direct_monitor_error_stream", "direct_monitor_quiet", "direct_monitor_gap_max_us",
            "direct_monitor_callback_count", "direct_monitor_expired", "direct_monitor_input_peak",
            "direct_monitor_drop_counts_available", "direct_monitor_output_only",
            "direct_monitor_mode", "direct_monitor_output_api_actual", "direct_monitor_input_api_actual",
            "direct_monitor_output_mmap", "direct_monitor_input_mmap", "direct_monitor_output_sharing",
            "direct_monitor_input_sharing", "direct_monitor_output_state", "direct_monitor_input_state",
            "direct_monitor_output_route", "direct_monitor_input_route", "direct_monitor_output_capacity",
            "direct_monitor_input_capacity", "direct_monitor_output_burst", "direct_monitor_input_burst",
            "direct_monitor_output_frames_written", "direct_monitor_output_frames_read",
            "direct_monitor_input_frames_written", "direct_monitor_input_frames_read",
            "direct_monitor_output_timestamp_available", "direct_monitor_output_timestamp_error",
            "direct_monitor_output_timestamp_position", "direct_monitor_output_timestamp_age_us",
            "direct_monitor_output_pending_frames", "direct_monitor_input_timestamp_available",
            "direct_monitor_input_timestamp_error", "direct_monitor_input_timestamp_position",
            "direct_monitor_input_timestamp_age_us", "direct_monitor_input_pending_frames",
            "direct_monitor_snapshot_elapsed_ms", "direct_monitor_snapshot_final",
            "direct_monitor_last_callback_age_us", "direct_monitor_mmap_policy_available",
            "direct_monitor_mmap_policy_before", "direct_monitor_mmap_policy_set_error",
            "direct_monitor_mmap_policy_restored", "direct_monitor_output_pending_available",
            "direct_monitor_input_pending_available", "direct_monitor_output_timestamp_future",
            "direct_monitor_input_timestamp_future",
            "direct_monitor_output_sample_rate_actual", "direct_monitor_input_sample_rate_actual",
            "direct_monitor_baseline_input_frames", "direct_monitor_baseline_output_frames",
            "direct_monitor_baseline_voice_frames", "direct_monitor_baseline_missing",
            "direct_monitor_baseline_elapsed_ms", "direct_monitor_gate_failures",
            "direct_monitor_steady_start_ms", "direct_monitor_startup_discarded_frames",
            "direct_monitor_steady_input_frames", "direct_monitor_steady_output_frames",
            "direct_monitor_steady_missing_frames", "direct_monitor_steady_gap_max_us",
            "direct_monitor_steady_short_read_callbacks", "direct_monitor_input_observed_samples",
            "direct_monitor_input_full_scale_samples", "direct_monitor_input_full_scale_run_max_frames",
            "direct_monitor_audible_output_frames", "direct_monitor_output_limited_frames",
            "direct_monitor_output_limited_run_max_frames", "direct_monitor_audible_input_mono_abs_sum",
            "direct_monitor_output_abs_sum", "direct_monitor_steady_poll_count",
            "direct_monitor_output_pending_valid_count", "direct_monitor_output_pending_min_us",
            "direct_monitor_output_pending_max_us", "direct_monitor_output_pending_last_us",
            "direct_monitor_input_pending_valid_count", "direct_monitor_input_pending_min_us",
            "direct_monitor_input_pending_max_us", "direct_monitor_input_pending_last_us",
            "direct_monitor_output_counter_min_us", "direct_monitor_output_counter_max_us",
            "direct_monitor_input_counter_min_us", "direct_monitor_input_counter_max_us",
            "direct_monitor_output_xrun_available", "direct_monitor_output_xrun_delta",
            "direct_monitor_input_xrun_available", "direct_monitor_input_xrun_delta",
            "direct_monitor_clock_span_ms", "direct_monitor_input_produced_frames_delta",
            "direct_monitor_output_presented_frames_delta", "direct_monitor_legacy_gain_would_limit_frames",
            "duplex_attempted", "duplex_open_code", "duplex_start_code",
            "duplex_open_failures", "duplex_start_failures", "duplex_input_dropped_available",
            "duplex_input_read_frames", "duplex_startup_input_discarded_frames",
            "duplex_output_callback_count", "duplex_output_callback_min_frames",
            "duplex_output_callback_max_frames", "duplex_output_callback_batch_max_frames",
            "duplex_output_callback_batch_count", "duplex_input_read_wait_max_us",
            "duplex_input_read_wait_p95_us", "duplex_output_queue_budget_frames",
            "duplex_output_raw_rate", "duplex_input_raw_rate",
            "duplex_output_raw_capacity", "duplex_input_raw_capacity",
            "duplex_output_raw_burst", "duplex_input_raw_burst",
            "duplex_output_raw_written", "duplex_output_raw_read",
            "duplex_input_raw_written", "duplex_input_raw_read",
            "duplex_output_mmap", "duplex_input_mmap", "duplex_mmap_policy_restored",
            "duplex_output_timestamp_available", "duplex_output_pending_raw_frames",
            "duplex_input_timestamp_available", "duplex_input_pending_raw_frames",
            "duplex_output_pending_available", "duplex_input_pending_available",
            "duplex_input_callback_metrics_available", "duplex_mmap_policy_available",
            "duplex_mmap_policy_before", "duplex_mmap_policy_set_error",
            "duplex_startup_drain_completed", "duplex_output_state", "duplex_input_state",
            "duplex_schema_version",
            "duplex_sample_client_returned_frames", "duplex_sample_client_discarded_frames",
            "duplex_sample_begin_ms", "duplex_sample_span_us", "duplex_sample_input_timestamp_error",
            "duplex_sample_input_timestamp_position", "duplex_sample_input_timestamp_age_us",
            "duplex_sample_input_timestamp_future", "duplex_read_call_count", "duplex_read_lock_wait_max_us",
            "duplex_read_total_max_us", "duplex_read_success_count", "duplex_delivery_gap_max_us",
            "duplex_delivery_gap_over_10ms_count", "duplex_delivery_gap_over_20ms_count",
            "duplex_delivery_gap_over_50ms_count", "duplex_platform_refresh_count",
            "duplex_platform_refresh_max_us", "duplex_platform_refresh_last_us",
            "duplex_route_refresh_lock_wait_max_us", "duplex_sample_started_valid",
            "duplex_delivery_last_age_us", "duplex_read_return_min_frames", "duplex_read_return_max_frames",
            "duplex_input_hardware_rate", "duplex_input_hardware_rate_available", "duplex_input_rate_mode",
            "audio_mixed_peak",
            "sdk", "input_sample_rate", "output_sample_rate", "mic_target_frames", "mic_peak_queue_frames", "mic_rate_ppm", "mic_rebuffers", "input_age_max_us",
            "input_open_failures", "input_fallbacks",
            "input_read_p95_us", "output_buffer_requested",
            "input_age_available", "underruns_available", "io_input_delivery_age_us", "native_clock_estimated", "native_hardware_timing_available", "native_hardware_xruns_available", "audio_backend", "last_session_backend", "native_open_failures", "native_open_code", "io_presented_frames", "io_queued_frames", "io_output_device", "io_input_device", "io_output_buffer", "io_input_buffer", "io_output_burst", "io_input_burst", "io_output_performance", "io_input_performance", "io_input_dropped", "io_output_missing", "io_output_xruns", "io_input_xruns", "io_error", "io_input_age_us", "io_output_rate", "io_input_rate", "io_input_queue", "io_input_gap_p95_us", "io_input_gap_max_us", "io_output_callback_frames", "io_output_callback_size", "io_output_gap_max_us", "io_output_api", "io_observed_at_ms", "io_input_callback_frames",
            "has_low_latency", "has_pro_audio", "native_output_code", "native_output_performance", "native_output_burst", "native_output_buffer", "native_output_sharing", "native_output_device", "native_output_frames", "native_output_peak", "native_input_code", "native_input_performance", "native_input_burst", "native_input_buffer", "native_input_sharing", "native_input_device", "native_input_frames", "native_input_peak",
            "last_session_input_frames", "last_session_dropped", "last_session_missing", "last_session_input_age_us", "last_session_output_type", "last_session_fault",
            "tv_cache_bytes", "tv_cache_limit", "input_source", "input_effects_disabled", "input_effects_failed",
            "backing_switches", "backing_failures",
            "last_input_frames", "last_input_peak_a", "last_input_peak_b", "last_input_clipped_a", "last_input_clipped_b",
            "last_input_rms_a", "last_input_rms_b", "last_input_dc_a", "last_input_dc_b", "last_input_ready", "last_input_fault", "safety_reason",
            "feedback_notches",
            "feedback_events",
            "feedback_peak_hz",
            "input_frames",
            "output_frames",
            "mic_peak",
            "permission",
            "inputs",
            "usb",
            "output_device_id",
            "output_buffer_frames",
            "underruns",
            "mv_ready",
            "input_device_id",
            "output_type",
            "input_buffer_frames",
            "input_age_us",
            "dsp_max_us",
            "output_queued_frames",
            "missing_mic_frames",
            "dropped_mic_frames",
            "queued_mic_frames",
            "limited_samples",
            "safety_latched",
            "input_clipped_samples",
            "performance_mode",
            "dsp_benchmark_p95_us",
            "video_seek_count",
            "video_drift_abs_ms",
            "video_buffer_events",
            "video_surface_frames",
            "video_speed_supported",
            "apk_version",
            "next_prefetched",
            "warmup_active", "warmup_pool", "lyric_lines",
            "video_position_ms",
            "video_target_ms",
            "video_seek_result_ms",
            "video_prepare_count",
            "route_restart_count",
        }
        allowed.update({
            'vendor_microphone_mode',
            'vendor_mic_enabled',
            'vendor_mic_permission',
            'vendor_mic_desired',
            'vendor_mic_muted',
            'vendor_mic_state',
            'vendor_mic_hal_status',
            'vendor_gain_setpoint',
            'vendor_mic_gain_applied',
            'vendor_mic_owned',
            'vendor_mic_fault',
            'vendor_carrier_pending',
            'software_voice_processing_active',
            'vendor_mic_start_calls',
            'vendor_mic_stop_calls',
            'vendor_mic_lease_id',
            'vendor_mic_command_exceptions',
            'vendor_mic_started_ms',
            'vendor_mic_stopped_ms',
            'vendor_carrier_ready',
            'vendor_carrier_released',
            'vendor_carrier_frames_written',
            'vendor_carrier_playback_head_frames',
            'vendor_carrier_underruns',
            'vendor_carrier_actual_route',
            'vendor_carrier_actual_route_type',
            'vendor_carrier_fault',
        })
        wet_flags = {
            "wet_ready", "wet_active", "wet_fault", "wet_explicit_trial", "wet_cache_verified",
            "wet_capture_reopen_excluded", "wet_audition_verified", "wet_process_isolated",
            "wet_timestamp_available", "wet_muted", "wet_bypass", "wet_rendered",
            "wet_cache_hook_requested",
            "software_voice_processing_wet_only", "software_dry_voice_processing_active",
            "audio_backend_selected", "audio_backend_selection_pending",
            "system_audio_contract_ready", "system_audio_silent_start", "system_audio_cache_only",
            "compat_effects_capable", "compat_effects_applied", "compat_effects_gated", "compat_voice_zero_gate",
        }
        wet_counters = {
            "wet_requested_profile", "wet_applied_profile", "wet_pid", "wet_starts", "wet_closes",
            "wet_input_frames", "wet_input_nonzero_samples", "wet_input_peak_pcm16",
            "wet_output_frames", "wet_output_nonzero_samples", "wet_output_peak_pcm16",
            "wet_input_timestamp_age_us", "wet_queue_frames", "wet_queue_max_frames",
            "wet_producer_dropped_frames", "wet_consumer_dropped_frames", "wet_missing_input_frames",
            "wet_stale_input_frames", "wet_output_actual_device_id", "wet_command_epoch",
            "wet_applied_epoch", "wet_reason_code",
            "wet_native_fault", "wet_native_input_device_id", "wet_native_sample_rate",
            "wet_native_channels", "wet_native_format", "wet_native_sharing_mode",
            "wet_native_hardware_rate", "wet_native_hardware_channels", "wet_native_hardware_format",
            "wet_native_output_stage", "wet_native_input_open_result_error", "wet_native_input_start_result_error",
            "wet_native_output_open_result_error", "wet_native_output_start_result_error",
            "wet_cache_enable_calls", "wet_cache_disable_calls", "wet_cache_hook_error",
            "audio_backend_choice", "compat_voice_gate_frames",
        }
        voice_flags={"voice_protection_ready", "voice_protection_latched"}
        voice_limits={"voice_feedback_frames":10**12,"voice_feedback_sequence":10**12,"voice_feedback_level":3,"voice_feedback_hz":12000,
                      "voice_feedback_peak":32768,"voice_feedback_clipped":10**12,"voice_feedback_ready":1,"voice_feedback_latched":1,
                      "voice_input_discarded_frames":10**12,"voice_input_backlog_frames":10**12,"voice_protection_gain_cap":100,
                      "voice_protection_level":3,"voice_protection_reason":6,"voice_protection_sequence":10**12,"voice_requested_gain_setpoint":100}
        allowed.update(wet_flags | wet_counters | voice_flags | set(voice_limits))
        production_summary = (
            "result", "valid", "quiet", "finished_at_s", "elapsed_ms", "steady_ms", "baseline_elapsed_ms",
            "gate_failures", "cancel_reason", "backend", "expected_input_route", "expected_output_route",
            "actual_input_route", "actual_output_route", "input_rate", "output_rate", "input_delta_frames",
            "output_delta_frames", "render_delta_frames", "native_input_delta_frames", "java_missing_delta_frames",
            "java_drop_delta_frames", "rebuffer_delta", "native_output_missing_delta_frames", "output_peak",
            "input_fault", "safety_reason", "native_error", "target_frames", "queue_frames", "queue_peak_frames",
            "output_queue_frames", "dsp_max_us", "raw_input_rate", "raw_output_rate", "input_drop_available",
            "input_dropped_frames", "java_drop_available", "snapshot_before_stop", "stop_completed", "schema_version",
            "captured_frames", "rendered_frames", "java_missing_frames", "java_dropped_frames", "rebuffer_count",
            "input_read_p95_us", "rate_ppm", "native_baseline_snapshot_ms", "native_final_snapshot_ms",
            "input_monitor_ready", "input_peak", "output_type", "duplex_attempted", "duplex_open_code", "duplex_start_code", "engine_end_code",
        )
        allowed.update("production_check_" + suffix for suffix in production_summary)
        allowed.update(f"production_check_native_{index:02d}" for index in range(91))
        if set(value) - allowed:
            raise HTTPException(400, "invalid_diagnostics")
        for key, item in value.items():
            if key in voice_flags and type(item) is not bool:
                raise HTTPException(400, "invalid_diagnostics")
            if key in voice_limits and (type(item) is not int or not 0 <= item <= voice_limits[key]):
                raise HTTPException(400, "invalid_diagnostics")
            if key in wet_flags and type(item) is not bool:
                raise HTTPException(400, "invalid_diagnostics")
            if key in wet_counters:
                limit = {"wet_requested_profile": 6, "wet_applied_profile": 6,
                         "audio_backend_choice": 2,
                         "wet_input_peak_pcm16": 32768, "wet_output_peak_pcm16": 32768,
                         "wet_reason_code": 7}.get(key, 10**12)
                if type(item) is not int or not 0 <= item <= limit:
                    raise HTTPException(400, "invalid_diagnostics")
            if (key == "wet_input_timestamp_age_us" and item > 0
                    and value.get("wet_timestamp_available") is not True):
                raise HTTPException(400, "invalid_diagnostics")
            if key in {"inputs", "usb"}:
                if not isinstance(item, list) or len(item) > 16:
                    raise HTTPException(400, "invalid_diagnostics")
                fields = {"id", "type", "channels", "vendor", "product", "interfaces"}
                for device in item:
                    if not isinstance(device, dict) or set(device) - fields:
                        raise HTTPException(400, "invalid_diagnostics")
                    for field, number in device.items():
                        values = number if field in {"channels", "interfaces"} else [number]
                        if (
                            not isinstance(values, list)
                            or len(values) > 16
                            or any(type(n) is not int or not 0 <= n <= 65535 for n in values)
                        ):
                            raise HTTPException(400, "invalid_diagnostics")
            elif key == "production_check_rate_ppm":
                if type(item) is not int or not -1000 <= item <= 1000:
                    raise HTTPException(400, "invalid_diagnostics")
            elif type(item) not in {int, bool} or not -1 <= item <= 10**12:
                raise HTTPException(400, "invalid_diagnostics")
            if key.startswith("production_check_"):
                if type(item) is not int:
                    raise HTTPException(400, "invalid_diagnostics")
                # -1 is an explicit unobserved value; these are historical check receipts,
                # not permission to start the microphone or an acoustic-latency claim.
                production_limits = {
                    "result": 7, "cancel_reason": 6, "backend": 2, "schema_version": 1,
                    "gate_failures": 131071, "output_peak": 32768, "input_peak": 32768,
                    "input_rate": 384000, "output_rate": 384000,
                    "raw_input_rate": 384000, "raw_output_rate": 384000,
                    "target_frames": 11520, "queue_frames": 11520, "queue_peak_frames": 11520,
                    "native_16": 384000, "native_17": 384000,
                    "native_37": 384000, "native_38": 384000,
                    "native_47": 2, "native_48": 2, "native_58": 3,
                    "native_61": 14, "native_62": 14, "native_63": 1,
                    "native_88": 384000, "native_89": 1, "native_90": 1,
                    "duplex_attempted": 1, "duplex_open_code": 100000, "duplex_start_code": 100000, "engine_end_code": 10,
                }
                for suffix in ("valid", "quiet", "input_drop_available", "java_drop_available",
                               "snapshot_before_stop", "stop_completed", "input_monitor_ready",
                               "native_49", "native_50", "native_52", "native_54", "native_55",
                               "native_56", "native_57", "native_60", "native_71", "native_84"):
                    production_limits[suffix] = 1
                suffix = key.removeprefix("production_check_")
                if suffix in production_limits and item > production_limits[suffix]:
                    raise HTTPException(400, "invalid_diagnostics")
            if key == "audio_mixed_peak" and (type(item) is not int or not 0 <= item <= 32768):
                raise HTTPException(400, "invalid_diagnostics")
            if key.startswith("duplex_"):
                if key in {"duplex_attempted", "duplex_input_dropped_available"}:
                    if type(item) is not bool:
                        raise HTTPException(400, "invalid_diagnostics")
                else:
                    if type(item) is not int or item < 0:
                        raise HTTPException(400, "invalid_diagnostics")
                    duplex_limits = {
                        "duplex_output_raw_rate": 384000, "duplex_input_raw_rate": 384000,
                        "duplex_output_mmap": 2, "duplex_input_mmap": 2,
                        "duplex_output_state": 14, "duplex_input_state": 14,
                        "duplex_mmap_policy_before": 3, "duplex_schema_version": 1,
                        "duplex_input_hardware_rate": 384000, "duplex_input_rate_mode": 1,
                    }
                    for suffix in ("mmap_policy_restored", "output_timestamp_available",
                                   "input_timestamp_available", "output_pending_available",
                                   "input_pending_available", "input_callback_metrics_available",
                                   "mmap_policy_available", "startup_drain_completed",
                                   "sample_started_valid", "sample_input_timestamp_future", "input_hardware_rate_available"):
                        duplex_limits["duplex_" + suffix] = 1
                    if key in duplex_limits and item > duplex_limits[key]:
                        raise HTTPException(400, "invalid_diagnostics")
            if key.startswith("direct_monitor_"):
                if type(item) is not int or item < 0:
                    raise HTTPException(400, "invalid_diagnostics")
                limits = {"direct_monitor_peak": 2621, "direct_monitor_result": 7,
                          "direct_monitor_elapsed_ms": 20000, "direct_monitor_output_api": 2,
                          "direct_monitor_error_stream": 2, "direct_monitor_quiet": 1,
                          "direct_monitor_expired": 1, "direct_monitor_input_peak": 32768,
                          "direct_monitor_drop_counts_available": 1, "direct_monitor_output_only": 1}
                limits.update({"direct_monitor_mode": 2,
                               "direct_monitor_output_api_actual": 2, "direct_monitor_input_api_actual": 2,
                               "direct_monitor_output_mmap": 2, "direct_monitor_input_mmap": 2,
                               "direct_monitor_output_sharing": 1, "direct_monitor_input_sharing": 1,
                               "direct_monitor_output_state": 14, "direct_monitor_input_state": 14,
                               "direct_monitor_snapshot_elapsed_ms": 20000,
                               "direct_monitor_baseline_elapsed_ms": 20000,
                               "direct_monitor_steady_start_ms": 20000,
                               "direct_monitor_clock_span_ms": 20000,
                               "direct_monitor_output_sample_rate_actual": 384000,
                               "direct_monitor_input_sample_rate_actual": 384000,
                               "direct_monitor_gate_failures": 1023,
                               "direct_monitor_mmap_policy_before": 3})
                for suffix in ("output_timestamp_available", "input_timestamp_available", "snapshot_final",
                               "mmap_policy_available", "mmap_policy_restored", "output_pending_available",
                               "input_pending_available", "output_timestamp_future", "input_timestamp_future",
                               "output_xrun_available", "input_xrun_available"):
                    limits["direct_monitor_" + suffix] = 1
                if key in limits and item > limits[key]:
                    raise HTTPException(400, "invalid_diagnostics")
                if key == "direct_monitor_output_type" and item not in {0, 2}:
                    raise HTTPException(400, "invalid_diagnostics")
        tv_telemetry.clear()
        tv_telemetry.update(value, received_at=int(time.time()))
        return {"accepted": True}

    @app.get("/v1/tv/diagnostics")
    def read_tv():
        return tv_telemetry

    @app.middleware("http")
    async def guard(request: Request, call_next):
        from fastapi.responses import JSONResponse

        # Native clients send an authorization header. No URL tokens, cookies, CORS or cross-site writes.
        supplied = request.headers.get("X-KTV-Key", "")
        console_path = request.url.path.startswith("/console")
        local_console = (
            console_origin
            and request.client
            and request.client.host == console_host
            and request.headers.get("host") == console_origin.removeprefix("http://")
        )
        owner_path = bool(re.fullmatch(r"/v1/manage/(?:library|preview|delete|policy|room|search|room/(?:theme|controls|command|order|queue-remove|songs|retry|preferences))", request.url.path))
        if request.url.path.startswith("/v1/manage/") and not local_console:
            return JSONResponse({"code": "not_found"}, status_code=404)
        if (request.url.path.startswith("/v1/source/") or request.url.path == "/v1/cache/normalize-video") and not local_console:
            return JSONResponse({"code": "not_found"}, status_code=404)
        session = request.cookies.get("friday-ktv-owner", "")
        scoped_path = owner_path or request.url.path in {
            "/v1/cache",
            "/v1/cache/delete",
            "/v1/cache/normalize-video",
            "/v1/cache/clear-local",
            "/v1/jobs",
            "/v1/scheduler",
            "/v1/source/status",
            "/v1/source/login",
            "/v1/source/poll",
            "/v1/source/qr",
            "/v1/source/settings",
            "/v1/archive",
        } or bool(re.fullmatch(r"/v1/jobs/[a-f0-9-]{36}/journal", request.url.path))
        if request.url.path == "/v1/jobs" and request.method != "GET":
            scoped_path = False
        console_auth = local_console and scoped_path and console_sessions.get(session, 0) > time.monotonic()
        if console_path:
            if (
                not local_console
                or request.method != "GET"
                or (
                    request.headers.get("sec-fetch-site") == "cross-site"
                    and not (
                        request.headers.get("sec-fetch-mode") == "navigate"
                        and request.headers.get("sec-fetch-dest") == "document"
                    )
                )
            ):
                return JSONResponse({"code": "not_found"}, status_code=404)
        elif console_auth:
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                request.method == "POST" and request.headers.get("origin") != console_origin
            ):
                return JSONResponse({"code": "origin_refused"}, status_code=403)
        else:
            media_match = re.fullmatch(r"/v1/jobs/([a-f0-9-]{36})/media/(original|instrumental|video|guide_original|guide_reference)", request.url.path)
            media_authorized = False
            if request.method in {"GET", "HEAD"} and media_match and request.headers.get("X-KTV-Media"):
                try:
                    media_authorized = media_tickets.permits(request.headers["X-KTV-Media"], queue.get(media_match[1]))
                except Exception:
                    pass
            if not media_authorized and (len(supplied) > 128 or not hmac.compare_digest(supplied, token)):
                return JSONResponse({"code": "unauthorized"}, status_code=401)
            if request.headers.get("origin"):
                return JSONResponse({"code": "origin_refused"}, status_code=403)
        if request.method not in {"GET", "HEAD", "POST"}:
            return JSONResponse({"code": "method_refused"}, status_code=405)
        if request.method == "POST":
            total = 0
            chunks = []
            # Audio diagnostics carries the complete bounded capability/counter
            # schema. Keep the smaller limit for control and job requests.
            body_limit = 32768 if owner_path else 24576 if request.url.path == "/v1/tv/diagnostics" else 4096
            async for chunk in request.stream():
                total += len(chunk)
                if total > body_limit:
                    return JSONResponse({"code": "request_too_large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if console_path:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            if request.url.path == "/console":
                for stale in [k for k, v in console_sessions.items() if v <= time.monotonic()]:
                    console_sessions.pop(stale, None)
                if len(console_sessions) >= 8:
                    console_sessions.pop(next(iter(console_sessions)))
                credential = secrets.token_urlsafe(32)
                console_sessions[credential] = time.monotonic() + 14400
                response.set_cookie(
                    "friday-ktv-owner",
                    credential,
                    httponly=True,
                    samesite="strict",
                    max_age=14400,
                    path="/v1",
                )
        return response

    @app.get("/console")
    @app.get("/console/{asset}")
    def console(asset: str = "index.html"):
        from fastapi.responses import Response

        names = {
            "index.html": "text/html; charset=utf-8",
            "app.js": "text/javascript; charset=utf-8",
            "style.css": "text/css; charset=utf-8",
            "party-audio.js": "text/javascript; charset=utf-8",
            "icon.png": "image/png",
        }
        if asset not in names:
            raise HTTPException(404, "not_found")
        if asset == "party-audio.js":
            return Response((Path(__file__).resolve().parents[1] / "integrations/ktv_public/web/party-audio.js").read_bytes(), media_type=names[asset])
        if asset == "icon.png":
            return Response((Path(__file__).with_name("static") / "brand/friday.png").read_bytes(), media_type=names[asset])
        return Response(
            (Path(__file__).with_name("static") / "ktv-console" / asset).read_bytes(), media_type=names[asset]
        )

    @app.get("/v1/scheduler")
    def scheduler():
        return queue.scheduler_status()

    def owner_library():
        if library is None:
            raise HTTPException(503, "library_unavailable")
        return library

    @app.get("/v1/manage/library")
    def library_inventory():
        return {**owner_library().inventory(), "simulated": queue.simulated}

    @app.post("/v1/manage/preview")
    def library_preview(value: dict):
        return owner_library().preview(value)

    @app.post("/v1/manage/delete")
    def library_delete(value: dict):
        return owner_library().delete(value)

    @app.post("/v1/manage/policy")
    def library_policy(value: dict):
        if (set(value) != {"host", "value"} or not isinstance(value["host"], str)
                or value["host"] not in {"mac", "nas"} or not isinstance(value["value"], dict)):
            raise HTTPException(400, "invalid_policy")
        manager = owner_library()
        return manager.policy(value["value"]) if value["host"] == "mac" else manager.nas_policy(value["value"])

    @app.get("/v1/manage/room")
    def owner_room():
        return {**owner_library().remote("room"), "simulated": queue.simulated}

    @app.get("/v1/manage/search")
    def owner_search(q: str = "", source: str = "song", offset: int = 0):
        if not (0 if source == "cloud" else 1) <= len(q.strip()) <= 120 or source not in {"song", "mv", "cloud"} or not 0 <= offset <= 100000:
            raise HTTPException(400, "invalid_query")
        return owner_library().remote("search", {"q": q.strip(), "source": source, "offset": offset})

    @app.post("/v1/manage/room/{operation}")
    def owner_room_action(operation: str, value: dict):
        if operation not in {"theme", "controls", "command", "order", "queue-remove", "songs", "retry", "preferences"}:
            raise HTTPException(404, "not_found")
        manager = owner_library()
        if operation == "controls" and isinstance(value.get("value"), dict) and value["value"].get("mic_muted") == 0:
            current = manager.remote("room")
            if not current.get("active") or current.get("room_id") != value.get("room_id"):
                raise HTTPException(409, "room_changed")
            if not current.get("status", {}).get("playback", {}).get("online"):
                raise HTTPException(409, "tv_offline")
        return manager.remote(operation, value)

    def source_call(method):
        if source_api is None:
            raise HTTPException(503, "source_api_unavailable")
        try:
            return getattr(source_api, method)()
        except ValueError as error:
            code = str(error)
            raise HTTPException(503, code if code in ERRORS else "source_api_unavailable") from None

    @app.get("/v1/source/status")
    def source_status():
        return source_call("status")

    @app.get("/v1/archive")
    def archive_status():
        return archive_manager.status() if archive_manager else {"configured": False}

    @app.post("/v1/source/settings")
    def source_settings(value: dict):
        if source_api is None:
            raise HTTPException(503, "source_api_unavailable")
        try:
            return source_api.configure(value)
        except ValueError:
            raise HTTPException(400, "invalid_request") from None

    @app.post("/v1/source/login")
    def source_login():
        return source_call("official_login")

    @app.post("/v1/source/poll")
    def source_poll():
        return source_call("poll_login")

    @app.get("/v1/source/qr")
    def source_qr():
        from fastapi.responses import Response
        return Response(source_call("qr"), media_type="image/png")

    @app.post("/v1/scheduler")
    def configure_scheduler(value: dict):
        return queue.configure(value)

    @app.get("/v1/cache")
    def cache():
        if cache_manager is None:
            raise HTTPException(503, "cache_not_configured")
        return cache_manager.inventory()

    @app.post("/v1/cache/delete")
    def delete_cache(value: dict):
        if cache_manager is None:
            raise HTTPException(503, "cache_not_configured")
        if (
            set(value) != {"id", "revision"}
            or not isinstance(value["id"], str)
            or not re.fullmatch(r"(?:song:[1-9][0-9]{0,17}|stem:[0-9a-f]{64})", value["id"])
            or not isinstance(value["revision"], str)
            or not re.fullmatch("[0-9a-f]{64}", value["revision"])
        ):
            raise HTTPException(400, "invalid_cache")
        return cache_manager.delete(value["id"], value["revision"])

    @app.post("/v1/cache/clear-local")
    def clear_local_cache(value: dict):
        if cache_manager is None or set(value) != {"id", "revision"}:
            raise HTTPException(400, "invalid_cache")
        if not isinstance(value["id"], str) or not re.fullmatch(r"song:[1-9][0-9]{0,17}", value["id"]):
            raise HTTPException(400, "invalid_cache")
        if not isinstance(value["revision"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["revision"]):
            raise HTTPException(400, "invalid_cache")
        return cache_manager.delete(value["id"], value["revision"], local_only=True)

    @app.post("/v1/cache/normalize-video")
    def normalize_cached_video(value: dict):
        if set(value) != {"id"} or not isinstance(value["id"], str) or not re.fullmatch(r"[a-f0-9-]{36}", value["id"]):
            raise HTTPException(400, "invalid_job")
        job_id = value["id"]
        with queue.lock:
            item = queue.jobs.get(job_id)
            if not item or item["state"] != "ready" or item.get("video", {}).get("status") != "ready":
                raise HTTPException(409, "video_not_ready")
            before = dict(item["video"])
            if before.get("delivery_revision") == 3:
                return {"optimized": True, "cache_hit": True}
            source = safe_file(queue.jobs_dir / job_id / "video.mp4", queue.root)
        from .ktv_video import normalize_video
        output = source.with_name("video-delivery-" + secrets.token_hex(6) + ".mp4")
        try:
            delivery = normalize_video(source, output, threading.Event(), ffmpeg=shutil.which("ffmpeg"),
                            ffprobe=shutil.which("ffprobe"), duration=before["duration_seconds"])
            with queue.lock:
                item = queue.jobs.get(job_id)
                if not item or item["state"] != "ready" or item["video"] != before:
                    raise HTTPException(409, "video_changed")
                output.chmod(0o600)
                checksum, length = digest(output), output.stat().st_size
                output.replace(source)
                item["video"].update(sha256=checksum, bytes=length, delivery_revision=3, **delivery)
                queue._save(item)
                return {"optimized": True, "bytes": length, "sha256": checksum}
        finally:
            output.unlink(missing_ok=True)

    @app.get("/v1/status")
    def status():
        return {
            "protocol": 1,
            "simulated": queue.simulated,
            "audio_started": False,
            "format": {"sample_rate": 48000, "channels": 2, "encoding": "pcm_s16le_wav"},
        }

    @app.get("/v1/catalog")
    def songs(q: str = ""):
        if len(q) > 120:
            raise HTTPException(400, "query_too_long")
        songs = {
            s["song_id"]: s
            for s in catalog()
            if not q or q.casefold() in s["title"].casefold() or q == s["song_id"]
        }
        search_status = "not_requested"
        if q and search is not None:
            try:
                for item in search(q):
                    songs.setdefault(item["song_id"], item)
                search_status = "ready"
            except Exception:
                search_status = "unavailable"
        return {"songs": list(songs.values())[:100], "search_status": search_status}

    @app.get("/v1/jobs")
    def jobs():
        from .ktv_metadata import song_title
        return {"jobs": [{**job, "title": song_title(job["title"])} for job in queue.list(for_playback=True)]}

    @app.get("/v1/party")
    def party():
        return bridge.current() if bridge is not None else {"active": False, "nas_online": False}

    @app.post("/v1/tv/playback")
    def playback(value: dict):
        if bridge is None:
            raise HTTPException(503, "public_not_configured")
        try:
            return bridge.report_playback(value)
        except ValueError:
            raise HTTPException(400, "invalid_playback") from None

    @app.post("/v1/tv/controls")
    def tv_controls(value: dict):
        if bridge is None:
            raise HTTPException(503, "public_not_configured")
        try:
            return bridge.control_from_tv(value)
        except ValueError:
            raise HTTPException(409, "control_unavailable") from None

    @app.post("/v1/party")
    def create_party():
        if bridge is None:
            raise HTTPException(503, "public_not_configured")
        try:
            return bridge.create_party()
        except Exception:
            raise HTTPException(503, "public_unavailable") from None

    @app.post("/v1/party/close")
    def close_party():
        if bridge is None:
            raise HTTPException(503, "public_not_configured")
        try:
            return bridge.close_party()
        except Exception:
            raise HTTPException(503, "public_unavailable") from None

    @app.get("/v1/party/qr")
    def party_qr():
        from fastapi.responses import Response

        if bridge is None:
            raise HTTPException(503, "public_not_configured")
        try:
            return Response(bridge.qr(), media_type="image/png")
        except Exception:
            raise HTTPException(503, "qr_unavailable") from None

    @app.post("/v1/jobs", status_code=202)
    def submit(value: JobRequest):
        return queue.submit(value)

    @app.get("/v1/jobs/{job_id}/journal")
    def journal(job_id: str):
        return queue.journal(job_id)

    @app.get("/v1/jobs/{job_id}")
    def job(job_id: str):
        return queue.get(job_id)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return queue.cancel(job_id)

    @app.post("/v1/jobs/{job_id}/remove")
    def remove(job_id: str):
        return queue.remove(job_id)

    @app.get("/v1/jobs/{job_id}/lyrics")
    def song_lyrics(job_id: str):
        return prepared_lyrics(queue, job_id, lyrics, source_api)

    @app.api_route("/v1/jobs/{job_id}/media/{kind}", methods=["GET", "HEAD"])
    def media(job_id: str, kind: str, request: Request):
        if kind in {"guide_original", "guide_reference"}:
            try:
                stream, item = bridge.guide.media(job_id, kind)
            except (AttributeError, OSError, ValueError):
                raise HTTPException(404, "guide_unavailable") from None
        else:
            stream, item = queue.media(job_id, kind)
        size, start, end = item["bytes"], 0, item["bytes"] - 1
        etag = '"' + item["sha256"] + '"'
        status_code = 200
        value = request.headers.get("range")
        if value and request.headers.get("if-range", etag) == etag:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
            if not match or not any(match.groups()):
                stream.close()
                raise HTTPException(416, "invalid_range", headers={"Content-Range": f"bytes */{size}"})
            a, b = match.groups()
            if a:
                start, end = int(a), min(int(b), size - 1) if b else size - 1
            else:
                start = max(0, size - int(b))
            if not 0 <= start <= end < size:
                stream.close()
                raise HTTPException(416, "invalid_range", headers={"Content-Range": f"bytes */{size}"})
            status_code = 206
        headers = {
            "Content-Length": str(end - start + 1),
            "ETag": etag,
            "Accept-Ranges": "bytes",
            "X-Audio-SHA256": item["sha256"],
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        def blocks():
            try:
                if request.method == "HEAD":
                    return
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                stream.close()

        return StreamingResponse(
            blocks(),
            status_code=status_code,
            media_type="video/mp4" if kind == "video" else "audio/mpeg" if kind.startswith("guide_") else "audio/wav",
            headers=headers,
        )

    return app


def local_host(value: str) -> str:
    address = ipaddress.ip_address(value)
    if address.version != 4 or not (
        address.is_loopback
        or any(
            address in ipaddress.ip_network(cidr)
            for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
    ):
        raise ValueError("private_ipv4_required")
    return str(address)
