"""私有 NAS 电视应用发布存储。发布入口校验 Android 签名，服务端只读取当前成品。"""

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path


UPDATE_INFO_PATH = "/v1/tv/update"
UPDATE_APK_PATH = "/v1/tv/update.apk"
MAX_APK_BYTES = 64 * 1024 * 1024
APK_CONTENT_TYPE = "application/vnd.android.package-archive"
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
VERSION = re.compile(r"[0-9][A-Za-z0-9._+-]{0,63}\Z")
STATE_FIELDS = ("current", "previous", "pending", "retired")
MANIFEST_LIMIT = 8192


class TvUpdateError(ValueError):
    pass


@dataclass(frozen=True)
class Release:
    version_code: int
    version_name: str
    bytes: int
    sha256: str
    certificate_sha256: str

    def __post_init__(self):
        if (type(self.version_code) is not int or not 0 < self.version_code <= 2147483647
                or not isinstance(self.version_name, str) or not VERSION.fullmatch(self.version_name)
                or type(self.bytes) is not int or not 0 < self.bytes <= MAX_APK_BYTES
                or not isinstance(self.sha256, str) or not SHA256.fullmatch(self.sha256)
                or not isinstance(self.certificate_sha256, str) or not SHA256.fullmatch(self.certificate_sha256)):
            raise TvUpdateError("invalid_tv_release")

    def public(self):
        return {"version_code": self.version_code, "version_name": self.version_name,
                "bytes": self.bytes, "sha256": self.sha256, "url": UPDATE_APK_PATH}


def _private_regular_file(path, flags=os.O_RDONLY):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_mode & 0o077:
            raise TvUpdateError("private_tv_release_file_required")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path, *chunks, part_name=".tv-update-publish.part"):
    # 发布锁内只使用一个暂存路径，中断后可回收本次发布留下的未提交文件。
    part = path.parent / part_name
    try:
        os.close(_private_regular_file(part))
    except FileNotFoundError:
        pass
    else:
        part.unlink()
    fd = _private_regular_file(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "wb") as stream:
            for chunk in chunks:
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, path)
        _fsync_directory(path.parent)
    finally:
        part.unlink(missing_ok=True)


class TvUpdateStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).absolute()
        self.directory = self.state_dir / "tv-updates"
        self.manifest = self.directory / "release.json"
        self.verification_lock = threading.Lock()
        self.download_slots = threading.BoundedSemaphore(2)
        self.verified = None

    def _check_directory(self):
        if self.directory.resolve() != self.directory:
            raise TvUpdateError("private_tv_release_directory_required")
        value = self.directory.stat()
        if not stat.S_ISDIR(value.st_mode) or value.st_mode & 0o077:
            raise TvUpdateError("private_tv_release_directory_required")

    def _state(self):
        try:
            self._check_directory()
            with os.fdopen(_private_regular_file(self.manifest), "rb") as stream:
                raw = stream.read(MANIFEST_LIMIT + 1)
        except FileNotFoundError:
            return dict.fromkeys(STATE_FIELDS)
        try:
            if len(raw) > MANIFEST_LIMIT:
                raise ValueError
            doc = json.loads(raw)
            if (not isinstance(doc, dict) or set(doc) != {"schema", *STATE_FIELDS}
                    or type(doc["schema"]) is not int or doc["schema"] != 1):
                raise ValueError
            releases = {key: Release(**doc[key]) if doc[key] is not None else None for key in STATE_FIELDS}
            current, previous, pending, retired = (releases[key] for key in STATE_FIELDS)
            items = [release for release in releases.values() if release]
            if ((previous and (not current or previous.version_code >= current.version_code))
                    or (pending and current and pending.version_code <= current.version_code)
                    or (retired and (not previous or retired.version_code >= previous.version_code))
                    or len({release.sha256 for release in items}) != len(items)
                    or len({release.certificate_sha256 for release in items}) > 1):
                raise ValueError
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise TvUpdateError("invalid_tv_release_state") from None
        return releases

    def _write_state(self, releases):
        doc = {"schema": 1, **{key: asdict(value) if value else None for key, value in releases.items()}}
        _atomic_write(self.manifest, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())

    def _apk(self, release):
        return self.directory / (release.sha256 + ".apk")

    def _verified_apk(self, release):
        stream = os.fdopen(_private_regular_file(self._apk(release)), "rb")
        try:
            before = _fingerprint(os.fstat(stream.fileno()))
            if before[2] != release.bytes:
                raise TvUpdateError("tv_release_apk_changed")
            identity = (release.sha256, before)
            with self.verification_lock:
                if self.verified != identity:
                    digest = hashlib.sha256()
                    remaining = release.bytes
                    while remaining:
                        chunk = stream.read(min(256 * 1024, remaining))
                        if not chunk:
                            raise TvUpdateError("tv_release_apk_changed")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if digest.hexdigest() != release.sha256 or _fingerprint(os.fstat(stream.fileno())) != before:
                        raise TvUpdateError("tv_release_apk_changed")
                    self.verified = identity
            stream.seek(0)
            return stream
        except BaseException:
            stream.close()
            raise

    def open_current(self):
        """将已核验的打开文件交给调用者，发布切换时仍可读取同一成品。"""
        try:
            release = self._state()["current"]
            if release:
                return release, self._verified_apk(release)
        except (OSError, TvUpdateError):
            return None
        return None

    def _plan(self, release, state):
        current, pending = state["current"], state["pending"]
        if current == release:
            with self._verified_apk(current):
                return "already_current"
        for item in (value for value in state.values() if value is not None):
            if item.certificate_sha256 != release.certificate_sha256:
                raise TvUpdateError("tv_release_signer_changed")
            if release.sha256 == item.sha256 and release != item:
                raise TvUpdateError("tv_release_content_reused")
        for item in (value for value in (current, pending) if value is not None):
            if release.version_code < item.version_code or (
                    release.version_code == item.version_code and release != item):
                raise TvUpdateError("tv_release_version_must_increase")
        return "planned"

    def plan(self, release):
        return self._plan(release, self._state())

    @contextmanager
    def _publisher_lock(self):
        if (self.state_dir.resolve() != self.state_dir or not self.state_dir.is_dir()
                or self.state_dir.stat().st_mode & 0o077):
            raise TvUpdateError("private_nas_state_directory_required")
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            _fsync_directory(self.state_dir)
        self._check_directory()
        with os.fdopen(_private_regular_file(self.directory / ".publish.lock", os.O_CREAT | os.O_RDWR), "r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _discard(self, release):
        # 清理范围来自发布记录，只涉及本链路旧 APK。
        path = self._apk(release)
        try:
            os.close(_private_regular_file(path))
        except FileNotFoundError:
            return
        path.unlink()
        _fsync_directory(self.directory)

    def publish(self, release, body):
        if len(body) != release.bytes or hashlib.sha256(body).hexdigest() != release.sha256:
            raise TvUpdateError("tv_release_bytes_mismatch")
        with self._publisher_lock():
            state = self._state()
            result = self._plan(release, state)
            if state["retired"]:
                self._discard(state["retired"])
                state["retired"] = None
                self._write_state(state)
            if result == "already_current":
                return result
            if state["pending"] and state["pending"] != release:
                self._discard(state["pending"])
            # 先记录待发布摘要，异常重试只会处置这里已登记的文件。
            state["pending"] = release
            self._write_state(state)
            try:
                self._verified_apk(release).close()
            except FileNotFoundError:
                _atomic_write(self._apk(release), body)
                self._verified_apk(release).close()
            state = {"current": release, "previous": state["current"], "pending": None,
                     "retired": state["previous"]}
            self._write_state(state)
            if state["retired"]:
                try:
                    self._discard(state["retired"])
                    state["retired"] = None
                    self._write_state(state)
                except (OSError, TvUpdateError):
                    return "published_cleanup_pending"
            return "published"
