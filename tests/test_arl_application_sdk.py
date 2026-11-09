"""Opt-in owning SDK on real private Unix sockets and synthetic model processes.

Set FRIDAY_TEST_ARL_PLATFORM_SOURCE to an explicitly selected ARL source checkout.
Nothing connects to the installed ARL host or invokes an account/model service.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from homeplane.arl import ARLError
from homeplane.arl_application import APP_ID, FridayApplication


@pytest.fixture
def sdk_host():
    source = os.environ.get('FRIDAY_TEST_ARL_PLATFORM_SOURCE')
    if not source:
        pytest.skip('Explicit owning application SDK source is required; no installed-host fallback')
    pytest.importorskip('arl.application_session')
    source = Path(source).resolve()
    from arl.application_platform import ApplicationPlatform
    from arl.client import Client
    from arl.codex_server import CodexService, CodexTextAdapter
    from arl.contracts import AppPolicy
    from arl.service import Runtime

    class Attachment:
        def start(self, runtime):
            # This SDK host lifecycle hook runs after its existing Store opens.
            runtime.application_platform = ApplicationPlatform(runtime)
        def begin_shutdown(self): pass
        def close(self, **kwargs): pass

    class Host:
        def __init__(self):
            self.temp = tempfile.TemporaryDirectory(prefix='friday-arl-sdk-', dir='/private/tmp')
            self.home = Path(self.temp.name)
            self.start()
        def start(self):
            self.route = CodexService(CodexTextAdapter(enabled=True,
                test_command=[sys.executable,'-I','-B',str(source/'tests/fixtures/CodexProductionServer.py')]),
                caller='synthetic-host', model='synthetic-model', max_calls=None, reasoning_effort='medium',
                authorization_current=lambda:True, peer_current=lambda:True, allow_model_selection=True,
                allow_test_command=True, package_home=self.home/'CodexPackages')
            policy = AppPolicy(frozenset(),frozenset(),0,0,0,
                methods=frozenset({'ready','settings.snapshot','settings.logs.v3'}), public_metadata=True)
            self.runtime = Runtime(self.home, policies={'settings':policy}, providers={}, local_enabled=False,
                extra_tools=[self.route.tool()], identity_verifier=lambda *_:None, codex_service=self.route,
                admission=self.route.admit, work_scope=frozenset(), host_attachment=Attachment(),
                application_catalog=lambda _:self.route.status()['execution_catalog'])
            self.route.runtime = self.runtime
            self.route.preflight()
            self.thread = threading.Thread(target=self.runtime.run, daemon=True)
            self.thread.start()
            Client(self.home,'settings').connect(timeout=8)
        def stop(self):
            self.runtime.shutdown()
            self.thread.join(12)
            assert not self.thread.is_alive()
    host = Host()
    try:
        yield host, source
    finally:
        host.stop()
        host.temp.cleanup()


async def registered(app, *, timeout=8, different_session=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if app.status()['connected'] and (different_session is None or app.session.client.app_session != different_session):
            return
        await asyncio.sleep(.025)
    raise AssertionError(app.status())


@pytest.mark.asyncio
async def test_real_sdk_registers_metadata_only_and_uses_one_shared_queue_with_frozen_package(sdk_host, tmp_path):
    host, _ = sdk_host
    app = FridayApplication(lambda: host.home, lambda: tmp_path.resolve(), allow_simulated=True)
    app.start()
    try:
        await registered(app)
        entry = host.runtime.control.apps.entries[APP_ID]
        assert entry['descriptor'].name == 'Friday'
        from arl.application_models import validate_icon
        from arl.contracts import Fault
        try:
            validate_icon(app.session.icon)
        except Fault as error:
            assert error.code == 'application_icon_invalid'
            assert not app.status()['icon']['registered']
            assert app.status()['icon']['error'] == 'application_icon_profile_unsupported'
        else:
            snapshot = host.runtime.application_models.icon_snapshot(APP_ID)
            assert app.status()['icon']['registered'] and snapshot['sha256'] == app.session.icon['sha256']
        assert (await app.ping())['application_registered']
        assert host.runtime.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        reply = await app.complete('voice_intent', 'synthetic input', request_id='friday-sdk-1', deadline=time.monotonic()+15)
        assert reply['simulated'] and reply['native_acknowledgements_verified'] and not reply['device_authority']
        assert reply['application_admission']['resolved']['reasoning_effort'] == 'low'
        again = await app.complete('voice_intent', 'synthetic input', request_id='friday-sdk-1', deadline=time.monotonic()+15)
        assert reply['application_admission'] == again['application_admission']
        assert host.runtime.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        other = await app.complete('home_planning', 'synthetic input', request_id='friday-sdk-2', deadline=time.monotonic()+15)
        assert other['application_admission']['resolved']['reasoning_effort'] == 'high'
        assert host.runtime.application_platform.snapshot()['max_parallel'] == 2
    finally:
        await app.close()
    assert host.runtime.control.apps.live_session(entry) is None
    from scripts.check_arl import check
    metadata = await check(host.home)
    assert metadata['status'] == 'PASS_APPLICATION_METADATA'
    assert metadata['application']['connected'] and metadata['model_tasks_submitted'] == 0
    assert host.runtime.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 2
    assert host.runtime.control.apps.live_session(entry) is None


@pytest.mark.asyncio
async def test_real_sdk_lease_expiry_auto_rehandshake_and_heartbeat_submit_no_models(sdk_host, tmp_path):
    host, _ = sdk_host
    app = FridayApplication(lambda: host.home, lambda: tmp_path.resolve())
    app.start()
    try:
        await registered(app)
        registry = host.runtime.control.apps
        entry = registry.entries[APP_ID]
        session = app.session.client.app_session
        initial = entry['session']['renewed_mono']
        end = time.monotonic() + 13
        while entry['session']['renewed_mono'] == initial and time.monotonic() < end:
            await asyncio.sleep(.05)
        assert entry['session']['renewed_mono'] > initial
        clock = registry.monotonic
        registry.monotonic = lambda: clock() + 31000
        assert registry.live_session(entry) is None
        await registered(app, timeout=16, different_session=session)
        assert host.runtime.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_real_sdk_rejects_duplicate_process_and_models_from_unregistered_scenarios(sdk_host, tmp_path):
    host, source = sdk_host
    app = FridayApplication(lambda: host.home, lambda: tmp_path.resolve())
    app.start()
    try:
        await registered(app)
        code = """import sys,json
