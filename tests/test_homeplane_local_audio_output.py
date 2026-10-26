"""Actual local adapter code, controlled child substitutes; no audio/native IO."""
import asyncio
import fcntl
import hashlib
import os
import time
import wave

import pytest

from homeplane import local_audio_output as audio
from homeplane.feedback_contracts import PROMPTS
from homeplane.local_audio_output import LocalPromptInstallation, MacLocalOutput, PromptAsset


@pytest.fixture(autouse=True)
def restore_afplay():
    original = audio.AFPLAY
    yield
    audio.AFPLAY = original


class Child:
    def __init__(self, code=0, *, blocked=False, ignore_term=False):
        self.code, self.ignore_term = code, ignore_term
        self.returncode = None
        self.finished = asyncio.Event()
        self.terminated = self.killed = self.waited = 0
        if not blocked:
            self.finished.set()

    async def wait(self):
        await self.finished.wait()
        self.waited += 1
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if not self.ignore_term:
            self.returncode = -15
            self.finished.set()

    def kill(self):
        self.killed += 1
        self.returncode = -9
        self.finished.set()


class Spawn:
    def __init__(self, *children, gate=None):
        self.children = list(children or [Child()])
        self.gate = gate
        self.calls = []
        self.asset_sha256 = []
        self.entered = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        descriptor, = kwargs["pass_fds"]
        self.asset_sha256.append(hashlib.sha256(os.pread(descriptor, 1048577, 0)).hexdigest())
        return self.children.pop(0)


def installation(tmp_path):
    root = tmp_path.resolve()
    binary = root / "INERT-NEVER-EXECUTED-afplay-fixture"
    with os.fdopen(os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700), "wb") as writer:
        writer.write(b"INERT controlled substitute; never executed\n")
    audio.AFPLAY = binary
    path = root / "SIMULATED-fixed-prompt.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 160)
    asset = PromptAsset(path, hashlib.sha256(path.read_bytes()).hexdigest())
    # Synthetic receipt only. This silence fixture cannot qualify prompt content/rooms.
    return LocalPromptInstallation(root, hashlib.sha256(binary.read_bytes()).hexdigest(),
                                   {key: asset for key in PROMPTS}, True, True)


def call(output, *, deadline=None, current=lambda: True):
    return output.play(operation_id="operation-a", room_id="room-a", endpoint_id="mac-a",
                       prompt_id="ack", volume=0.2, deadline_mono=deadline or time.monotonic() + 2,
                       is_current=current)


