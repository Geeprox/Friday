"""Explicit SIMULATED lifecycle/faults, plus installed source/decoder checks.

No household SDK client/authentication/camera runs in these tests.
"""
import asyncio
import base64
import hashlib
import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from media_fixture import packets

from homeplane.media_contracts import NativeMediaConnection
from homeplane.media_probe import MediaProbe
from homeplane.native_media_worker import (
    DB,
    PROJECT,
    SDK_HOME,
    CameraReadSDK,
    NativeCounter,
    ProducerImportFence,
    qualify_sdk,
    read_camera,
    read_login,
)
from homeplane.settings import SettingsService


def test_pinned_installed_sdk_is_not_imported_and_manifest_change_is_rejected(tmp_path, monkeypatch):
    assert "miot.client" not in sys.modules
    if not SDK_HOME.exists():
        pytest.skip("Conditional installed SDK absent")
    assert qualify_sdk()
    manifest = json.loads((PROJECT / "homeplane/native_media_sdk.json").read_text())
    manifest["files"]["client.py"] = "0" * 64
    (tmp_path / "homeplane").mkdir()
    (tmp_path / "homeplane/native_media_sdk.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("homeplane.native_media_worker.PROJECT", tmp_path)
    assert not qualify_sdk()
    assert "miot.client" not in sys.modules


def test_producer_fence_denies_model_and_agent_imports():
    fence = ProducerImportFence()
    for name in ["miloco.manager", "miloco.dispatch", "torch", "mlx.core", "homeplane.arl", "openai"]:
        with pytest.raises(ImportError, match="producer_import_denied"):
            fence.find_spec(name)
    assert fence.find_spec("miot.camera") is None
    with pytest.raises(ImportError):
        fence.find_spec("miot.client")
    sys.meta_path.insert(0, fence)
    try:
        with pytest.raises(ImportError):
            importlib.import_module("miloco.manager")
    finally:
        sys.meta_path.remove(fence)


def login_database(path, expires=100000):
    connection = sqlite3.connect(path)
    connection.execute("create table kv(key text,value text)")
    connection.executemany("insert into kv values (?,?)", [
        ("DEVICE_UUID_KEY", "SIMULATED-uuid"),
        ("MIOT_TOKEN_INFO_KEY", json.dumps({"access_token": "SIMULATED-private-token", "refresh_token": "SIMULATED-refresh", "expires_ts": expires})),
        ("PERCEPTION_ENABLED_KEY", "true"),
        ("SIMULATED_OLD_RULE", "must-remain")])
    connection.commit()
    connection.close()
    path.chmod(0o600)


def test_login_read_is_query_only_bounded_and_preserves_native_rules(tmp_path):
    path = tmp_path / "auth.db"
    login_database(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    uuid, login = read_login(path, now=lambda: 1000)
    assert uuid == "SIMULATED-uuid" and login["access_token"] == "SIMULATED-private-token"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_cold_readonly_wal_snapshot_precedes_media_file_write_restriction(tmp_path):
    path = tmp_path / "SIMULATED-cold-wal.db"
    login_database(path)
    connection = sqlite3.connect(path)
    connection.execute("pragma journal_mode=wal")
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    script = r'''
import json,os,resource,sys
from pathlib import Path
from homeplane.native_media_worker import read_login
os.umask(0o077)
uuid,login=read_login(Path(sys.argv[1]),now=lambda:1000)
resource.setrlimit(resource.RLIMIT_FSIZE,(0,0))
print(json.dumps({'simulated':True,'uuid_ok':uuid=='SIMULATED-uuid','snapshot_read':bool(login['access_token'])}))
'''
    result = subprocess.run([sys.executable, "-c", script, str(path)], text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"simulated": True, "uuid_ok": True, "snapshot_read": True}
    assert "private-token" not in result.stdout and hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not Path(str(path) + "-shm").exists() or Path(str(path) + "-shm").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="account_token_expired"):
        read_login(path, now=lambda: 99980)
    linked = tmp_path / "link.db"
    linked.symlink_to(path)
    with pytest.raises(ValueError):
        read_login(linked, now=lambda: 1000)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_login(path, now=lambda: 1000)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


class SimulatedCamera:
    def __init__(self):
        self.callbacks = {}
        self.starts = []
    async def register_raw_video_async(self, callback, **options):
        self.callbacks["raw_video"] = callback
        assert options == {"channel": 0, "multi_reg": True}
    async def register_raw_audio_async(self, callback, **options):
        self.callbacks["raw_audio"] = callback
        assert options == {"channel": 0, "multi_reg": True}
    async def register_status_changed_async(self, callback, **options):
        self.callbacks["status"] = callback
        assert options == {"multi_reg": True}
    async def register_decode_video_frame_async(self, callback, **options):
        self.callbacks["video"] = callback
        assert options == {"channel": 0, "multi_reg": True}
    async def register_decode_audio_frame_async(self, callback, **options):
        self.callbacks["audio"] = callback
        assert options == {"channel": 0, "multi_reg": True}
    async def start_async(self, **options):
        self.starts.append(options)
        did = "SIMULATED-camera"
        for ts in (100, 90):
            await self.callbacks["raw_video"](did, b"SIMULATED-encoded", ts, 1, 0)
            await self.callbacks["raw_audio"](did, b"SIMULATED-encoded", ts, 1, 0, SimpleNamespace(name="AUDIO_OPUS"))
            await self.callbacks["video"](did, SimpleNamespace(shape=(120, 160, 3), dtype="uint8"), ts, 0, 1000, 1010)
            await self.callbacks["audio"](did, SimpleNamespace(shape=(320,), dtype="int16"), ts, 0, 1000, 1010)


class SimulatedSDK:
    def __init__(self, *, missing=False, offline=False, channels=1):
        self.instance = SimulatedCamera()
        self.info = SimpleNamespace(online=not offline, is_set_pincode=0, channel_count=channels)
        self.missing = missing
        self.calls = []
    async def init_async(self):
        self.calls.append("init")
    async def get_cameras_async(self):
        self.calls.append("read_inventory")
        return {} if self.missing else {"SIMULATED-camera": self.info, "SIMULATED-other": self.info}
    async def create_camera_instance_async(self, camera, **options):
        assert camera is self.info and options == {"frame_interval": 1000, "enable_hw_accel": False}
        self.calls.append("create_selected_camera")
        return self.instance
    async def deinit_async(self):
        self.calls.append("deinit")
    def __getattr__(self, name):
        raise AssertionError("Unexpected SDK surface " + name)


async def immediate(_):
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sdk_adapter_reads_selected_did_only_and_closes_both_primitives():
    calls = []
    class Http:
        async def get_devices_with_dids_async(self, dids):
            assert dids == ["SIMULATED-camera"]
            calls.append("selected_metadata")
            return {"SIMULATED-camera": SimpleNamespace(model="SIMULATED.camera.model", model_dump=lambda: {"model": "SIMULATED.camera.model"})}
        async def deinit_async(self):
            calls.append("http_closed")
    class Camera:
        async def init_async(self, **options):
            assert options == {"frame_interval": 1000, "enable_hw_accel": False}
            calls.append("native_initialized")
        async def create_camera_async(self, **options):
            calls.append("camera_created")
            return options
        async def deinit_async(self):
            calls.append("native_closed")
    async def extra():
        return SimpleNamespace(extra_info={"SIMULATED.camera.model": SimpleNamespace(channel_count=1)})
    async def classify(model, metadata):
        assert model == "SIMULATED.camera.model"
        return True
    client = CameraReadSDK("SIMULATED-camera", Http(), Camera(), extra, classify, SimpleNamespace, "SIMULATED-disconnected")
    await client.init_async()
    info = (await client.get_cameras_async())["SIMULATED-camera"]
    assert info.channel_count == 1 and info.camera_status == "SIMULATED-disconnected"
    await client.create_camera_instance_async(info, frame_interval=1000, enable_hw_accel=False)
    await client.deinit_async()
    assert calls == ["native_initialized", "selected_metadata", "camera_created", "native_closed", "http_closed"]


@pytest.mark.asyncio
async def test_native_lifecycle_uses_one_camera_no_recording_reconnect_or_producers():
    client = SimulatedSDK()
    report = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera", seconds=1.0), client,
        codec_quality="SIMULATED-LOW", sleep=immediate)
    report.simulated = True
    assert client.calls == ["init", "read_inventory", "create_selected_camera", "deinit"]
    assert client.instance.starts == [{"qualities": "SIMULATED-LOW", "enable_audio": True, "enable_reconnect": False, "enable_record": False}]
    assert report.status == "decoded_sync_unknown" and report.provider == "miot_sdk"
    assert report.video.codec is None  # Installed video callback does not expose codec.
    assert report.audio.pcm_16k_samples == 640 and report.audio.pcm_format == "mono_s16_16000"
    assert report.audio.sample_rate is None and report.audio.channels is None
    assert report.video.source_clock_discontinuities == report.audio.source_clock_discontinuities == 1
    assert report.audio.source_timestamps_seen == 2 and report.alignment_error_bound_ms is None
    assert report.alignment_reason == "sdk_source_timestamps_clock_domain_unverified"
    assert not report.device_write and not report.media_persisted and not report.model_invoked


