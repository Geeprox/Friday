"""Synthetic endpoints only. These tests never use household credentials or NAS files."""

import asyncio
import base64
import hashlib
import io
import json
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from homeplane.nas_connection import (
    NASConnectionConfig,
    NASConnectionError,
    NASConnectionManager,
    NASCredentials,
)
from homeplane.nas_connection.adapters import Reader, SFTPReader, SMBReader, signature
from homeplane.nas_connection.contracts import relative_path
from homeplane.nas_connection.transport import WorkerTransport
from homeplane.nas_connection.worker import error_code

SOURCE = "SYNTHETIC-source"
SECRET = "SYNTHETIC-password-do-not-log"
CREDS = NASCredentials("SYNTHETIC-user", SECRET)
DATA = b"SYNTHETIC-payload"
RECORD = {"path": "photo.jpg", "kind": "file", "size": len(DATA), "modified_ns": 10,
          "revision": signature("SYNTHETIC-version")}


def config(**changes):
    return NASConnectionConfig(source_ref=SOURCE, transport="smb", host="synthetic.invalid",
                               share="SYNTHETIC-share", credential_ref="SYNTHETIC-ref",
                               health_interval_seconds=10, backoff_initial_seconds=0.05,
                               backoff_max_seconds=0.1, **changes)


async def wait_for(manager, state="online"):
    try:
        async with asyncio.timeout(3):
            while manager.status().state != state:
                await asyncio.sleep(0.005)
    except TimeoutError:
        pytest.fail(f"Expected {state}; received {manager.status().model_dump_json()}")


class FakeTransport:
    def __init__(self, *, connect_error=None, hang_connect=False, hang_read=False):
        self.connect_error, self.hang_connect, self.hang_read = connect_error, hang_connect, hang_read
        self.closed = False
        self.calls = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.record = RECORD.copy()

    async def connect(self, cfg, credentials):
        self.calls.append("connect")
        self.entered.set()
        if self.hang_connect:
            await self.release.wait()
        if self.connect_error:
            raise self.connect_error
        assert credentials == CREDS
        return {"reachable": True, "authenticated": True, "readable": True}

    async def request(self, op, payload):
        self.calls.append(op)
        if op == "list":
            return {"entries": [self.record.copy()], "cursor": None, "complete": True}
        if op == "stat":
            if self.record is None:
                raise NASConnectionError("nas_missing")
            return self.record.copy()
        if op == "read":
            self.entered.set()
            if self.hang_read:
                await self.release.wait()
            if payload["revision"] != self.record["revision"]:
                raise NASConnectionError("nas_revision_changed")
            data = DATA[payload["offset"]:payload["offset"] + payload["length"]]
            return {"data": base64.b64encode(data).decode()}
        if op == "health":
            return {"reachable": True, "authenticated": True, "readable": True}
        raise AssertionError(op)

    async def close(self):
        self.closed = True
        self.release.set()


async def setup_manager(tmp_path, *, cfg=None, factory=FakeTransport):
    calls = []

    async def resolve(reference):
        calls.append(reference)
        return CREDS

    manager = NASConnectionManager(tmp_path / "state.json", credential_resolver=resolve,
                                   transport_factory=factory)
    await manager.configure(cfg or config(enabled=True), expected_generation=0)
    return manager, calls


async def test_default_disabled_no_constructor_or_get_network_and_restart(tmp_path):
    manager, calls = await setup_manager(tmp_path, cfg=config())
    assert manager.status().state == "disabled" and not calls
    assert manager.status().reachable is None
    await manager.start()
    assert not calls
    with pytest.raises(NASConnectionError, match="nas_disabled"):
        await manager.list_page(SOURCE)
    await manager.close()
    reopened = NASConnectionManager(tmp_path / "state.json", credential_resolver=lambda _: None)
    await reopened.start()
    assert reopened.status().state == "disabled"
    assert reopened.status().config_generation == 1
    await reopened.close()


