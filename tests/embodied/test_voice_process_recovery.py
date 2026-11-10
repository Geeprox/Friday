"""Exercise the real home voice owner with silent, model-free pipe stand-ins."""

import asyncio
import ast
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied import home_service


class SilentProcess:
    def __init__(self, *, delayed_exit=False, ready=True):
        self.returncode = None
        self.stdin = self.stdout = self
        self.output = asyncio.Queue()
        if ready:
            self.output.put_nowait(b'{"type":"ready","asr":"synthetic"}\n')
        self.exited = asyncio.Event()
        self.terminated = asyncio.Event()
        self.delayed_exit = delayed_exit
        self.input_closed = False
        self.requests = []

    async def readline(self):
        return await self.output.get()

    async def read(self, _):
        await self.exited.wait()
        return b''

    def write(self, data):
        self.requests.append(json.loads(data))

    async def drain(self):
        pass

    def terminate(self):
        self.terminated.set()
        if not self.delayed_exit:
            self.finish()

    def kill(self):
        self.terminate()

    def finish(self):
        self.returncode = 0
        self.exited.set()
        self.output.put_nowait(b'')

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def close(self):
        self.input_closed = True


async def until(predicate):
    async with asyncio.timeout(.5):
        while not predicate():
            await asyncio.sleep(0)


def make_home(tmp_path, monkeypatch):
    monkeypatch.setattr(home_service, 'DATA', tmp_path)
    monkeypatch.setattr(home_service, 'VOICE_INPUT_PAUSED', tmp_path / 'paused')
    home = home_service.HomeService(voice_preferences=lambda: SimpleNamespace(wake_word='Friday', revision=0))
    home.poll_curtain = AsyncMock()
    home.curtain.close = AsyncMock()
    home.speaker.say = AsyncMock()
    return home