@pytest.mark.parametrize("options,failure", [({"missing": True}, "camera_not_found"), ({"offline": True}, "camera_offline"), ({"channels": 2}, "sdk_not_qualified")])
@pytest.mark.asyncio
async def test_native_inventory_failure_never_starts_a_camera(options, failure):
    client = SimulatedSDK(**options)
    report = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera"), client,
        codec_quality="SIMULATED-LOW", sleep=immediate)
    assert report.status == "failed" and report.failure == failure
    assert client.calls == ["init", "read_inventory", "deinit"] and not client.instance.starts


@pytest.mark.asyncio
async def test_native_callback_scope_budgets_and_late_frames():
    counter = NativeCounter("SIMULATED-camera")
    await counter.video("SIMULATED-other", SimpleNamespace(shape=(120, 160, 3), dtype="uint8"), 100, 0, 1, 1)
    await counter.audio("SIMULATED-camera", SimpleNamespace(shape=(320,), dtype="int16"), 100, 1, 1, 1)
    assert counter.report.video.decoded_frames == counter.report.audio.decoded_frames == 0
    await counter.raw_audio("SIMULATED-camera", b"x" * (1024 * 1024 + 1), 1, 1, 0, SimpleNamespace(name="AUDIO_OPUS"))
    assert counter.finish().failure == "input_budget"
    await counter.video("SIMULATED-camera", SimpleNamespace(shape=(120, 160, 3), dtype="uint8"), 100, 0, 1, 1)
    assert counter.report.video.decoded_frames == 0