async def test_path_free_reader_generation_and_reference_only_persistence(tmp_path):
    manager, calls = await setup_manager(tmp_path)
    try:
        await wait_for(manager)
        status = manager.status()
        assert status.configured and status.reachable and status.authenticated and status.readable
        page = await manager.list_page(SOURCE)
        assert page.enumeration_complete and page.completed_scan_token and not page.snapshot_consistent
        entry = page.entries[0]
        assert entry.media_kind == "image" and entry.closed is None and entry.revision_basis == "metadata"
        assert await manager.stat(SOURCE, entry.object_ref, entry.revision) == entry
        assert await manager.read_range(SOURCE, entry.object_ref, entry.revision, 3, 5) == DATA[3:8]
        assert await manager.read_range(SOURCE, entry.object_ref, entry.revision, len(DATA), 10) == b""
        assert calls == ["SYNTHETIC-ref"]
        public = page.model_dump_json() + status.model_dump_json()
        assert "photo.jpg" not in public and SECRET not in public and "synthetic.invalid" not in public
        saved = (tmp_path / "state.json").read_text()
        assert "SYNTHETIC-ref" in saved and SECRET not in saved and "SYNTHETIC-user" not in saved
        assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
        assert SECRET not in repr(CREDS)
    finally:
        await manager.close()


@pytest.mark.parametrize("code", ["nas_auth_failed", "nas_access_denied", "nas_host_key_mismatch",
                                  "nas_dependency_missing"])
async def test_auth_or_security_failure_pauses_durably_without_retry(tmp_path, code):
    manager, calls = await setup_manager(tmp_path, factory=lambda: FakeTransport(connect_error=NASConnectionError(code)))
    await wait_for(manager, "paused")
    await asyncio.sleep(0.12)
    assert len(calls) == 1 and manager.status().failure == code and not manager.status().readable
    assert manager.status().retry_in_seconds is None
    await manager.close()
    reopened = NASConnectionManager(tmp_path / "state.json", credential_resolver=lambda _: pytest.fail("retry"))
    await reopened.start()
    assert reopened.status().state == "paused" and reopened.status().connection_epoch is None
    await reopened.close()


async def test_explicit_resume_and_disabled_survive_restart(tmp_path):
    transports = []

    def factory():
        item = FakeTransport(connect_error=NASConnectionError("nas_auth_failed") if not transports else None)
        transports.append(item)
        return item

    manager, _ = await setup_manager(tmp_path, factory=factory)
    await wait_for(manager, "paused")
    await manager.resume(expected_generation=1)
    await wait_for(manager)
    assert manager.status().config_generation == 2
    await manager.disable(expected_generation=2)
    assert manager.status().state == "disabled" and transports[-1].closed
    await manager.close()
    reopened = NASConnectionManager(tmp_path / "state.json", credential_resolver=lambda _: pytest.fail("retry"))
    await reopened.start()
    assert reopened.status().state == "disabled" and reopened.generation == 3
    await reopened.close()


async def test_restart_enabled_reconnects_fresh_and_rejects_old_object(tmp_path):
    manager, _ = await setup_manager(tmp_path)
    await wait_for(manager)
    epoch = manager.status().connection_epoch
    entry = (await manager.list_page(SOURCE)).entries[0]
    await manager.close()

    async def resolve(_):
        return CREDS

    reopened = NASConnectionManager(tmp_path / "state.json", credential_resolver=resolve,
                                    transport_factory=FakeTransport)
    assert reopened.status().reachable is None
    await reopened.start()
    await wait_for(reopened)
    assert reopened.status().connection_epoch != epoch
    with pytest.raises(NASConnectionError, match="nas_object_unknown"):
        await reopened.stat(SOURCE, entry.object_ref)
    await reopened.close()