@pytest.mark.asyncio
async def test_dead_reader_cannot_be_reported_ready_by_transcription_finally(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    process = SilentProcess()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    try:
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        process.output.put_nowait(b'[]\n')
        await until(lambda: home.public()['voice'] == 'unavailable')
        home.request = AsyncMock(side_effect=RuntimeError('synthetic_closed_pipe'))
        await home.transcribe(b'synthetic-pcm', None, time.monotonic())
        assert home.public()['voice'] == 'unavailable'
    finally:
        await home.close()


@pytest.mark.asyncio
async def test_failed_reader_is_retired_even_when_child_stays_alive(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    process = SilentProcess()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    try:
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        process.output.put_nowait(b'[]\n')
        await until(lambda: home.public()['voice'] == 'unavailable')
        await until(process.terminated.is_set)
        assert process.returncode is not None
    finally:
        await home.close()


@pytest.mark.asyncio
async def test_enable_does_not_wait_for_process_creation(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    process = SilentProcess()

    async def delayed_spawn(*args, **kwargs):
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)
    try:
        await asyncio.wait_for(home.enable(), .05)
        await until(started.is_set)
        assert home.public()['voice'] == 'loading_local_models'
    finally:
        release.set()
        await home.close()


@pytest.mark.asyncio
async def test_request_timeout_covers_a_child_not_reading_stdin(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    process = SilentProcess()
    blocked = asyncio.Event()
    process.drain = blocked.wait
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    home.voice_process.request_timeout = .02
    try:
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        with pytest.raises(TimeoutError):
            await home.request('transcribe', pcm='SYNTHETIC')
        await until(process.exited.is_set)
        assert home.public()['voice'] == 'unavailable'
        assert len(process.requests) == 1 and not home.voice_process.futures
    finally:
        await home.close()


@pytest.mark.asyncio
async def test_no_new_child_until_failed_generation_actually_exits_then_control_recovers(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    first, second = SilentProcess(delayed_exit=True), SilentProcess()
    spawn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    home.voice_process.retry_delay = 0
    command = AsyncMock(return_value={'status': 'reported_match', 'reached_target': True})
    monkeypatch.setattr(home_service.bridge, 'command', command)
    work = None
    try:
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        first.output.put_nowait(b'[]\n')
        await until(first.terminated.is_set)
        for _ in range(5):
            await asyncio.wait_for(home.enable(), .05)
        assert spawn.await_count == 1, 'still-running failed child lost its slot'
        first.finish()
        await until(lambda: not home.voice_process.child.slot)
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        assert spawn.await_count == 2
        home.selected, home.last_video = True, time.monotonic()
        work = asyncio.create_task(home.transcribe(b'SYNTHETIC', None, time.monotonic()))
        await until(lambda: len(second.requests) == 1)
        second.output.put_nowait((json.dumps(dict(id=second.requests[0]['id'], type='transcript',
            text='Friday，打开客厅灯', voice_revision=0,
            address={'admitted': True, 'wake_word': True})) + '\n').encode())
        await asyncio.wait_for(work, .5)
        assert command.await_count == 1 and command.call_args.args[:2] == ('客厅灯', 'on')
        assert home.state['voice_command']['state'] == 'readback'
        assert home.state['reason'] == '已读回目标状态'
        assert home.public()['voice'] == 'ready'
        assert len(first.requests) == 0, 'failed audio must not be automatically replayed'
    finally:
        first.finish()
        if work and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        await home.close()


@pytest.mark.asyncio
async def test_shared_camera_queue_cancel_keeps_the_admitted_request_alive(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    other = home_service.HomeService(camera_id='cam_2222222222', voice_provider=home)
    other.curtain.close = AsyncMock()
    process = SilentProcess()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    tasks = []
    try:
        await home.enable()
        await other.enable()
        await until(lambda: other.public()['voice'] == 'ready')
        tasks.append(asyncio.create_task(home.request('transcribe', pcm='FIRST')))
        await until(lambda: len(process.requests) == 1)
        tasks.append(asyncio.create_task(other.request('transcribe', pcm='SECOND')))
        await asyncio.sleep(0)
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        assert not process.terminated.is_set() and len(process.requests) == 1
        process.output.put_nowait((json.dumps(dict(id=process.requests[0]['id'], type='transcript',
                                                   text='SYNTHETIC')) + '\n').encode())
        assert (await asyncio.wait_for(tasks[0], .5))['text'] == 'SYNTHETIC'
        assert other.public()['voice'] == 'ready' and spawn.await_count == 1
        await other.close()
        assert other.public()['voice'] == 'stopped' and home.public()['voice'] == 'ready'
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await other.close()
        await home.close()


@pytest.mark.asyncio
async def test_cancelled_inflight_reply_cannot_enter_the_next_generation(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    first, second = SilentProcess(), SilentProcess()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=[first, second]))
    home.voice_process.retry_delay = 0
    tasks = []
    try:
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        tasks.append(asyncio.create_task(home.request('transcribe', pcm='FIRST')))
        await until(lambda: len(first.requests) == 1)
        old_id = first.requests[0]['id']
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        await until(lambda: not home.voice_process.child.slot)
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        tasks.append(asyncio.create_task(home.request('transcribe', pcm='SECOND')))
        await until(lambda: len(second.requests) == 1)
        assert second.requests[0]['id'] != old_id
        for process in (first, second):
            process.output.put_nowait((json.dumps(dict(id=old_id, type='transcript', text='RETIRED')) + '\n').encode())
        await asyncio.sleep(0)
        assert not tasks[1].done()
        second.output.put_nowait((json.dumps(dict(id=second.requests[0]['id'], type='transcript', text='CURRENT')) + '\n').encode())
        assert (await asyncio.wait_for(tasks[1], .5))['text'] == 'CURRENT'
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await home.close()


@pytest.mark.asyncio
async def test_startup_deadline_reaps_a_child_without_ready_and_respects_backoff(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    process = SilentProcess(ready=False)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    home.voice_process.startup_timeout = .02
    try:
        await home.enable()
        assert home.public()['voice'] == 'loading_local_models'
        await until(process.terminated.is_set)
        await until(lambda: not home.voice_process.child.slot)
        assert home.public()['voice'] == 'unavailable'
        for _ in range(5):
            await home.enable()
        assert spawn.await_count == 1
        with pytest.raises(RuntimeError, match='voice_not_ready'):
            await home.request('transcribe', pcm='NOT-SENT')
        assert not process.requests
    finally:
        await home.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt', [b'not-json\n', b'[]\n', b'{}\n',
                                   b'{"type":"ready","asr":123}\n',
                                   b'{"type":"ready","asr":"synthetic","intelligence":[]}\n'])
async def test_malformed_startup_never_opens_voice_input(tmp_path, monkeypatch, receipt):
    home = make_home(tmp_path, monkeypatch)
    process = SilentProcess(ready=False)
    process.output.put_nowait(receipt)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    try:
        await home.enable()
        await until(process.terminated.is_set)
        assert home.public()['voice'] == 'unavailable'
        with pytest.raises(RuntimeError, match='voice_not_ready'):
            await home.request('transcribe', pcm='NOT-SENT')
        assert not process.requests
    finally:
        await home.close()


@pytest.mark.asyncio
async def test_wrong_reply_kind_never_reaches_the_home_actuator(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    process = SilentProcess()
    command = AsyncMock()
    monkeypatch.setattr(home_service.bridge, 'command', command)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    work = None
    try:
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        home.selected, home.last_video = True, time.monotonic()
        work = asyncio.create_task(home.transcribe(b'SYNTHETIC', None, time.monotonic()))
        await until(lambda: len(process.requests) == 1)
        process.output.put_nowait((json.dumps(dict(id=process.requests[0]['id'], type='task_result',
            voice_revision=0, text='Friday，打开客厅灯', address={'admitted': True, 'wake_word': True}))+'\n').encode())
        await asyncio.wait_for(work, .5)
        command.assert_not_awaited()
        home.speaker.say.assert_not_awaited()
        assert home.public()['voice'] == 'unavailable'
    finally:
        if work and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        await home.close()


@pytest.mark.asyncio
async def test_close_during_spawn_keeps_late_handle_owned_through_repeated_cancel(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    late = SilentProcess()
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        started.set()
        await release.wait()
        return late

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    closing = None
    try:
        await home.enable()
        await until(started.is_set)
        closing = asyncio.create_task(home.close())
        await until(lambda: home.voice_process.phase == 'stopped')
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        await home.enable()
        assert len(calls) == 1
        release.set()
        await until(late.terminated.is_set)
        await until(lambda: not home.voice_process.child.slot)
        assert late.input_closed
    finally:
        release.set()
        await home.close()


@pytest.mark.asyncio
async def test_worker_failure_does_not_expose_exception_content(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=RuntimeError('PRIVATE-FAILURE-TEXT')))
    try:
        await home.enable()
        await until(lambda: home.public()['voice'] == 'unavailable')
        assert 'PRIVATE-FAILURE-TEXT' not in json.dumps(home.public())
    finally:
        await home.close()


@pytest.mark.asyncio
async def test_invalid_unsent_request_does_not_take_a_ready_voice_process_down(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    process = SilentProcess()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    try:
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        for op, payload in [('unknown', {}), ('transcribe', {'pcm': 'X' * 400000}),
                            ('transcribe', {'pcm': float('nan')})]:
            with pytest.raises(ValueError):
                await home.request(op, **payload)
            assert home.public()['voice'] == 'ready'
        assert not process.requests and not process.terminated.is_set()
    finally:
        await home.close()


def original_voice_projection(home, secondary_homes=None):
    """Run only the unchanged view projections; never import the worker entry."""
    from homeplane.embodied.motion_games import MotionGames

    worker = Path(__file__).resolve().parents[2] / 'homeplane/embodied/worker.py'
    view = next(n for n in ast.parse(worker.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'view')
    selected = []
    for statement in view.body:
        if isinstance(statement, ast.Assign):
            target = statement.targets[0]
            if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and target.value.id == 'result' and isinstance(target.slice, ast.Constant)
                    and target.slice.value in {'home', 'voice_sessions', 'voice_input'}):
                selected.append(statement)
    assert len(selected) == 3
    scope = dict(result={'status': 'live', 'camera_id': 'cam_1111111111',
                         'game': MotionGames().viewer_state()},
                 home=home, secondary_homes=secondary_homes or {}, SELECTED={'id': 'cam_1111111111'}, PRIVATE=home_service.VOICE_INPUT_PAUSED.parent)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(worker), 'exec'), scope)
    return scope['result']


def voice_row(report):
    project = Path(__file__).resolve().parents[2]
    code = '''import {readFileSync} from 'node:fs';
import {homeServiceSummary} from './homeplane/static/home-status.js';
console.log(JSON.stringify(homeServiceSummary({worker:JSON.parse(readFileSync(0,'utf8'))}).rows.find(r=>r.label==='语音交互')));
'''
    result = subprocess.run(['node', '--input-type=module', '-e', code], cwd=project,
                            input=json.dumps(report), capture_output=True, text=True, check=True, timeout=5)
    return json.loads(result.stdout)


@pytest.mark.asyncio
async def test_actual_voice_owner_projection_reaches_console_and_home_status(tmp_path, monkeypatch):
    from homeplane.embodied import service as console

    home = make_home(tmp_path, monkeypatch)
    process = SilentProcess()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    monkeypatch.setattr(console, 'PRIVATE', tmp_path)
    monkeypatch.setattr(console, 'configuration', lambda: {'cameras': [{'id': 'cam_1111111111'}]})
    owner = console.EmbodiedService()
    owner.configured = lambda: True
    owner.process = SimpleNamespace(returncode=None, pid=1234)

    async def request(path):
        assert path == '/api/status'
        return original_voice_projection(home)

    owner.request = request
    try:
        await home.enable()
        await until(lambda: home.public()['voice'] == 'ready')
        report = await owner.status()
        assert report['home']['voice'] == report['voice_sessions'][0]['voice'] == 'ready'
        assert report['game'], 'retain the original business projection'
        assert voice_row(report)['state'] == 'on'
        process.output.put_nowait(b'[]\n')
        await until(lambda: home.public()['voice'] == 'unavailable')
        report = await owner.status()
        assert report['voice_sessions'][0]['voice'] == 'unavailable'
        assert voice_row(report)['state'] == 'warning'
        owner.process = None
        assert voice_row(await owner.status())['state'] == 'waiting'
    finally:
        await owner.client.aclose()
        await home.close()


@pytest.mark.asyncio
async def test_secondary_voice_does_not_rearm_an_inactive_primary_camera(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    other = home_service.HomeService(camera_id='cam_2222222222', voice_provider=home)
    other.curtain.close = AsyncMock()
    process = SilentProcess()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    try:
        await other.enable()
        await until(lambda: other.public()['voice'] == 'ready')
        assert not home.enabled and not home.armed
        projection = original_voice_projection(home, {other.camera_id: other})
        assert projection['voice_sessions'][0]['enabled'] is False
        assert projection['voice_sessions'][1]['enabled'] is True
        assert voice_row(dict(configured=True, owned=True, **projection))['state'] == 'on'
    finally:
        await other.close()
        await home.close()


@pytest.mark.asyncio
async def test_secondary_frames_recover_shared_voice_without_primary_frames(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    other = home_service.HomeService(camera_id='cam_2222222222', voice_provider=home)
    other.curtain.close = AsyncMock()
    first, second = SilentProcess(), SilentProcess()
    spawn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    home.voice_process.retry_delay = 0
    try:
        await other.enable()
        await until(lambda: other.public()['voice'] == 'ready')
        first.output.put_nowait(b'[]\n')
        await until(lambda: not home.voice_process.child.slot)
        await other.observe([], [], {'status': 'locked'}, time.monotonic(), None, True,
                            session_id='SYNTHETIC-secondary')
        await until(lambda: other.public()['voice'] == 'ready')
        assert spawn.await_count == 2
        assert not home.enabled and not home.armed
    finally:
        await other.close()
        await home.close()


@pytest.mark.asyncio
async def test_global_voice_pause_is_observed_by_secondary_frames_without_primary_frames(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    other = home_service.HomeService(camera_id='cam_2222222222', voice_provider=home)
    other.curtain.close = AsyncMock()
    first, second = SilentProcess(), SilentProcess()
    spawn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    try:
        await other.enable()
        await until(lambda: other.public()['voice'] == 'ready')
        home_service.VOICE_INPUT_PAUSED.touch()
        await other.observe([], [], {'status': 'locked'}, time.monotonic(), None, True,
                            session_id='SYNTHETIC-secondary')
        await until(lambda: not home.voice_process.child.slot)
        assert other.public()['voice'] == 'paused'
        assert spawn.await_count == 1 and not home.armed
        home_service.VOICE_INPUT_PAUSED.unlink()
        await other.observe([], [], {'status': 'locked'}, time.monotonic(), None, True,
                            session_id='SYNTHETIC-secondary')
        await until(lambda: other.public()['voice'] == 'ready')
        assert spawn.await_count == 2 and not home.armed
    finally:
        await other.close()
        await home.close()
