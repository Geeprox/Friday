"""Review owners keep their file lease until delayed child handles are reaped."""

import asyncio
import fcntl
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied import sound_model, temporal_model


class InertProcess:
    """A silent pipe/exit stand-in; never opens a camera or loads a model."""

    def __init__(self, kind, *, delayed_exit=False):
        self.kind = kind
        self.returncode = None
        self.delayed_exit = delayed_exit
        self.exited = asyncio.Event()
        self.terminated = asyncio.Event()
        self.waiting = asyncio.Event()
        self.stdin = self.stdout = self
        self.reads = 0
        self.requests = 0
        self.input_closed = False

    def write(self, data):
        self.requests += 1

    async def drain(self):
        pass

    async def readline(self):
        self.reads += 1
        if self.kind == "sound":
            result = (
                dict(type="ready", version=1, sample_rate=16000, known_classifications=["baby_crying"])
                if self.reads == 1
                else dict(type="result", version=1, id=self.requests, windows=[
                    dict(offset=0, duration=2, scores={"baby_crying": .95}, competing_confidence=.01)
                ])
            )
        else:
            result = (dict(version=1,ready=True) if self.reads==1 else
                      dict(version=1, result=dict(event="none", observation="unchanged"), peak_mlx_bytes=0))
        return json.dumps(result).encode() + b"\n"

    async def read(self, limit):
        await self.exited.wait()
        return b""

    def terminate(self):
        self.terminated.set()
        if not self.delayed_exit:
            self.finish()

    def kill(self):
        self.terminate()

    def finish(self):
        self.returncode = 0
        self.exited.set()

    async def wait(self):
        self.waiting.set()
        await self.exited.wait()
        return self.returncode

    def close(self):
        self.input_closed = True


def make_owner(kind, private, monkeypatch):
    if kind == "sound":
        binary = private / "silent-worker-reference"
        binary.write_text("Inert test reference; never executed")
        binary.chmod(0o700)
        monkeypatch.setattr(sound_model.platform, "system", lambda: "Darwin")
        owner = sound_model.AppleSoundProcess(
            binary, private,
            budget=lambda: SimpleNamespace(total_bytes=16 * 1024**3, free_percent=50, load=0, cores=4),
        )
        return owner, bytes(64000), private / "sound-analysis.lock"
    monkeypatch.setattr(temporal_model, "model_readiness", lambda model: dict(available=True))
    owner = temporal_model.MlxWindowProcess(
        private, private, budget=lambda: temporal_model.HostBudget(16 * 1024**3, 50)
    )
    return owner, [(float(i), b"synthetic-frame") for i in range(4)], private / "temporal-model.lock"