async def test_transport_backoff_deadline_then_recovery_without_read_replay(tmp_path):
    transports, times = [], []

    def factory():
        times.append(time.monotonic())
        item = FakeTransport(hang_connect=len(transports) == 0,
                             connect_error=OSError(SECRET) if len(transports) == 1 else None)
        transports.append(item)
        return item

    manager, calls = await setup_manager(tmp_path, cfg=config(enabled=True, connect_timeout_seconds=0.05),
                                         factory=factory)
    try:
        await wait_for(manager)
        assert len(calls) == 3 and transports[0].closed and transports[1].closed
        assert times[1] - times[0] >= 0.09 and times[2] - times[1] >= 0.09
        assert all("read" not in t.calls for t in transports)
        assert SECRET not in manager.status().model_dump_json()
    finally:
        await manager.close()


async def test_cancelled_reader_drops_epoch_reaps_and_never_replays(tmp_path):
    transports = []

    def factory():
        item = FakeTransport(hang_read=not transports)
        transports.append(item)
        return item

    manager, _ = await setup_manager(tmp_path, factory=factory)
    await wait_for(manager)
    entry = (await manager.list_page(SOURCE)).entries[0]
    epoch = manager.status().connection_epoch
    task = asyncio.create_task(manager.read_range(SOURCE, entry.object_ref, entry.revision, 0, 10))
    while "read" not in transports[0].calls:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transports[0].closed
    await wait_for(manager)
    assert manager.status().connection_epoch != epoch
    assert transports[1].calls == ["connect"]
    await manager.close()


async def test_read_deadline_is_bounded_and_busy_requests_do_not_queue(tmp_path):
    transport = FakeTransport(hang_read=True)
    manager, _ = await setup_manager(tmp_path, cfg=config(enabled=True, operation_timeout_seconds=0.05),
                                     factory=lambda: transport)
    await wait_for(manager)
    entry = (await manager.list_page(SOURCE)).entries[0]
    task = asyncio.create_task(manager.read_range(SOURCE, entry.object_ref, entry.revision, 0, 10))
    while "read" not in transport.calls:
        await asyncio.sleep(0.005)
    with pytest.raises(NASConnectionError, match="nas_busy"):
        await manager.list_page(SOURCE)
    with pytest.raises(NASConnectionError, match="nas_timeout"):
        await task
    assert transport.closed and transport.calls.count("read") == 1
    await manager.close()


async def test_config_change_discards_late_response_and_stops_connect(tmp_path):
    transport = FakeTransport(hang_read=True)
    manager, _ = await setup_manager(tmp_path, factory=lambda: transport)
    await wait_for(manager)
    entry = (await manager.list_page(SOURCE)).entries[0]
    task = asyncio.create_task(manager.read_range(SOURCE, entry.object_ref, entry.revision, 0, 10))
    while "read" not in transport.calls:
        await asyncio.sleep(0.005)
    await manager.configure(config(), expected_generation=1)
    with pytest.raises(NASConnectionError, match="nas_source_changed"):
        await task
    assert manager.status().state == "disabled" and manager.status().connection_epoch is None
    await manager.close()
    stalled = FakeTransport(hang_connect=True)
    manager, _ = await setup_manager(tmp_path / "stalled", factory=lambda: stalled)
    await stalled.entered.wait()
    await manager.disable(expected_generation=1)
    assert stalled.closed and manager.status().state == "disabled"
    await manager.close()


async def test_missing_is_explicit_stat_only_and_revision_mismatch_is_nonfatal(tmp_path):
    transport = FakeTransport()
    manager, _ = await setup_manager(tmp_path, factory=lambda: transport)
    await wait_for(manager)
    entry = (await manager.list_page(SOURCE)).entries[0]
    with pytest.raises(NASConnectionError, match="nas_revision_changed"):
        await manager.stat(SOURCE, entry.object_ref, "0" * 64)
    transport.record = None
    with pytest.raises(NASConnectionError, match="nas_missing"):
        await manager.stat(SOURCE, entry.object_ref)
    assert manager.status().state == "online"
    await manager.close()


