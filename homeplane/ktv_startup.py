"""launchd-owned KTV sockets: fixed listener activation, no remotely supplied commands."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager, redirect_stderr
from contextvars import ContextVar
import argparse
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import tempfile
import threading
import time


def activate(host, port, *, library=None):
    """Adopt only the exact LAN/loopback listeners declared by our launch agent."""
    lib = library or ctypes.CDLL(None)
    method = lib.launch_activate_socket
    method.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)), ctypes.POINTER(ctypes.c_size_t)]
    method.restype = ctypes.c_int
    lib.free.argtypes = [ctypes.c_void_p]
    listeners = []
    try:
        for name in (b"KTVLAN", b"KTVLocal"):
            descriptors = ctypes.POINTER(ctypes.c_int)()
            count = ctypes.c_size_t()
            error = method(name, ctypes.byref(descriptors), ctypes.byref(count))
            if error:
                raise ValueError("launchd_socket_unavailable")
            try:
                for i in range(count.value):
                    listener = socket.socket(fileno=descriptors[i])
                    listeners.append(listener)
            finally:
                lib.free(descriptors)
        expected = {(host, port), ("127.0.0.1", port)}
        actual = {s.getsockname() for s in listeners}
        if (len(listeners) != len(expected) or actual != expected
                or any(s.family != socket.AF_INET or s.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM for s in listeners)):
            raise ValueError("launchd_socket_mismatch")
        return listeners
    except Exception:
        for listener in listeners:
            listener.close()
        raise


def definition(root, host, port=8769):
    root = root.absolute()
    arguments = [str(root/".venv/bin/python"), str(root/"scripts/ktv_server.py"), "serve", "--host", host,
                 "--port", str(port), "--allow-download", "--public-config", str(root/"data/ktv/nas.json"), "--launchd-sockets"]
    return {"Label": "local.friday.ktv", "ProgramArguments": arguments, "WorkingDirectory": str(root),
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 3, "ExitTimeOut": 30,
        "ProcessType": "Background", "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONDONTWRITEBYTECODE": "1"},
        "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
        "Sockets": {name: {"SockFamily": "IPv4", "SockType": "stream", "SockNodeName": address,
                          "SockServiceName": str(port), "SockPassive": True} for name, address in
                    (("KTVLAN", host), ("KTVLocal", "127.0.0.1"))}}


class StartupBudgetError(ValueError):
    """Fixed category only; never include configuration or filesystem details."""


def _owned_private(path, *, limit=65536):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        value = os.fstat(descriptor)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                or value.st_mode & 0o077 or value.st_nlink != 1 or value.st_size > limit):
            raise StartupBudgetError("startup_budget_unavailable")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(limit + 1)
            if len(content) > limit:
                raise StartupBudgetError("startup_budget_unavailable")
            return content
    finally:
        os.close(descriptor)


class StartupBudget:
    """Five startup admissions per ten minutes, durable across process crashes."""
    DELAYS = (0, 1, 2, 4, 8)
    WINDOW = 600
    STABLE = 60

    def __init__(self, path, fingerprint, *, clock=time.time):
        self.path = Path(path).absolute()
        self.fingerprint, self.clock = fingerprint, clock
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise StartupBudgetError("startup_budget_unavailable")

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**12:
            raise StartupBudgetError("startup_budget_unavailable")
        return value

    @contextmanager
    def _locked(self):
        parent = self.path.parent
        if parent.resolve() != parent or self.path.name in {"", ".", ".."}:
            raise StartupBudgetError("startup_budget_unavailable")
        value = parent.stat()
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or value.st_mode & 0o077:
            raise StartupBudgetError("startup_budget_unavailable")
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            value = os.fstat(descriptor)
            if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                    or value.st_mode & 0o077 or value.st_nlink != 1):
                raise StartupBudgetError("startup_budget_unavailable")
            # Contention is a bounded, fail-closed admission failure, never a
            # blocking startup waiting indefinitely for another old process.
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)

    def _new(self, now):
        return {"schema": 1, "fingerprint": self.fingerprint, "window_started": now,
                "attempts": 0, "attempt_id": secrets.token_hex(16), "started_at": 0, "stable": False}

    def _read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        try:
            value = json.loads(_owned_private(self.path))
        except (ValueError, UnicodeError):
            raise StartupBudgetError("startup_budget_unavailable") from None
        keys = {"schema", "fingerprint", "window_started", "attempts", "attempt_id", "started_at", "stable"}
        if (not isinstance(value, dict) or set(value) != keys or type(value["schema"]) is not int or value["schema"] != 1
                or not isinstance(value["fingerprint"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["fingerprint"])
                or type(value["attempts"]) is not int or not 0 <= value["attempts"] <= len(self.DELAYS)
                or not isinstance(value["attempt_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["attempt_id"])
                or type(value["stable"]) is not bool
                or any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) or not 0 <= value[k] <= 10**12
                       for k in ("window_started", "started_at"))):
            raise StartupBudgetError("startup_budget_unavailable")
        return value

    def _write(self, value):
        if self.path.exists() or self.path.is_symlink():
            _owned_private(self.path)
        descriptor, temporary = tempfile.mkstemp(prefix=".ktv-startup-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as target:
                os.fchmod(target.fileno(), 0o600)
                json.dump(value, target, sort_keys=True, allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def reserve(self):
        with self._locked():
            now, value = self._now(), self._read()
            if value is None or value["fingerprint"] != self.fingerprint or now >= value["window_started"] + self.WINDOW:
                value = self._new(now)
            if now < value["window_started"]:
                raise StartupBudgetError("startup_budget_unavailable")
            if value["attempts"] >= len(self.DELAYS):
                return {"allowed": False, "code": "startup_budget_exhausted", "attempt": value["attempts"]}
            value.update(attempts=value["attempts"] + 1, attempt_id=secrets.token_hex(16), started_at=now, stable=False)
            self._write(value)
            return {"allowed": True, "code": "startup_admitted", "attempt": value["attempts"],
                    "delay": self.DELAYS[value["attempts"] - 1], "attempt_id": value["attempt_id"]}

    def reset(self):
        with self._locked():
            self._write(self._new(self._now()))

    def mark_stable(self, attempt_id):
        with self._locked():
            value, now = self._read(), self._now()
            if (value is None or value["fingerprint"] != self.fingerprint or value["attempt_id"] != attempt_id
                    or value["attempts"] == 0 or now - value["started_at"] < self.STABLE):
                return False
            value = self._new(now)
            value["stable"] = True
            self._write(value)
            return True


_session = ContextVar("ktv_startup_session", default=None)


class _StartupSession:
    def __init__(self, budget, attempt, *, timer_factory, emit):
        self.budget, self.attempt, self.timer_factory, self.emit = budget, attempt, timer_factory, emit
        self.timer = None
        self.storage_failed = False
        self.closed = False

    def ready(self, stop):
        if self.timer is not None:
            return
        ready_at = self.budget._now()
        def stable():
            if self.closed:
                return
            try:
                if self.budget._now() - ready_at < StartupBudget.STABLE:
                    return
                self.budget.mark_stable(self.attempt["attempt_id"])
            except Exception:
                self.storage_failed = True
                self.emit({"status": "startup_blocked", "code": "startup_budget_unavailable"})
                if stop is not None:
                    try:
                        stop()
                    except Exception:
                        self.emit({"status": "startup_blocked", "code": "startup_stop_unavailable"})
        self.timer = self.timer_factory(StartupBudget.STABLE, stable)
        self.timer.daemon = True
        self.timer.start()

    def close(self):
        self.closed = True
        if self.timer is not None:
            self.timer.cancel()


def runtime_ready(stop=None):
    """Arm stability only after the HTTP server's real startup has completed."""
    current = _session.get()
    if current is not None:
        current.ready(stop)


