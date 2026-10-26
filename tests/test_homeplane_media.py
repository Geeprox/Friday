"""Real standard decoders and IPC over explicit SIMULATED camera streams."""
import asyncio
import json
import socket
import struct
import sys

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from media_fixture import packets

from homeplane.api import create_app
from homeplane.media_contracts import MediaConnection, MediaProbeRequest, MediaProbeResult
from homeplane.media_probe import MediaProbe
from homeplane.media_worker import MAX_PACKET, MediaError, StreamDecoder
from homeplane.settings import SettingsService


@pytest.fixture(scope="module")
def encoded():
    return packets()


def test_actual_decoders_and_clock_discontinuity_remain_unknown(encoded):
    video, audio = StreamDecoder("video"), StreamDecoder("audio")
    video.initialize({"codec": "h264", "container": "annexb"})
    audio.initialize({"codec": "opus", "sampleRate": 48000, "numberOfChannels": 1})
    assert not video.decoder.options, "native max_pixels option must be consumed"
    for index, (data, key) in enumerate(encoded[0][:8]):
        header = struct.pack(">B7xQ", int(key), 100 if index == 0 else index)
        video.feed(header + data, float(index * 50))
    for index, data in enumerate(encoded[1][:10]):
        audio.feed(data, float(index * 20))
    assert video.result.decoded_frames >= 6 and (video.result.width, video.result.height) == (160, 120)
    assert video.result.video_clock_discontinuities == 1
    assert audio.result.decoded_frames == 10 and audio.result.pcm_16k_samples > 2500
    assert MediaProbeResult().source_av_alignment == "unavailable"


@pytest.mark.parametrize("kind,meta,packet,code", [
    ("video", None, b"SIMULATED-no-init", "missing_init"),
    ("video", {"codec": "h264", "container": "annexb"}, b"short", "malformed_packet"),
    ("audio", {"codec": "opus", "sampleRate": 48000, "numberOfChannels": 1}, b"x" * (MAX_PACKET + 1), "input_budget"),
])
def test_untrusted_packet_boundaries(kind, meta, packet, code):
    decoder = StreamDecoder(kind)
    if meta:
        decoder.initialize(meta)
    with pytest.raises(MediaError) as error:
        decoder.feed(packet, 0.0)
    assert error.value.code == code


def test_init_change_and_unknown_codecs_are_rejected():
    decoder = StreamDecoder("audio")
    decoder.initialize({"codec": "g711a", "sampleRate": 8000, "numberOfChannels": 1})
    with pytest.raises(MediaError) as error:
        decoder.initialize({"codec": "opus", "sampleRate": 48000, "numberOfChannels": 1})
    assert error.value.code == "codec_changed"
    with pytest.raises(MediaError):
        StreamDecoder("video").initialize({"codec": "SIMULATED-unknown", "container": "annexb"})
    with pytest.raises(ValueError):
        MediaConnection(origin="https://example.com", camera_id="SIMULATED", token="SIMULATED")


@pytest.fixture
async def camera(tmp_path, encoded):
    app = FastAPI()
    @app.get("/api/miot/status")
    async def account():
        return {"code": 0, "data": {"is_bound": True}}
    @app.websocket("/api/miot/ws/{kind}_stream")
    async def stream(ws: WebSocket, kind: str):
        assert ws.headers["authorization"] == "Bearer SIMULATED-media-secret"
        assert ws.query_params["camera_id"] == "SIMULATED-camera"
        await ws.accept()
        await ws.send_json({"type": "init", **({"codec": "h264", "container": "annexb"} if kind == "video" else {"codec": "opus", "sampleRate": 48000, "numberOfChannels": 1})})
        try:
            for index, value in enumerate(encoded[0] if kind == "video" else encoded[1]):
                payload = struct.pack(">B7xQ", int(value[1]), index * 50) + value[0] if kind == "video" else value
                await ws.send_bytes(payload)
                await asyncio.sleep(.03 if kind == "video" else .01)
            await asyncio.sleep(3)
        except Exception:
            pass  # Test-owned WebSocket stops when the bounded client closes.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(.01)
    assert server.started
    settings = SettingsService(tmp_path / "settings.json", token_path=tmp_path / "service.json")
    settings.update({"camera_service_url": origin, "camera_id": "SIMULATED-camera"})
    settings.token_path.write_text(json.dumps({"server": {"token": "SIMULATED-media-secret"}}))
    settings.token_path.chmod(0o600)
    try:
        yield settings
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_real_worker_simulated_source_decodes_without_retaining_media(camera):
    verifier = MediaProbe(camera, simulated=True)
    try:
        assert (await verifier.start(MediaProbeRequest(seconds=1.0))).status == "running"
        await asyncio.wait_for(verifier.task, 5)
        result = verifier.status()
        assert result.status == "decoded_sync_unknown"
        assert result.simulated is True
        assert result.video.decoded_frames > 2 and result.audio.pcm_16k_samples > 1000
        assert result.reception_overlap_ms > 100 and result.alignment_error_bound_ms is None
        assert not result.media_persisted and not result.model_invoked and not result.device_write
        assert "SIMULATED-media-secret" not in result.model_dump_json()
        assert "SIMULATED-camera" not in result.model_dump_json()
        assert verifier.process is None
    finally:
        await verifier.stop()


