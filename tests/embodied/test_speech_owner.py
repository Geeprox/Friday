"""Owned speech with actual HTTP/parser/producer paths and silent transports."""

import asyncio
import json
import os
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from homeplane.embodied import airplay, speech, synthesis
from homeplane.embodied.speech_http import AsyncHA
from homeplane.werewolf_effects import HostEffects
from homeplane.werewolf_service import Join, WerewolfService
from tests.test_werewolf_service import SilentLights


@pytest.fixture
def rig(tmp_path, monkeypatch):
    config = dict(
        speech_output="panel",
        speaker_notify="notify.synthetic",
        speaker_entity="media_player.synthetic",
        viewer_host="127.0.0.1",
        viewer_port=12345,
    )
    for module in (speech, synthesis, airplay):
        monkeypatch.setattr(module, "PRIVATE", tmp_path)
        monkeypatch.setattr(module, "configuration", lambda: dict(config))
    monkeypatch.setattr(speech, "VOICE_PAUSED", tmp_path / "voice-paused")
    monkeypatch.setattr(synthesis, "DATA", tmp_path)
    monkeypatch.setattr(synthesis, "_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(airplay, "_play_lock", asyncio.Lock())
    session = tmp_path / "ha-session.json"
    session.write_text(
        json.dumps(
            dict(
                ha_url="http://ha.invalid",
                client_id="SYNTHETIC",
                access_token="SYNTHETIC-access",
                refresh_token="SYNTHETIC-refresh",
                expires_at=time.time() + 3600,
            )
        )
    )
    session.chmod(0o600)
    (tmp_path / "viewer-key").write_text("SYNTHETIC-viewer-key")
    state = SimpleNamespace(
        config=config,
        private=tmp_path,
        session=session,
        calls=[],
        hook=None,
        states=deque(),
        synth=0,
        audio=tmp_path / "SYNTHETIC.m4a",
    )

    async def handle(request):
        state.calls.append(request)
        if state.hook:
            result = await state.hook(request)
            if result is not None:
                return result
        if request.url.path == "/auth/token":
            return httpx.Response(200, json=dict(access_token="SYNTHETIC-fresh", expires_in=3600))
        if request.url.path.startswith("/api/states/"):
            value = state.states.popleft() if state.states else "idle"
            return httpx.Response(200, json=dict(state=value, attributes=dict(volume_level=0.01)))
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handle)

    def speaker():
        owner = speech.Speaker()
        owner.ha.request = Mock(side_effect=AssertionError("speech may not queue synchronous HA work"))
        owner.ha.asynchronous = AsyncHA(
            lambda: tmp_path,
            client_factory=lambda **options: httpx.AsyncClient(transport=transport, **options),
        )
        return owner

    async def synth(text, **options):
        state.synth += 1
        state.audio.write_bytes(b"SYNTHETIC audio")
        return state.audio, "SYNTHETIC"

    state.speaker = speaker
    state.synthesizer = synth
    return state


@pytest.mark.asyncio
async def test_notify_uses_cancellable_transport_and_never_executor_sends(rig, monkeypatch):
    monkeypatch.setattr(asyncio, "to_thread", Mock(side_effect=AssertionError("unowned thread")))
    owner = rig.speaker()
    result = await owner.say("合成主持语句")
    assert result["status"] == "accepted" and result["physical_audio_verified"] is False
    assert [r.url.path for r in rig.calls] == ["/api/services/notify/send_message"]
    assert json.loads(rig.calls[0].content)["entity_id"] == "notify.synthetic"
    owner.ha.request.assert_not_called()


@pytest.mark.asyncio
async def test_expired_scope_never_starts_an_output(rig):
    assert (await rig.speaker().say("合成主持语句", current=lambda: False))["status"] == "expired"
    assert not rig.calls


@pytest.mark.asyncio
async def test_revocation_while_waiting_for_transport_lock_cannot_send_later(rig):
    owner = rig.speaker()
    current = [True]
    await owner.ha.asynchronous.lock.acquire()
    try:
        task = asyncio.create_task(owner.say("合成主持语句", current=lambda: current[0]))
        await asyncio.sleep(0.02)
        current[0] = False
        assert (await asyncio.wait_for(task, 1))["status"] == "expired"
    finally:
        owner.ha.asynchronous.lock.release()
    await asyncio.sleep(0.02)
    assert not rig.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["scope", "selection", "oauth"])