def available(path):
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        os.close(descriptor)


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sound", "visual"])
async def test_cancelled_start_keeps_lease_until_late_handle_is_reaped(tmp_path, monkeypatch, kind):
    owner, request, lease = make_owner(kind, tmp_path, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    late = InertProcess(kind)
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            started.set()
            # OS creation may already have happened before Python receives its
            # handle. Cancellation cannot make that child cease to exist.
            await asyncio.shield(release.wait())
            return late
        return InertProcess(kind)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    work = asyncio.create_task(owner.analyze(request))
    try:
        await asyncio.wait_for(started.wait(), .5)
        work.cancel()
        await settle()
        work.cancel()
        await settle()
        assert not available(lease), "a delayed child lost its exclusive model lease"
        with pytest.raises(ValueError, match="busy"):
            await asyncio.wait_for(owner.analyze(request), .5)
        assert len(calls) == 1
        release.set()
        await asyncio.wait_for(late.terminated.wait(), .5)
        await owner.close()
        assert late.returncode == 0 and available(lease)
        await owner.analyze(request)
        assert len(calls) == 2
    finally:
        release.set()
        late.finish()
        await asyncio.gather(work, return_exceptions=True)
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sound", "visual"])
async def test_cancelled_close_keeps_lease_until_actual_exit(tmp_path, monkeypatch, kind):
    owner, request, lease = make_owner(kind, tmp_path, monkeypatch)
    process = InertProcess(kind, delayed_exit=True)

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await owner.analyze(request)
    closing = asyncio.create_task(owner.close())
    try:
        await asyncio.wait_for(process.waiting.wait(), .5)
        closing.cancel()
        await settle()
        closing.cancel()
        await settle()
        assert process.returncode is None
        assert not available(lease), "model lease was released before its child exited"
        process.finish()
        await owner.close()
        assert available(lease) and owner.process is owner.lease is None
    finally:
        process.finish()
        await asyncio.gather(closing, return_exceptions=True)
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sound", "visual"])
async def test_startup_deadline_does_not_abandon_late_child(tmp_path, monkeypatch, kind):
    owner, request, lease = make_owner(kind, tmp_path, monkeypatch)
    module = sound_model if kind == "sound" else temporal_model
    monkeypatch.setattr(module, "START_TIMEOUT", .01)
    entered, release = asyncio.Event(), asyncio.Event()
    process = InertProcess(kind)

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    work = asyncio.create_task(owner.analyze(request))
    try:
        await asyncio.wait_for(entered.wait(), .5)
        done, _ = await asyncio.wait({work}, timeout=1.3)
        assert work in done, "startup had no bounded deadline"
        with pytest.raises((TimeoutError, ValueError)):
            await work
        assert not available(lease)
        release.set()
        await asyncio.wait_for(process.terminated.wait(), .5)
        await owner.close()
        assert process.returncode == 0 and available(lease)
    finally:
        release.set()
        process.finish()
        await asyncio.gather(work, return_exceptions=True)
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sound", "visual"])
@pytest.mark.parametrize("damage", ["symlink", "hardlink", "content", "permissions", "fifo", "directory"])
async def test_unsafe_model_lease_is_rejected_before_spawn(tmp_path, monkeypatch, kind, damage):
    owner, request, lease = make_owner(kind, tmp_path, monkeypatch)
    original = tmp_path / "unrelated"
    original.write_bytes(b"")
    original.chmod(0o600)
    if damage == "symlink":
        lease.symlink_to(original)
    elif damage == "hardlink":
        os.link(original, lease)
    elif damage == "fifo":
        os.mkfifo(lease, 0o600)
    elif damage == "directory":
        lease.mkdir(mode=0o700)
    else:
        lease.write_bytes(b"not a lock" if damage == "content" else b"")
        lease.chmod(0o644 if damage == "permissions" else 0o600)
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    try:
        with pytest.raises(ValueError, match="runtime_missing"):
            await owner.analyze(request)
        spawn.assert_not_called()
        assert owner.process is owner.lease is None
        assert original.read_bytes() == b""
    finally:
        await owner.close()


INERT_PROTOCOL = """
import json,sys
kind=sys.argv[1]
if kind=='sound':
 print(json.dumps(dict(type='ready',version=1,sample_rate=16000,known_classifications=['baby_crying'])),flush=True)
else:
 print(json.dumps(dict(version=1,ready=True)),flush=True)
for line in sys.stdin:
 request=json.loads(line)
 if kind=='sound':
  reply=dict(type='result',version=1,id=request['id'],windows=[dict(offset=0,duration=2,scores={'baby_crying':.95},competing_confidence=.01)])
 else:
  reply=dict(version=1,result=dict(event='none',observation='synthetic unchanged scene'),peak_mlx_bytes=0)
 print(json.dumps(reply),flush=True)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sound", "visual"])
async def test_actual_silent_protocol_child_is_reaped_and_restartable(tmp_path, monkeypatch, kind):
    owner, request, lease = make_owner(kind, tmp_path, monkeypatch)
    real_spawn = asyncio.create_subprocess_exec
    created = []

    async def spawn(*args, **kwargs):
        # Replace the entire worker command, keeping only its actual pipe,
        # environment and process-owner plumbing. No native/model entry runs.
        assert kwargs["pass_fds"] == (owner.lease,)
        if kind == "visual":
            assert kwargs["env"]["FRIDAY_REVIEW_OWNER_PID"] == str(os.getpid())
        process = await real_spawn(sys.executable, "-u", "-c", INERT_PROTOCOL, kind, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    try:
        await asyncio.wait_for(owner.analyze(request), 2)
        assert len(created) == 1 and not available(lease)
        created[-1].terminate()
        await asyncio.wait_for(created[-1].wait(), 1)
        await asyncio.wait_for(owner.analyze(request), 2)
        assert len(created) == 2 and not available(lease)
        await owner.close()
        assert available(lease) and owner.process is owner.lease is None
        for process in created:
            assert process.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)
        await asyncio.wait_for(owner.analyze(request), 2)
        assert len(created) == 3
    finally:
        await owner.close()
        for process in created:
            if process.returncode is None:
                process.kill()
                await process.wait()


INERT_VISUAL_LOAD = """
import json,os,threading
from pathlib import Path
from homeplane.embodied import temporal_worker
def blocked_readiness(model):
 print(json.dumps(dict(pid=os.getpid(),parent=os.getppid(),owner=int(os.environ['FRIDAY_REVIEW_OWNER_PID']))),flush=True)
 threading.Event().wait()
temporal_worker.model_readiness=blocked_readiness
temporal_worker.main(Path('SYNTHETIC-NO-MODEL-OPENED'))
"""


@pytest.mark.asyncio
async def test_visual_worker_exits_after_owner_crash_even_before_model_readiness():
    launcher = f"""
import os,subprocess,sys,threading
environment=dict(os.environ,FRIDAY_REVIEW_OWNER_PID=str(os.getpid()))
subprocess.Popen([sys.executable,'-B','-u','-c',{INERT_VISUAL_LOAD!r}],env=environment)
threading.Event().wait()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-u", "-c", launcher,
        cwd=Path(__file__).resolve().parents[2],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    child_pid = None
    child_exited = False
    try:
        record = json.loads(await asyncio.wait_for(process.stdout.readline(), 2))
        child_pid = record["pid"]
        assert record["parent"] == record["owner"] == process.pid
        process.kill()
        # The inherited pipe can close only after the otherwise-blocked child
        # exits. No model, camera, SDK, weights or inference provider runs.
        await asyncio.wait_for(process.communicate(), 2)
        child_exited = True
    finally:
        if process.returncode is None:
            process.kill()
        if child_pid and not child_exited:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.communicate(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "999999999"])