class _EntryParser(argparse.ArgumentParser):
    def error(self, message):
        raise StartupBudgetError("startup_budget_unavailable")


def _entry(argv):
    parser = _EntryParser(add_help=False, allow_abbrev=False)
    parser.add_argument("action", nargs="?")
    for option, default in (("config", "data/ktv/config.json"), ("public-config", None), ("host", "127.0.0.1"), ("port", "8769"), ("download-dir", None)):
        parser.add_argument("--" + option, default=default)
    for option in ("launchd-sockets", "allow-download", "reset-startup-budget"):
        parser.add_argument("--" + option, action="store_true")
    return parser.parse_known_args(argv)[0]


def startup_fingerprint(config, public_config, *, root=None, code_paths=None):
    """Hash private bytes and fixed local source; never persist paths or values."""
    root = Path(root or Path(__file__).resolve().parents[1])
    files = [Path(config).absolute()]
    if public_config is not None:
        files.append(Path(public_config).absolute())
    digest = hashlib.sha256()
    for path in files:
        if path.resolve() != path:
            raise StartupBudgetError("startup_budget_unavailable")
        digest.update(hashlib.sha256(_owned_private(path)).digest())
    sources = code_paths if code_paths is not None else [root / name for name in (
        "scripts/ktv_server.py", "homeplane/ktv_startup.py", "homeplane/ktv_worker.py",
        "homeplane/ktv_service.py", "homeplane/ktv_source_api.py", "uv.lock")]
    for path in sources:
        path = Path(path).absolute()
        if path.resolve() != path or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise StartupBudgetError("startup_budget_unavailable")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def drain_pending_listeners(host, port, *, activate_fn=None, limit=512):
    """Drop only queued connections on our exact inherited listeners; read none."""
    address = ipaddress.ip_address(host)
    private = address.version == 4 and (address.is_loopback or any(address in ipaddress.ip_network(value)
              for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
    if not private or type(port) is not int or not 1024 <= port <= 65535 or type(limit) is not int or not 1 <= limit <= 512:
        raise StartupBudgetError("startup_listener_unavailable")
    listeners, dropped = (activate_fn or activate)(host, port), 0
    try:
        for listener in listeners:
            listener.setblocking(False)
        while dropped < limit:
            progress = False
            for listener in listeners:
                if dropped >= limit:
                    break
                try:
                    connection, _ = listener.accept()
                except (BlockingIOError, InterruptedError):
                    continue
                except ConnectionAbortedError:
                    dropped += 1
                    progress = True
                    continue
                try:
                    try:
                        connection.close()
                    except OSError:
                        pass
                finally:
                    dropped += 1
                    progress = True
            if not progress:
                break
        return dropped
    finally:
        for listener in listeners:
            listener.close()


def guarded_run(main, argv, *, clock=time.time, sleep=time.sleep, timer_factory=threading.Timer,
                emit=None, fingerprint_fn=startup_fingerprint, drain_fn=drain_pending_listeners):
    """Keep launchd's crash retry finite, including failure before server setup."""
    emit = emit or (lambda value: print(json.dumps(value)))
    args = None
    def blocked(code):
        if args is not None and args.launchd_sockets:
            try:
                drain_fn(args.host, int(args.port))
            except Exception:
                emit({"status": "startup_blocked", "code": "startup_listener_unavailable"})
        emit({"status": "startup_blocked", "code": code})
        return 0  # SuccessfulExit=false must not create another crash retry.
    try:
        args = _entry(list(argv))
    except StartupBudgetError:
        return blocked("startup_budget_unavailable")
    if any(option in argv for option in ("--help", "-h")) or (args.action != "serve" and not args.reset_startup_budget):
        return main(list(argv))
    try:
        anchor = Path(args.public_config or args.config).absolute()
        fingerprint = fingerprint_fn(args.config, args.public_config)
        budget = StartupBudget(anchor.with_name("startup-budget.json"), fingerprint, clock=clock)
        if args.reset_startup_budget:
            budget.reset()
            emit({"status": "startup_budget_reset", "code": "explicit_local_reset"})
            return 0
        attempt = budget.reserve()
    except Exception:
        return blocked("startup_budget_unavailable")
    if not attempt["allowed"]:
        return blocked(attempt["code"])
    session = _StartupSession(budget, attempt, timer_factory=timer_factory, emit=emit)
    token = _session.set(session)
    try:
        sleep(attempt["delay"])
        # The ordinary launch agent already discards stderr; the CLI serve
        # guard also emits only fixed failure categories, never raw exceptions.
        with open(os.devnull, "w") as sink, redirect_stderr(sink):
            result = main(list(argv))
        if result is not None and type(result) is not int:
            emit({"status": "startup_failed", "code": "service_startup_failed"})
            return 0 if session.storage_failed else 1
        return 0 if session.storage_failed else result or 0
    except KeyboardInterrupt:
        return 0
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code in (None, 0):
            return 0
        emit({"status": "startup_failed", "code": "service_startup_failed"})
        return 0 if session.storage_failed else 1
    finally:
        session.close()
        _session.reset(token)
