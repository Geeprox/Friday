#!/usr/bin/env python3
"""Explicit, bounded selected-speaker HA/HTTP stream; never a macOS default audio-route change.

Run with data/embodied/runtime/bin/python. Audio enters stdin as a finite canonical
44.1-kHz stereo s16le WAV. It is never persisted or emitted in diagnostics.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import select
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer

ROOT = Path(__file__).resolve().parents[1]
MAX_SECONDS = 30
FRAME_BYTES = 4
SAMPLE_RATE = 44100
MAX_BUFFER = 65536
CODES = frozenset({"none", "invalid_arguments", "private_ipv4_required", "dependency_unavailable",
                   "receiver_not_found", "receiver_identity_ambiguous", "receiver_pairing_required",
                   "receiver_volume_unavailable", "receiver_busy", "receiver_muted", "connection_unavailable",
                   "stream_unavailable", "wav_header_invalid", "wav_format_unsupported", "wav_input_timeout",
                   "wav_input_truncated", "cancelled", "deadline_exceeded", "report_unavailable"})
CODES = CODES | frozenset({"receiver_http_unavailable", "media_fetch_timeout"})
JOURNAL = None
PHASE = "startup"
LAST_PROGRESS = {}
PHASES = frozenset({"startup", "discover", "ha_read", "connect", "volume", "wav_header", "send", "close",
                   "server", "ha_play", "fetch", "ha_stop"})


class Unavailable(Exception):
    def __init__(self, code: str):
        self.code = code if code in CODES else "stream_unavailable"


def private_host(value: str) -> str:
    try:
        host = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as exc:
        raise Unavailable("private_ipv4_required") from exc
    if not host.is_private or any((host.is_loopback, host.is_link_local, host.is_multicast,
                                   host.is_unspecified, host.is_reserved)):
        raise Unavailable("private_ipv4_required")
    return str(host)


def write_report(path: Path, status: str, code: str = "none", **extra) -> dict:
    if code not in CODES:
        code = "stream_unavailable"
    value = {"schema_version": 1, "status": status, "code": code,
             "transport": "raop", "independent_connection": True,
             "system_audio_redirected": False, "raw_audio_saved": False,
             "physical_audio_confirmed": False, "end_to_end_latency_measured": False, **extra}
    if JOURNAL is not None:
        start = status in {"discovering", "connecting", "streaming"}
        mapped = ("flow_cancelled" if code == "cancelled" else "deadline_exceeded" if code.endswith("timeout")
                  or code == "deadline_exceeded" else "runtime_busy" if code == "receiver_busy"
                  else "unsupported" if code in {"wav_header_invalid", "wav_format_unsupported", "invalid_arguments"}
                  else "transport_error" if code != "none" else "none")
        result = ("cancelled" if code == "cancelled" else "timeout" if mapped == "deadline_exceeded"
                  else "unavailable" if status == "unavailable" else "ok")
        JOURNAL.record("service.start" if start else "service.stop", "start" if start else "result",
                       result=result, code=mapped)
        value["diagnostic_logging_available"] = JOURNAL.disk_available is True
    try:
        parent = path.parent.resolve(strict=True)
        if not parent.is_relative_to(ROOT) or path.is_symlink():
            raise OSError()
        fd, name = tempfile.mkstemp(prefix=".ktv-network-", dir=parent)
        try:
            with os.fdopen(fd, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    except OSError as exc:
        raise Unavailable("report_unavailable") from exc
    return value


def raop_identifier(config, protocol) -> str | None:
    service = config.get_service(protocol)
    return service.identifier if service and service.identifier else None


def exact_receiver(configs, identifier: str, protocol):
    selected = [config for config in configs
                if raop_identifier(config, protocol) == identifier]
    if len(selected) != 1:
        raise Unavailable("receiver_identity_ambiguous" if selected else "receiver_not_found")
    private_host(str(selected[0].address))
    return selected[0]


async def discover(report: Path, *, scan=None, protocol=None) -> dict:
    if scan is None:
        try:
            from pyatv import scan
            from pyatv.const import Protocol
        except ImportError as exc:
            raise Unavailable("dependency_unavailable") from exc
        protocol = Protocol.RAOP
    write_report(report, "discovering")
    configs = await asyncio.wait_for(scan(asyncio.get_running_loop(), timeout=5), 8)
    devices = []
    for config in configs:
        identifier = raop_identifier(config, protocol)
        if identifier:
            devices.append({"name": config.name, "identifier": identifier,
                            "host": private_host(str(config.address)), "transport": "raop"})
    return write_report(report, "ready", devices=devices)


class LiveWav(io.BufferedIOBase):
    """Pipe reader with finite bytes, cancellation and bounded reads.

    miniaudio reads on worker threads. select's small timeout lets a cancelled
    protocol operation release those threads instead of leaving them on stdin.
    """
    name = "FridayLivePCM"

    def __init__(self, fd: int, stopped: threading.Event, seconds: int):
        self.fd = fd
        self.stopped = stopped
        self.deadline = time.monotonic() + 40
        self.bytes_read = 0
        self.data_bytes = 0
        self.header = self._read_exact(44, first=True)
        if len(self.header) != 44:
            raise Unavailable("wav_header_invalid")
        riff, riff_size, wave, fmt, fmt_size, encoding, channels, rate, byte_rate, alignment, bits, data, size = \
            struct.unpack("<4sI4s4sIHHIIHH4sI", self.header)
        if (riff, wave, fmt, data) != (b"RIFF", b"WAVE", b"fmt ", b"data") or fmt_size != 16:
            raise Unavailable("wav_header_invalid")
        if (encoding, channels, rate, byte_rate, alignment, bits) != (1, 2, 44100, 176400, 4, 16):
            raise Unavailable("wav_format_unsupported")
        maximum = seconds * SAMPLE_RATE * FRAME_BYTES
        if not 0 < size <= maximum or size % FRAME_BYTES or riff_size != size + 36:
            raise Unavailable("wav_header_invalid")
        self.data_bytes = size
        self._pending = self.header

    def _read_exact(self, count: int, *, first=False) -> bytes:
        output = bytearray()
        idle_deadline = time.monotonic() + (8 if first else 5)
        while len(output) < count and not self.stopped.is_set():
            if time.monotonic() >= min(self.deadline, idle_deadline):
                raise Unavailable("wav_input_timeout")
            if not select.select([self.fd], [], [], 0.1)[0]:
                continue
            block = os.read(self.fd, count - len(output))
            if not block:
                break
            output.extend(block)
            idle_deadline = time.monotonic() + 5
        return bytes(output)

    def read(self, size=-1):
        if size == 0 or self.stopped.is_set():
            return b""
        count = min(MAX_BUFFER, size if size >= 0 else MAX_BUFFER)
        prefix = self._pending[:count]
        self._pending = self._pending[len(prefix):]
        remaining = min(count - len(prefix), self.data_bytes - self.bytes_read)
        data = self._read_exact(remaining) if remaining > 0 else b""
        self.bytes_read += len(data)
        return prefix + data

    def seekable(self):
        return False

    def readable(self):
        return True

    def close(self):
        self.stopped.set()
        super().close()


def speaker_entity():
    from homeplane.embodied.paths import configuration
    entity = configuration().get('speaker_entity')
    if not isinstance(entity, str) or not re.fullmatch(r'media_player\.[a-z0-9_]+', entity):
        raise Unavailable('receiver_not_found')
    return entity


def jbl_state() -> tuple[str, float]:
    # Reuse the explicitly selected HA media player; do not
    # export credentials/URLs/exception text. An unavailable read never guesses volume.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from homeplane.embodied.speech import HA
        result = HA().request("states/" + speaker_entity())
        state = result.get("state")
        attributes = result.get("attributes", {})
        volume = attributes.get("volume_level")
        muted = attributes.get("is_volume_muted")
    except Exception as exc:
        raise Unavailable("receiver_volume_unavailable") from exc
    if state not in {"idle", "off", "paused"}:
        raise Unavailable("receiver_busy")
    if muted is True:
        raise Unavailable("receiver_muted")
    if type(volume) not in {int, float} or not 0 <= volume <= 1:
        raise Unavailable("receiver_volume_unavailable")
    return state, float(volume)


async def stream_raop(args, stopped: threading.Event, *, scan=None, connect=None, state=jbl_state):
    global PHASE
    try:
        from pyatv import connect as pyatv_connect
        from pyatv import scan as pyatv_scan
        from pyatv.const import FeatureName, FeatureState, PairingRequirement, Protocol
        from pyatv.interface import MediaMetadata
    except ImportError as exc:
        raise Unavailable("dependency_unavailable") from exc
    scan = scan or pyatv_scan
    connect = connect or pyatv_connect
    host = private_host(args.host)
    PHASE = "discover"
    write_report(args.report, "connecting", max_seconds=args.seconds)
    configs = await asyncio.wait_for(scan(asyncio.get_running_loop(), hosts=[host], timeout=3), 5)
    config = exact_receiver(configs, args.identifier, Protocol.RAOP)
    service = config.get_service(Protocol.RAOP)
    if service.pairing == PairingRequirement.Mandatory and not service.credentials:
        raise Unavailable("receiver_pairing_required")
    PHASE = "ha_read"
    _, volume = await asyncio.to_thread(state)
    PHASE = "connect"
    device = await asyncio.wait_for(connect(config, asyncio.get_running_loop(), protocol=Protocol.RAOP), 6)
    source = None
    try:
        version = getattr(args, "raop_version", "auto")
        if version != "auto":
            from pyatv.settings import AirPlayVersion
            device.settings.protocols.raop.protocol_version = AirPlayVersion.V1 if version == "1" else AirPlayVersion.V2
        if device.features.get_feature(FeatureName.StreamFile).state != FeatureState.Available:
            raise Unavailable("stream_unavailable")
        # pyatv otherwise uses its 33% default if the receiver omits initialVolume.
        # Seed the exact HA-read volume; no user-level increase is performed.
        PHASE = "volume"
        await asyncio.wait_for(device.audio.set_volume(volume * 100), 3)
        PHASE = "wav_header"
        source = await asyncio.to_thread(LiveWav, sys.stdin.buffer.fileno(), stopped, args.seconds)
        PHASE = "send"
        write_report(args.report, "streaming", max_seconds=args.seconds,
                     receiver_volume=volume, receiver_volume_seeded_from_existing_state=True,
                     protocol_stream_started=True, protocol_version=version)
        await device.stream.stream_file(source, metadata=MediaMetadata(title="Friday KTV 试听"))
        if stopped.is_set():
            return write_report(args.report, "completed", "cancelled", bytes_consumed=source.bytes_read)
        if source.bytes_read < source.data_bytes:
            raise Unavailable("wav_input_truncated")
        return write_report(args.report, "completed", bytes_consumed=source.bytes_read,
                            receiver_volume=volume, backend_completed=True)
    finally:
        stopped.set()
        if source:
            source.close()
        tasks = device.close()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            except asyncio.TimeoutError:
                pass


def byte_range(header: str | None, length: int) -> tuple[int, int, int]:
    if header is None:
        return 0, length - 1, 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not any(match.groups()):
        raise ValueError()
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), length - 1) if last else length - 1
    else:
        count = int(last)
        if count <= 0:
            raise ValueError()
        start, end = max(0, length - count), length - 1
    if not 0 <= start <= end < length:
        raise ValueError()
    return start, end, 206


class MemoryWav:
    """At most 30 seconds of stdin PCM, retained only in bounded RAM for HTTP ranges."""
    def __init__(self, source: LiveWav, stopped: threading.Event):
        self.source, self.stopped = source, stopped
        self.length = source.data_bytes + 44
        self.data = bytearray(source.read(44))
        self.condition = threading.Condition()
        self.eof = False
        self.failure = None
        self.requests: list[dict] = []
        self.active_clients: set[socket.socket] = set()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        try:
            while not self.stopped.is_set():
                block = self.source.read(16384)
                if not block:
                    break
                with self.condition:
                    if len(self.data) + len(block) > self.length:
                        self.failure = "wav_header_invalid"
                        break
                    self.data.extend(block)
                    self.condition.notify_all()
        except Unavailable as error:
            self.failure = error.code
        except OSError:
            self.failure = "stream_unavailable"
        finally:
            with self.condition:
                self.eof = True
                if not self.stopped.is_set() and len(self.data) != self.length and not self.failure:
                    self.failure = "wav_input_truncated"
                self.condition.notify_all()

    @property
    def report(self) -> dict:
        with self.condition:
            requests = [dict(row) for row in self.requests]
            return {"http_requests": requests, "input_bytes_read": self.source.bytes_read,
                    "http_bytes_sent": sum(row["bytes_sent"] for row in requests),
                    "http_media_requested": any(row["method"] == "GET" for row in requests),
                    "http_full_media_served": any(row["method"] == "GET" and row["start"] == 0
                                                  and row["bytes_sent"] == self.length for row in requests),
                    "input_complete": self.eof and self.failure is None and len(self.data) == self.length}

    def close(self):
        self.stopped.set()
        self.source.close()
        with self.condition:
            self.condition.notify_all()
            clients = list(self.active_clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
                client.close()
            except OSError:
                pass
        self.thread.join(0.5)


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 4

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(address, handler)

    def server_bind(self):
        # The listener is an explicitly validated numeric address. HTTPServer's
        # reverse-DNS lookup can stall startup while live input times out.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never print upstream socket details or a traceback.


def start_http(memory: MemoryWav, host: str):
    token_path = "/" + secrets.token_urlsafe(24) + ".wav"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            self.serve(False)

        def do_GET(self):
            self.serve(True)

        def serve(self, body):
            if self.path != token_path:
                self.send_error(404)
                return
            try:
                start, end, status = byte_range(self.headers.get("Range"), memory.length)
            except ValueError:
                self.send_error(416)
                return
            with memory.condition:
                if len(memory.requests) >= 16:
                    self.send_error(429)
                    return
                row = {"method": "GET" if body else "HEAD", "status": status,
                       "start": start, "end": end, "bytes_sent": 0}
                memory.requests.append(row)
                memory.active_clients.add(self.connection)
            self.connection.settimeout(1)
            self.send_response(status)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{memory.length}")
            try:
                self.end_headers()
                cursor = start
                while body and cursor <= end and not memory.stopped.is_set():
                    with memory.condition:
                        while cursor >= len(memory.data) and not memory.eof and not memory.stopped.is_set():
                            memory.condition.wait(0.1)
                        if cursor >= len(memory.data) or memory.stopped.is_set():
                            break
                        block = bytes(memory.data[cursor:min(cursor + 16384, end + 1, len(memory.data))])
                    self.wfile.write(block)
                    cursor += len(block)
                    with memory.condition:
                        row["bytes_sent"] += len(block)
            except OSError:
                pass
            finally:
                with memory.condition:
                    memory.active_clients.discard(self.connection)

    server = BoundedHTTPServer((private_host(host), 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    return server, thread, f"http://{host}:{server.server_port}{token_path}"


def local_interface(target: str) -> str:
    route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        route.connect((private_host(target), 9))
        return private_host(route.getsockname()[0])
    finally:
        route.close()


def stop_owned_media(ha, url: str) -> bool:
    deadline = time.monotonic() + 1
    try:
        current = ha.request("states/" + speaker_entity())
        if time.monotonic() > deadline:
            return False
        if current.get("attributes", {}).get("media_content_id") == url and current.get("state") in {"playing", "buffering"}:
            ha.request("services/media_player/media_stop", {"entity_id": speaker_entity()})
            return True
    except Exception:
        pass
    return False


async def stream_ha(args, stopped: threading.Event):
    global PHASE, LAST_PROGRESS
    try:
        from pyatv import scan
        from pyatv.const import Protocol
    except ImportError as exc:
        raise Unavailable("dependency_unavailable") from exc
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from homeplane.embodied.speech import HA
    host = private_host(args.host)
    PHASE = "discover"
    write_report(args.report, "connecting", max_seconds=args.seconds, transport="ha_http")
    choices = await asyncio.wait_for(scan(asyncio.get_running_loop(), hosts=[host], timeout=3), 5)
    exact_receiver(choices, args.identifier, Protocol.RAOP)
    PHASE = "ha_read"
    _, volume = await asyncio.to_thread(jbl_state)
    PHASE = "wav_header"
    source = await asyncio.to_thread(LiveWav, sys.stdin.buffer.fileno(), stopped, args.seconds)
    memory = MemoryWav(source, stopped)
    write_report(args.report, "input_ready", transport="ha_http", max_seconds=args.seconds,
                 protocol_stream_started=False, receiver_volume=volume)
    server = thread = url = None
    ha = HA()
    playing_seen = False
    try:
        PHASE = "server"
        server, thread, url = start_http(memory, local_interface(host))
        PHASE = "ha_play"
        await asyncio.to_thread(ha.request, "services/media_player/play_media",
                                {"entity_id": speaker_entity(), "media_content_id": url,
                                 "media_content_type": "audio/wav"})
        PHASE = "fetch"
        write_report(args.report, "streaming", transport="ha_http", max_seconds=args.seconds,
                     protocol_stream_started=True, receiver_volume=volume, ha_service_accepted=True)
        started = asyncio.get_running_loop().time()
        while True:
            if memory.failure:
                raise Unavailable(memory.failure)
            progress = memory.report
            LAST_PROGRESS = progress
            current = await asyncio.to_thread(ha.request, "states/" + speaker_entity())
            playing_seen |= current.get("state") == "playing"
            elapsed = asyncio.get_running_loop().time() - started
            if progress["input_complete"] and progress["http_full_media_served"]:
                if (playing_seen and current.get("state") in {"idle", "off", "paused"}) or elapsed >= args.seconds + 5:
                    return write_report(args.report, "completed", transport="ha_http", backend_completed=True,
                                        receiver_volume=volume, ha_service_accepted=True,
                                        ha_playback_observed=playing_seen, **progress)
            if elapsed >= 6 and not progress["http_media_requested"]:
                raise Unavailable("media_fetch_timeout")
            await asyncio.sleep(0.25)
    finally:
        completed_phase = PHASE
        LAST_PROGRESS = memory.report
        memory.close()
        if server:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(0.5)
        if url:
            PHASE = "ha_stop"
            try:
                await asyncio.wait_for(asyncio.to_thread(stop_owned_media, ha, url), 1.2)
            except asyncio.TimeoutError:
                pass
        PHASE = completed_phase


async def stream(args, stopped: threading.Event, **kwargs):
    if getattr(args, "transport", "ha") == "raop":
        return await stream_raop(args, stopped, **kwargs)
    return await stream_ha(args, stopped)


async def execute(args) -> int:
    global PHASE, LAST_PROGRESS
    PHASE, LAST_PROGRESS = "startup", {}
    stopped = threading.Event()
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    transport = "ha_http" if args.stream and getattr(args, "transport", "ha") == "ha" else "raop"
    def cancel():
        stopped.set()
        current.cancel()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, cancel)
    try:
        if args.discover:
            await discover(args.report)
        else:
            await asyncio.wait_for(stream(args, stopped), 40)
        return 0
    except asyncio.CancelledError:
        write_report(args.report, "completed", "cancelled", transport=transport, **LAST_PROGRESS)
        return 0
    except asyncio.TimeoutError:
        write_report(args.report, "unavailable", "deadline_exceeded", transport=transport, **LAST_PROGRESS)
        return 1
    except Unavailable as exc:
        write_report(args.report, "unavailable", exc.code, transport=transport, **LAST_PROGRESS)
        return 1
    except Exception as exc:
        error_type = type(exc).__name__
        allowed_types = {"KeyError", "ProtocolError", "AuthenticationError", "ConnectionFailedError", "TypeError",
                         "AttributeError", "OSError", "InvalidStateError", "NoServiceError", "NotSupportedError",
                         "ValueError", "RuntimeError", "ConnectionRefusedError", "ConnectionResetError",
                         "HttpError", "PlaybackError", "InvalidResponseError", "OperationTimeoutError",
                         "DecodeError", "NoCredentialsError", "InvalidCredentialsError", "BackOffError"}
        http_status = getattr(exc, "status_code", None)
        write_report(args.report, "unavailable", "connection_unavailable",
                     transport=transport, **LAST_PROGRESS,
                     failure_phase=PHASE if PHASE in PHASES else "startup",
                     error_type=error_type if error_type in allowed_types else "unknown",
                     http_status=http_status if type(http_status) is int and 100 <= http_status <= 599 else None)
        return 1
    finally:
        stopped.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> int:
    global JOURNAL
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true")
    mode.add_argument("--stream", action="store_true")
    parser.add_argument("--identifier")
    parser.add_argument("--host")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--raop-version", choices=["auto", "1", "2"], default="auto")
    parser.add_argument("--transport", choices=["ha", "raop"], default="ha")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    # Third-party logging can contain private endpoints. Only fixed reports are retained.
    logging.disable(logging.CRITICAL)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from homeplane.runtime_log import RuntimeLog
        JOURNAL = RuntimeLog(ROOT / ".artifacts/ktv-audio/network/journal")
    except ImportError:
        pass
    try:
        if not 1 <= args.seconds <= MAX_SECONDS or (args.stream and (not args.identifier or not args.host)):
            write_report(args.report, "unavailable", "invalid_arguments")
            return 1
        return asyncio.run(execute(args))
    except Unavailable:
        print(json.dumps({"status": "unavailable", "code": "report_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
