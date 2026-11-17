"""Private NAS media archive with verified atomic copies and local source restore."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path

from .ktv_download import _hash_file, _write, canonical_song_id


class MediaArchive:
    def __init__(self, queue, config, *, mounted=os.path.ismount, shared_reference=None):
        config = Path(config)
        if config.is_symlink() or config.stat().st_mode & 0o077:
            raise ValueError("private_archive_config_required")
        value = json.loads(config.read_text())
        self.mount, self.root = Path(value["mount"]), Path(value["root"])
        if (self.mount.resolve() != self.mount or self.root.resolve() != self.root
                or not self.root.is_relative_to(self.mount) or self.root == self.mount):
            raise ValueError("unsafe_archive")
        shared = value.get("coordinator_media_root")
        self.coordinator_media_root = Path(shared) if shared else None
        if self.coordinator_media_root is not None:
            shared = self.coordinator_media_root
            if (shared.resolve() != shared or not shared.is_relative_to(self.mount) or shared.name != "coordinator-media"
                    or shared.is_relative_to(self.root) or self.root.is_relative_to(shared)):
                raise ValueError("unsafe_archive")
        self.queue, self.mounted = queue, mounted
        self.shared_reference = shared_reference
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.active = set()
        self.known = {}
        self.duplicate_candidates = {}
        self.results = {}
        self.entries = {}
        self.inventory_catalogs = {}
        self.cache_manager = None
        self.guide_root = None
        self.guide_links = {}
        self.on_exclude = lambda jobs: None
        guide_config = config.with_name("guide.json")
        if guide_config.is_file():
            if guide_config.is_symlink() or guide_config.stat().st_mode & 0o077:
                raise ValueError("private_guide_config_required")
            guide = json.loads(guide_config.read_text())
            directory = Path(guide["directory"]).absolute()
            if (Path(guide["mount"]).absolute() != self.mount or directory.resolve() != directory
                    or directory == self.mount or not directory.is_relative_to(self.mount)):
                raise ValueError("unsafe_guide_archive")
            self.guide_root = directory
        self.exclusions_path = self.queue.root / "archive-exclusions.json"
        self.exclusions = {}
        if self.exclusions_path.is_file() and not self.exclusions_path.is_symlink():
            self.exclusions = json.loads(self.exclusions_path.read_text())
        self.hot_limit = int(value.get("hot_cache_bytes", 2 * 1024**3))
        if not 256 * 1024**2 <= self.hot_limit <= 4 * 1024**3:
            raise ValueError("invalid_hot_cache_limit")

    def available(self):
        return self.mounted(self.mount) and self.root.is_dir() and not self.root.is_symlink()

    def _catalog(self, song_id):
        return self.root / "catalog" / (canonical_song_id(song_id) + ".json")

    def _shared_wav(self, name):
        if self.coordinator_media_root is None or not re.fullmatch(r"[a-f0-9]{64}\.(?:wav|m4a)", name):
            return None
        shared = self.coordinator_media_root / name[:-4]
        if shared.resolve() == shared and shared.is_file():
            return shared
        return None

    def _playback_blob(self, name):
        local = self.root / "blobs" / name
        return local if local.exists() or local.is_symlink() else self._shared_wav(name) or local

    def _shared_registered(self, name, size, songs):
        try:
            return self.shared_reference is not None and self.shared_reference(name[:-4], size, songs)
        except (OSError, ValueError, RuntimeError):
            return False

    @staticmethod
    def _identity(path):
        info = path.stat(follow_symlinks=False)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def compact_wav(self, name, *, apply=False):
        """合并与现用 NAS 归档完全相同的旧 WAV，旧清单仍可按摘要回档。"""
        if not re.fullmatch(r"[a-f0-9]{64}\.wav", name) or not self.available():
            return 0
        original = self.root / "blobs" / name
        try:
            shared = self._shared_wav(name)
            if not shared or original.resolve() != original or not original.is_file():
                return 0
            original_id, shared_id = self._identity(original), self._identity(shared)
            size = original_id[2]
            # 任一旧清单有引用时，现用归档也必须保留同一首歌的关联。
            from .ktv_library import read_record
            songs = set()
            for path in (self.root / "catalog").glob("*.json"):
                entry = read_record(path, 65536)
                files, retained = entry.get("files", {}), entry.get("retained", [])
                if (entry.get("song_id") == path.stem and isinstance(files, dict) and isinstance(retained, list)
                        and any(isinstance(item, dict) and item.get("file") == name for item in [*files.values(), *retained])):
                    songs.add(path.stem)
            if (not 44 < size <= 512 * 1024**2 or size != shared_id[2]
                    or not self._shared_registered(name, size, songs)
                    or _hash_file(shared) != name[:-4] or _hash_file(original) != name[:-4]):
                return 0
            with self.lock:
                if self._identity(original) != original_id or self._identity(shared) != shared_id:
                    return 0
                if apply:
                    original.unlink()
                return size
        except OSError:
            return 0

    def _copy(self, source, destination, expected):
        if destination.parent.resolve() != destination.parent:
            raise ValueError("archive_copy_invalid")
        if source.is_symlink() or source.resolve() != source or not source.is_file():
            raise ValueError("archive_source_changed")
        if not 1 <= source.stat().st_size <= 512 * 1024**2:
            raise ValueError("archive_source_changed")
        if destination.exists():
            if destination.is_symlink() or _hash_file(destination) != expected:
                raise ValueError("archive_copy_invalid")
            return
        temporary = destination.with_name("." + destination.name + "." + secrets.token_hex(6) + ".part")
        checksum = hashlib.sha256()
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                for block in iter(lambda: reader.read(1024 * 1024), b""):
                    if self.stop.is_set():
                        raise ValueError("archive_cancelled")
                    checksum.update(block)
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
            if checksum.hexdigest() != expected or _hash_file(temporary) != expected:
                raise ValueError("archive_copy_invalid")
            temporary.chmod(0o600)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _source(self, song_id, origin="song"):
        if origin in {"mv", "mv_direct"}:
            return None, None
        folder = self.queue.root / ("cloud-downloads" if origin == "cloud" else "api-downloads") / song_id
        if (folder / "manifest.json").is_file():
            metadata = json.loads((folder / "manifest.json").read_text())
            return folder / "source.audio", metadata
        if origin == "cloud":
            return None, None
        index = self.queue.root / "downloads/index" / (song_id + ".json")
        if index.is_file():
            metadata = json.loads(index.read_text())
            key, filename = metadata.get("cache_key", ""), metadata.get("filename", "")
            if re.fullmatch(r"[a-f0-9]{64}", key) and filename in {"source.mp3", "source.flac"}:
                return self.queue.root / "downloads/decoded" / key / filename, metadata
        return None, None

    def archive(self, value):
        with self.lock:
            if value["id"] in self.exclusions.get(value["song_id"], []):
                raise ValueError("archive_excluded")
        if not self.available():
            raise ValueError("archive_offline")
        folder = self.queue.jobs_dir / value["id"]
        catalog, blobs = self.root / "catalog", self.root / "blobs"
        if catalog.resolve() != catalog or blobs.resolve() != blobs:
            raise ValueError("unsafe_archive")
        catalog.mkdir(exist_ok=True, mode=0o700)
        blobs.mkdir(exist_ok=True, mode=0o700)
        entries = {}
        source, metadata = self._source(value["song_id"], value.get("audio_origin", "song"))
        compact = all(spec.get("encoding") == "aac_lc_m4a" for spec in value.get("media", {}).values())
        if value.get("audio_origin") in {"mv", "mv_direct"} and (folder / "mv-source.json").is_file():
            source, metadata = folder / ".mv-audio.wav", json.loads((folder / "mv-source.json").read_text())
        specs = []
        if source is not None and not compact:
            if metadata.get("song_id") != value["song_id"] or not metadata.get("full_decode_verified"):
                raise ValueError("archive_source_changed")
            specs.append(("source", source, metadata["source_sha256"], metadata["source_bytes"]))
        for kind, spec in value.get("media", {}).items():
            from .ktv_audio import audio_suffix
            if kind not in {"original", "instrumental"} or spec["filename"] != kind + audio_suffix(spec):
                raise ValueError("archive_source_changed")
            specs.append((kind, folder / spec["filename"], spec["sha256"], spec["bytes"]))
        video = value.get("video", {})
        if video.get("status") == "ready":
            specs.append(("video", folder / "video.mp4", video["sha256"], video["bytes"]))
        for kind, path, checksum, length in specs:
            if not re.fullmatch(r"[a-f0-9]{64}", checksum) or path.stat().st_size != length:
                raise ValueError("archive_source_changed")
            suffix = ".audio" if kind == "source" else ".mp4" if kind == "video" else audio_suffix(value["media"][kind])
            name = checksum + suffix
            destination = blobs / name
            shared = self._shared_wav(name)
            if (shared is None or shared.stat().st_size != length
                    or not self._shared_registered(name, length, {value["song_id"]}) or _hash_file(shared) != checksum):
                self._copy(path, destination, checksum)
            if self.coordinator_media_root is not None and kind in {"original", "instrumental"} and destination.is_file():
                self.duplicate_candidates[name] = None
            entries[kind] = {"file": name, "sha256": checksum, "bytes": length}
        previous = self.entries.get(value["song_id"], {})
        if source is None and previous.get("files", {}).get("source"):
            entries["source"] = previous["files"]["source"]
            metadata = previous.get("source_metadata")
        originals = {item["file"]: item for item in previous.get("originals", [])}
        old_source, new_source = previous.get("files", {}).get("source"), entries.get("source")
        if old_source and new_source and old_source["file"] != new_source["file"]:
            originals[old_source["file"]] = old_source
        retained = {item["file"]: item for item in previous.get("retained", [])}
        current_names = {item["file"] for item in entries.values()}
        for kind, item in previous.get("files", {}).items():
            if kind != "source" and item["file"] not in current_names:
                retained[item["file"]] = item
        record = {"song_id": value["song_id"], "title": value["title"], "binding": value.get("binding"),
            "last_used": max(previous.get("last_used", 0), value.get("last_used", value.get("created", 0))),
            "updated": int(time.time()), "files": entries, "source_metadata": metadata,
            "media": value.get("media", {}), "video": video, "retained": list(retained.values()),
            "audio_origin": value.get("audio_origin", "song"), "originals": list(originals.values())}
        with self.lock:
            if value["id"] in self.exclusions.get(value["song_id"], []):
                raise ValueError("archive_excluded")
            _write(self._catalog(value["song_id"]), record)
            self.entries[value["song_id"]] = record
        return {"state": "archived", "updated": int(time.time()), "bytes": sum(x["bytes"] for x in entries.values())}

    @staticmethod
    def _same_audio_origin(value, origin):
        if value.get("audio_origin", "song") != origin:
            return False
        source = value.get("source_metadata") or {}
        return not (origin == "song" and (
            (value.get("video") or {}).get("method") == "same_mv_audio"
            or source.get("audio_origin") in {"mv", "mv_direct"}
            or source.get("identity_basis") == "exact_mv_id_full_decode"))

    def restore_job(self, song_id, folder, binding, *, audio_origin="song", reserve=None, playback_encoding=None):
        if not self.available():
            return None
        try:
            catalog = self._catalog(song_id)
            if catalog.resolve() != catalog or catalog.stat().st_size > 65536:
                return None
            value = json.loads(catalog.read_text())
            if value.get("song_id") != song_id or not self._same_audio_origin(value, audio_origin):
                return None
            if value.get("binding") != binding or set(value.get("media", {})) != {"original", "instrumental"}:
                return None
            if (playback_encoding == "pcm_s16le_wav"
                    and value["media"]["original"].get("encoding", "pcm_s16le_wav") != "pcm_s16le_wav"):
                return None
            video = value.get("video") or {"status": "unavailable"}
            expected = dict(value["media"])
            if video.get("status") == "ready":
                expected["video"] = video
            copies = []
            for kind, spec in expected.items():
                from .ktv_audio import audio_suffix
                suffix = ".mp4" if kind == "video" else audio_suffix(spec)
                item = value["files"][kind]
                sha, size = item["sha256"], item["bytes"]
                if (not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)
                        or item["file"] != sha + suffix or sha != spec["sha256"] or size != spec["bytes"]
                        or type(size) is not int or not 1 <= size <= 512 * 1024**2
                        or kind != "video" and spec.get("filename") != kind + suffix):
                    return None
                source = self._playback_blob(item["file"])
                if source.resolve() != source or source.stat().st_size != size:
                    return None
                copies.append((source, folder / (kind + suffix), sha, size))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        if reserve is not None:
            reserve(sum(size for _, _, _, size in copies))
        restored = []
        try:
            for source, destination, sha, _ in copies:
                existed = destination.exists()
                self._copy(source, destination, sha)
                if not existed:
                    restored.append(destination)
        except (OSError, ValueError):
            for path in restored:
                path.unlink(missing_ok=True)
            return None
        return {"media": value["media"], "video": video, "cache_hit": True, "audio_origin": audio_origin}

    def verify_prepared(self, song_id, *, job=None):
        """清理本机成品前核对 NAS 副本及对应音源版本。"""
        try:
            if not self.available():
                return False
            with self.lock:
                entry = json.loads(json.dumps(self.entries.get(song_id, {})))
            catalog = self._catalog(song_id)
            if catalog.is_symlink() or json.loads(catalog.read_text()) != entry:
                return False
            files = entry.get("files", {})
            if not {"original", "instrumental"}.issubset(files):
                return False
            if job is not None:
                video = job.get("video") or {}
                media = {**job["media"], **({"video": video} if video.get("status") == "ready" else {})}
                if entry.get("binding") != job.get("binding") or not self._same_audio_origin(entry, job.get("audio_origin", "song")):
                    return False
                if any((files.get(kind, {}).get("sha256"), files.get(kind, {}).get("bytes"))
                       != (spec["sha256"], spec["bytes"]) for kind, spec in media.items()):
                    return False
            if entry.get("video", {}).get("status") == "ready" and "video" not in files:
                return False
            for item in files.values():
                if not re.fullmatch(r"[a-f0-9]{64}\.(?:audio|wav|m4a|mp4)", item["file"]):
                    return False
                path = self._playback_blob(item["file"])
                if (path.is_symlink() or path.resolve() != path or path.stat().st_size != item["bytes"]
                        or _hash_file(path) != item["sha256"]):
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def summaries(self):
        with self.lock:
            result = {}
            for song_id, entry in self.entries.items():
                processed = {k: v for k, v in entry.get("files", {}).items() if k != "source"}
                unique = {item["file"]: item["bytes"] for item in list(processed.values()) + entry.get("retained", [])}
                result[song_id] = {"title": entry["title"], "bytes": sum(unique.values()),
                    "source_bytes": entry.get("files", {}).get("source", {}).get("bytes", 0),
                    "updated": entry["updated"], "revision": hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()}
            return result

    def remove_prepared(self, song_id):
        if not self.available():
            raise ValueError("archive_offline")
        with self.lock:
            if any(self.queue.jobs.get(job, {}).get("song_id") == song_id for job in self.active):
                raise ValueError("archive_busy")
            entry = self.entries.get(song_id)
            if not entry:
                return
            old_files = {v["file"] for k, v in entry["files"].items() if k != "source"}
            old_files |= {v["file"] for v in entry.get("retained", [])}
            keep = {v["file"] for sid, e in self.entries.items() if sid != song_id for v in e.get("files", {}).values()}
            keep |= {v["file"] for sid, e in self.entries.items() if sid != song_id for v in e.get("retained", [])}
            keep |= {v["file"] for e in self.entries.values() for v in e.get("originals", [])}
            entry = {**entry, "files": {k: v for k, v in entry["files"].items() if k == "source"}, "retained": [], "media": {}, "video": {"status": "unavailable"}, "updated": int(time.time())}
            _write(self._catalog(song_id), entry)
            for filename in old_files - keep:
                if not re.fullmatch(r"[a-f0-9]{64}\.(?:wav|m4a|mp4)", filename):
                    raise ValueError("archive_copy_invalid")
                (self.root / "blobs" / filename).unlink(missing_ok=True)
            self.entries[song_id] = entry
            self.results.pop(song_id, None)

    def prune_source_local(self, song_id):
        """Only our verified local copies; never the official client's originals."""
        try:
            if not self.available():
                return
            record = self.entries.get(song_id, {})
            source, metadata = self._source(song_id, record.get("audio_origin", "song"))
            candidates = [record.get("files", {}).get("source"), *record.get("originals", [])]
            raw = next((v for v in candidates if v and metadata and v["sha256"] == metadata.get("source_sha256")), None)
            if not raw or source is None or raw["sha256"] != metadata.get("source_sha256"):
                return
            if _hash_file(self.root / "blobs" / raw["file"]) != raw["sha256"] or _hash_file(source) != raw["sha256"]:
                return
            allowed = (self.queue.root / "api-downloads", self.queue.root / "cloud-downloads", self.queue.root / "downloads/decoded")
            if not any(source.is_relative_to(root) for root in allowed) or source.parent.resolve() != source.parent:
                return
            shutil.rmtree(source.parent)
            index = self.queue.root / "downloads/index" / (song_id + ".json")
            if index.is_file() and metadata.get("cache_key"):
                index.unlink()
        except (OSError, ValueError, KeyError):
            return

    def restore_source(self, song_id, cache, *, audio_origin="song"):
        song_id = canonical_song_id(song_id)
        if not self.available() or not self._catalog(song_id).is_file():
            return False
        value = json.loads(self._catalog(song_id).read_text())
        item, metadata = value.get("files", {}).get("source"), value.get("source_metadata")
        if not self._same_audio_origin(value, audio_origin):
            return False
        if not item or not metadata or metadata.get("song_id") != song_id:
            return False
        if not re.fullmatch(r"[a-f0-9]{64}\.audio", item["file"]):
            raise ValueError("archive_copy_invalid")
        folder = Path(cache) / song_id
        folder.mkdir(exist_ok=True, mode=0o700)
        self._copy(self.root / "blobs" / item["file"], folder / "source.audio", item["sha256"])
        _write(folder / "manifest.json", metadata)
        return True

    def tick(self):
        if self.available():
            for path in list((self.root / "catalog").glob("*.json"))[:2048]:
                try:
                    if re.fullmatch(r"[1-9][0-9]{0,18}\.json", path.name) and not path.is_symlink() and path.stat().st_size < 65536:
                        record = json.loads(path.read_text())
                        with self.lock:
                            self.entries[path.stem] = record
                except (OSError, ValueError):
                    pass
        with self.queue.lock:
            values = [json.loads(json.dumps(v)) for v in self.queue.jobs.values() if v["state"] == "ready"]
        for value in values:
            fingerprint = hashlib.sha256(json.dumps([value.get("media"), value.get("video")], sort_keys=True).encode()).hexdigest()
            if self.known.get(value["id"]) == fingerprint:
                continue
            with self.lock:
                self.active.add(value["id"])
            try:
                result = self.archive(value)
                with self.lock:
                    self.results[value["song_id"]] = result
                    self.known[value["id"]] = fingerprint
                if value.get("audio_origin") in {"mv", "mv_direct"}:
                    # 归档成功后才回收抓取的 MV 中间音频。
                    folder = self.queue.jobs_dir / value["id"]
                    for name in (".mv-audio.wav", "mv-source.json"):
                        path = folder / name
                        if path.resolve() == path:
                            path.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError):
                with self.lock:
                    self.results[value["song_id"]] = {"state": "waiting", "updated": int(time.time())}
            finally:
                with self.lock:
                    self.active.discard(value["id"])

    def status(self):
        with self.lock:
            unique = {item["file"]: item["bytes"] for entry in self.entries.values()
                      for item in list(entry.get("files", {}).values()) + entry.get("retained", []) + entry.get("originals", [])}
            return {"configured": True, "available": self.available(), "songs": dict(self.results),
                    "active": len(self.active), "hot_cache_bytes": self.hot_limit,
                    "bytes": sum(unique.values())}

    def trim_local(self):
        if getattr(self.queue, "storage_manager", None):
            # 同一个 Mac 热缓存由主清理线程统一管理。
            return
        if not self.cache_manager or not self.available():
            return
        inventory = self.cache_manager.inventory()
        used = sum(row.get("local_bytes", row["bytes"]) for row in inventory["entries"])
        for row in sorted(inventory["entries"], key=lambda item: item["last_used"]):
            if used <= self.hot_limit:
                break
            if row.get("local_bytes", 0) <= 0 or not row.get("nas_bytes") or not row["can_delete"]:
                continue
            try:
                self.cache_manager.delete(row["id"], row["revision"], local_only=True)
                used -= row["local_bytes"]
            except Exception:
                continue

    def start(self):
        def run():
            while not self.stop.is_set():
                try:
                    self.tick()
                    for name in list(self.duplicate_candidates)[:2]:
                        self.duplicate_candidates.pop(name)
                        try:
                            if not self.compact_wav(name, apply=True) and self._shared_wav(name) is None:
                                self.duplicate_candidates[name] = None
                        except (OSError, ValueError):
                            pass
                    self.trim_local()
                except (OSError, ValueError, KeyError):
                    pass
                self.stop.wait(10)
        self.thread = threading.Thread(target=run, name="ktv-nas-archive", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def managed_inventory(self, *, cache_catalogs=False):
        """All managed archive copies, including old sources and retained editions."""
        from .ktv_library import read_record, signature
        if not self.available():
            raise ValueError("archive_offline")
        with self.lock:
            catalogs = {}
            catalog_root = self.root / "catalog"
            if catalog_root.is_dir() and catalog_root.resolve() == catalog_root:
                with os.scandir(catalog_root) as files:
                    for item in files:
                        if not re.fullmatch(r"[1-9][0-9]{0,17}\.json", item.name):
                            continue
                        try:
                            info = item.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                            continue
                        sid = item.name[:-5]
                        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        previous = self.inventory_catalogs.get(sid) if cache_catalogs else None
                        value = previous[1] if previous and previous[0] == identity else read_record(Path(item.path), 65536)
                        if value.get("song_id") == sid and isinstance(value.get("files"), dict):
                            self.entries[sid] = value
                            catalogs[sid] = (identity, value)
            self.inventory_catalogs = catalogs
            groups, referenced = {}, set()
            for sid, entry in self.entries.items():
                paths = {self._catalog(sid)}
                unsafe = False
                for item in [*entry.get("files", {}).values(), *entry.get("retained", []), *entry.get("originals", [])]:
                    name = item.get("file", "")
                    if not re.fullmatch(r"[a-f0-9]{64}\.(?:audio|wav|m4a|mp4)", name):
                        unsafe = True
                        continue
                    path = self.root / "blobs" / name
                    paths.add(path)
                    referenced.add(path)
                active = any(self.queue.jobs.get(job, {}).get("song_id") == sid for job in self.active)
                groups["song:" + sid] = {"id": "song:" + sid, "title": entry["title"], "last_used": entry.get("last_used", entry["updated"]),
                    "kind": "song", "paths": paths, "can_delete": not (unsafe or active),
                    "reason": "transferring" if active else "unsafe" if unsafe else ""}
            blobs = self.root / "blobs"
            if blobs.is_dir() and not blobs.is_symlink():
                for path in blobs.iterdir():
                    if path not in referenced and re.fullmatch(r"[a-f0-9]{64}\.(?:audio|wav|m4a|mp4)", path.name):
                        key = "legacy:" + path.name
                        groups[key] = {"id": key, "title": "未关联歌曲的 NAS 归档", "last_used": int(path.lstat().st_mtime),
                            "kind": "orphan", "paths": {path}, "can_delete": not self.active,
                            "reason": "transferring" if self.active else ""}
            guide_files, linked_guide_files = 0, 0
            if self.guide_root and self.guide_root.is_dir() and not self.guide_root.is_symlink():
                for path in self.guide_root.glob("*.mp3"):
                    if not re.fullmatch(r"[a-f0-9]{64}\.mp3", path.name):
                        continue
                    owners = self.guide_links.get(path.stem, [])
                    guide_files += 1
                    linked_guide_files += bool(owners)
                    if not owners:
                        key = "legacy-guide:" + path.stem
                        groups[key] = {"id": key, "title": "旧版未关联伴唱缓存", "kind": "orphan", "paths": {path},
                            "last_used": int(path.lstat().st_mtime), "can_delete": not self.active,
                            "reason": "transferring" if self.active else ""}
                    for owner in owners:
                        key = "song:" + owner["song_id"]
                        row = groups.setdefault(key, {"id": key, "title": owner["title"], "kind": "song", "paths": set(),
                            "last_used": owner["last_used"], "can_delete": True, "reason": ""})
                        row["paths"].add(path)
                        row["last_used"] = max(row["last_used"], owner["last_used"])
            unique = {}
            for item in groups.values():
                revision, files, unsafe = signature(item["paths"])
                item.update(revision=revision, bytes=sum(files.values()))
                if unsafe:
                    item.update(can_delete=False, reason="unsafe")
                unique.update(files)
            return {"entries": groups, "bytes": sum(unique.values()), "legacy_guide_files": guide_files,
                    "legacy_guide_linked_files": linked_guide_files}

    def remove_all(self, items):
        """Explicit owner removal; no source exception and no automatic resurrection."""
        with self.queue.lock, self.lock:
            data = self.managed_inventory()
            selected = {item["id"] for item in items}
            for item in items:
                row = data["entries"].get(item["id"])
                if not row or row["revision"] != item["revision"] or not row["can_delete"]:
                    raise ValueError("archive_changed")
            paths = set().union(*(data["entries"][key]["paths"] for key in selected))
            keep = set().union(*(row["paths"] for key, row in data["entries"].items() if key not in selected)) if len(data["entries"]) > len(selected) else set()
            for key in selected:
                if key.startswith("song:"):
                    sid = key[5:]
                    self.exclusions[sid] = sorted({*self.exclusions.get(sid, []), *(j["id"] for j in self.queue.jobs.values() if j["song_id"] == sid)})[-128:]
            _write(self.exclusions_path, self.exclusions)
            self.on_exclude({job for key in selected if key.startswith("song:") for job in self.exclusions.get(key[5:], [])})
            removed = 0
            for path in paths - keep:
                if path.resolve() != path or path.is_symlink():
                    raise ValueError("archive_changed")
                if path.is_file():
                    removed += path.stat().st_size
                    path.unlink()
            for key in selected:
                if key.startswith("song:"):
                    self.entries.pop(key[5:], None)
                    self.results.pop(key[5:], None)
            return {"removed": len(selected), "removed_bytes": removed}