async def test_no_device_send_after_source_selection_or_login_changes_during_refresh(rig, change):
    raw = json.loads(rig.session.read_text())
    raw["expires_at"] = 0
    rig.session.write_text(json.dumps(raw))
    before = rig.session.read_bytes()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(request):
        if request.url.path == "/auth/token":
            entered.set()
            await release.wait()

    rig.hook = hook
    current = [True]
    task = asyncio.create_task(rig.speaker().say("合成主持语句", current=lambda: current[0]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "scope":
            current[0] = False
        elif change == "selection":
            rig.config["speaker_notify"] = "notify.other"
        else:
            rig.session.write_text(json.dumps({**raw, "access_token": "SYNTHETIC-new-login"}))
        expected_session = rig.session.read_bytes()
        release.set()
        assert (await asyncio.wait_for(task, 1))["status"] == "expired"
        assert [r.url.path for r in rig.calls] == ["/auth/token"]
        assert rig.session.read_bytes() == expected_session
        if change != "oauth":
            assert expected_session == before
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_send_retains_output_slot_until_actual_transport_cleanup(rig):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hook(request):
        if not entered.is_set():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise

    rig.hook = hook
    task = asyncio.create_task(rig.speaker().say("合成主持语句"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await rig.speaker().say("下一句"))["status"] == "busy"
        assert len(rig.calls) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.02)
    assert (await rig.speaker().say("下一句"))["status"] == "accepted"


@pytest.mark.asyncio
async def test_oversize_device_response_is_bounded_and_not_reported_as_completed(rig):
    async def hook(request):
        return httpx.Response(200, content=b"x" * 262145)

    rig.hook = hook
    result = await rig.speaker().say("合成主持语句")
    assert result["status"] == "unconfirmed" and result["error_class"] == "ValueError"
    assert "http" not in str(result) and "合成主持语句" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [True, False])
async def test_media_rechecks_occupancy_after_tts_and_never_changes_volume(rig, monkeypatch, busy):
    rig.config["speech_output"] = "media_player"
    rig.states.extend(["idle", "playing" if busy else "idle", "playing", "idle"])
    monkeypatch.setattr(synthesis, "synthesize", rig.synthesizer)
    result = await rig.speaker().say("合成主持语句")
    assert result["status"] == ("busy" if busy else "accepted")
    writes = [r.url.path for r in rig.calls if r.method == "POST"]
    assert writes == ([] if busy else ["/api/services/media_player/play_media"])
    assert not rig.audio.exists()


@pytest.mark.asyncio
async def test_homepod_stream_is_owned_and_revoked_without_optional_runtime(rig, monkeypatch):
    target = rig.private / "homepod.json"
    target.write_text(json.dumps(dict(host="192.168.50.123", identifier="SYNTHETIC-homepod")))
    target.chmod(0o600)
    rig.config["speech_output"] = "homepod"
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def stream(path):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    device = SimpleNamespace(stream=SimpleNamespace(stream_file=stream))
    closed = []

    @asynccontextmanager
    async def receiver():
        try:
            yield device
        finally:
            closed.append(True)

    monkeypatch.setattr(airplay, "_receiver", receiver)
    monkeypatch.setattr(airplay, "status", lambda: dict(configured=True, dependency_available=True))
    monkeypatch.setattr(airplay, "_availability", AsyncMock(return_value="ready"))
    monkeypatch.setattr(synthesis, "synthesize", rig.synthesizer)
    current = [True]
    task = asyncio.create_task(rig.speaker().say("合成主持语句", current=lambda: current[0]))
    await asyncio.wait_for(entered.wait(), 1)
    current[0] = False
    result = await asyncio.wait_for(task, 1)
    assert result["status"] == "unconfirmed" and cancelled.is_set() and closed == [True]
    assert not rig.audio.exists()


@pytest.mark.asyncio
async def test_late_native_spawn_is_reaped_before_its_temporary_files_are_removed(rig, monkeypatch):
    original = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def spawn(*command, **options):
        entered.set()
        await release.wait()
        process = await original(
            sys.executable,
            "-B",
            "-c",
            "import sys,time;from pathlib import Path;Path(sys.argv[1]).write_bytes(b'SYNTHETIC');time.sleep(30)",
            command[4],
            **options,
        )
        processes.append(process)
        deadline = time.monotonic() + 2
        while not Path(command[4]).exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(synthesis.synthesize("合成主持语句"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert synthesis._SLOTS.acquire(blocking=False) is False
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while (
        not processes or processes[0].returncode is None or list((rig.private / "speech").glob("reply-*"))
    ) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert processes and processes[0].returncode is not None
    assert not list((rig.private / "speech").glob("reply-*"))
    assert synthesis._SLOTS.acquire(blocking=False)
    synthesis._SLOTS.release()


@pytest.mark.asyncio
async def test_synthesis_does_not_delete_an_old_file_it_does_not_own(rig, monkeypatch):
    folder = rig.private / "speech"
    folder.mkdir(mode=0o700)
    old = folder / "reply-unowned.aiff"
    old.write_bytes(b"keep")
    os.utime(old, (0, 0))
    monkeypatch.setattr(synthesis.secrets, "token_hex", lambda _: "a" * 32)
    collision = folder / ("reply-" + "a" * 32 + ".wav")
    collision.write_bytes(b"keep")
    with pytest.raises(ValueError, match="speech_temporary_collision"):
        await synthesis.synthesize("合成主持语句")
    assert old.read_bytes() == collision.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_another_process_holds_the_same_speech_output_slot(rig):
    marker = rig.private / "lock-held"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-c",
        "import fcntl,sys,time;from pathlib import Path;"
        "f=open(sys.argv[1],'a+');fcntl.flock(f,fcntl.LOCK_EX);Path(sys.argv[2]).touch();time.sleep(30)",
        str(rig.private / "speech-output.lock"),
        str(marker),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert marker.exists()
        # The inert lock holder follows the production private-file contract.
        (rig.private / "speech-output.lock").chmod(0o600)
        assert (await rig.speaker().say("合成主持语句"))["status"] == "busy"
        assert not rig.calls
    finally:
        process.terminate()
        await process.wait()
    assert (await rig.speaker().say("合成主持语句"))["status"] == "accepted"


@pytest.mark.asyncio
async def test_native_tts_gil_stall_is_killed_reaped_and_does_not_block_parent(rig, monkeypatch):
    original = asyncio.create_subprocess_exec
    marker = rig.private / "native-entered"
    processes = []

    async def spawn(*command, **options):
        process = await original(
            sys.executable,
            "-B",
            "-c",
            "import ctypes,signal,sys;from pathlib import Path;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(sys.argv[1]).touch();ctypes.PyDLL(None).sleep(30)",
            str(marker),
            **options,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(synthesis.synthesize("合成主持语句"))
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert marker.exists()
        task.cancel()
        heartbeats = 0
        while not task.done() and heartbeats < 100:
            heartbeats += 1
            await asyncio.sleep(0.01)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert heartbeats >= 2 and processes[0].returncode is not None
        assert not list((rig.private / "speech").glob("reply-*"))
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_fast_phase_changes_do_not_cancel_prior_output_cleanup_twice():
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, repeated = [], []

    async def speak(text, *, current):
        assert current()
        calls.append(text)
        if len(calls) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    repeated.append(True)
                    raise
                raise
        return dict(status="accepted")

    effects = HostEffects(
        SilentLights(), speak=speak, speech_status=lambda: dict(quiet=False, configured=True)
    )
    service = WerewolfService(effects)
    try:
        await service.create(6, narration=True)
        service.pump.cancel()
        await asyncio.gather(service.pump, return_exceptions=True)
        service.pump = None
        for seat in range(1, 7):
            joined = await service.join(Join(code=service.join_code, seat=seat, name=f"合成玩家{seat}"))
            await service.ready(joined["token"], True)
        await service.control("start")
        await asyncio.wait_for(entered.wait(), 1)
        await service.control("pause")
        await asyncio.wait_for(cleaning.wait(), 1)
        await service.control("resume")
        await asyncio.sleep(0.01)
        await service.control("advance")
        await asyncio.sleep(0.01)
        assert not repeated and len(calls) == 1
        expected = service.game.public()["host_script"]["text"]
        release.set()
        await asyncio.wait_for(asyncio.shield(service.effect_task), 1)
        assert calls[-1] == expected and len(calls) == 2 and not repeated
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected", [("unconfirmed", "unconfirmed"), ("expired", "cancelled"), ("muted", "paused")]
)
async def test_safety_retains_uncertain_output_without_claiming_success(tmp_path, status, expected):
    from homeplane.embodied.safety import SafetyPolicy, SafetyRuntime, SafetyStore

    store = SafetyStore(tmp_path / "safety.sqlite")
    policy = store.configure(SafetyPolicy(enabled=True, announce=True))
    runtime = SafetyRuntime(store, speaker=SimpleNamespace(say=AsyncMock(return_value={"status": status})))
    event = store.create(
        dict(
            kind="fall",
            severity="test",
            simulated=True,
            status="test",
            camera=None,
            session=None,
            track=None,
            person_id=None,
            person_name=None,
            message="合成提醒",
            basis="synthetic",
            detector=None,
        )
    )
    try:
        await runtime.announce(event, revision=policy.revision)
        receipt = store.event(event["id"])["announcement"]
        assert receipt == {"status": expected, "physical_audio_confirmed": False}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_completed_audio_is_removed_if_cancellation_wins_before_handoff(rig, monkeypatch):
    consumer = None

    async def produce(*args, **kwargs):
        rig.audio.write_bytes(b"SYNTHETIC")
        asyncio.get_running_loop().call_soon(consumer.cancel)
        return rig.audio, "SYNTHETIC"

    monkeypatch.setattr(synthesis, "_synthesize", produce)
    consumer = asyncio.create_task(synthesis.synthesize("合成主持语句"))
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert not rig.audio.exists()
