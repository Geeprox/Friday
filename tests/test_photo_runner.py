"""使用真实自有子进程验证超时、内存闸门及取消后的回收责任。"""

import asyncio
import hashlib
import json
import struct
import sys

import pytest

from homeplane.family_media.photo_codec import PhotoCodecError
from homeplane.family_media.photo_contracts import PhotoWorkResult
from homeplane.family_media.photo_runner import PhotoRunner, RSS_LIMIT
from tests.test_photo_codec import colors, image_bytes, request


async def child(code):
    return await asyncio.create_subprocess_exec(sys.executable, "-B", "-I", "-c", code,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)


async def idle_child():
    return await child("import sys,time;sys.stderr.write('SIMULATED-PHOTO-READY\\n');sys.stderr.flush();"
                       "sys.stdin.buffer.read();time.sleep(60)")


async def zero_rss(process):
    return 0


@pytest.mark.asyncio
async def test_timeout_reaps_the_real_owned_process_and_releases_capacity():
    processes = []
    async def launch():
        process = await idle_child();processes.append(process);return process
    runner = PhotoRunner(launcher=launch, timeout=.2, rss_reader=zero_rss)
    payload = image_bytes(colors())
    try:
        with pytest.raises(PhotoCodecError, match="photo_timeout"):
            await runner.render(payload, request(payload))
        assert len(processes) == 1 and processes[0].returncode is not None
        assert runner.active is None and runner.process is None
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_busy_and_repeated_caller_cancellation_do_not_queue_another_worker():
    processes, started = [], asyncio.Event()
    async def launch():
        process = await idle_child();processes.append(process)
        assert await process.stderr.readline() == b"SIMULATED-PHOTO-READY\n"
        started.set();return process
    runner = PhotoRunner(launcher=launch, rss_reader=zero_rss)
    payload = image_bytes(colors())
    task = asyncio.create_task(runner.render(payload, request(payload)))
    try:
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(PhotoCodecError, match="photo_renderer_busy"):
            await runner.render(payload, request(payload))
        task.cancel();task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert len(processes) == 1 and processes[0].returncode is not None
        assert runner.active is None and runner.process is None
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_late_launch_is_owned_until_close_and_repeated_cancel_finish_reaping():
    processes, started, release = [], asyncio.Event(), asyncio.Event()
    async def launch():
        process = await idle_child();processes.append(process)
        assert await process.stderr.readline() == b"SIMULATED-PHOTO-READY\n"
        started.set()
        await release.wait()
        return process
    runner = PhotoRunner(launcher=launch, rss_reader=zero_rss)
    payload = image_bytes(colors())
    task = asyncio.create_task(runner.render(payload, request(payload)))
    closing = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and runner.active is not None and processes[0].returncode is None
        with pytest.raises(PhotoCodecError, match="photo_renderer_busy"):
            await runner.render(payload, request(payload))
        closing = asyncio.create_task(runner.close())
        await asyncio.sleep(0)
        task.cancel()
        assert not closing.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if closing:
            await asyncio.wait_for(closing, 3)
        await runner.close()
    assert len(processes) == 1 and processes[0].returncode is not None
    assert runner.active is None and runner.process is None


@pytest.mark.asyncio
async def test_memory_limit_stops_the_real_process():
    processes = []
    async def launch():
        process = await idle_child();processes.append(process);return process
    async def oversized(process):
        return RSS_LIMIT + 1
    runner = PhotoRunner(launcher=launch, rss_reader=oversized)
    payload = image_bytes(colors())
    try:
        with pytest.raises(PhotoCodecError, match="photo_memory_limit"):
            await runner.render(payload, request(payload))
        assert processes[0].returncode is not None and runner.process is None
    finally:
        await runner.close()


@pytest.mark.parametrize("fault", ["signature", "digest", "extra", "private_error"])
@pytest.mark.asyncio
async def test_unverified_or_extra_worker_bytes_cannot_become_a_success(fault):
    payload = image_bytes(colors())
    encoded = b"SYNTHETIC-NOT-A-PNG" if fault == "signature" else image_bytes(colors())
    result = PhotoWorkResult(original_sha256=hashlib.sha256(payload).hexdigest(),
        output_sha256="0"*64 if fault=="digest" else hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded), source_format="PNG",source_width=4,source_height=2,width=4,height=2,
        output_format="PNG",content_type="image/png",color_basis="assumed_srgb")
    header = json.dumps({"ok":True,"result":result.model_dump(mode="json")}).encode()
    if fault == "private_error":
        header = json.dumps({"ok":False,"code":"PRIVATE-PATH-NEVER-ECHO"}).encode();encoded=b""
    wire = struct.pack(">I",len(header))+header+encoded+(b"UNEXPECTED" if fault=="extra" else b"")
    processes = []
    async def launch():
        process = await child("import sys;sys.stdin.buffer.read();sys.stdout.buffer.write("+repr(wire)+");sys.stdout.flush()")
        processes.append(process);return process
    runner = PhotoRunner(launcher=launch,rss_reader=zero_rss)
    try:
        with pytest.raises(PhotoCodecError) as caught:
            await runner.render(payload,request(payload))
        assert caught.value.code in {"photo_output_unverified","photo_protocol_invalid","photo_processing_failed"}
        assert "PRIVATE" not in str(caught.value) and processes[0].returncode is not None
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_worker_effect_guard_refuses_real_file_network_and_process_attempts(tmp_path):
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "homeplane/family_media/photo_worker.py"
    target = tmp_path / "must-not-exist"
    code = """import importlib.util,json,pathlib,socket,subprocess,sys
spec=importlib.util.spec_from_file_location('photo_effect_guard',SCRIPT)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
sys.addaudithook(module._audit)
refused=[]
for action in (lambda:pathlib.Path(TARGET).write_text('SYNTHETIC'),lambda:socket.socket(),lambda:subprocess.Popen(['/usr/bin/true'])):
 try:action()
 except PermissionError:refused.append(True)
 else:refused.append(False)
print(json.dumps(refused))
""".replace("SCRIPT",repr(str(script))).replace("TARGET",repr(str(target)))
    process = await child(code)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0 and json.loads(stdout) == [True, True, True]
        assert not target.exists() and stderr == b""
    finally:
        if process.returncode is None:
            process.kill();await process.wait()