@pytest.mark.asyncio
async def test_disconnected_or_idle_partial_decodes_cannot_qualify():
    for failure in ("stream_closed", "stream_idle"):
        now = [0.0]
        counter = NativeCounter("SIMULATED-camera", clock=lambda: now[0])
        await counter.raw_video("SIMULATED-camera", b"SIMULATED", 1, 1, 0)
        await counter.raw_audio("SIMULATED-camera", b"SIMULATED", 1, 1, 0, SimpleNamespace(name="AUDIO_OPUS"))
        await counter.video("SIMULATED-camera", SimpleNamespace(shape=(120, 160, 3), dtype="uint8"), 1, 0, 1, 1)
        await counter.audio("SIMULATED-camera", SimpleNamespace(shape=(320,), dtype="int16"), 1, 0, 1, 1)
        if failure == "stream_closed":
            await counter.status("SIMULATED-camera", SimpleNamespace(name="DISCONNECTED"))
        else:
            now[0] = 2.0
        assert counter.finish().status == "failed" and counter.report.failure == failure


@pytest.mark.asyncio
async def test_cancel_closes_sdk_and_does_not_return_partial_success():
    client = SimulatedSDK()
    waiting = asyncio.Event()
    async def hang(_):
        waiting.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(read_camera(NativeMediaConnection(camera_id="SIMULATED-camera"), client,
        codec_quality="SIMULATED-LOW", sleep=hang))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.calls[-1] == "deinit"


@pytest.mark.asyncio
async def test_sampling_window_starts_after_native_first_packet_not_start_return():
    client = SimulatedSDK()
    ready, sample = asyncio.Event(), []
    old_start = client.instance.start_async
    async def cold_start(**options):
        async def later():
            await asyncio.sleep(.1)
            await old_start(**options)
            ready.set()
        asyncio.create_task(later())
    client.instance.start_async = cold_start
    async def sampling(seconds):
        sample.append((ready.is_set(), seconds))
    result = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera", seconds=1.0), client,
        codec_quality="SIMULATED-LOW", sleep=sampling)
    assert result.status == "decoded_sync_unknown" and sample == [(True, 1.0)]
    assert client.calls[-1] == "deinit"


@pytest.mark.asyncio
async def test_parent_native_launch_is_fixed_and_contains_no_credential(tmp_path, monkeypatch):
    settings = SettingsService(tmp_path / "settings.json")
    settings.update({"camera_id": "SIMULATED-camera", "camera_media_source": "miot_sdk"})
    verifier = MediaProbe(settings, simulated=True)
    calls = []
    async def launch(*args, **kwargs):
        calls.append((args, kwargs))
        return "SIMULATED-process"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    verifier.report.provider = "miot_sdk"
    assert await verifier._launch() == "SIMULATED-process"
    args, options = calls[0]
    assert args == (str(SDK_HOME / "bin/python"), "-B", "-I", str(PROJECT / "homeplane/native_media_worker.py"))
    assert options["stderr"] == asyncio.subprocess.DEVNULL
    assert "PYTHONPATH" not in options["env"] and "MILOCO_HOME" not in options["env"]
    assert not any("token" in str(arg) for arg in args)
    assert settings.config.camera_service_url is None