@pytest.mark.asyncio
async def test_cancel_and_late_launch_cannot_publish_result_or_leave_worker(camera):
    ready, released = asyncio.Event(), asyncio.Event()
    processes, results = [], []
    async def late_launch():
        ready.set()
        await released.wait()
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time;time.sleep(20)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        processes.append(process)
        return process
    verifier = MediaProbe(camera, launcher=late_launch, on_result=results.append)
    await verifier.start(MediaProbeRequest(seconds=1.0))
    await ready.wait()
    await verifier.stop(changed=True)
    released.set()
    for _ in range(100):
        if processes and processes[0].returncode is not None:
            break
        await asyncio.sleep(.02)
    assert processes and processes[0].returncode is not None
    assert verifier.status().status == "discarded_configuration_changed" and not results


@pytest.mark.asyncio
async def test_sampled_memory_guard_ends_owned_worker(camera):
    async def rss(_):
        return 300 * 1024 * 1024
    verifier = MediaProbe(camera, rss_reader=rss)
    try:
        await verifier.start(MediaProbeRequest(seconds=1.0))
        await asyncio.wait_for(verifier.task, 5)
        assert verifier.status().failure == "worker_memory_limit" and verifier.process is None
    finally:
        await verifier.stop()


def test_contract_does_not_accept_sync_claim_or_packets_as_decoded():
    for forged in [{"source_av_alignment": "verified", "alignment_error_bound_ms": 0},
                   {"status": "decoded_sync_unknown"}, {"model_invoked": True},
                   {"checked_at": "2026-10-01T00:00:00"}]:
        with pytest.raises(ValueError):
            MediaProbeResult.model_validate(forged)


@pytest.mark.asyncio
async def test_api_vertical_loop_uses_real_decoders_on_simulated_media(camera):
    now = [0.0]
    probe = MediaProbe(camera, simulated=True, clock=lambda: now[0])
    app = create_app(db_path=camera.path.parent / "traces.sqlite", settings_service=camera, media_probe=probe)
    origin = "http://127.0.0.1:8740"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            endpoint = "/api/settings/camera/media"
            assert (await client.get(endpoint)).json()["status"] == "not_requested"
            assert probe.task is None  # Merely viewing settings cannot start media.
            assert (await client.post(endpoint, json={"seconds": 1})).status_code == 403
            headers = {"origin": origin}
            assert (await client.post(endpoint, json={"seconds": 11}, headers=headers)).status_code == 422
            assert (await client.post(endpoint, json={"seconds": 1}, headers=headers)).json()["status"] == "running"
            assert (await client.post(endpoint, json={"seconds": 1}, headers=headers)).status_code == 409
            await asyncio.wait_for(probe.task, 5)
            result = (await client.get(endpoint)).json()
            assert result["status"] == "decoded_sync_unknown" and result["simulated"]
            snapshot = (await client.get("/api/snapshot")).json()
            events = [e for e in snapshot["events"] if e["source"] == "camera-media"]
            assert len(events) == 1 and events[0]["payload"]["trigger_eligible"] is False
            assert not events[0]["payload"]["device_write"] and app.state.core_rule_runtime.status()["pending_count"] == 0
            now[0] = 61
            assert (await client.get(endpoint)).json()["status"] == "expired"
            assert not probe.process  # Expiry never schedules a new reader.
            changed = await client.put("/api/settings", json={"camera_id": "SIMULATED-replaced-camera"}, headers=headers)
            assert changed.status_code == 200
            assert (await client.get(endpoint)).json()["status"] == "discarded_configuration_changed"
            assert len([e for e in (await client.get("/api/snapshot")).json()["events"] if e["source"] == "camera-media"]) == 1


@pytest.mark.asyncio
async def test_hung_camera_does_not_block_core_and_change_reaps_owned_child(camera):
    from tests.test_homeplane_core import request
    processes = []
    async def hang():
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time;time.sleep(20)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        processes.append(process)
        return process
    probe = MediaProbe(camera, simulated=True, launcher=hang)
    app = create_app(db_path=camera.path.parent / "traces.sqlite", settings_service=camera, media_probe=probe)
    origin = "http://127.0.0.1:8740"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            await client.post("/api/settings/camera/media", json={"seconds": 1}, headers={"origin": origin})
            for _ in range(100):
                if probe.process:
                    break
                await asyncio.sleep(.01)
            snapshot = await asyncio.wait_for(client.get("/api/snapshot"), 1)
            trace = await asyncio.wait_for(client.post("/api/actions/dry-run", json=request(snapshot.json()["version"])), 1)
            assert trace.status_code == 200 and trace.json()["code"] == "dry_run_only"
            changed = await client.put("/api/settings", json={"camera_id": "SIMULATED-new"}, headers={"origin": origin})
            assert changed.status_code == 200 and processes[0].returncode is not None and probe.process is None
            assert not [e for e in app.state.core.store.recent_events(simulated=True) if e.source == "camera-media"]
            assert (await client.get("/api/settings/camera/media")).json()["status"] == "discarded_configuration_changed"
