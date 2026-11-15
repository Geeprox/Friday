"""Mac 以私有文件投递已验签的电视安装包，NAS 用本机权限登记发布。"""

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict

try:
    from integrations.ktv_public.tv_updates import (
        MAX_APK_BYTES, SHA256, Release, TvUpdateError, _atomic_write, _fingerprint, _fsync_directory,
        _private_regular_file,
    )
except ImportError:
    from tv_updates import (
        MAX_APK_BYTES, SHA256, Release, TvUpdateError, _atomic_write, _fingerprint, _fsync_directory,
        _private_regular_file,
    )


INBOX_NAME = "tv-update-inbox"
REQUEST_LIFETIME = 15 * 60
HEADER_LIMIT = 2048
REQUEST_ID = re.compile(r"[a-f0-9]{32}\Z")
PROOF_PREFIX = b"friday-ktv-tv-update-v1\n"
REQUEST_FIELDS = {"request_id", "issued_at", "release"}
COMMITTED = {"published", "published_cleanup_pending", "already_current"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _proof(key, kind, payload):
    return hmac.new(key, PROOF_PREFIX + kind + b"\n" + _json(payload), hashlib.sha256).hexdigest()


def _envelope(key, kind, payload):
    return _json({"payload": payload, "proof": _proof(key, kind, payload)}) + b"\n"


def _verified(key, kind, raw):
    try:
        if len(raw) > HEADER_LIMIT or not raw.endswith(b"\n"):
            raise ValueError
        record = json.loads(raw)
        if not isinstance(record, dict) or set(record) != {"payload", "proof"}:
            raise ValueError
        payload, proof = record["payload"], record["proof"]
        if (not isinstance(payload, dict) or not isinstance(proof, str) or not SHA256.fullmatch(proof)
                or not hmac.compare_digest(_proof(key, kind, payload), proof)):
            raise ValueError
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise TvUpdateError("tv_release_inbox_authentication_failed") from None
    return payload


def _open_linked_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        item = os.fstat(descriptor)
        if not stat.S_ISREG(item.st_mode) or item.st_uid != os.geteuid() or item.st_nlink != 1:
            raise TvUpdateError("tv_release_inbox_owned_file_required")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


class TvUpdateInbox:
    def __init__(self, store, agent_key):
        if not isinstance(agent_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,100}", agent_key):
            raise TvUpdateError("tv_release_inbox_requires_worker_key")
        self.key = agent_key.encode("ascii")
        self.store = store
        self.directory = store.state_dir / INBOX_NAME
        self.request = self.directory / "request.bin"
        self.ack = self.directory / "receipt.json"
        self.lock = self.directory / ".accept.lock"
        self.stage_file = self.directory / ".incoming.part"

    def _check(self):
        if self.directory.resolve() != self.directory:
            raise TvUpdateError("tv_release_inbox_private_directory_required")
        item = self.directory.stat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.geteuid() or item.st_mode & 0o077:
            raise TvUpdateError("tv_release_inbox_private_directory_required")

    def initialize(self):
        """NAS 进程创建入口及锁，Mac 只写入已建好的目录。"""
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            _fsync_directory(self.store.state_dir)
        self._check()
        os.close(_private_regular_file(self.lock, os.O_CREAT | os.O_RDWR))

    @contextmanager
    def _locked(self):
        self._check()
        with os.fdopen(_private_regular_file(self.lock, os.O_RDWR), "r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def _read_request(self, stream):
        info = os.fstat(stream.fileno())
        if not 0 < info.st_size <= MAX_APK_BYTES + HEADER_LIMIT:
            raise TvUpdateError("tv_release_inbox_invalid_size")
        raw = stream.readline(HEADER_LIMIT + 1)
        payload = _verified(self.key, b"request", raw)
        try:
            key = payload["request_id"]
            at = payload["issued_at"]
            if (set(payload) != REQUEST_FIELDS or not isinstance(key, str) or not REQUEST_ID.fullmatch(key)
                    or type(at) is not int or at <= 0):
                raise ValueError
            release = Release(**payload["release"])
        except (KeyError, TypeError, ValueError):
            raise TvUpdateError("tv_release_inbox_invalid_request") from None
        if info.st_size != len(raw) + release.bytes:
            raise TvUpdateError("tv_release_inbox_invalid_size")
        return key, at, release

    def stage(self, release, apk):
        if len(apk) != release.bytes or hashlib.sha256(apk).hexdigest() != release.sha256:
            raise TvUpdateError("tv_release_bytes_mismatch")
        with self._locked():
            try:
                with _open_linked_file(self.request) as source:
                    existing, _, candidate = self._read_request(source)
            except FileNotFoundError:
                pass
            else:
                if candidate == release:
                    return existing
                raise TvUpdateError("tv_release_inbox_busy")
            request_id = uuid.uuid4().hex
            header = _envelope(self.key, b"request", {
                "request_id": request_id, "issued_at": int(time.time()), "release": asdict(release),
            })
            _atomic_write(self.request, header, apk, part_name=self.stage_file.name)
            return request_id

    @staticmethod
    def _unlink_exact(path, original):
        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        if _fingerprint(current) == _fingerprint(original):
            path.unlink()
            _fsync_directory(path.parent)

    def _reap_uncommitted(self, now):
        for path in (self.stage_file, self.directory / ".receipt.part"):
            try:
                item = path.lstat()
            except FileNotFoundError:
                continue
            # 只回收本工序、同一所有者、锁内确认超时的未提交文件。
            if (stat.S_ISREG(item.st_mode) and item.st_uid == os.geteuid() and item.st_nlink == 1
                    and now - item.st_mtime > REQUEST_LIFETIME):
                self._unlink_exact(path, item)

    def consume_once(self):
        now = int(time.time())
        with self._locked():
            self._reap_uncommitted(now)
            try:
                stream = _open_linked_file(self.request)
            except FileNotFoundError:
                return None
            with stream:
                before = os.fstat(stream.fileno())
                try:
                    request_id, issued, release = self._read_request(stream)
                except TvUpdateError:
                    self._unlink_exact(self.request, before)
                    return None
                result = {"request_id": request_id, **release.public()}
                if issued > now + 60 or now - issued >= REQUEST_LIFETIME:
                    result.update(state="refused", code="tv_release_inbox_expired")
                else:
                    body = stream.read(release.bytes + 1)
                    try:
                        result["state"] = self.store.publish(release, body)
                    except TvUpdateError as error:
                        result.update(state="refused", code=str(error))
                _atomic_write(self.ack, _envelope(self.key, b"receipt", result), part_name=".receipt.part")
                self._unlink_exact(self.request, before)
                return result

    def wait_for_receipt(self, request_id, seconds=20):
        deadline = time.monotonic() + seconds
        while True:
            try:
                with os.fdopen(_private_regular_file(self.ack), "rb") as stream:
                    result = _verified(self.key, b"receipt", stream.read(HEADER_LIMIT + 1))
                if result.get("request_id") == request_id:
                    return result
            except (OSError, TvUpdateError):
                pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.4)

    def run(self, stop):
        while not stop.is_set():
            try:
                self.initialize()
                self.consume_once()
            except (OSError, TvUpdateError):
                # 只重试仍有请求的私有入口，电视与房间线程照常服务。
                pass
            stop.wait(4)