async def test_page_cursor_checkpoint_is_one_use_directory_and_epoch_fenced(tmp_path):
    class Pages(FakeTransport):
        async def request(self, op, payload):
            if op == "list":
                return {"entries": [RECORD], "cursor": "private-worker-token", "complete": False}
            return await super().request(op, payload)

    manager, _ = await setup_manager(tmp_path, factory=Pages)
    await wait_for(manager)
    page = await manager.list_page(SOURCE)
    assert not page.enumeration_complete and page.completed_scan_token is None
    await manager.list_page(SOURCE, page.next_cursor)
    with pytest.raises(NASConnectionError, match="nas_cursor_expired"):
        await manager.list_page(SOURCE, page.next_cursor)
    page = await manager.list_page(SOURCE)
    await manager.configure(config(enabled=True), expected_generation=1)
    await wait_for(manager)
    with pytest.raises(NASConnectionError, match="nas_cursor_expired"):
        await manager.list_page(SOURCE, page.next_cursor)
    await manager.close()


async def test_owner_lock_and_generation_cas_prevent_parallel_configuration(tmp_path):
    manager, _ = await setup_manager(tmp_path, cfg=config())
    other = NASConnectionManager(tmp_path / "state.json", credential_resolver=lambda _: None)
    with pytest.raises(NASConnectionError, match="nas_busy"):
        await other.configure(config(), expected_generation=1)
    with pytest.raises(NASConnectionError, match="nas_source_changed"):
        await manager.configure(config(), expected_generation=0)
    await manager.close()
    await other.configure(config(), expected_generation=1)
    assert other.generation == 2
    await other.close()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a//b", "a/./b", "a/../b", "a\\b",
                                 "a:b", "%2e%2e", "a\n", "", "x" * 256])
def test_paths_reject_traversal_ambiguous_encoded_and_stream_names(path):
    with pytest.raises(NASConnectionError, match="nas_invalid_request"):
        relative_path(path)


@pytest.mark.parametrize("changes", [{"host": "smb://host/share"}, {"host": "u@host"},
                                     {"host": "host:445"}, {"share": "a/b"}, {"root": "../x"},
                                     {"credential_ref": "secret://password"}])
def test_config_does_not_accept_urls_credentials_or_unsafe_paths(changes):
    values = config().model_dump()
    values.update(changes)
    with pytest.raises(ValueError):
        NASConnectionConfig.model_validate(values)


def test_state_rejects_symlinks_invalid_version_and_noncanonical_generation(tmp_path):
    target = tmp_path / "target"
    target.write_text("{}")
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(NASConnectionError):
        NASConnectionManager(tmp_path / "link", credential_resolver=lambda _: None)
    with pytest.raises(NASConnectionError):
        NASConnectionManager(target, credential_resolver=lambda _: None)
    target.write_text(json.dumps({"schema_version": 1, "config": config().model_dump(),
                                  "config_generation": True, "paused_reason": None}))
    with pytest.raises(NASConnectionError):
        NASConnectionManager(target, credential_resolver=lambda _: None)


async def test_bounds_and_wrong_source_ref_never_reach_transport(tmp_path):
    transport = FakeTransport()
    manager, _ = await setup_manager(tmp_path, factory=lambda: transport)
    await wait_for(manager)
    with pytest.raises(NASConnectionError, match="nas_source_changed"):
        await manager.list_page("wrong")
    for count in [0, 129, True]:
        with pytest.raises(NASConnectionError, match="nas_invalid_request"):
            await manager.list_page(SOURCE, limit=count)
    entry = (await manager.list_page(SOURCE)).entries[0]
    for offset, length in [(-1, 10), (0, 1048577), (True, 10), (0, 0)]:
        with pytest.raises(NASConnectionError, match="nas_invalid_request"):
            await manager.read_range(SOURCE, entry.object_ref, entry.revision, offset, length)
    assert "read" not in transport.calls
    await manager.close()


