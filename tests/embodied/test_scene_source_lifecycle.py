"""Actual scene producer plus private subprocess fixtures; no real models/cameras."""

import ast
import asyncio
import base64
import io
import json
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied.scene_process import SceneProcess
from homeplane.embodied.scene_protocol import MAX_JPEG, scene_result
from homeplane.embodied.scene_worker import serve
from tests.embodied.test_camera_audio import actual


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['generation', 'motion', 'wall', 'context', 'session', 'age', 'none'])
async def test_actual_scene_producer_never_publishes_a_reply_after_source_change(tmp_path, change):
    entered, release = threading.Event(), threading.Event()
    after_reply, leave = asyncio.Event(), asyncio.Event()
    objects = [dict(class_='synthetic-only')]

    class Detector:
        def __init__(self, *_):
            pass

        def detect(self, _jpeg):
            entered.set()
            release.wait(.5)
            return dict(objects=objects, scene_ms=1)

    class Process:
        def __init__(self, *_):
            self.close = AsyncMock(return_value=True)

        async def analyze(self, _jpeg, **_kwargs):
            entered.set()
            await asyncio.to_thread(release.wait, .5)
            return dict(objects=objects, scene_ms=1)

        def status(self):
            return dict(state='ready', reason=None)

    async def pause(_):
        after_reply.set()
        await leave.wait()

    item = dict(type='frame', received_at=1000., jpeg=base64.b64encode(b'SYNTHETIC').decode(),
                _motion_generation=4, capture_session='a' * 32, camera_source_context='b' * 64)
    scope = dict(SceneDetector=Detector, SceneProcess=Process, MODELS=tmp_path,
                 asyncio=SimpleNamespace(to_thread=asyncio.to_thread, sleep=pause),
                 time=SimpleNamespace(time=lambda: 1000., monotonic=lambda: 10.), base64=base64,
                 latest_item=item, my_generation=1, generation=1, wall_epoch=2, primary_wall_epoch=2,
                 motion_generation=4, SELECTED={'id': 'cam_1111111111'}, state={'status': 'live'},
                 scene_data=dict(objects=[], observed_at=0))
    source = Path('homeplane/embodied/worker.py').read_text()
    for name in ('primary_frame_current', 'scene_frame_current'):
        if f'def {name}(' in source:
            actual('homeplane/embodied/worker.py', name, scope)
    producer = actual('homeplane/embodied/worker.py', 'observe_scene', scope)
    job = asyncio.create_task(producer())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if change == 'generation':
            scope['generation'] = 2
        elif change == 'motion':
            scope['motion_generation'] = 5
        elif change == 'wall':
            scope['primary_wall_epoch'] = 3
        elif change == 'context':
            scope['latest_item'] = dict(item, camera_source_context='c' * 64)
        elif change == 'session':
            scope['latest_item'] = dict(item, capture_session='c' * 32)
        elif change == 'age':
            scope['time'].time = lambda: 1002.
        release.set()
        await asyncio.wait_for(after_reply.wait(), 1)
        assert scope['scene_data']['objects'] == (objects if change == 'none' else []), (
            'A retired camera scene was admitted as current')
    finally:
        release.set()
        scope['generation'] = 2
        leave.set()
        await asyncio.gather(job, return_exceptions=True)


CONTEXT = dict(generation=1, wall_epoch=2, motion_generation=4,
               capture_session='a' * 32, camera_source_context='b' * 64)
RESULT = dict(objects=[{'class': 'person', 'name': '人体', 'confidence': .9,
                      'box': dict(x=.1, y=.2, w=.3, h=.4), 'partial': False}], scene_ms=1)

