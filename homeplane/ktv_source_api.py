"""Local authenticated source acquisition; no client windows or device actions.

Only the owner console can initiate QR authorization. Credentials travel through
private pipes, stay in a 0600 local file and never appear in public snapshots.
"""
from __future__ import annotations

import fcntl
import concurrent.futures
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .ktv_download import _check, _directory, _hash_file, _run, _write, canonical_song_id
from .ktv_metadata import song_title

VERSION = "4.41.1"
MAX_BYTES = 128 * 1024 * 1024
SOURCE_ERRORS = {"source_login_required", "source_rights_denied", "source_api_unavailable", "source_file_invalid"}


class LocalSourceAPI:
    def __init__(self, root: Path, provider: Path, *, node=None, rpc=None):
        self.root = _directory(root, create=True)
        self.provider = provider.absolute()
        self.node = node or shutil.which("node")
        self.bridge = Path(__file__).resolve().parents[1] / "integrations/ktv_source/bridge.cjs"
        self.tmp = _directory(self.root / "tmp", create=True)
        self.session_path = self.root / "session.json"
        self.lock = threading.RLock()
        self.qr_key, self.qr_expires, self.login_state = "", 0, "not_logged_in"
        self.rpc = rpc or self._rpc
        self.lyric_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="ktv-cloud-lyrics")
        self.lyric_futures = {}

    def _rpc(self, op, **values):
        if not self.node or not (self.provider / "package.json").is_file():
            raise ValueError("source_api_unavailable")
        try:
            # Credentials are stdin only. No inherited proxy, cookie or environment secrets.
            result = subprocess.run(
                [self.node, str(self.bridge), str(self.provider)],
                input=json.dumps({"op": op, **values}).encode(), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=22, cwd=self.root,
                env={"PATH": "/usr/bin:/bin", "TMPDIR": str(self.tmp), "LANG": "en_US.UTF-8"},
            )
            if result.returncode or len(result.stdout) > 1048576:
                raise ValueError()
            value = json.loads(result.stdout)
            if value.get("ok") is not True:
                _write(self.root / "last-failure.json", {"operation": op, "at": int(time.time()),
                    "code": "upstream_refused", "status": value.get("status"), "upstream_code": value.get("upstream_code"),
                    "phase": value.get("phase"), "error_type": value.get("error_type"), "local_code": value.get("local_code")})
                raise ValueError()
            return value
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ValueError("source_api_unavailable") from None

    def _session(self):
        with self.lock:
            if not self.session_path.exists():
                return ""
            if self.session_path.is_symlink() or self.session_path.stat().st_mode & 0o077:
                raise ValueError("source_api_unavailable")
            value = json.loads(self.session_path.read_text())
            cookie = value.get("cookie", "")
            if not isinstance(cookie, str) or len(cookie) > 32768:
                raise ValueError("source_api_unavailable")
            return cookie

    def status(self):
        logged_in = bool(self._session())
        return {"provider": "local_api", "version": VERSION, "logged_in": logged_in,
                "state": "logged_in" if logged_in else self.login_state, "qr_available": bool(self.qr_key and time.time() < self.qr_expires),
                "client_window_required": False, "settings": self.settings()}

    def settings(self):
        path = self.root / "settings.json"
        value = {"level": "standard"}
        if path.is_file():
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError("source_api_unavailable")
            value = json.loads(path.read_text())
        if value not in ({"level": "standard"}, {"level": "exhigh"}):
            raise ValueError("source_api_unavailable")
        return value

    def configure(self, value):
        if value not in ({"level": "standard"}, {"level": "exhigh"}):
            raise ValueError("invalid_request")
        with self.lock:
            _write(self.root / "settings.json", value)
            return self.settings()

    def official_login(self):
        bundle = Path(__file__).resolve().parents[1] / ".artifacts/ktv-source/OfficialLogin.app"
        try:
            subprocess.run(["/usr/bin/codesign", "--verify", "--deep", str(bundle)], check=True,
                           capture_output=True, timeout=8)
            subprocess.run(["/usr/bin/open", "-n", str(bundle), "--args", str(self.session_path)],
                           check=True, capture_output=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            raise ValueError("source_api_unavailable") from None
        self.qr_key, self.login_state = "", "waiting_official"
        return self.status()

    def begin_login(self):
        with self.lock:
            response = self.rpc("qr", cookie="")["body"]
            data = response.get("data", response)
            key = data.get("unikey", "")
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", key):
                raise ValueError("source_api_unavailable")
            self.qr_key, self.qr_expires, self.login_state = key, time.time() + 180, "waiting_scan"
            return self.status()

    def qr(self):
        with self.lock:
            if not self.qr_key or time.time() >= self.qr_expires:
                raise ValueError("source_login_required")
            import qrcode
            from qrcode.image.pure import PyPNGImage
            image = qrcode.make("https://music.163.com/login?codekey=" + self.qr_key,
                                image_factory=PyPNGImage, box_size=8, border=4)
            output = io.BytesIO()
            image.save(output)
            return output.getvalue()

    def poll_login(self):
        with self.lock:
            if not self.qr_key:
                return self.status()
            if time.time() >= self.qr_expires:
                self.qr_key, self.login_state = "", "expired"
                return self.status()
            value = self.rpc("poll", key=self.qr_key, cookie="")
            code = value["body"].get("code")
            self.login_state = {800: "expired", 801: "waiting_scan", 802: "waiting_confirm", 803: "logged_in"}.get(code, "unavailable")
            if code == 803:
                cookies = value.get("cookie", [])
                if not isinstance(cookies, list) or not all(isinstance(c, str) for c in cookies):
                    raise ValueError("source_api_unavailable")
                # Store name=value pairs only; Set-Cookie attributes are not credentials.
                cookie = "; ".join(c.split(";", 1)[0] for c in cookies)
                if not cookie or len(cookie) > 32768 or "MUSIC_U=" not in cookie:
                    raise ValueError("source_api_unavailable")
                _write(self.session_path, {"cookie": cookie, "created": int(time.time())})
                self.qr_key = ""
            return self.status()

    def source(self, song_id):
        song_id = canonical_song_id(song_id)
        cookie = self._session()
        if not cookie:
            raise ValueError("source_login_required")
        details = self.rpc("detail", id=song_id, cookie=cookie)["body"]
        songs = details.get("songs", [])
        if details.get("code") in {301, 302}:
            raise ValueError("source_login_required")
        if details.get("code") != 200 or len(songs) != 1 or str(songs[0].get("id")) != song_id:
            raise ValueError("source_rights_denied")
        duration = songs[0].get("dt")
        if type(duration) not in {int, float} or not math.isfinite(duration) or not 1000 < duration <= 1800000:
            raise ValueError("source_file_invalid")
        level = self.settings()["level"]
        value = self.rpc("download", id=song_id, cookie=cookie, level=level)["body"]
        if value.get("code") in {301, 302}:
            raise ValueError("source_login_required")
        item = value.get("data")
        if (value.get("code") != 200 or not isinstance(item, dict) or item.get("code") != 200
                or not item.get("url") or item.get("freeTrialInfo") is not None):
            raise ValueError("source_rights_denied")
        if str(item.get("id")) != song_id:
            raise ValueError("download_identity_mismatch")
        if type(item.get("size")) is not int or not 1024 < item["size"] <= MAX_BYTES:
            raise ValueError("source_file_invalid")
        if not isinstance(item.get("md5"), str) or not re.fullmatch(r"[a-fA-F0-9]{32}", item["md5"]):
            raise ValueError("source_file_invalid")
        return {**item, "duration_seconds": duration / 1000, "requested_level": level}

    def cloud_catalog(self):
        """Owner-authorized metadata only; never publish account IDs, filenames or URLs."""
        cookie = self._session()
        if not cookie:
            raise ValueError("source_login_required")
        songs, owned = {}, {}
        for offset in range(0, 5000, 100):
            body = self.rpc("cloud", offset=offset, cookie=cookie)["body"]
            rows = body.get("data")
            if body.get("code") != 200 or not isinstance(rows, list) or len(rows) > 100:
                raise ValueError("source_api_unavailable")
            for row in rows:
                song_id = canonical_song_id(str(row.get("songId", "")))
                simple = row.get("simpleSong", {})
                title = simple.get("name") or row.get("songName")
                if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
                    continue
                title = song_title("".join(c for c in title if ord(c) >= 32 and ord(c) != 127))
                artists = [a["name"][:100] for a in simple.get("ar", [])[:8]
                           if isinstance(a, dict) and isinstance(a.get("name"), str)]
                private = row.get("privateCloud", {})
                if isinstance(private, dict) and isinstance(private.get("md5"), str) and re.fullmatch(r"[a-fA-F0-9]{32}", private["md5"]):
                    owned[song_id] = {"md5": private["md5"].lower(), "bytes": row.get("fileSize"),
                        "title": simple.get("name") or title, "artists": artists, "duration_ms": simple.get("dt"),
                        "lyric_id": str(row.get("lyricId", "0"))}
                songs[song_id] = {"song_id": song_id, "title": title, "artists": artists, "source": "cloud"}
            if not body.get("hasMore") or len(rows) < 100:
                _write(self.root / "cloud-owned.json", {"updated": int(time.time()), "songs": owned})
                return list(songs.values())
        raise ValueError("cloud_catalog_too_large")

    def cloud_source(self, song_id):
        song_id = canonical_song_id(song_id)
        cookie = self._session()
        if not cookie:
            raise ValueError("source_login_required")
        details = self.rpc("cloud_detail", id=song_id, cookie=cookie)["body"]
        rows = details.get("data", [])
        if (details.get("code") != 200 or not isinstance(rows, list) or len(rows) != 1
                or str(rows[0].get("songId")) != song_id):
            raise ValueError("source_rights_denied")
        row = rows[0]
        path = self.root / "cloud-owned.json"
        record = {}
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 1048576 and not path.stat().st_mode & 0o077:
            record = json.loads(path.read_text())
        if time.time() - record.get("updated", 0) > 300 or song_id not in record.get("songs", {}):
            self.cloud_catalog()
            record = json.loads(path.read_text())
        owned = record.get("songs", {}).get(song_id)
        if not owned:
            raise ValueError("source_rights_denied")
        duration = row.get("simpleSong", {}).get("dt")
        if type(duration) not in {int, float} or not math.isfinite(duration) or not 1000 < duration <= 1800000:
            raise ValueError("source_file_invalid")
        value = self.rpc("cloud_url", id=song_id, cookie=cookie)["body"]
        items = value.get("data", [])
        if value.get("code") != 200 or not isinstance(items, list) or len(items) != 1:
            raise ValueError("source_rights_denied")
        item = items[0]
        if (item.get("code") != 200 or str(item.get("id")) != song_id or not item.get("url")
                or item.get("freeTrialInfo") is not None):
            raise ValueError("source_rights_denied")
        # Match the owner's uploaded bytes, not a public matched-song substitute.
        expected_md5 = owned.get("md5")
        expected_size = row.get("fileSize")
        if (type(item.get("size")) is not int or not 1024 < item["size"] <= MAX_BYTES
                or item["size"] != expected_size or item["size"] != owned.get("bytes") or not isinstance(expected_md5, str)
                or not re.fullmatch(r"[a-fA-F0-9]{32}", expected_md5)
                or str(item.get("md5", "")).lower() != expected_md5.lower()):
            raise ValueError("download_identity_mismatch")
        return {**item, "duration_seconds": duration / 1000, "requested_level": "cloud_original"}

    def prepare_cloud_lyrics(self, song_id):
        song_id = canonical_song_id(song_id)
        with self.lock:
            previous = self.lyric_futures.get(song_id)
            if previous and not previous.done():
                return previous
            if len([f for f in self.lyric_futures.values() if not f.done()]) >= 16:
                return None
            self.lyric_futures = {k: f for k, f in self.lyric_futures.items() if not f.done()}
            future = self.lyric_pool.submit(self._cloud_lyrics, song_id)
            self.lyric_futures[song_id] = future
            return future

    def cloud_lyrics(self, song_id):
        future = self.prepare_cloud_lyrics(song_id)
        try:
            return future.result(timeout=6) if future else {"status": "unavailable", "lines": []}
        except (concurrent.futures.TimeoutError, OSError, ValueError):
            return {"status": "unavailable", "lines": []}

    def _cloud_lyrics(self, song_id):
        from .ktv_search import parse_lrc, search_songs
        cookie = self._session()
        if not cookie:
            return {"status": "unavailable", "lines": []}
        owned_path = self.root / "cloud-owned.json"
        owned = json.loads(owned_path.read_text()).get("songs", {}).get(song_id, {}) if owned_path.is_file() else {}
        if not owned.get("title"):
            self.cloud_catalog()
            owned = json.loads(owned_path.read_text()).get("songs", {}).get(song_id, {})
        if not owned.get("md5"):
            return {"status": "unavailable", "lines": []}
        owned = {**owned, "title": song_title(owned["title"])}
        directory = _directory(self.root / "cloud-lyrics", create=True)
        path = directory / (song_id + ".json")
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 262144:
            cached = json.loads(path.read_text())
            ttl = 7 * 86400 if cached.get("status") == "ready" else 300
            if cached.get("upload_md5") == owned["md5"] and time.time() - cached.get("at", 0) < ttl:
                return {"status": cached["status"], "lines": cached["lines"]}

        def timed(body):
            if body.get("uncollected") or body.get("nolyric") or body.get("pureMusic"):
                return []
            lines = parse_lrc(body.get("lrc", {}).get("lyric", ""))
            return lines if len(lines) >= 3 and not any("暂无歌词" in r["text"] or "纯音乐，请欣赏" in r["text"] for r in lines) else []

        def fetch_lyrics(identity, credential):
            try:
                return timed(self.rpc("lyrics", id=identity, cookie=credential)["body"])
            except (ValueError, KeyError, TypeError):
                return []

        lines = fetch_lyrics(song_id, cookie)
        basis = "owned_cloud_id"
        lyric_id = owned.get("lyric_id", "0")
        if not lines and re.fullmatch(r"[1-9][0-9]{0,17}", lyric_id) and lyric_id != song_id:
            lines = fetch_lyrics(lyric_id, cookie)
            basis = "cloud_lyric_id"
        if not lines and owned.get("artists") and isinstance(owned.get("duration_ms"), (int, float)):
            # Metadata matching supplies lyrics only. Never substitute the uploaded audio.
            try:
                candidates = search_songs(owned["title"] + " " + _lyric_artist(owned["artists"][0]))
            except (ValueError, httpx.HTTPError):
                candidates = []
            candidates = [c for c in candidates if _same_lyric_identity(owned, c)]
            if not candidates:
                candidates = [c for c in self._artist_lyric_candidates(owned) if _same_lyric_identity(owned, c)]
            for candidate in candidates[:3]:
                try:
                    songs = self.rpc("detail", id=candidate["song_id"], cookie="")["body"].get("songs", [])
                    if len(songs) != 1 or str(songs[0].get("id")) != candidate["song_id"]:
                        continue
                    duration = songs[0].get("dt")
                    if not isinstance(duration, (int, float)) or abs(duration - owned["duration_ms"]) > 2500:
                        continue
                    lines = fetch_lyrics(candidate["song_id"], "")
                    if lines:
                        basis = "exact_title_artist_duration"
                        break
                except (ValueError, KeyError, TypeError):
                    continue
        duration = owned.get("duration_ms")
        if isinstance(duration, (int, float)):
            lines = [r for r in lines if r["time_ms"] <= duration + 2000]
        result = {"status": "ready" if lines else "unavailable", "lines": lines}
        _write(path, {**result, "upload_md5": owned["md5"], "basis": basis, "at": int(time.time())})
        return result

    def _artist_lyric_candidates(self, owned):
        artist = _lyric_artist(owned["artists"][0])
        path = _directory(self.root / "lyric-artists", create=True) / (hashlib.sha256(artist.encode()).hexdigest() + ".json")
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 262144:
            cached = json.loads(path.read_text())
            if time.time() - cached.get("at", 0) < (7 * 86400 if cached.get("songs") else 300):
                return cached["songs"]
        songs = []
        try:
            rows = self.rpc("artist_search", artist=artist, cookie="")["body"].get("result", {}).get("artists", [])
            match = next((r for r in rows[:5] if _lyric_normalize(r.get("name", "")) == _lyric_normalize(artist)), None)
            if match:
                identity = canonical_song_id(str(match["id"]))
                for offset in (0, 100, 200):
                    body = self.rpc("artist_songs", id=identity, offset=offset, cookie="")["body"]
                    values = body.get("songs", [])
                    if body.get("code") != 200 or not isinstance(values, list) or len(values) > 100:
                        break
                    for row in values:
                        songs.append({"song_id": canonical_song_id(str(row["id"])), "title": row.get("name", ""),
                            "artists": [a["name"] for a in row.get("ar", []) if isinstance(a.get("name"), str)]})
                    if not body.get("more") or len(values) < 100:
                        break
        except (ValueError, KeyError, TypeError):
            pass
        _write(path, {"at": int(time.time()), "songs": songs})
        return songs

    def close(self):
        self.lyric_pool.shutdown(wait=False, cancel_futures=True)


def _lyric_normalize(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _lyric_artist(value):
    return re.split(r"\s*[（(]\s*(?:feat\.?|ft\.?|featuring)\s", value, flags=re.IGNORECASE)[0].strip()


def _same_lyric_identity(owned, candidate):
    return (_lyric_normalize(song_title(owned.get("title", ""))) == _lyric_normalize(song_title(candidate.get("title", "")))
            and bool({_lyric_normalize(_lyric_artist(a)) for a in owned.get("artists", []) if a}
                     & {_lyric_normalize(_lyric_artist(a)) for a in candidate.get("artists", []) if a}))


def source_url(value):
    try:
        u = urlsplit(value)
        host = u.hostname or ""
        if (u.scheme not in {"http", "https"} or u.username or u.password or u.fragment
                or u.port not in {None, 80, 443} or not re.fullmatch(r"[a-z0-9.-]+", host)
                or not host.endswith(".music.126.net")):
            raise ValueError()
        return urlunsplit(("https", host, u.path, u.query, ""))
    except (TypeError, ValueError):
        raise ValueError("source_file_invalid") from None


class APIAudioAdapter:
    def __init__(self, api, cache: Path, *, legacy=None, enabled=False, archive=None, source="song"):
        self.api, self.cache, self.legacy, self.enabled = api, _directory(cache, create=True), legacy, enabled
        self.archive = archive
        if source not in {"song", "cloud"} or (source == "cloud" and legacy is not None):
            raise ValueError("invalid_source")
        self.source = source

    def catalog(self):
        return self.legacy.catalog() if self.legacy else []

    def acquire(self, song_id, *, timeout=240, cancel=None, progress=lambda value: None):
        song_id = canonical_song_id(song_id)
        if self.source == "cloud" and callable(getattr(self.api, "prepare_cloud_lyrics", None)):
            self.api.prepare_cloud_lyrics(song_id)
        cancel = cancel or threading.Event()
        deadline = time.monotonic() + timeout
        destination = self.cache / song_id
        locks = _directory(self.cache / "locks", create=True)
        with (locks / (song_id + ".lock")).open("a") as lock:
            lock_path = locks / (song_id + ".lock")
            lock_path.chmod(0o600)
            while True:
                _check(cancel, deadline)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    cancel.wait(.1)
            if self.archive and not (destination / "manifest.json").is_file():
                try:
                    self.archive.restore_source(song_id, self.cache, audio_origin=self.source)
                except (OSError, ValueError):
                    pass
            if (destination / "manifest.json").is_file():
                data = json.loads((destination / "manifest.json").read_text())
                audio = destination / "source.audio"
                if (data.get("song_id") != song_id or data.get("source", "song") != self.source or not data.get("full_decode_verified")
                        or _hash_file(audio) != data.get("source_sha256")):
                    raise ValueError("source_file_invalid")
                progress({"phase": "source_cache_hit", "bytes": data["source_bytes"]})
                return {**data, "status": "ready", "cache_hit": True, "source_path": str(audio)}
            if self.legacy:
                # Existing user-owned NCM files remain usable; this adapter can never launch the client.
                old = self.legacy.acquire(song_id, timeout=min(30, timeout), cancel=cancel, progress=progress)
                if old.get("status") == "ready":
                    return old
            if not self.enabled:
                raise ValueError("source_login_required")
            progress({"phase": "source_authorizing", "bytes": 0})
            spec = self.api.cloud_source(song_id) if self.source == "cloud" else self.api.source(song_id)
            url = source_url(spec["url"])
            destination.mkdir(mode=0o700, exist_ok=True)
            audio = destination / "source.audio"
            part = destination / "source.part"
            # Failed partial transfers are private and never eligible as complete source cache.
            part.unlink(missing_ok=True)
            total, md5 = 0, hashlib.md5()
            try:
                with httpx.stream("GET", url, timeout=12, follow_redirects=False, trust_env=False) as reply:
                    if reply.status_code != 200:
                        raise ValueError("source_api_unavailable")
                    if reply.headers.get("content-length") and int(reply.headers["content-length"]) != spec["size"]:
                        raise ValueError("source_file_invalid")
                    with part.open("xb") as output:
                        part.chmod(0o600)
                        last = 0
                        for chunk in reply.iter_bytes(65536):
                            _check(cancel, deadline)
                            total += len(chunk)
                            if total > spec["size"]:
                                raise ValueError("source_file_invalid")
                            output.write(chunk)
                            md5.update(chunk)
                            if time.monotonic() - last > .3:
                                progress({"phase": "source_downloading", "bytes": total, "total": spec["size"]})
                                last = time.monotonic()
                if total != spec["size"] or md5.hexdigest() != spec["md5"].lower():
                    raise ValueError("source_file_invalid")
                progress({"phase": "decoding", "bytes": total, "total": total})
                ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
                probe = json.loads(_run([ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
                    "stream=codec_name,channels,sample_rate:format=duration", "-of", "json", str(part)],
                    cancel, deadline, destination, capture=True))
                streams = probe.get("streams", [])
                duration = float(probe.get("format", {}).get("duration", 0))
                if (len(streams) != 1 or streams[0].get("codec_name") not in {"mp3", "flac", "aac"}
                        or not 1 <= int(streams[0].get("channels", 0)) <= 2
                        or not math.isfinite(duration)
                        or abs(duration - spec["duration_seconds"]) > max(1, spec["duration_seconds"] * .01)):
                    raise ValueError("source_file_invalid")
                _run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(part),
                      "-map", "0:a:0", "-f", "null", "-"], cancel, deadline, destination)
                part.replace(audio)
                data = {"song_id": song_id, "source_sha256": _hash_file(audio), "source_bytes": total,
                        "duration_seconds": duration, "full_decode_verified": True,
                        "exact_song_id_verified": True, "identity_basis": "authenticated_download_id_md5_duration",
                        "provider": "local_api", "provider_version": VERSION, "created": int(time.time())}
                data.update(requested_level=spec.get("requested_level", "unknown"), bitrate=spec.get("br", 0), source=self.source)
                _write(destination / "manifest.json", data)
                return {**data, "status": "ready", "cache_hit": False, "source_path": str(audio)}
            except httpx.HTTPError:
                raise ValueError("source_api_unavailable") from None
            finally:
                part.unlink(missing_ok=True)