async def test_stale_health_stops_cached_green_without_get_reconnect(tmp_path):
    manager, calls = await setup_manager(tmp_path)
    await wait_for(manager)
    manager._checked_mono -= 20
    assert manager.status().state == "backoff" and manager.status().reachable is None
    assert not manager.status().readable
    with pytest.raises(NASConnectionError, match="nas_unavailable"):
        await manager.list_page(SOURCE)
    assert len(calls) == 1
    await manager._request("health", {})
    assert manager.status().state == "online"
    await manager.close()


def test_reader_cursor_budget_and_false_snapshot_semantics():
    class Endpoint(Reader):
        def iter_entries(self, path):
            for _ in range(5):
                yield RECORD.copy()

    reader = Endpoint()
    page = reader.list_page("", None, 2)
    assert not page["complete"] and len(page["entries"]) == 2
    second = reader.list_page("", page["cursor"], 2)
    third = reader.list_page("", second["cursor"], 2)
    assert third["complete"] and len(third["entries"]) == 1
    with pytest.raises(NASConnectionError, match="nas_cursor_expired"):
        reader.list_page("", page["cursor"], 2)
    reader.close_cursors()


def test_protocol_error_sanitization_does_not_use_exception_text():
    assert error_code(RuntimeError(SECRET)) == "nas_transport_failed"
    assert error_code(FileNotFoundError(SECRET)) == "nas_missing"
    assert error_code(PermissionError(SECRET)) == "nas_access_denied"
    assert NASConnectionError(SECRET).code == "nas_transport_failed"
    for status_code, expected in [(0xC000006D, "nas_auth_failed"), (0xC0000034, "nas_missing")]:
        assert error_code(SimpleNamespace(status=status_code)) == expected


def test_worker_protocol_fixed_ops_bounded_frames_and_redacted_credentials(monkeypatch):
    from homeplane.nas_connection import worker

    class Endpoint(Reader):
        def __init__(self, cfg, credentials):
            super().__init__()
            assert credentials == CREDS

        def health(self):
            return {"reachable": True, "authenticated": True, "readable": True}

        def close(self):
            pass

    messages = [
        {"id": 1, "op": "connect", "payload": {"config": config().model_dump(), "credentials": CREDS.payload()}},
        {"id": 2, "op": "exec", "payload": {"command": SECRET}},
        {"id": 3, "op": "health", "payload": {}},
    ]
    incoming = io.TextIOWrapper(io.BytesIO(b"".join(json.dumps(x).encode() + b"\n" for x in messages)))
    output_bytes = io.BytesIO()
    outgoing = io.TextIOWrapper(output_bytes)
    monkeypatch.setattr(sys, "stdin", incoming)
    monkeypatch.setattr(sys, "stdout", outgoing)
    worker.main(factory=Endpoint)
    text = output_bytes.getvalue().decode()
    replies = [json.loads(x) for x in text.splitlines()]
    assert replies[1]["error"] == "nas_invalid_request" and replies[2]["result"]["readable"]
    assert SECRET not in text and "SYNTHETIC-user" not in text