# This child exercises the production pipe server with a trusted synthetic
# detector. Native/GIL stalls use libc sleep, never ONNX, media or a device.
CHILD = r'''
import ctypes, json, signal, sys, time
from homeplane.embodied.scene_worker import serve
mode = sys.argv[1]
class Detector:
    def __init__(self, _):
        if mode == 'stall_init': ctypes.PyDLL(None).sleep(60)
        if mode == 'slow_init': time.sleep(.15)
        if mode == 'error_init': raise RuntimeError('PRIVATE MODEL PATH')
    def detect(self, _):
        if mode == 'stall_frame':
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            ctypes.PyDLL(None).sleep(60)
        if mode == 'error_frame': raise RuntimeError('PRIVATE FRAME DATA')
        return dict(objects=[{'class':'person','name':'人体','confidence':.9,
                    'box':dict(x=.1,y=.2,w=.3,h=.4),'partial':False}],scene_ms=1)
if mode in {'ok','slow_init','stall_init','stall_frame','error_init','error_frame'}:
    serve(sys.stdin.buffer, sys.stdout.buffer, 'unused', detector_factory=Detector)
else:
    for raw in sys.stdin.buffer:
        request = json.loads(raw)
        reply = {k:request[k] for k in ('v','nonce','sequence','action','context')}
        reply.update(ok=True,result={},reason=None)
        if request['action'] == 'frame':
            reply['result'] = Detector('unused').detect(b'')
            if mode == 'wrong_source': reply['context']['motion_generation'] += 1
            if mode == 'wrong_nonce': reply['nonce'] = '0'*32
            if mode == 'wrong_sequence': reply['sequence'] += 1
            if mode == 'nan': reply['result']['objects'][0]['confidence'] = float('nan')
            if mode == 'unknown_label': reply['result']['objects'][0]['class'] = 'private description'
            if mode == 'oversize':
                sys.stdout.write('X'*150000+'\n');sys.stdout.flush();continue
        print(json.dumps(reply), flush=True)
'''


def child(mode='ok', **kwargs):
    return SceneProcess('unused', command=(sys.executable, '-B', '-c', CHILD, mode), **kwargs)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.mark.asyncio
async def test_scene_is_lazy_and_success_keeps_the_original_context_out_of_public_objects():
    service = child()
    assert service.process is None and service.status()['state'] == 'idle'
    try:
        assert await service.analyze(b'SYNTHETIC', context=CONTEXT) == RESULT
        process = service.process
        assert service.status()['state'] == 'ready'
        assert await service.analyze(b'SYNTHETIC2', context=CONTEXT) == RESULT
        assert service.process is process
    finally:
        assert await service.close()
    assert process.returncode is not None and service.process is None


@pytest.mark.asyncio
async def test_model_warmup_can_outlive_one_frame_without_processing_or_renewing_it():
    service = child('slow_init')
    began = time.monotonic()
    try:
        assert await service.analyze(b'SYNTHETIC', context=CONTEXT,
            current=lambda: time.monotonic() - began < .05, active=lambda: True) is None
        process = service.process
        assert process is not None and process.returncode is None and service.ready
        assert await service.analyze(b'FRESH SYNTHETIC', context=CONTEXT) == RESULT
        assert process is service.process
    finally:
        assert await service.close()


@pytest.mark.asyncio
async def test_scene_adapter_import_and_status_do_not_import_native_detectors():
    script = ('import sys;from homeplane.embodied.scene_process import SceneProcess;'
              's=SceneProcess("unused");s.status();'
              'assert not any(k in sys.modules for k in ("cv2","onnxruntime",'
              '"homeplane.embodied.scene_detector"))')
    process = await asyncio.create_subprocess_exec(sys.executable, '-B', '-c', script)
    assert await process.wait() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['stall_init', 'stall_frame'])
async def test_native_gil_stall_is_actually_terminated_and_reaped(mode):
    service = child(mode, startup_timeout=.25, frame_timeout=.12)
    job = asyncio.create_task(service.analyze(b'SYNTHETIC', context=CONTEXT))
    try:
        await until(lambda: service.process is not None)
        process = service.process
        assert await asyncio.wait_for(job, 2) is None
        assert service.status()['reason'] == 'scene_timeout'
    finally:
        assert await service.close()
    assert process.returncode is not None
    if mode == 'stall_frame':
        assert process.returncode == -signal.SIGKILL


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['wrong_source', 'wrong_nonce', 'wrong_sequence', 'nan',
                                 'unknown_label', 'oversize', 'error_init', 'error_frame'])
async def test_child_faults_never_return_objects_or_private_diagnostics(mode):
    service = child(mode)
    try:
        assert await service.analyze(b'SYNTHETIC', context=CONTEXT) is None
        status = service.status()
        assert status['reason'] in {'scene_protocol_invalid', 'scene_model_unavailable',
                                    'scene_frame_unavailable'}
        assert 'PRIVATE' not in json.dumps(status)
    finally:
        assert await service.close()