sys.path.insert(0,sys.argv[1])
from arl.application_session import ApplicationSession
try:
 ApplicationSession(sys.argv[2],sys.argv[3],'Synthetic second process',[]).register()
except Exception as error:
 print(json.dumps({'code':getattr(error,'code','error')}))
"""
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, '-I', '-B', '-c', code, str(source), str(host.home), APP_ID],
            capture_output=True, text=True, timeout=12, env={'PATH':os.defpath})
        assert result.returncode == 0 and json.loads(result.stdout)['code'] == 'app_instance_conflict'
        with pytest.raises(ARLError, match='application_request_invalid'):
            await app.complete('undeclared', 'synthetic input', request_id='friday-sdk-3', deadline=time.monotonic()+1)
        assert app.status()['connected']
        assert host.runtime.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_real_sdk_reconnects_after_the_shared_host_restarts(sdk_host, tmp_path):
    host, _ = sdk_host
    app = FridayApplication(lambda: host.home, lambda: tmp_path.resolve(), allow_simulated=True)
    app.start()
    try:
        await registered(app)
        first = await app.complete('voice_intent', 'synthetic input', request_id='restart-request', deadline=time.monotonic()+15)
        previous = app.session.client.app_session
        host.stop()
        host.start()
        await registered(app, timeout=16, different_session=previous)
        assert host.runtime.control.apps.live_session(host.runtime.control.apps.entries[APP_ID])
        assert app.status()['icon']['registered']
        assert host.runtime.application_models.icon_snapshot(APP_ID)['sha256'] == app.session.icon['sha256']
        again = await app.complete('voice_intent', 'synthetic input', request_id='restart-request', deadline=time.monotonic()+15)
        assert first['application_admission'] == again['application_admission']
        assert host.runtime.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_application_jobs_obey_shared_one_and_two_parallel_settings(sdk_host, tmp_path):
    from dataclasses import replace
    host, _ = sdk_host
    app = FridayApplication(lambda:host.home, lambda:tmp_path.resolve(), allow_simulated=True)
    app.start()
    gate, entered = threading.Event(), []
    tool = host.runtime.tools[host.route.name]
    def hold(args, context):
        entered.append(context.row['id'])
        gate.wait(5)
        return tool.handler(args, context)
    host.runtime.tools[tool.name] = replace(tool, handler=hold)
    try:
        await registered(app)
        for parallel in (1, 2):
            host.runtime.application_platform.concurrency = parallel
            entered.clear(); gate.clear()
            tasks = [asyncio.create_task(app.complete('voice_intent','synthetic input',
                     request_id=f'parallel-{parallel}-{i}', deadline=time.monotonic()+15)) for i in range(2)]
            try:
                end = time.monotonic()+3
                while len(entered) < parallel and time.monotonic() < end:
                    await asyncio.sleep(.02)
                assert len(entered) == parallel
                snapshot = host.runtime.application_platform.snapshot()
                assert snapshot['running'] == parallel and snapshot['queued'] == 2-parallel
            finally:
                gate.set()
                results = await asyncio.gather(*tasks)
            assert all(result['native_acknowledgements_verified'] for result in results)
    finally:
        gate.set()
        await app.close()
