"""逐曲预取和复用背景；返回本地主题后才排队生成，绝不阻塞起播。"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import stat
import struct
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from integrations.ktv_public.audio_format import song_title
from .budget import EnhancementUnavailable
from .settings import atomic_json, private_path

PROMPT_VERSION = "ktv-background-v2"
LEGACY_REQUEST_SIZE = (1536, 1024)
LEGACY_CANVAS = (1536, 864)
QHD_CANVAS = (2560, 1440)
UHD_CANVAS = (3840, 2160)
DELIVERY_SIZES = {LEGACY_CANVAS, QHD_CANVAS, UHD_CANVAS}
NATIVE_WIDESCREEN_MODELS = {
    "gpt-image-2", "gpt-image-2-2026-04-21",
    "gpt-image-2.5-sunburst", "gpt-image-2.5-sunburst-2026-09-08",
    "gpt-image-2.5-flare", "gpt-image-2.5-flare-2026-09-08",
}
MAX_PARALLEL = 24
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_ENCODED_IMAGE_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_METADATA_BYTES = 8192
SHA256 = re.compile(r"[a-f0-9]{64}")
SONG_FIELDS = ("song_id", "title", "artist", "audio_origin")
_VERSION_SUFFIX = re.compile(
    r"\s*\((?:live(?:\s+version)?|现场(?:版)?|unplugged|acoustic(?:\s+version)?|不插电(?:版)?|"
    r"remaster(?:ed)?(?:\s+\d{4})?|\d{4}\s+remaster(?:ed)?|重制版|录音室版|单曲版)\)\s*$", re.I)
_ART_DIRECTIONS = (
    "矿物颜料叠绘，以纸纤维、干刷和色块塑造深浅",
    "丝网印刷与版画，用剪影、套色偏移和粗细有别的纹理构图",
    "胶片纪实摄影，用真实光源、空间比例和细腻颗粒组织画面",
    "现代主义编辑拼贴，用剪纸、形体错位和精确的色面讲故事",
    "手工搭景与雕塑质感，强调材质、布光和舞台空间",
    "克制的水墨与工笔，用留白、笔触和淡彩建立纵深",
    "观念摄影，以一组有象征意味的物件和自然光表达主题",
    "当代数字绘画，以节奏鲜明的几何结构和互补色建立秩序",
)


def song_identity(song, model):
    fields = [str(song.get(name, "")) for name in SONG_FIELDS]
    if any(len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value) for value in fields) or not fields[1]:
        raise ValueError("invalid_background_song")
    return hashlib.sha256(
        json.dumps([PROMPT_VERSION, model, *fields], ensure_ascii=False).encode()
    ).hexdigest()


def composition_key(song):
    title, artist = song.get("title"), song.get("artist")
    if not isinstance(title, str) or not isinstance(artist, str):
        return None
    title = song_title(unicodedata.normalize("NFKC", title))
    title = _VERSION_SUFFIX.sub("", title)
    artist = re.split(r"\s+/\s+|、|\s+\(feat\.?\s+", unicodedata.normalize("NFKC", artist),
                      maxsplit=1, flags=re.I)[0]
    parts = tuple("".join(c for c in value.casefold() if c.isalnum()) for value in (title, artist))
    return parts if all(parts) else None


def _work_identifier(work):
    return hashlib.sha256(json.dumps(["ktv-background-work", *work], ensure_ascii=False).encode()).hexdigest()


def _source_key(song):
    source, identifier = song.get("audio_origin"), song.get("song_id")
    return (source, identifier) if source and identifier else None



def png_dimensions(data, *, decode=False):
    if not 33 <= len(data) <= MAX_IMAGE_BYTES or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("invalid_generated_background")
    width, height = struct.unpack(">II", data[16:24])
    if not 256 <= width <= 4096 or not 256 <= height <= 4096 or width * height > 9000000:
        raise ValueError("invalid_generated_background")
    if decode:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as picture:
            if picture.format != "PNG" or picture.size != (width, height):
                raise ValueError("invalid_generated_background")
            picture.verify()
        with Image.open(io.BytesIO(data)) as picture:
            picture.load()
    return width, height


def prepare_backdrop(data, *, requested_size=LEGACY_REQUEST_SIZE, encoding="png"):
    """宽屏 JPEG 原样入库；旧比例的图片裁切成 16:9 后编码为 JPEG。"""
    from PIL import Image

    if not 33 <= len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("invalid_generated_background")
    if requested_size == LEGACY_REQUEST_SIZE and encoding == "png":
        png_dimensions(data)
    elif requested_size not in {QHD_CANVAS, UHD_CANVAS} or encoding != "jpeg":
        raise ValueError("invalid_generated_background")
    with Image.open(io.BytesIO(data), formats=[encoding.upper()]) as source:
        if source.size != requested_size or getattr(source, "n_frames", 1) != 1:
            raise ValueError("invalid_generated_background")
        source.load()
        if encoding == "jpeg":
            return data
        width, height = LEGACY_CANVAS
        top = (source.height - height) // 2
        frame = source.convert("RGB").crop((0, top, width, top + height))
        output = io.BytesIO()
        frame.save(output, format="JPEG", quality=95, subsampling=0)
        return output.getvalue()


def backdrop_dimensions(data, encoding):
    if encoding == "png":
        return png_dimensions(data)
    from PIL import Image

    if encoding != "jpeg" or not 33 <= len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("invalid_generated_background")
    with Image.open(io.BytesIO(data), formats=["JPEG"]) as picture:
        return picture.size


def decode_backdrop(payload, requested_size, encoding):
    entries = payload.get("data")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ValueError("invalid_generated_background")
    encoded = entries[0].get("b64_json")
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_ENCODED_IMAGE_BYTES:
        raise ValueError("invalid_generated_background")
    return prepare_backdrop(base64.b64decode(encoded, validate=True), requested_size=requested_size, encoding=encoding)


def checked_public_metadata(value):
    if (not isinstance(value, dict) or not {"title", "artist"} <= set(value) <= {"title", "artist", "context"}
            or any(not isinstance(text, str) or not text.strip() or len(text) > (400 if name == "context" else 120)
                   or any(ord(char) < 32 or ord(char) == 127 for char in text)
                   for name, text in value.items())):
        raise ValueError("invalid_public_background_metadata")
    return dict(value)


class SongBackgrounds:
    def __init__(self, runtime, directory=None, *, recording=None, max_parallel=1, context_resolver=None,
                 context_timeout=4, debug=False, cloud_recording=None, cloud_metadata=None, include_mv=False):
        self.runtime = runtime
        self.directory = Path(directory or runtime.settings.directory / "song-backgrounds")
        self.recording = recording
        self.cloud_recording = cloud_recording
        self.cloud_metadata = cloud_metadata
        self.context_resolver = context_resolver
        self.debug = debug
        self.include_mv = include_mv
        private_path(self.directory)
        self.tasks = {}
        self.closed = False
        if type(max_parallel) is not int or not 1 <= max_parallel <= MAX_PARALLEL:
            raise ValueError("invalid_background_parallelism")
        if type(context_timeout) not in {int, float} or not 1 <= context_timeout <= 30:
            raise ValueError("invalid_background_context_timeout")
        self.context_timeout = context_timeout
        self.max_parallel = max_parallel
        self.gate = asyncio.Semaphore(max_parallel)
        self.failures = {}
        self.loop = None
        self.cache_lock = threading.Lock()
        self.verified = OrderedDict()
        self.index_lock = threading.Lock()
        self.index_version = None
        self.work_records = {}
        self.source_records = {}

    def bind_loop(self):
        self.loop = asyncio.get_running_loop()

    def enabled(self):
        return self.runtime.settings.read().cloud_images

    def prefetch_from_thread(self, songs):
        if not self.loop or self.closed:
            return
        config = self.runtime.settings.read()
        if not config.cloud_images or not self.debug and config.daily_images == 0:
            return
        candidates = []
        found = set()
        for song in songs:
            try:
                key = self.identifier_for(song, model=config.image_model)
                if key not in found and not self.cached(key):
                    found.add(key)
                    candidates.append(song)
            except (OSError, ValueError, TypeError):
                pass
        if candidates:
            self.loop.call_soon_threadsafe(self.prefetch, candidates)

    def prefetch(self, songs):
        for song in songs:
            if len(self.tasks) >= self.max_parallel:
                break
            self._schedule(song)

    def _schedule(self, song):
        try:
            self.request(song)
        except (OSError, ValueError, RuntimeError, TypeError):
            pass

    def cached_for(self, song):
        return self.cached(self.identifier_for(song))

    def _display_metadata(self, song):
        if self.cloud_metadata and song.get("audio_origin") == "cloud":
            try:
                return checked_public_metadata(self.cloud_metadata(song["song_id"]))
            except (KeyError, OSError, ValueError):
                pass
        return None

    def _record_indexes(self):
        with self.index_lock:
            try:
                info = self.directory.stat()
                version = info.st_ino, info.st_mtime_ns
            except FileNotFoundError:
                return {}, {}
            if version != self.index_version:
                works, sources = {}, {}
                records = []
                for path in self.directory.glob("*.json"):
                    if not SHA256.fullmatch(path.stem):
                        continue
                    try:
                        record, file_version = self._record(path.stem)
                        song = self._song_ref(path.stem, record)
                        if song:
                            records.append((file_version[-1], path.stem, song, record.get("public_metadata")))
                    except (OSError, ValueError, TypeError):
                        continue
                for _, identifier, song, public in sorted(records, reverse=True):
                    for work in {composition_key(song), composition_key(public) if isinstance(public, dict) else None} - {None}:
                        works.setdefault(work, []).append(identifier)
                    source = _source_key(song)
                    if source:
                        sources.setdefault(source, []).append(identifier)
                self.work_records, self.source_records, self.index_version = works, sources, version
            return self.work_records, self.source_records

    def identifier_for(self, song, *, model=None, public=None):
        legacy = song_identity(song, model or self.runtime.settings.read().image_model)
        public = public or self._display_metadata(song)
        work = composition_key(public or song) if public or song.get("audio_origin") != "cloud" else None
        works, sources = self._record_indexes()
        for identifier in dict.fromkeys([*works.get(work, ()), *sources.get(_source_key(song), ()), legacy]):
            if self.cached(identifier):
                return identifier
        return _work_identifier(work) if work else legacy

    def _generation_block(self, song, config, *, public_metadata=None, force=False):
        if self.closed or not config.cloud_images:
            return "cloud_images_disabled"
        if config.provider == "local":
            return "cloud_capability_disabled"
        if (song.get("audio_origin") == "cloud" and public_metadata is None
                and self.cloud_recording is None and self.cloud_metadata is None):
            return "private_song_metadata"
        if self.recording is not None and song.get("simulated") is True:
            return "simulated_song"
        video = song.get("video")
        if isinstance(video, dict) and video.get("status") == "ready" and not (self.include_mv or config.background_with_mv or force):
            return "verified_mv_available"
        return None

    @staticmethod
    def _file_version(path, limit):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > limit:
            raise ValueError("invalid_generated_background")
        return info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns, info.st_mtime_ns

    @property
    def versions(self):
        return self.directory / "versions"

    def _record(self, identifier):
        if not isinstance(identifier, str) or not SHA256.fullmatch(identifier):
            raise ValueError("invalid_background_identifier")
        path = self.directory / (identifier + ".json")
        private_path(path)
        version = self._file_version(path, MAX_METADATA_BYTES)
        metadata = json.loads(path.read_text())
        if not isinstance(metadata, dict) or metadata.get("prompt_version") != PROMPT_VERSION:
            raise ValueError("invalid_generated_background")
        return metadata, version

    def _image_path(self, identifier, reference):
        if not isinstance(reference, dict):
            raise ValueError("invalid_generated_background")
        digest, format_version = reference.get("sha256"), reference.get("image_version", 0)
        if not isinstance(digest, str) or not SHA256.fullmatch(digest) or type(format_version) is not int or format_version not in {0, 1, 2}:
            raise ValueError("invalid_generated_background")
        suffix = ".jpg" if format_version == 2 else ".png"
        image = self.versions / (identifier + "." + digest + suffix) if format_version else self.directory / (identifier + suffix)
        private_path(image)
        return image

    def _read_verified(self, identifier, reference, *, with_bytes=False):
        image = self._image_path(identifier, reference)
        version = self._file_version(image, MAX_IMAGE_BYTES)
        digest = reference["sha256"]
        key = identifier, digest
        with self.cache_lock:
            previous = self.verified.get(key)
            if not with_bytes and previous and previous[0] == version:
                self.verified.move_to_end(key)
                return dict(previous[1])
        data = image.read_bytes()
        encoding = "jpeg" if image.suffix == ".jpg" else "png"
        dimensions = backdrop_dimensions(data, encoding)
        if (dimensions not in DELIVERY_SIZES or digest != hashlib.sha256(data).hexdigest()
                or self._file_version(image, MAX_IMAGE_BYTES) != version):
            raise ValueError("invalid_generated_background")
        value = {"status": "ready", "kind": "generated", "id": identifier, "sha256": digest,
                 "bytes": len(data), "encoding": encoding, "mime": "image/" + encoding,
                 "width": dimensions[0], "height": dimensions[1]}
        with self.cache_lock:
            self.verified[key] = (version, value)
            self.verified.move_to_end(key)
            if len(self.verified) > 256:
                self.verified.popitem(last=False)
        return data if with_bytes else dict(value)

    def cached(self, identifier):
        try:
            metadata, _ = self._record(identifier)
            return self._read_verified(identifier, metadata)
        except (ValueError, OSError, KeyError, struct.error):
            return None

    def read_image(self, identifier, expected_sha=None):
        """读取当前已提交的图片，或读取上一版的精确摘要。"""
        if expected_sha is not None and (not isinstance(expected_sha, str) or not SHA256.fullmatch(expected_sha)):
            return None
        for _ in range(2):
            try:
                metadata, _ = self._record(identifier)
                reference = metadata
                if expected_sha is not None and metadata.get("sha256") != expected_sha:
                    reference = metadata.get("previous")
                    if not isinstance(reference, dict) or reference.get("sha256") != expected_sha:
                        return None
                return self._read_verified(identifier, reference, with_bytes=True)
            except (OSError, ValueError, KeyError, struct.error):
                # 读取中切换两版时，重读已提交的指针。
                continue
        return None

    def public_metadata_for(self, identifier):
        try:
            metadata, _ = self._record(identifier)
            if "song_ref" in metadata and self._song_ref(identifier, metadata) is None:
                return None
            public = checked_public_metadata(metadata.get("public_metadata"))
            self._read_verified(identifier, metadata)
            return public
        except (OSError, ValueError, KeyError, struct.error):
            return None

    @staticmethod
    def _song_ref(identifier, metadata):
        song = metadata.get("song_ref")
        if (not isinstance(song, dict) or set(song) != set(SONG_FIELDS)
                or not all(isinstance(value, str) for value in song.values())
                or not isinstance(metadata.get("model"), str)):
            return None
        public = metadata.get("public_metadata")
        work = composition_key(public if isinstance(public, dict) else song)
        identity_model = metadata.get("identity_model", metadata["model"])
        if song_identity(song, identity_model) != identifier and (not work or _work_identifier(work) != identifier):
            return None
        return dict(song)

    def song_ref_for(self, identifier):
        try:
            metadata, _ = self._record(identifier)
            song = self._song_ref(identifier, metadata)
            self._read_verified(identifier, metadata)
            return song
        except (OSError, ValueError, KeyError, struct.error):
            return None

    def request(self, song, *, force=False, public_metadata=None):
        if public_metadata is not None:
            public_metadata = checked_public_metadata(public_metadata)
        config = self.runtime.settings.read()
        public_metadata = public_metadata or self._display_metadata(song)
        identifier = self.identifier_for(song, model=config.image_model, public=public_metadata)
        cached = self.cached(identifier)
        if cached and not force:
            return cached
        public_metadata = public_metadata or self.public_metadata_for(identifier)
        fallback = {"status": "local", "kind": "local", "id": identifier, "theme": "content"}
        reason = self._generation_block(song, config, public_metadata=public_metadata, force=force)
        if reason:
            return fallback if reason == "simulated_song" else {**fallback, "reason": reason}
        failure = self.failures.get(identifier)
        if not force and failure and time.monotonic() < failure[1] and (len(failure) == 2 or failure[2] == config):
            return {**fallback, "reason": failure[0]}
        if identifier not in self.tasks:
            snapshot = self._record_version(identifier)
            task = asyncio.create_task(self._generate(identifier, dict(song), force=force,
                                                       initial_version=snapshot, public_metadata=public_metadata),
                                       name="friday-song-background")
            self.tasks[identifier] = task
            task.add_done_callback(lambda done: self._finished(identifier, done))
        return {**fallback, "status": "preparing"}

    def _finished(self, identifier, task):
        self.tasks.pop(identifier, None)
        if not task.cancelled() and task.exception() is not None:
            self.failures[identifier] = ("background_unavailable", time.monotonic() + 900)

    def _record_version(self, identifier):
        try:
            return self._record(identifier)[1]
        except (OSError, ValueError):
            return None

    @staticmethod
    def _open_lock(path):
        private_path(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        stream = os.fdopen(fd, "r+b")
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            stream.close()
            raise ValueError("unsafe_background_lock")
        return stream

    def _prune_versions(self, identifier):
        try:
            metadata, _ = self._record(identifier)
            retained = {self._image_path(identifier, metadata)}
            if metadata.get("previous"):
                retained.add(self._image_path(identifier, metadata["previous"]))
        except FileNotFoundError:
            retained = set()
        except (ValueError, OSError):
            return
        files = [
            *self.versions.glob(identifier + ".*.png"),
            *self.versions.glob(identifier + ".*.jpg"),
            *self.versions.glob("." + identifier + ".*.tmp"),
            *self.directory.glob("." + identifier + ".json.*"),
        ]
        name = re.compile(rf"(?:{identifier}\.[a-f0-9]{{64}}\.(?:png|jpg)|\.{identifier}\.[a-f0-9]{{16}}\.tmp|\.{identifier}\.json\.[a-f0-9]{{16}})")
        for path in files:
            if path in retained or not name.fullmatch(path.name):
                continue
            try:
                private_path(path)
                self._file_version(path, MAX_IMAGE_BYTES)
                path.unlink()
            except (OSError, ValueError):
                continue

    def _claim(self, identifier, limit):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # 旧进程可能持有全局排他锁。新版活跃任务共享这把锁，同时按歌曲独占预留。
        compatibility = self._open_lock(self.directory / "generation.lock")
        holder = None
        try:
            fcntl.flock(compatibility, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self._open_lock(self.directory / "reservations.lock") as catalog:
                fcntl.flock(catalog, fcntl.LOCK_EX | fcntl.LOCK_NB)
                leases = self.directory / ".reservations"
                private_path(leases)
                leases.mkdir(mode=0o700, exist_ok=True)
                active = set()
                for path in leases.glob("*.lease"):
                    if not SHA256.fullmatch(path.stem):
                        continue
                    with self._open_lock(path) as prior:
                        try:
                            fcntl.flock(prior, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            active.add(path.stem)
                            continue
                        self._prune_versions(path.stem)
                        path.unlink()
                if identifier in active or len(active) >= self.max_parallel:
                    raise EnhancementUnavailable("background_in_progress")
                stored = {path.stem for path in self.directory.iterdir()
                          if path.suffix in {".json", ".png"} and SHA256.fullmatch(path.stem)}
                if identifier not in stored and len(stored | active) >= limit:
                    raise EnhancementUnavailable("background_storage_limit_reached")
                path = leases / (identifier + ".lease")
                holder = self._open_lock(path)
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return compatibility, holder, path
        except BaseException:
            if holder is not None:
                holder.close()
            compatibility.close()
            raise

    def _release(self, reservation):
        compatibility, holder, path = reservation
        try:
            with self._open_lock(self.directory / "reservations.lock") as catalog:
                fcntl.flock(catalog, fcntl.LOCK_EX | fcntl.LOCK_NB)
                path.unlink()
        except (OSError, ValueError):
            # 预留文件若仍在，下个进程取得该文件锁后可识别并回收它。
            pass
        finally:
            holder.close()
            compatibility.close()

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _publish(self, identifier, song, config, data, public_metadata):
        width, height = backdrop_dimensions(data, "jpeg")
        if (width, height) not in DELIVERY_SIZES:
            raise ValueError("invalid_generated_background")
        digest = hashlib.sha256(data).hexdigest()
        record = self.directory / (identifier + ".json")
        metadata = {
            "sha256": digest, "width": width, "height": height, "image_version": 2,
            "source": "openai_image_api", "model": config.image_model,
            "created_at": datetime.now(UTC).isoformat(), "prompt_version": PROMPT_VERSION,
            "render_revision": 4,
            "song_ref": {field: str(song.get(field, "")) for field in SONG_FIELDS},
        }
        if public_metadata is not None:
            metadata["public_metadata"] = public_metadata
        obsolete = None
        try:
            old, _ = self._record(identifier)
            old_song = self._song_ref(identifier, old)
            if old_song is not None:
                metadata["song_ref"] = old_song
                metadata["identity_model"] = old.get("identity_model", old["model"])
            obsolete = self._image_path(identifier, old.get("previous", old))
            previous = old.get("previous") if old["sha256"] == digest else old
            if previous:
                self._read_verified(identifier, previous)
                metadata["previous"] = {"sha256": previous["sha256"], "image_version": previous.get("image_version", 0)}
        except (OSError, ValueError, KeyError, struct.error):
            pass
        private_path(self.versions)
        self.versions.mkdir(mode=0o700, exist_ok=True)
        image = self._image_path(identifier, metadata)
        part = self.versions / ("." + identifier + "." + secrets.token_hex(8) + ".tmp")
        try:
            with part.open("xb") as stream:
                part.chmod(0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            part.replace(image)
            self._sync_directory(self.versions)
            atomic_json(record, metadata)
            self._sync_directory(self.directory)
        finally:
            part.unlink(missing_ok=True)
        if obsolete == self.directory / (identifier + ".png") and (
                not metadata.get("previous") or metadata["previous"]["image_version"] != 0):
            try:
                self._file_version(obsolete, MAX_IMAGE_BYTES)
                obsolete.unlink()
            except (OSError, ValueError):
                pass

    async def _resolve_context(self, public):
        if self.context_resolver is None or "context" in public:
            return None
        try:
            checked = checked_public_metadata(public)
            brief = await asyncio.wait_for(self.context_resolver(dict(checked)), timeout=self.context_timeout)
            return checked_public_metadata({**checked, "context": brief})
        except Exception:
            return None

    async def _request_backdrop(self, identifier, config, prompt):
        sizes = ((UHD_CANVAS, QHD_CANVAS, LEGACY_REQUEST_SIZE) if config.image_model in NATIVE_WIDESCREEN_MODELS
                 else (LEGACY_REQUEST_SIZE,))
        deadline = time.monotonic() + 120
        request_id = identifier + ":" + secrets.token_hex(8)
        for size in sizes:
            encoding = "png" if size == LEGACY_REQUEST_SIZE else "jpeg"
            body = {"model": config.image_model, "n": 1, "size": f"{size[0]}x{size[1]}",
                    "quality": "high", "output_format": encoding, "prompt": prompt}
            if encoding == "jpeg":
                body.update(background="opaque", output_compression=96)
            try:
                payload = await self.runtime.client.post("images", "images/generations", body,
                                                         request_id=request_id, deadline=deadline, images=1,
                                                         enforce_daily_limits=not self.debug)
            except EnhancementUnavailable as exc:
                if (exc.code != "api_image_size_rejected" or size == sizes[-1] or self.closed
                        or self.runtime.settings.read() != config):
                    raise
            else:
                return await asyncio.to_thread(decode_backdrop, payload, size, encoding)

    async def _generate(self, identifier, song, *, force=False, initial_version=None, public_metadata=None):
        reservation = None
        config = None
        requested = identifier
        try:
            async with self.gate:
                config = self.runtime.settings.read()
                if self._generation_block(song, config, public_metadata=public_metadata, force=force):
                    return
                if public_metadata is None and song.get("audio_origin") == "cloud":
                    verified = (await asyncio.to_thread(self.cloud_recording, song["song_id"])
                                if self.cloud_recording else None)
                    if verified is None:
                        raise EnhancementUnavailable("private_song_metadata")
                    public_metadata = checked_public_metadata(verified)
                public = public_metadata or {key: song.get(key, "") for key in ("title", "artist")}
                if public_metadata is None and self.recording is not None:
                    try:
                        detail = await asyncio.to_thread(self.recording, song["song_id"])
                        if detail["id"] == song["song_id"]:
                            public = public_metadata = checked_public_metadata({
                                "title": detail["title"], "artist": " / ".join(detail["artists"])})
                    except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
                        pass
                # 同一作品的并发任务在调用生图前汇合。旧背景保留原资产标识供电视继续读取。
                if not self.cached(requested):
                    identifier = self.identifier_for(song, model=config.image_model, public=public_metadata)
                reservation = self._claim(identifier, config.cached_background_limit)
                if self.cached(identifier) and (not force or identifier != requested
                                                or self._record_version(identifier) != initial_version):
                    return
                if public_metadata is not None or self.recording is not None:
                    contextualized = await self._resolve_context(public)
                    if contextualized is not None:
                        public_metadata = public = contextualized
                if self.closed or self.runtime.settings.read() != config:
                    return
                work = composition_key(public)
                seed = _work_identifier(work) if work else identifier
                direction = _ART_DIRECTIONS[int(seed[:8], 16) % len(_ART_DIRECTIONS)]
                prompt = ("为电视 KTV 创作一张原创的 16:9 横幅美术作品。根据这首歌的题材、年代、"
                    "文化背景和情绪决定画面意象、光色、材质和空间结构。美术方向可采用"
                    + direction + "；若发行资料指出更贴切的手法，以歌曲特点为准。把作品的代表性色彩和"
                    "独有意象读得清楚，避免用背影、暮色、雾景反复表达不同作品。"
                    "资料稀少时用抽象的色面和材质表达听感，以免把歌名误当作品情节。"
                    "把主要视觉重心放在画面的右半部。左侧约 44% 留给两行白色歌词；采用与主体连续的"
                    "低细节留白，调节这一带的局部明度和反差，保留全幅中间色和光线。"
                    "不要绘制黑色蒙层、光晕边框、左右分割线或空的矩形色块。"
                    "画面只有场景，不画任何文字、歌词、标志、歌手肖像、已有封面或 UI。"
                    "下一段为歌曲资料。\n"
                    + json.dumps(public, ensure_ascii=False))
                data = await self._request_backdrop(identifier, config, prompt)
                if self.closed or self.runtime.settings.read() != config:
                    return
                self._publish(identifier, song, config, data, public_metadata)
                self.failures.pop(requested, None)
        except asyncio.CancelledError:
            raise
        except (EnhancementUnavailable, ValueError, OSError, KeyError, TypeError, sqlite3.Error, httpx.HTTPError) as exc:
            code = ("background_in_progress" if isinstance(exc, BlockingIOError)
                    else exc.code if isinstance(exc, EnhancementUnavailable)
                    else "background_catalog_unavailable" if isinstance(exc, httpx.HTTPError)
                    else "background_unavailable")
            delay = {"background_in_progress": 2, "api_timeout": 30, "api_unavailable": 30,
                     "api_rate_limited": 60, "background_catalog_unavailable": 30}.get(code, 900)
            self.failures[requested] = (code, time.monotonic() + delay, config)
            if len(self.failures) > 256:
                self.failures.pop(next(iter(self.failures)))
        finally:
            if reservation is not None:
                try:
                    self._prune_versions(identifier)
                finally:
                    self._release(reservation)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