@pytest.mark.asyncio
async def test_retiring_source_interrupts_a_native_stall_before_its_inference_deadline():
    service = child('stall_frame', frame_timeout=20)
    current = [True]
    job = asyncio.create_task(service.analyze(b'SYNTHETIC', context=CONTEXT, current=lambda: current[0]))
    try:
        await until(lambda: service.status()['state'] == 'detecting')
        process = service.process
        current[0] = False
        assert await asyncio.wait_for(job, .3) is None
        assert service.status()['reason'] == 'scene_source_retired'
    finally:
        assert await service.close()
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_cancelled_spawn_and_repeated_close_keep_the_slot_until_the_late_child_is_reaped(monkeypatch):
    import homeplane.embodied.scene_process as module
    original = module.asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', delayed_spawn)
    service, other = child(reap_timeout=.03), child(reap_timeout=.03)
    job = asyncio.create_task(service.analyze(b'SYNTHETIC', context=CONTEXT))
    try:
        await entered.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not await service.close()
        assert service.status()['cleanup_pending']
        assert await other.analyze(b'SYNTHETIC', context=CONTEXT) is None
        assert other.status()['reason'] == 'scene_model_busy' and not processes
        release.set()
        await until(lambda: service.reaper.done())
        assert await service.close()
        assert len(processes) == 1 and processes[0].returncode is not None
    finally:
        release.set()
        await service.close()
        await other.close()


@pytest.mark.asyncio
async def test_failed_child_backs_off_then_recovers_without_reusing_a_retired_process():
    service = child('error_frame', retry_delay=.12)
    try:
        assert await service.analyze(b'SYNTHETIC', context=CONTEXT) is None
        await until(lambda: service.reaper.done())
        assert await service.analyze(b'SYNTHETIC', context=CONTEXT) is None
        assert service.process is None
        service.command = child().command
        await asyncio.sleep(.13)
        assert await service.analyze(b'SYNTHETIC', context=CONTEXT) == RESULT
    finally:
        assert await service.close()


@pytest.mark.asyncio
async def test_concurrent_scene_requests_do_not_build_a_queue():
    service = child('stall_frame', frame_timeout=.12)
    job = asyncio.create_task(service.analyze(b'SYNTHETIC', context=CONTEXT))
    try:
        await until(lambda: service.status()['state'] == 'detecting')
        assert await service.analyze(b'SYNTHETIC2', context=CONTEXT) is None
        assert await job is None
    finally:
        assert await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,context', [(b'', CONTEXT), (b'x' * (MAX_JPEG + 1), CONTEXT),
                                          (b'x', dict(CONTEXT, motion_generation=True))])
async def test_invalid_or_oversize_input_does_not_spawn(payload, context):
    service = child()
    assert await service.analyze(payload, context=context) is None
    assert service.process is service.spawn is None
    assert await service.close()


@pytest.mark.parametrize('value', [dict(RESULT, scene_ms=float('inf')),
                                 dict(RESULT, objects=RESULT['objects'] * 257),
                                 dict(RESULT, objects=[dict(RESULT['objects'][0], confidence=10**1000)]),
                                 dict(RESULT, objects=[dict(RESULT['objects'][0], box=dict(x=.9, y=0, w=.2, h=.2))])])
def test_result_validation_rejects_unbounded_or_invalid_scene_values(value):
    with pytest.raises(ValueError, match='scene_protocol_invalid'):
        scene_result(value)


def test_child_initialization_failure_returns_a_fixed_reason_without_model_paths():
    request = dict(v=1, nonce='a' * 32, sequence=1, action='init', context=None, value={})
    reader, writer = io.BytesIO((json.dumps(request) + '\n').encode()), io.BytesIO()

    def fail(_):
        raise RuntimeError('PRIVATE PATH')

    serve(reader, writer, 'PRIVATE MODEL', detector_factory=fail)
    result = json.loads(writer.getvalue())
    assert result['reason'] == 'scene_model_unavailable' and result['result'] is None
    assert 'PRIVATE' not in writer.getvalue().decode()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'queued_motion', 'cached_motion', 'cached_session',
                                 'cached_context', 'cached_wall', 'cached_age'])
