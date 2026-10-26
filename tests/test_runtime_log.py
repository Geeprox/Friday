"""Production boundaries with generated inputs only; socket calls fail closed."""
import asyncio
import importlib.util
import io
import json
import logging
import os
import socket
import stat
import threading
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from homeplane.arl import ARLRuntime, ARLError
from homeplane.runtime_log import RuntimeLog, SafeServerHandler, binding, FILE_BYTES, result_projection
from starlette.responses import JSONResponse, Response, StreamingResponse
from test_homeplane_actions import rig, plan, close
from test_homeplane_nas_connection import FakeTransport, config, CREDS, wait_for
from test_homeplane_home_flow import application, bootstrap, turn, request, PHRASE
from homeplane.nas_connection import NASConnectionManager, NASConnectionError
from homeplane.home_flow_contracts import FlowAbort

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('logging_desktop_backend', ROOT / 'native/FridayDesktop/backend/desktop_backend.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
PRIVATE = ('SIMULATED-private-secret', 'SIMULATED-secret-do-not-echo', 'light.simulated_study',
           'Simulated study lamp', 'synthetic.invalid', 'SIMULATED-key', 'SYNTHETIC-password-do-not-log',
           'SYNTHETIC-user', 'SYNTHETIC-share', 'SYNTHETIC-ref', 'SYNTHETIC-source',
           'PRIVATE-HOUSEHOLD-PROMPT', 'PRIVATE-VOICE-IMAGE', 'PRIVATE-GRANT', 'PRIVATE-URL')
PRIVATE = (*PRIVATE, PHRASE)


def check_private(journal):
    encoded = json.dumps(journal.export())
    for value in PRIVATE:
        assert value not in encoded
    for row in journal.export()['events']:
        assert set(row) <= {'schema','sequence','at','session','correlation','stage','event','result','code','upstream','elapsed_ms','http_status'}
        assert len(row['correlation']) == 32 and len(row['session']) == 32
        assert 0 <= row['elapsed_ms'] <= 86400000
    if journal.directory and journal.directory.exists() and not journal.directory.is_symlink():
        for path in journal.directory.glob('runtime*.jsonl'):
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= 3 * FILE_BYTES:
                raw = path.read_text()
                for value in PRIVATE:
                    assert value not in raw


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*_a, **_k):
        raise AssertionError('No sockets/DNS are authorized by the synthetic logging qualification')
    for name in ('connect', 'connect_ex', 'bind', 'listen'):
        monkeypatch.setattr(socket.socket, name, blocked)
    monkeypatch.setattr(socket, 'getaddrinfo', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


@pytest.mark.asyncio
async def test_production_plan_approval_dispatch_readback_share_cross_request_correlation(tmp_path):
    journal = RuntimeLog(tmp_path.resolve() / 'logs', session='1' * 32)
    core, _, _, calls, _, _, _ = rig(tmp_path.resolve())
    try:
        with binding(journal):
            trace = await plan(core)
        with binding(journal):
            result = await core.actions.execute(trace.action_id)
        assert result.status == 'reported_match' and not result.physically_confirmed
        assert len(calls) == 1
        rows = [r for r in journal.rows if r['stage'] in {'action.plan','action.approve','device.call','device.readback'}]
        assert {r['stage'] for r in rows} == {'action.plan','action.approve','device.call','device.readback'}
        assert len({r['correlation'] for r in rows}) == 1
        assert all(r['session'] == '1' * 32 for r in rows)
        assert all(r['result'] == 'ok' for r in rows)
        check_private(journal)
    finally:
        with binding(journal):
            await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault_name, result_kind', [('deny','unavailable'),('timeout','timeout'),('cancel','cancelled')])
async def test_production_dispatch_failure_cancel_and_timeout_keep_business_outcome(tmp_path, fault_name, result_kind):
    journal = RuntimeLog(tmp_path.resolve() / 'logs')
    core, _, _, calls, fault, _, _ = rig(tmp_path.resolve())
    try:
        with binding(journal):
            trace = await plan(core)
            fault['send'] = fault_name
            if fault_name == 'cancel':
                with pytest.raises(asyncio.CancelledError):
                    await core.actions.execute(trace.action_id)
            else:
                result = await core.actions.execute(trace.action_id)
                assert result.status == ('failed' if fault_name == 'deny' else 'unknown_outcome')
        assert len(calls) == 1
        assert any(r['stage'] == 'device.call' and r['event'] == 'result' and r['result'] == result_kind for r in journal.rows)
        check_private(journal)
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_production_ha_auth_rejection_uses_fixed_code_not_provider_body(tmp_path):
    journal = RuntimeLog(tmp_path.resolve() / 'logs')
    core, _, _, _, fault, _, _ = rig(tmp_path.resolve())
    try:
        with binding(journal):
            fault['offline'] = True
            with pytest.raises(httpx.HTTPStatusError):
                await core.adapter._get('/PRIVATE-URL')
        assert any(r['stage'] == 'ha.read' and r['code'] == 'ha_http_error' for r in journal.rows)
        check_private(journal)
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_production_arl_bounded_worker_timeout_cancel_and_unavailable(tmp_path):
    journal = RuntimeLog(tmp_path.resolve() / 'logs')
    runtime = ARLRuntime(lambda: tmp_path / 'missing-runtime')
    with binding(journal):
        assert (await runtime.status())['ready'] is False
        release = threading.Event()
        try:
            with pytest.raises(ARLError) as error:
                await runtime._bounded(lambda _stop: release.wait(.5), .02)
            assert error.value.code == 'deadline_exceeded'
        finally:
            release.set()
        async with asyncio.timeout(1):
            while runtime._busy.locked():
                await asyncio.sleep(.001)
        release = threading.Event()
        entered = threading.Event()
        def worker(_stop):
            entered.set()
            release.wait(.5)
            raise ValueError('PRIVATE-HOUSEHOLD-PROMPT PRIVATE-GRANT')
        task = asyncio.create_task(runtime._bounded(worker, .3))
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        async with asyncio.timeout(1):
            while runtime._busy.locked():
                await asyncio.sleep(.001)
        await asyncio.sleep(.01)
    assert {'timeout','cancelled','unavailable'} <= {r['result'] for r in journal.rows}
    assert len([r for r in journal.rows if r['stage'] == 'arl.thread' and r['event'] == 'result']) == 2
    check_private(journal)


@pytest.mark.asyncio
async def test_production_nas_background_auth_failure_and_close(tmp_path):
    journal = RuntimeLog(tmp_path.resolve() / 'logs')
    transport = FakeTransport(connect_error=NASConnectionError('nas_auth_failed'))
    async def resolver(_reference):
        return CREDS
    manager = NASConnectionManager(tmp_path.resolve() / 'nas.json', credential_resolver=resolver,
                                   transport_factory=lambda: transport)
    with binding(journal):
        try:
            await manager.configure(config(enabled=True), expected_generation=0)
            await wait_for(manager, 'paused')
            assert manager.status().failure == 'nas_auth_failed'
            with pytest.raises(NASConnectionError):
                await manager.stat('SYNTHETIC-source', 'PRIVATE-VOICE-IMAGE')
        finally:
            await manager.close()
    assert transport.closed and manager._task is None
    assert any(r['stage'] == 'nas.health' and r['code'] == 'nas_auth_failed' and r['result'] == 'unavailable' for r in journal.rows)
    assert any(r['stage'] == 'nas.connect' and r['code'] == 'nas_auth_failed' and r['event'] == 'result' for r in journal.rows)
    assert any(r['stage'] == 'nas.owner' and r['event'] == 'result' and r['result'] in {'ok','cancelled'} for r in journal.rows)
    check_private(journal)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_production_nas_positive_connect_then_read_timeout_or_cancel(tmp_path, cancel):
    journal = RuntimeLog(tmp_path.resolve() / 'logs')
    transport = FakeTransport(hang_read=True)
    async def resolver(_reference): return CREDS
    manager = NASConnectionManager(tmp_path.resolve() / 'nas.json', credential_resolver=resolver,
                                   transport_factory=lambda: transport)
    with binding(journal):
        try:
            await manager.configure(config(enabled=True, operation_timeout_seconds=.05), expected_generation=0)
            await wait_for(manager)
            page = await manager.list_page('SYNTHETIC-source')
            entry = page.entries[0]
            transport.entered.clear()
            task = asyncio.create_task(manager.read_range('SYNTHETIC-source',entry.object_ref,entry.revision,0,8))
            await transport.entered.wait()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
            else:
                with pytest.raises(NASConnectionError) as caught: await task
                assert caught.value.code == 'nas_timeout'
        finally:
            await manager.close()
    assert any(r['stage'] == 'nas.connect' and r['event'] == 'result' and r['result'] == 'ok' for r in journal.rows)
    assert any(r['stage'] == 'nas.read' and r['event'] == 'result' and r['result'] == ('cancelled' if cancel else 'timeout') for r in journal.rows)
    assert manager._task is None and transport.closed
    check_private(journal)


@pytest.mark.asyncio
async def test_production_desktop_diagnostics_before_connection_and_failure_lifecycle(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    journal = RuntimeLog(root / 'logs')
    for name in ('BASE','STATE'):
        monkeypatch.setattr(backend, name, root)
    for name in ('CONFIG','CREDENTIAL','NEW_CREDENTIAL'):
        monkeypatch.setattr(backend, name, root / (name + '.absent'))
    monkeypatch.setattr(backend, 'connection_configuration', lambda: {'configured':False})
    monkeypatch.setattr(backend, 'arl_runtime', lambda: ARLRuntime(lambda: root / 'missing-runtime'))
    async def failed_adapter(_renewal):
        raise backend.RealHomeFailure('ha_authorization_rejected', [{'private':'PRIVATE-GRANT PRIVATE-URL'}])
    monkeypatch.setattr(backend, 'real_adapter', failed_adapter)
    service = backend.DesktopService(runtime_log=journal)
    events = iter([{'type':'lifespan.startup'}, {'type':'lifespan.shutdown'}])
    messages = []
    async def receive(): return next(events)
    async def send(message): messages.append(message)
    await service({'type':'lifespan'}, receive, send)
    assert [m['type'] for m in messages] == ['lifespan.startup.complete','lifespan.shutdown.complete']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url=backend.ORIGIN,
                                headers={'Origin':backend.ORIGIN}) as client:
        diagnostic = await client.get('/api/diagnostics/runtime')
        assert diagnostic.status_code == 200 and diagnostic.json()['redacted'] is True
        assert diagnostic.headers['cache-control'] == 'no-store'
        connected = await client.post('/desktop/connect')
        assert connected.json()['connection'] == 'failed'
        refused = await client.post('/api/actions/execute', json={'secret':'PRIVATE-GRANT'})
        assert refused.status_code == 409
        diagnostic = await client.get('/api/diagnostics/runtime')
        assert 'PRIVATE-GRANT' not in diagnostic.text and 'PRIVATE-URL' not in diagnostic.text
    assert any(r['stage'] == 'desktop.connect' and r['code'] == 'ha_authorization_rejected' for r in journal.rows)
    assert any(r['stage'] == 'http.request' and r['http_status'] == 409 and r['code'] == 'desktop_read_only'
               for r in journal.rows if 'http_status' in r)
    assert {'service.start','service.stop'} <= {r['stage'] for r in journal.rows}
    assert service.arl_task.done()
    check_private(journal)


@pytest.mark.asyncio
async def test_production_desktop_busy_gate_refusal_keeps_HTTP_and_function_log_consistent(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    journal = RuntimeLog(root / 'logs')
    for name in ('BASE','STATE'):
        monkeypatch.setattr(backend, name, root)
    for name in ('CONFIG','CREDENTIAL','NEW_CREDENTIAL'):
        monkeypatch.setattr(backend, name, root / (name + '.absent'))
    monkeypatch.setattr(backend, 'connection_configuration', lambda: {'configured':False})
    monkeypatch.setattr(backend, 'arl_runtime', lambda: ARLRuntime(lambda: root / 'missing-runtime'))
    reads = []
    async def forbidden_adapter(_renewal):
        reads.append('forbidden')
        raise AssertionError('Busy refusal must never reach an adapter/credential/provider')
    monkeypatch.setattr(backend, 'real_adapter', forbidden_adapter)
    service = backend.DesktopService(runtime_log=journal)
    await service.gate.acquire()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url=backend.ORIGIN,
                                    headers={'Origin':backend.ORIGIN}) as client:
            refused = await client.post('/desktop/connect', json={'grant':'PRIVATE-GRANT','text':'PRIVATE-HOUSEHOLD-PROMPT'})
            assert refused.status_code == 409 and refused.json() == {'code':'connection_in_progress'}
        assert service.gate.locked() and service.inner is None and service.connection == 'not_connected'
        assert reads == []
        function = [r for r in journal.rows if r['stage'] == 'desktop.connect' and r['event'] == 'result']
        http = [r for r in journal.rows if r['stage'] == 'http.request' and r['event'] == 'result']
        assert len(function) == len(http) == 1
        assert function[0]['result'] == http[0]['result'] == 'refused'
        assert function[0]['code'] == http[0]['code'] == 'connection_in_progress'
        assert http[0]['http_status'] == 409
        assert function[0]['correlation'] == http[0]['correlation']
        check_private(journal)
    finally:
        service.gate.release()
        await service.close()


@pytest.mark.parametrize('response, expected', [
    (JSONResponse({'code':'connection_in_progress','credential':'PRIVATE-GRANT'}, status_code=409), ('refused','connection_in_progress')),
    (JSONResponse({'code':'PRIVATE-GRANT','text':'PRIVATE-HOUSEHOLD-PROMPT'}, status_code=409), ('refused','unclassified')),
    (JSONResponse({'code':{'grant':'PRIVATE-GRANT'}}, status_code=503), ('unavailable','unclassified')),
    (Response(b'PRIVATE-GRANT',status_code=403), ('refused','request_refused')),
    (Response(b'x' * 4097,status_code=409), ('refused','request_refused')),
    (Response(b'{"code":"connection_in_progress"}',status_code=504), ('timeout','connection_in_progress')),
    (StreamingResponse(iter([b'PRIVATE-GRANT']),status_code=503), ('unavailable','internal_error')),
    (JSONResponse({'credential':'PRIVATE-GRANT'}), ('ok','none')),
])
def test_production_Response_projection_only_fixed_bounded_code(response, expected):
    assert result_projection(response) == expected
    assert 'PRIVATE' not in json.dumps(result_projection(response))


@pytest.mark.asyncio
async def test_production_arl_exception_with_malformed_or_private_code_is_redacted(tmp_path):
    journal = RuntimeLog(tmp_path.resolve() / 'logs')
    runtime = ARLRuntime(lambda: tmp_path / 'missing-runtime')
    for code in ({'credential':'PRIVATE-GRANT'}, 'PRIVATE-HOUSEHOLD-PROMPT'):
        def worker(_stop):
            raise ARLError(code)
        with binding(journal):
            with pytest.raises(ARLError) as caught:
                await runtime._bounded(worker, .1)
        assert caught.value.code == code
    assert all(r['code'] == 'unclassified' for r in journal.rows if r['event'] == 'result')
    check_private(journal)


def test_retention_permissions_correlations_and_server_exception_redaction(tmp_path):
    journal = RuntimeLog(tmp_path.resolve() / 'logs', session='PRIVATE-GRANT')
    handler = SafeServerHandler(journal)
    handler.emit(logging.LogRecord('uvicorn.error', 40, 'PRIVATE-URL', 1,
                 'PRIVATE-GRANT PRIVATE-HOUSEHOLD-PROMPT', (), (ValueError,ValueError('PRIVATE-VOICE-IMAGE'),None)))
    for number in range(3000):
        with binding(journal, 'PRIVATE-HOUSEHOLD-PROMPT' + str(number)):
            journal.record('flow.turn','result',code='PRIVATE-GRANT',upstream='local')
        journal.link('PRIVATE-VOICE-IMAGE' + str(number), journal.correlate())
    assert len(journal.rows) == 256 and len(journal.links) == 256
    paths = list(journal.directory.iterdir())
    assert {p.name for p in paths} == {'runtime.jsonl','runtime.1.jsonl','runtime.2.jsonl'}
    assert all(p.stat().st_size <= FILE_BYTES and stat.S_IMODE(p.stat().st_mode) == 0o600 for p in paths)
    assert sum(p.stat().st_size for p in paths) <= 3 * FILE_BYTES
    assert stat.S_IMODE(journal.directory.stat().st_mode) == 0o700
    assert len(journal.export()['events']) == 64 and len(json.dumps(journal.export()).encode()) < 65536
    assert all(r['code'] == 'unclassified' for r in journal.rows)
    check_private(journal)


def test_independent_log_owners_share_bounded_nonblocking_rotation(tmp_path):
    directory = tmp_path.resolve() / 'logs'
    journals = [RuntimeLog(directory), RuntimeLog(directory)]
    def write(journal):
        for _ in range(1000): journal.record('service.start','result')
    threads = [threading.Thread(target=write,args=(j,)) for j in journals]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert all(not t.is_alive() for t in threads)
    files = list(directory.iterdir())
    assert len(files) <= 3 and all(p.stat().st_size <= FILE_BYTES for p in files)
    assert sum(p.stat().st_size for p in files) <= 3 * FILE_BYTES
    for journal in journals:
        assert journal.sequence == 1000 and len(journal.rows) == 256
        check_private(journal)


@pytest.mark.asyncio
async def test_production_console_bootstrap_and_preview_are_successful_safe_events(tmp_path):
    async with application(tmp_path) as (app, client):
        journal = app.state.runtime_log
        ready = await bootstrap(client)
        reply = await turn(client, request(ready['session_id'], text=PHRASE))
        assert reply['phase'] == 'preview'
        events = [r for r in journal.rows if r['stage'] in {'flow.bootstrap','flow.turn'} and r['event'] == 'result']
        assert len(events) == 2 and all(r['result'] == 'ok' for r in events)
        check_private(journal)


@pytest.mark.asyncio
async def test_production_bootstrap_cancel_keeps_owned_child_cleanup_and_cancel_code(tmp_path, monkeypatch):
    async with application(tmp_path) as (app, _client):
        journal = app.state.runtime_log
        entered = asyncio.Event()
        async def snapshot():
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(app.state.core, 'snapshot', snapshot)
        with binding(journal):
            task = asyncio.create_task(app.state.home_flow.bootstrap())
            await entered.wait()
            task.cancel()
            with pytest.raises(FlowAbort) as caught: await task
            assert str(caught.value) == 'flow_cancelled'
            await asyncio.sleep(0)
        assert not app.state.home_flow.bootstrap_tasks
        assert any(r['stage'] == 'flow.bootstrap' and r['event'] == 'result' and r['result'] == 'cancelled' for r in journal.rows)
        check_private(journal)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['symlink_directory','symlink_file','hardlink','permissions','oversize','fifo'])