async def test_actual_worker_dependency_gate_or_fake_unreachable_endpoint_does_not_log_secret(monkeypatch):
    # The reserved .invalid hostname cannot be a household NAS. If optional dependencies
    # are absent it fails before network; if present it fails name resolution.
    transport = WorkerTransport()
    monkeypatch.setenv("HOME_HA_TOKEN", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    try:
        with pytest.raises(NASConnectionError) as caught:
            await asyncio.wait_for(transport.connect(config(), CREDS), 2)
        assert caught.value.code in {"nas_dependency_missing", "nas_transport_failed"}
    finally:
        process = transport.process
        await transport.close()
        assert transport.process is None
        assert process is None or process.returncode is not None


@pytest.mark.parametrize("cancel", [False, True])
async def test_hung_native_worker_is_killed_and_reaped_on_deadline_or_cancel(tmp_path, cancel):
    processes = []

    class HungWorker(WorkerTransport):
        async def connect(self, cfg, credentials):
            code = ("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    "print('ready',flush=True); time.sleep(100)")
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B", "-c", code, env={}, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            processes.append(self.process)
            await self.process.stdout.readline()
            return {"reachable": True, "authenticated": True, "readable": True}

        async def request(self, operation, payload):
            if operation == "list":
                return {"entries": [RECORD], "cursor": None, "complete": True}
            return await super().request(operation, payload)

    manager, _ = await setup_manager(tmp_path, cfg=config(enabled=True, operation_timeout_seconds=0.05),
                                     factory=HungWorker)
    await wait_for(manager)
    entry = (await manager.list_page(SOURCE)).entries[0]
    task = asyncio.create_task(manager.read_range(SOURCE, entry.object_ref, entry.revision, 0, 10))
    if cancel:
        await asyncio.sleep(0.01)
        task.cancel()
    began = time.monotonic()
    with pytest.raises(asyncio.CancelledError if cancel else NASConnectionError):
        await task
    assert time.monotonic() - began < 1.5
    assert processes[0].returncode is not None
    await manager.disable(expected_generation=1)
    await manager.close()
    assert all(p.returncode is not None for p in processes)


@pytest.fixture
def sftp_endpoint(tmp_path):
    """Real encrypted Paramiko SFTP on loopback serving only synthetic temp bytes."""
    paramiko = pytest.importorskip("paramiko")
    root = tmp_path / "SYNTHETIC-server"
    root.mkdir()
    (root / "photo.jpg").write_bytes(DATA)
    host_key = paramiko.RSAKey.generate(2048)

    class Account(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return (paramiko.AUTH_SUCCESSFUL if username == CREDS.username and password == CREDS.password
                    else paramiko.AUTH_FAILED)

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    class Files(paramiko.SFTPServerInterface):
        def local(self, path):
            parts = Path(path).parts[1:]
            if any(x in {"..", "."} for x in parts):
                raise PermissionError()
            return root.joinpath(*parts)

        def list_folder(self, path):
            try:
                entries = []
                for child in self.local(path).iterdir():
                    info = paramiko.SFTPAttributes.from_stat(child.lstat())
                    info.filename = child.name
                    entries.append(info)
                return entries
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(self.local(path).stat())
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def lstat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(self.local(path).lstat())
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def canonicalize(self, path):
            return os.path.normpath(path)

        def open(self, path, flags, attr):
            if flags != os.O_RDONLY:
                return paramiko.SFTP_PERMISSION_DENIED
            try:
                stream = self.local(path).open("rb")
                handle = paramiko.SFTPHandle(flags)
                handle.readfile = stream
                handle.stat = lambda: paramiko.SFTPAttributes.from_stat(os.fstat(stream.fileno()))
                return handle
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.1)
    stopped, sessions = threading.Event(), []
    errors = []

    def serve():
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            endpoint = paramiko.Transport(connection)
            sessions.append(endpoint)
            endpoint.add_server_key(host_key)
            endpoint.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
            try:
                endpoint.start_server(server=Account())
            except (paramiko.SSHException, EOFError, ConnectionResetError):
                endpoint.close()
            except Exception as exc:
                errors.append(type(exc).__name__)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(host_key.asbytes()).digest()).decode().rstrip("=")
    cfg = NASConnectionConfig(source_ref=SOURCE, transport="sftp", host="127.0.0.1", port=listener.getsockname()[1],
                               root="/", host_key_sha256=fingerprint, sftp_root_confined=True,
                               credential_ref="SYNTHETIC-ref", enabled=True)
    try:
        yield cfg, root
    finally:
        stopped.set()
        listener.close()
        for endpoint in sessions:
            endpoint.close()
        thread.join(timeout=2)
        assert not thread.is_alive() and not errors


def test_real_sftp_on_synthetic_loopback_pinned_auth_read_range_missing_and_replacement(sftp_endpoint):
    cfg, root = sftp_endpoint
    reader = SFTPReader(cfg, CREDS)
    try:
        page = reader.list_page("", None, 128)
        assert page["complete"] and len(page["entries"]) == 1
        entry = page["entries"][0]
        assert reader.stat(entry["path"])["revision"] == entry["revision"]
        assert base64.b64decode(reader.read_range(entry["path"], entry["revision"], 2, 5)["data"]) == DATA[2:7]
        (root / "photo.jpg").write_bytes(DATA + b"CHANGED")
        with pytest.raises(NASConnectionError, match="nas_revision_changed"):
            reader.read_range(entry["path"], entry["revision"], 0, 5)
        (root / "photo.jpg").unlink()
        with pytest.raises(FileNotFoundError):
            reader.stat(entry["path"])
    finally:
        reader.close()


def test_real_sftp_wrong_pinned_key_and_authentication_pause_codes(sftp_endpoint):
    cfg, _ = sftp_endpoint
    with pytest.raises(NASConnectionError, match="nas_host_key_mismatch"):
        SFTPReader(cfg.model_copy(update={"host_key_sha256": "SHA256:" + "A" * 43}), CREDS)
    paramiko = pytest.importorskip("paramiko")
    with pytest.raises(paramiko.AuthenticationException) as caught:
        SFTPReader(cfg, NASCredentials(CREDS.username, "SYNTHETIC-wrong"))
    assert error_code(caught.value) == "nas_auth_failed"


async def test_actual_supervised_sftp_worker_reader_and_reap(sftp_endpoint, tmp_path, monkeypatch):
    executable = os.environ.get("FRIDAY_NAS_TEST_PYTHON")
    if not executable:
        pytest.skip("set FRIDAY_NAS_TEST_PYTHON to a runtime with pinned optional dependencies")
    cfg, _ = sftp_endpoint
    spawned = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        assert "-I" in args and "-B" in args and SECRET not in repr(args)
        assert "HOME_HA_TOKEN" not in kwargs["env"] and "OPENAI_API_KEY" not in kwargs["env"]
        process = await original(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("HOME_HA_TOKEN", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    manager, _ = await setup_manager(tmp_path, cfg=cfg,
                                     factory=lambda: WorkerTransport(python_executable=executable))
    try:
        await wait_for(manager)
        entry = (await manager.list_page(SOURCE)).entries[0]
        assert await manager.read_range(SOURCE, entry.object_ref, entry.revision, 0, 1024) == DATA
        assert await manager.stat(SOURCE, entry.object_ref, entry.revision) == entry
        await manager.disable(expected_generation=1)
        assert spawned and spawned[0].returncode is not None
        assert manager.status().state == "disabled"
    finally:
        await manager.close()
        assert all(process.returncode is not None for process in spawned)


def test_sftp_observed_symlink_is_rejected_and_never_opened(sftp_endpoint):
    cfg, root = sftp_endpoint
    (root / "link.jpg").symlink_to(root / "photo.jpg")
    reader = SFTPReader(cfg, CREDS)
    try:
        with pytest.raises(NASConnectionError, match="nas_access_denied"):
            reader.stat("link.jpg")
        with pytest.raises(NASConnectionError, match="nas_access_denied"):
            reader.list_page("", None, 128)
    finally:
        reader.close()


def test_real_smb_library_against_synthetic_protocol_endpoint(monkeypatch):
    pytest.importorskip("smbprotocol")
    from smbprotocol import connection, file_info, session, tree
    from smbprotocol import open as smbopen
    from smbprotocol.exceptions import NoMoreFiles

    calls = []
    records = {"": {"kind": "directory", "data": b"", "id": 1},
               "photo.jpg": {"kind": "file", "data": DATA, "id": 2}}
    now = datetime(2026, 10, 1, tzinfo=UTC)

    class Connection:
        def __init__(self, guid, host, port, require_signing):
            assert require_signing and host == "synthetic.invalid" and port == 445

        def connect(self, timeout):
            calls.append("connect")

        def disconnect(self, close):
            calls.append("disconnect")

        def send(self, request, sid, tid):
            assert sid == 1 and tid == 2
            return request

        def receive(self, request):
            path = request["file_id"].get_value().rstrip(b"\0").decode()
            record = records[path]
            cls = request["file_info_class"].get_value()
            value = {file_info.FileBasicInformation.INFO_CLASS: file_info.FileBasicInformation,
                     file_info.FileInternalInformation.INFO_CLASS: file_info.FileInternalInformation,
                     file_info.FileStandardInformation.INFO_CLASS: file_info.FileStandardInformation}[cls]()
            if isinstance(value, file_info.FileBasicInformation):
                from smbprotocol.structure import DateTimeField

                filetime = DateTimeField.EPOCH_FILETIME + int(now.timestamp()) * 10_000_000
                value["last_write_time"], value["change_time"] = filetime, filetime
            elif isinstance(value, file_info.FileInternalInformation):
                value["index_number"] = record["id"]
            else:
                value["end_of_file"], value["directory"] = len(record["data"]), record["kind"] == "directory"
            response = smbopen.SMB2QueryInfoResponse()
            response["buffer"] = value.pack()
            return {"data": SimpleNamespace(get_value=lambda: response.pack())}

    class Session:
        def __init__(self, conn, username, password, require_encryption, auth_protocol):
            assert (username, password) == (CREDS.username, CREDS.password)
            assert require_encryption and auth_protocol == "ntlm"
            self.connection, self.session_id = conn, 1

        def connect(self):
            calls.append("auth")

    class Tree:
        def __init__(self, sess, unc):
            assert unc == r"\\synthetic.invalid\SYNTHETIC-share"
            self.session, self.tree_connect_id, self.is_dfs_share = sess, 2, False

        def connect(self, require_secure_negotiate):
            assert require_secure_negotiate

    class Open:
        def __init__(self, target, path):
            self.path, self.connected, self.queried = path, False, False
            self.file_id = path.encode().ljust(16, b"\0")

        def create(self, impersonation, access, attributes, sharing, disposition, options):
            assert disposition == smbopen.CreateDisposition.FILE_OPEN
            assert options & smbopen.CreateOptions.FILE_OPEN_REPARSE_POINT
            assert not access & (smbopen.FilePipePrinterAccessMask.FILE_WRITE_DATA |
                                 smbopen.FilePipePrinterAccessMask.FILE_APPEND_DATA |
                                 smbopen.FilePipePrinterAccessMask.DELETE)
            self.file_attributes, self.connected = 0, True
            calls.append(("open", self.path))

        def query_directory(self, pattern, info_class, flags=0, max_output=65536):
            assert info_class == file_info.FileIdFullDirectoryInformation.INFO_CLASS and max_output == 65536
            if self.queried:
                raise NoMoreFiles()
            self.queried = True
            value = file_info.FileIdFullDirectoryInformation()
            value["file_name"], value["end_of_file"] = "photo.jpg".encode("utf-16-le"), len(DATA)
            value["last_write_time"], value["change_time"], value["file_id"] = now, now, 2
            return [value]

        def read(self, offset, length):
            assert length <= 65536
            return records[self.path]["data"][offset:offset + length]

        def close(self):
            self.connected = False
            calls.append(("close", self.path))

    monkeypatch.setattr(connection, "Connection", Connection)
    monkeypatch.setattr(session, "Session", Session)
    monkeypatch.setattr(tree, "TreeConnect", Tree)
    monkeypatch.setattr(smbopen, "Open", Open)
    reader = SMBReader(config(), CREDS)
    try:
        entry = reader.list_page("", None, 128)["entries"][0]
        assert reader.stat("photo.jpg")["revision"] == entry["revision"]
        assert base64.b64decode(reader.read_range("photo.jpg", entry["revision"], 1, 5)["data"]) == DATA[1:6]
        records["photo.jpg"]["data"] += b"SYNTHETIC-change"
        with pytest.raises(NASConnectionError, match="nas_revision_changed"):
            reader.read_range("photo.jpg", entry["revision"], 0, 5)
    finally:
        reader.close()
    assert "disconnect" in calls and calls.count(("open", "photo.jpg")) == calls.count(("close", "photo.jpg"))