@pytest.mark.asyncio
async def test_native_callback_exceptions_are_redacted_and_do_not_qualify():
    client = SimulatedSDK()
    old = client.instance.start_async
    handler = sys.unraisablehook
    async def broken(**options):
        await old(**options)
        sys.unraisablehook(SimpleNamespace(exc_value=ValueError("SIMULATED-private-native-payload")))
    client.instance.start_async = broken
    report = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera"), client,
        codec_quality="SIMULATED-LOW", sleep=immediate)
    assert report.status == "failed" and report.failure == "protocol_error"
    assert "private-native-payload" not in report.model_dump_json()
    assert sys.unraisablehook is handler and client.calls[-1] == "deinit"


@pytest.mark.asyncio
async def test_sigterm_asks_sdk_to_close_and_parent_reaps_only_owned_worker(tmp_path):
    script = r'''
import asyncio,json,sys
from pathlib import Path
import homeplane.native_media_worker as worker
async def probe(config):
    try:
        print('SIMULATED-ready',flush=True)
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(.15)
        Path(sys.argv[1]).write_text('SIMULATED-owned-SDK-closed')
worker.probe=probe
result=asyncio.run(worker.cancellable_probe(worker.NativeMediaConnection(camera_id='SIMULATED-camera')))
print(result.model_dump_json(),flush=True)
'''
    closed = tmp_path / "SIMULATED-closed.txt"
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, str(closed), stdout=asyncio.subprocess.PIPE)
    assert (await asyncio.wait_for(process.stdout.readline(), 3)).strip() == b"SIMULATED-ready"
    process.terminate()
    output = await asyncio.wait_for(process.communicate(), 4)
    assert process.returncode == 0 and closed.read_text() == "SIMULATED-owned-SDK-closed"
    assert json.loads(output[0])["failure"] == "stream_closed"


def test_installed_decoder_callback_shapes_using_explicit_simulated_media():
    """Actual pinned SDK decoder; no MIoTClient/native camera/auth network."""
    if not SDK_HOME.exists():
        pytest.skip("Conditional installed SDK absent")
    video, audio = packets()
    payload = json.dumps({"video": [base64.b64encode(item[0]).decode() for item in video[:8]],
        "audio": [base64.b64encode(item).decode() for item in audio[:8]]})
    script = r'''
import asyncio,base64,json,sys
sys.path.insert(0,sys.argv[1])
from homeplane.native_media_worker import ProducerImportFence,NativeCounter
sys.meta_path.insert(0,ProducerImportFence())
from miot.decoder import MIoTMediaDecoder
from miot.types import MIoTCameraFrameData,MIoTCameraCodec
async def run():
    counter=NativeCounter('SIMULATED-camera')
    async def ignore(*args):pass
    async def video(frame,ts,ch,recv,decoded):await counter.video('SIMULATED-camera',frame,ts,ch,recv,decoded)
    async def audio(frame,ts,ch,recv,decoded):await counter.audio('SIMULATED-camera',frame,ts,ch,recv,decoded)
    decoder=MIoTMediaDecoder(1000,ignore,ignore,video,audio,enable_hw_accel=False,enable_audio=True)
    payload=json.load(sys.stdin)
    for kind,codec in [('video',MIoTCameraCodec.VIDEO_H264),('audio',MIoTCameraCodec.AUDIO_OPUS)]:
        for i,item in enumerate(payload[kind]):
            frame=MIoTCameraFrameData(codec_id=codec,data=base64.b64decode(item),length=len(base64.b64decode(item)),timestamp=100+i,sequence=i,frame_type=0,channel=0)
            if kind=='video':await counter.raw_video('SIMULATED-camera',frame.data,frame.timestamp,i,0)
            else:await counter.raw_audio('SIMULATED-camera',frame.data,frame.timestamp,i,0,codec)
            (decoder._on_video_callback if kind=='video' else decoder._on_audio_callback)(frame)
    await asyncio.sleep(.1)
    result=counter.finish();result.simulated=True
    print(result.model_dump_json())
asyncio.run(run())
'''
    result = subprocess.run([str(SDK_HOME / "bin/python"), "-I", "-c", script, str(PROJECT)],
        input=payload, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["simulated"] and report["status"] == "decoded_sync_unknown"
    assert report["video"]["width"] == 160 and report["audio"]["pcm_16k_samples"] > 1000
    assert "miloco.manager" not in sys.modules and not report["model_invoked"]
    assert DB.name == "miloco.db"  # No auth DB was read by the fixture subprocess.