async def test_original_vision_request_only_uses_current_scene_rois(change):
    # Execute the original frame prefix, including both scene admission and the
    # actual native-vision port. Do not import the private runtime entry point.
    request = AsyncMock(return_value={})
    source = dict(type='frame', received_at=999.5, _motion_generation=4,
                  capture_session='a' * 32, camera_source_context='b' * 64)
    item = dict(source, received_at=1000, jpeg='SYNTHETIC')
    scene = dict(RESULT, observed_at=999.5, _source=source)
    scope = dict(motion_generation=4, my_generation=1, generation=1, primary_wall_epoch=2,
        wall_epoch=2, time=SimpleNamespace(time=lambda: 1000, monotonic=lambda: 10),
        state=dict(status='live'), item=item, scene_data=scene, room=None,
        camera_planner=None, games=SimpleNamespace(mode='home'), vision=object(), request_vision=request)
    if change == 'queued_motion':
        scope['motion_generation'] = 5
    elif change == 'cached_motion':
        source['_motion_generation'] = 3
    elif change == 'cached_session':
        source['capture_session'] = 'c' * 32
    elif change == 'cached_context':
        source['camera_source_context'] = 'c' * 64
    elif change == 'cached_wall':
        scope['primary_wall_epoch'] = 3
    elif change == 'cached_age':
        source['received_at'] = 998
    for name in ('primary_frame_current', 'scene_frame_current'):
        actual('homeplane/embodied/worker.py', name, scope)
    tree = ast.parse(Path('homeplane/embodied/worker.py').read_text())
    run = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'run_source')
    loop = next(n for n in ast.walk(run) if isinstance(n, ast.While) and any(
        isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'frame_motion_generation'
                                       for t in s.targets) for s in n.body))
    start = next(i for i, n in enumerate(loop.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'frame_motion_generation' for t in n.targets))
    end = next(i for i, n in enumerate(loop.body) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == 'now' for t in n.targets))
    wrapper = ast.parse('async def detect_frame():\n for _sample in range(1):\n  pass').body[0]
    wrapper.body[0].body = loop.body[start:end]
    wrapper.body.insert(0, ast.Global(names=['current_scene']))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 '<original primary Vision request>', 'exec'), scope)
    await scope['detect_frame']()
    if change == 'queued_motion':
        request.assert_not_called()
    else:
        request.assert_awaited_once()
        assert request.call_args.args[1]['person_rois'] == (
            [RESULT['objects'][0]['box']] if change == 'none' else [])
        assert '_source' not in scope['current_scene']


@pytest.mark.asyncio
async def test_original_service_forwards_scene_degradation_without_marking_all_capture_unavailable(tmp_path, monkeypatch):
    from homeplane.embodied import service as module

    status = dict(state='backoff', reason='scene_timeout', cleanup_pending=False)
    value = dict(status='live', people=[], scene_service=status, safety_recording={'state': 'ready'})
    service = module.EmbodiedService()
    service.configured = lambda: True
    service.selected = 'cam_1111111111'
    service.background = True
    service.process = SimpleNamespace(returncode=None)
    service.request = AsyncMock(return_value=value)
    monkeypatch.setattr(module, 'PRIVATE', tmp_path)
    monkeypatch.setattr(module, 'configuration', lambda: dict(cameras=[]))
    try:
        reply = await service.status()
    finally:
        await service.client.aclose()
    assert reply['state'] == 'live' and reply['scene_service'] == status
    assert reply['safety_recording'] == {'state': 'ready'}


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['registration', 'home'])
async def test_original_frame_rechecks_scene_age_after_slow_downstream_work(tmp_path, stage):
    from tests.embodied.test_capture_stream_recovery import frame_continuation, primary_frame_scope

    scope, published = primary_frame_scope(tmp_path)
    clock = [1000.]
    scope['time'] = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0] - 990)
    scope['item'].update(type='frame', _motion_generation=4,
                         capture_session='a' * 32, camera_source_context='b' * 64)
    source = dict(scope['item'], received_at=999.5)
    scope['frame_scene'] = dict(RESULT, _source=source)
    scope['current_scene'] = dict(RESULT, observed_at=999.5)
    update = scope['room'].update = AsyncMock(return_value=dict(registration=dict(status='locked')))
    if stage == 'registration':
        async def slow_register(*_,**__):
            clock[0] = 1000.9
            return True
        scope['room'].register = slow_register
    else:
        async def slow_home(*_, **__):
            clock[0] = 1000.9
        scope['home'].observe = slow_home
    await frame_continuation(scope)()
    assert update.call_args.args[6] == ([] if stage == 'registration' else RESULT['objects'])
    assert scope['state']['scene'] == {'objects': []} and published == ['live']
