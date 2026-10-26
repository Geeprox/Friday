import asyncio
import http.client
import importlib.util
import json
import os
import struct
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SPEC = importlib.util.spec_from_file_location("ktv_network_audio", Path(__file__).parents[1] / "scripts/ktv_network_audio.py")
ktv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ktv)


@pytest.fixture(autouse=True)
def selected_private_speaker(monkeypatch):
    monkeypatch.setattr(ktv, 'speaker_entity', lambda: 'media_player.example_speaker')


def header(frames=441, *, rate=44100, channels=2):
    size = frames * channels * 2
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", size + 36, b"WAVE", b"fmt ", 16,
                       1, channels, rate, rate * channels * 2, channels * 2, 16, b"data", size)


class Config:
    def __init__(self, name="已选择的媒体播放器", identifier="SYNTHETIC-JBL", address="192.168.1.2"):
        self.name, self.address = name, address
        self.service = SimpleNamespace(identifier=identifier)
        self.service.pairing = "optional"
        self.service.credentials = None

    def get_service(self, protocol):
        return self.service if protocol == "RAOP" else None


def test_identity_never_falls_back_to_same_name_or_different_receiver():
    with pytest.raises(ktv.Unavailable) as mismatch:
        ktv.exact_receiver([Config(identifier="another")], "SYNTHETIC-JBL", "RAOP")
    assert mismatch.value.code == "receiver_not_found"
    receiver = Config(name="Different speaker brand")
    assert ktv.exact_receiver([receiver], "SYNTHETIC-JBL", "RAOP") is receiver
    with pytest.raises(ktv.Unavailable) as duplicate:
        ktv.exact_receiver([Config(), Config()], "SYNTHETIC-JBL", "RAOP")
    assert duplicate.value.code == "receiver_identity_ambiguous"


@pytest.mark.parametrize("host", ["8.8.8.8", "127.0.0.1", "169.254.1.1", "224.1.1.1", "0.0.0.0", "example.com", "::1"])
def test_network_target_is_private_ipv4_only(host):
    with pytest.raises(ktv.Unavailable) as error:
        ktv.private_host(host)
    assert error.value.code == "private_ipv4_required"


@pytest.mark.asyncio
async def test_silent_discovery_exports_all_identified_receivers_and_private_report(tmp_path, monkeypatch):
    monkeypatch.setattr(ktv, "ROOT", tmp_path)
    calls = []
    async def scan(loop, **kwargs):
        calls.append(kwargs)
        return [Config(), Config("Another receiver", identifier="SYNTHETIC-SECOND"), Config("TV receiver", identifier="SYNTHETIC-THIRD")]
    report = tmp_path / "discovery.json"
    result = await ktv.discover(report, scan=scan, protocol="RAOP")
    assert result["status"] == "ready" and len(result["devices"]) == 3
    assert result["devices"][0]["identifier"] == "SYNTHETIC-JBL"
    assert calls == [{"timeout": 5}]
    assert report.stat().st_mode & 0o777 == 0o600
    assert result["raw_audio_saved"] is False and result["system_audio_redirected"] is False
    assert result["physical_audio_confirmed"] is False


def test_live_wav_replays_header_and_consumes_exact_pcm_without_disk():
    reader, writer = os.pipe()
    try:
        pcm = struct.pack("<hhhh", 1, -1, 2, -2)
        data = header(frames=2) + pcm
        os.write(writer, data)
        os.close(writer)
        writer = None
        source = ktv.LiveWav(reader, threading.Event(), 1)
        assert source.read(12) + source.read(1024) == data
        assert source.read(1024) == b""
        assert source.bytes_read == 8
        assert source.seekable() is False
        source.close()
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)


def test_wrong_format_is_rejected_before_pcm_is_read():
    reader, writer = os.pipe()
    try:
        os.write(writer, header(rate=48000))
        with pytest.raises(ktv.Unavailable) as error:
            ktv.LiveWav(reader, threading.Event(), 1)
        assert error.value.code == "wav_format_unsupported"
    finally:
        os.close(reader)
        os.close(writer)