@pytest.mark.asyncio
async def test_actual_adapter_argv_window_and_zero_exit_are_only_simulated_call_completion(tmp_path):
    child, spawn, windows = Child(), Spawn(), []
    spawn.children = [child]
    receipt = installation(tmp_path)
    output = MacLocalOutput(receipt, spawn=spawn, simulated=True, window_hook=windows.append)
    result = await call(output)
    descriptor, = spawn.calls[0][1]["pass_fds"]
    assert spawn.calls[0][0] == (str(audio.AFPLAY), "-v", "0.200", f"/dev/fd/{descriptor}")
    assert spawn.asset_sha256 == [receipt.prompts["ack"].sha256]
    assert spawn.calls[0][1]["env"] == {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    assert spawn.calls[0][1]["start_new_session"] is True
    assert result.configured and result.attempted and result.completed and result.simulated
    assert result.audibility == "unverified" and result.cleanup == "reaped"
    assert "audible" not in result.json() and child.waited == 1
    assert windows[0].ended_mono is None and windows[-1].ended_mono >= windows[0].started_mono
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert await output.close() == "not_needed"


@pytest.mark.asyncio
async def test_missing_installation_changed_asset_and_unqualified_runner_never_spawn(tmp_path):
    spawn = Spawn()
    result = await call(MacLocalOutput(spawn=spawn, simulated=True))
    assert result.code == "local_output_installation_unqualified" and not result.attempted
    receipt = installation(tmp_path)
    receipt.prompts["ack"].path.write_bytes(b"X" * 100)
    result = await call(MacLocalOutput(receipt, spawn=spawn, simulated=True))
    assert result.code == "local_prompt_changed" and not spawn.calls
    with pytest.raises(ValueError, match="requires_simulated"):
        MacLocalOutput(receipt, spawn=spawn)


@pytest.mark.asyncio
async def test_stop_terminates_then_kills_and_reaps_only_owned_child(tmp_path):
    child = Child(blocked=True, ignore_term=True)
    spawn = Spawn(child)
    output = MacLocalOutput(installation(tmp_path), spawn=spawn, simulated=True, cleanup_timeout=0.01)
    pending = asyncio.create_task(call(output))
    await spawn.entered.wait()
    assert await output.cancel("operation-a") == "reaped"
    result = await pending
    assert result.status == "cancelled" and not result.completed
    assert child.terminated >= 1 and child.killed >= 1 and child.waited == 1
    assert output._active is None


@pytest.mark.asyncio
async def test_deadline_and_failure_visible_and_no_retry(tmp_path):
    child = Child(blocked=True, ignore_term=True)
    spawn = Spawn(child)
    output = MacLocalOutput(installation(tmp_path), spawn=spawn, simulated=True, cleanup_timeout=0.01)
    result = await call(output, deadline=time.monotonic() + 0.06)
    assert result.status == "timeout" and result.cleanup == "reaped" and not result.completed
    assert len(spawn.calls) == 1 and child.killed >= 1
    failed = MacLocalOutput(installation(tmp_path), spawn=Spawn(Child(7)), simulated=True)
    result = await call(failed)
    assert result.status == "failed" and not result.completed and result.audibility == "unverified"


@pytest.mark.asyncio
async def test_cancelled_spawn_late_child_is_owned_reaped_and_never_published_as_completed(tmp_path):
    gate, child = asyncio.Event(), Child(blocked=True)
    spawn = Spawn(child, gate=gate)
    windows = []
    output = MacLocalOutput(installation(tmp_path), spawn=spawn, simulated=True, cleanup_timeout=0.01,
                            window_hook=windows.append)
    pending = asyncio.create_task(call(output))
    await spawn.entered.wait()
    descriptor, = spawn.calls[0][1]["pass_fds"]
    pending.cancel()
    result = await pending
    assert result.status == "cancelled" and result.cleanup == "pending" and not result.completed
    assert result.playback_window.ended_mono is None and windows[-1].outcome == "cleanup_pending"
    assert os.fstat(descriptor).st_nlink == 0, "The late spawn still owns an unlinked asset image."
    gate.set()
    for _ in range(40):
        if output._active is None:
            break
        await asyncio.sleep(0)
    assert child.terminated >= 1 and child.waited == 1 and output._active is None
    assert windows[-1].ended_mono is not None and windows[-1].outcome == "cancelled"
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert await output.close() == "not_needed"


@pytest.mark.asyncio
async def test_late_wait_after_pending_reaper_finalizes_without_close_and_keeps_one_wait(tmp_path):
    class LateChild(Child):
        def terminate(self):
            self.terminated += 1

        def kill(self):
            self.killed += 1  # Even kill completion is held until the owned waiter joins.

    child, clock, windows = LateChild(blocked=True), {"mono": 100.0}, []
    spawn = Spawn(child, Child())
    output = MacLocalOutput(installation(tmp_path), spawn=spawn, simulated=True, cleanup_timeout=0.01,
                            monotonic=lambda: clock["mono"], window_hook=windows.append)
    pending = asyncio.create_task(call(output, deadline=100.05))
    await spawn.entered.wait()
    for _ in range(30):
        if output._active.get("wait"):
            break
        await asyncio.sleep(0)
    clock["mono"] = 100.1
    result = await pending
    entry = output._active
    await entry["cleanup_task"]
    assert result.cleanup == "pending" and result.playback_window.ended_mono is None
    descriptor, = spawn.calls[0][1]["pass_fds"]
    assert os.fstat(descriptor).st_nlink == 0 and fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
    child.returncode = -9
    child.finished.set()
    for _ in range(100):
        if output._active is None:
            break
        await asyncio.sleep(0)
    assert output._active is None and entry["cleanup_task"].result() == "reaped"
    assert child.waited == child.terminated == child.killed == 1
    assert windows[-1].ended_mono is not None and windows[-1].outcome == "timeout" and not result.completed
    with pytest.raises(OSError):
        os.fstat(descriptor)
    subsequent = await call(output, deadline=110.0)
    assert subsequent.completed and subsequent.audibility == "unverified"
    assert await output.close() == "not_needed"


@pytest.mark.asyncio
async def test_hook_asset_replacement_is_rejected_before_dispatch(tmp_path):
    receipt, spawn, windows = installation(tmp_path), Spawn(), []

    def hook(window):
        windows.append(window)
        if window.ended_mono is None:
            with wave.open(str(receipt.prompts["ack"].path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(8000)
                writer.writeframes(b"\x01\x00" * 160)

    output = MacLocalOutput(receipt, spawn=spawn, simulated=True, window_hook=hook)
    result = await call(output)
    assert result.code == "local_prompt_changed" and result.status == "unavailable"
    assert not result.attempted and not result.completed and not spawn.calls
    assert result.cleanup == "not_needed" and output._active is None and windows[-1].ended_mono is not None


@pytest.mark.asyncio
async def test_async_spawn_gap_uses_only_unlinked_readonly_receipt_image(tmp_path):
    receipt, gate, child = installation(tmp_path), asyncio.Event(), Child()
    spawn = Spawn(child, gate=gate)
    output = MacLocalOutput(receipt, spawn=spawn, simulated=True)
    pending = asyncio.create_task(call(output))
    await spawn.entered.wait()
    descriptor, = spawn.calls[0][1]["pass_fds"]
    assert os.fstat(descriptor).st_nlink == 0 and fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
    with wave.open(str(receipt.prompts["ack"].path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x01\x00" * 160)
    assert hashlib.sha256(receipt.prompts["ack"].path.read_bytes()).hexdigest() != receipt.prompts["ack"].sha256
    gate.set()
    result = await pending
    assert result.completed and result.audibility == "unverified"
    assert spawn.asset_sha256 == [receipt.prompts["ack"].sha256], "The effect consumes the receipt's fixed bytes."
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert output._active is None and await output.close() == "not_needed"


@pytest.mark.asyncio
async def test_expired_or_revoked_output_and_muted_volume_never_start(tmp_path):
    spawn = Spawn()
    output = MacLocalOutput(installation(tmp_path), spawn=spawn, simulated=True)
    assert (await call(output, deadline=time.monotonic() - 1)).status == "cancelled"
    assert (await call(output, current=lambda: False)).status == "cancelled"
    muted = await output.play(operation_id="operation-a", room_id="room-a", endpoint_id="mac-a",
                              prompt_id="ack", volume=0, deadline_mono=time.monotonic() + 2,
                              is_current=lambda: True)
    assert muted.status == "quiet" and not muted.attempted
    assert not spawn.calls


@pytest.mark.asyncio
async def test_window_hook_revision_change_fences_actual_spawn_and_closes_unplayed_window(tmp_path):
    state, spawn, windows = {"current": True}, Spawn(), []

    def hook(window):
        windows.append(window)
        state["current"] = False

    output = MacLocalOutput(installation(tmp_path), spawn=spawn, simulated=True, window_hook=hook)
    result = await call(output, current=lambda: state["current"])
    assert result.status == "cancelled" and result.configured and not result.attempted and not result.completed
    assert result.cleanup == "not_needed" and not spawn.calls and output._active is None
    assert windows[-1].ended_mono is not None and windows[-1].outcome == "cancelled"
    assert await output.close() == "not_needed"