async def test_visual_worker_without_its_actual_owner_never_reaches_model_readiness(owner):
    environment = dict(os.environ)
    environment.pop("FRIDAY_REVIEW_OWNER_PID", None)
    if owner is not None:
        environment["FRIDAY_REVIEW_OWNER_PID"] = owner
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-u", "-c", INERT_VISUAL_LOAD,
        cwd=Path(__file__).resolve().parents[2], env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 2)
        assert process.returncode == 0 and stdout == b""
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


@pytest.mark.asyncio
async def test_inherited_empty_lease_survives_owner_crash_until_child_exit(tmp_path):
    child = "import json,os,threading;print(json.dumps(dict(pid=os.getpid())),flush=True);threading.Event().wait()"
    launcher = f"""
import asyncio,sys
from pathlib import Path
from homeplane.embodied.review_child import ReviewChild
async def main():
 owner=ReviewChild((sys.executable,'-B','-u','-c',{child!r}),Path({str(tmp_path)!r}),'temporal-model.lock',busy='busy',unavailable='missing')
 process=await owner.start()
 print((await process.stdout.readline()).decode().strip(),flush=True)
 await asyncio.Event().wait()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-u", "-c", launcher,
        cwd=Path(__file__).resolve().parents[2],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    child_pid = None
    try:
        child_pid = json.loads(await asyncio.wait_for(process.stdout.readline(), 2))["pid"]
        lease = tmp_path / "temporal-model.lock"
        assert not available(lease)
        process.kill()
        await asyncio.wait_for(process.wait(), 1)
        assert not available(lease), "owner crash released its living child's lease"
        os.kill(child_pid, 9)
        child_pid = None
        async with asyncio.timeout(2):
            while not available(lease):
                await asyncio.sleep(.01)
        assert lease.read_bytes() == b""
    finally:
        if process.returncode is None:
            process.kill()
        if child_pid:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        await process.communicate()