def test_cancellation_releases_worker_waiting_for_live_input():
    reader, writer = os.pipe()
    stopped = threading.Event()
    try:
        os.write(writer, header())
        source = ktv.LiveWav(reader, stopped, 1)
        result = []
        worker = threading.Thread(target=lambda: result.append(source.read(4096)))
        worker.start()
        time.sleep(0.02)
        stopped.set()
        worker.join(0.5)
        assert not worker.is_alive()
        assert result == [header()]
        source.close()
    finally:
        stopped.set()
        os.close(reader)
        os.close(writer)


def test_report_never_serializes_exception_or_claims_audibility(tmp_path, monkeypatch):
    monkeypatch.setattr(ktv, "ROOT", tmp_path)
    report = tmp_path / "failure.json"
    result = ktv.write_report(report, "unavailable", "secret-token-from-exception")
    assert result["code"] == "stream_unavailable"
    assert "secret-token" not in report.read_text()
    assert not json.loads(report.read_text())["physical_audio_confirmed"]


def test_report_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(ktv, "ROOT", tmp_path)
    original = tmp_path / "original.json"
    original.write_text("preserve")
    link = tmp_path / "report.json"
    link.symlink_to(original)
    with pytest.raises(ktv.Unavailable) as error:
        ktv.write_report(link, "unavailable")
    assert error.value.code == "report_unavailable" and original.read_text() == "preserve"


@pytest.mark.parametrize("state,attributes,expected", [
    ("playing", {"volume_level": .35}, "receiver_busy"),
    ("idle", {"volume_level": .35, "is_volume_muted": True}, "receiver_muted"),
    ("idle", {"volume_level": None}, "receiver_volume_unavailable"),
    ("idle", {"volume_level": False}, "receiver_volume_unavailable"),
])
def test_ha_busy_muted_or_unknown_volume_never_guesses_audible_volume(monkeypatch, state, attributes, expected):
    speech = ModuleType("homeplane.embodied.speech")
    speech.HA = lambda: SimpleNamespace(request=lambda _: {"state": state, "attributes": attributes})
    monkeypatch.setitem(sys.modules, "homeplane.embodied.speech", speech)
    with pytest.raises(ktv.Unavailable) as error:
        ktv.jbl_state()
    assert error.value.code == expected


def fake_pyatv(monkeypatch):
    root = ModuleType("pyatv")
    root.connect = AsyncMock()
    root.scan = AsyncMock()
    const = ModuleType("pyatv.const")
    const.Protocol = SimpleNamespace(RAOP="RAOP")
    const.FeatureName = SimpleNamespace(StreamFile="file")
    const.FeatureState = SimpleNamespace(Available="available")
    const.PairingRequirement = SimpleNamespace(Mandatory="mandatory")
    interface = ModuleType("pyatv.interface")
    interface.MediaMetadata = lambda **kwargs: SimpleNamespace(**kwargs)
    for name, value in [("pyatv", root), ("pyatv.const", const), ("pyatv.interface", interface)]:
        monkeypatch.setitem(sys.modules, name, value)
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_stream_seeds_existing_volume_and_closes_own_device_on_all_exits(tmp_path, monkeypatch, cancelled):
    fake_pyatv(monkeypatch)
    monkeypatch.setattr(ktv, "ROOT", tmp_path)
    reader, writer = os.pipe()
    os.write(writer, header(frames=2) + struct.pack("<hhhh", 1, -1, 2, -2))
    os.close(writer)
    stopped = threading.Event()
    closed = []
    async def cleanup():
        closed.append(True)
    async def send(source, **kwargs):
        assert kwargs["metadata"].title == "Friday KTV 试听"
        if cancelled:
            raise asyncio.CancelledError()
        while await asyncio.to_thread(source.read, 4096):
            pass
    volume = AsyncMock()
    device = SimpleNamespace(features=SimpleNamespace(get_feature=lambda _: SimpleNamespace(state="available")),
                             audio=SimpleNamespace(set_volume=volume),
                             stream=SimpleNamespace(stream_file=send), close=lambda: [cleanup()])
    args = SimpleNamespace(host="192.168.1.2", identifier="SYNTHETIC-JBL", seconds=1,
                           report=tmp_path / "stream.json", transport="raop")
    with os.fdopen(reader, "rb") as pipe:
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=pipe))
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await ktv.stream(args, stopped, scan=AsyncMock(return_value=[Config()]),
                                 connect=AsyncMock(return_value=device), state=lambda: ("idle", .35))
        else:
            result = await ktv.stream(args, stopped, scan=AsyncMock(return_value=[Config()]),
                                      connect=AsyncMock(return_value=device), state=lambda: ("idle", .35))
            assert result["status"] == "completed" and result["bytes_consumed"] == 8
    volume.assert_awaited_once_with(35.0)
    assert stopped.is_set() and closed == [True]