async def test_log_sink_refuses_unsafe_files_without_changing_production_result(tmp_path, kind):
    root = tmp_path.resolve()
    directory = root / 'logs'
    target = root / 'untouched'
    target.write_bytes(b'PRIVATE-GRANT')
    target.chmod(0o600)
    if kind == 'symlink_directory':
        directory.symlink_to(root, target_is_directory=True)
    else:
        directory.mkdir(mode=0o700)
        path = directory / 'runtime.jsonl'
        if kind == 'symlink_file': path.symlink_to(target)
        elif kind == 'hardlink': os.link(target,path)
        elif kind == 'permissions': path.write_bytes(b''); path.chmod(0o644)
        elif kind == 'oversize': path.write_bytes(b'x' * (FILE_BYTES + 1)); path.chmod(0o600)
        elif kind == 'fifo': os.mkfifo(path,0o600)
    journal = RuntimeLog(directory)
    runtime = ARLRuntime(lambda: root / 'missing-runtime')
    with binding(journal):
        value = await runtime.status()
    assert value['ready'] is False
    assert journal.export()['code'] == 'logging_unavailable'
    assert journal.disk_available is False and journal.disk_failures == 2
    assert target.read_bytes() == b'PRIVATE-GRANT'
    assert all('PRIVATE-GRANT' not in json.dumps(r) for r in journal.rows)