@pytest.mark.parametrize("value,expected", [(None, (0, 99, 200)), ("bytes=0-", (0, 99, 206)),
                                           ("bytes=20-29", (20, 29, 206)), ("bytes=-10", (90, 99, 206))])
def test_single_http_ranges(value, expected):
    assert ktv.byte_range(value, 100) == expected


@pytest.mark.parametrize("value", ["bytes=100-", "bytes=30-20", "bytes=-0", "bytes=0-1,3-4", "garbage"])
def test_invalid_http_range_is_rejected(value):
    with pytest.raises(ValueError):
        ktv.byte_range(value, 100)


@pytest.mark.parametrize("current_url,expected", [("http://owned.invalid/token.wav", True),
                                                ("http://another.invalid/music.wav", False)])
def test_stop_only_exact_current_owned_media(current_url, expected):
    calls = []
    def request(path, data=None):
        calls.append((path, data))
        return {"state": "playing", "attributes": {"media_content_id": current_url}}
    assert ktv.stop_owned_media(SimpleNamespace(request=request), "http://owned.invalid/token.wav") is expected
    assert len(calls) == (2 if expected else 1)
    if expected:
        assert calls[-1] == ("services/media_player/media_stop", {"entity_id": "media_player.example_speaker"})


def test_http_serves_pcm_before_stdin_eof_and_supports_head_range(tmp_path, monkeypatch):
    # Loopback only for this local test; production private_host rejects loopback.
    monkeypatch.setattr(ktv, "private_host", lambda host: host)
    def forbidden_reverse_dns(*_):
        raise AssertionError('numeric loopback listener must not perform reverse DNS')
    monkeypatch.setattr(ktv.socket, 'getfqdn', forbidden_reverse_dns)
    reader, writer = os.pipe()
    release = threading.Event()
    stopped = threading.Event()
    payload = struct.pack("<hh", 100, -100) * 4096
    def produce():
        try:
            data = header(frames=44100) + payload
            offset = 0
            while offset < len(data):
                offset += os.write(writer, data[offset:])
            release.wait(10)
        finally:
            os.close(writer)
    producer = threading.Thread(target=produce)
    producer.start()
    source = ktv.LiveWav(reader, stopped, 1)
    memory = ktv.MemoryWav(source, stopped)
    server = thread = client = None
    try:
        server, thread, url = ktv.start_http(memory, "127.0.0.1")
        path = url.split(str(server.server_port), 1)[1]
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
        client.request("GET", path)
        response = client.getresponse()
        first = response.read(44 + 512)
        assert first == header(frames=44100) + payload[:512]
        assert memory.eof is False and producer.is_alive()
        assert len(memory.data) < memory.length
        client.close()
        second = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
        second.request("HEAD", path)
        assert second.getresponse().getheader("Content-Length") == str(176444)
        second.close()
        third = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
        third.request("GET", path, headers={"Range": "bytes=44-143"})
        ranged = third.getresponse()
        assert ranged.status == 206 and ranged.read() == payload[:100]
        third.close()
    finally:
        if client:
            client.close()
        memory.close()
        release.set()
        producer.join(1)
        if server:
            server.shutdown()
            server.server_close()
            thread.join(1)
        os.close(reader)
    assert not producer.is_alive() and not memory.thread.is_alive() and not thread.is_alive()
