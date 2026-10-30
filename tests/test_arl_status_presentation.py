"""Actual desktop metadata response consumed by the Web presentation; no live SDK."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from homeplane.arl import ARLError, ARLRuntime


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv('FRIDAY_HOME', str(tmp_path.resolve()))
    monkeypatch.setenv('FRIDAY_EMBODIED_DATA', str((tmp_path / 'embodied').resolve()))
    monkeypatch.setenv('FRIDAY_SETTINGS', str(tmp_path / 'settings.json'))
    spec = importlib.util.spec_from_file_location(
        'arl_presentation_backend', ROOT / 'native/FridayDesktop/backend/desktop_backend.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def web_projection(report, *, runtime=False):
    if shutil.which('node') is None:
        pytest.skip('Node unavailable for actual Web metadata consumer')
    script = r"""
import fs from 'node:fs';
const source=fs.readFileSync(process.argv[1], 'utf8');
const {arlPresentation,arlRuntimePresentation}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const report=JSON.parse(fs.readFileSync(0,'utf8'));
const project=process.argv[2]==='runtime'?arlRuntimePresentation:arlPresentation;
process.stdout.write(JSON.stringify(project(report)));
"""
    result = subprocess.run(
        ['node', '--input-type=module', '-e', script, str(ROOT / 'homeplane/static/arl-status.js'),
         'runtime' if runtime else 'desktop'],
        input=json.dumps(report), text=True, capture_output=True, timeout=10, check=True)
    return json.loads(result.stdout)


class MetadataPeer:
    def __init__(self):
        self.error = None
        self.calls = []

    def rpc(self, method):
        self.calls.append(method)
        assert method == 'overview', 'Metadata presentation may not submit a model task'
        if self.error:
            raise ARLError(self.error)
        return {
            'schema_version': 1, 'protocol': 'arl/1', 'version': 'synthetic',
            'health': {'live': True, 'ready': True},
            'policy': {'local_inference_enabled': True, 'identity_verifier_configured': True},
            'providers': [{'name': 'synthetic-provider', 'availability': 'ready', 'models': ['synthetic-model']}],
            'capabilities': [],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected', [
    ('fresh', 'connected'), ('pending', 'refreshing'), ('stale', 'stale'), ('missing', 'unknown'),
])
async def test_runtime_snake_case_uses_the_same_backend_health_projection(backend, tmp_path, kind, expected):
    peer = MetadataPeer()
    runtime = ARLRuntime(lambda: tmp_path.resolve(), settings_client_factory=lambda _: peer)
    if kind == 'missing':
        value = web_projection(None, runtime=True)
        assert value['state'] == expected and value['connected'] is False
        assert peer.calls == []
    else:
        raw = await runtime.status(start=False, timeout=3)
        if kind == 'pending':
            assert runtime._busy.acquire(blocking=False)
            try:
                raw = await runtime.status(start=False, timeout=3)
            finally:
                runtime._busy.release()
        elif kind == 'stale':
            # An actual retained metadata snapshot with no check in progress.
            raw = {**raw, 'metadata_freshness': 'last_known', 'metadata_refresh_pending': False}
        # Upstream declarations are not Friday's verified task capability.
        raw = {**raw, 'identity_verified': True, 'model_capability_verified': True,
               'identity': {'verified': True}, 'task_readiness': 'ready'}

        class CapturedMetadata:
            async def status(self, **kwargs):
                assert kwargs == {'start': False, 'timeout': 3}
                return raw

        desktop = await backend.arl_status(CapturedMetadata())
        value = web_projection(raw, runtime=True)
        assert value == web_projection(desktop)
        assert value['state'] == expected
        assert peer.calls == ['overview']
    assert value['taskReadiness'] == 'unverified'
    assert value['identityVerified'] is False and value['modelCapabilityVerified'] is False


@pytest.mark.asyncio
async def test_actual_runtime_health_does_not_qualify_identity_or_model_task(backend, tmp_path):
    peer = MetadataPeer()
    runtime = ARLRuntime(lambda: tmp_path.resolve(), settings_client_factory=lambda _: peer)
    report = await backend.arl_status(runtime)
    presentation = web_projection(report)
    assert report['connectionState'] == presentation['state'] == 'connected'
    assert presentation['connected'] is True
    assert presentation['label'] == '通信正常'
    assert '身份与模型能力尚未验证' in presentation['description']
    for value in (report, presentation):
        assert value['taskReadiness'] == 'unverified'
        assert value['identityVerified'] is False
        assert value['modelCapabilityVerified'] is False
    assert report['inferenceAttempted'] is False
    assert peer.calls == ['overview']
    assert 'synthetic-provider' not in json.dumps(report)


@pytest.mark.asyncio
async def test_cached_health_and_unknown_refresh_never_claim_current_connection(backend, tmp_path):
    peer = MetadataPeer()
    runtime = ARLRuntime(lambda: tmp_path.resolve(), settings_client_factory=lambda _: peer)
    await backend.arl_status(runtime)
    assert runtime._busy.acquire(blocking=False)
    try:
        report = await backend.arl_status(runtime)
    finally:
        runtime._busy.release()
    presentation = web_projection(report)
    assert report['ready'] is True  # Existing compatibility field is historical.
    assert report['connectionState'] == presentation['state'] == 'refreshing'
    assert presentation['connected'] is False
    assert '上次健康通信正常' in presentation['description']
    assert peer.calls == ['overview']

    cold = ARLRuntime(lambda: tmp_path.resolve(), settings_client_factory=lambda _: peer)
    assert cold._busy.acquire(blocking=False)
    try:
        report = await backend.arl_status(cold)
    finally:
        cold._busy.release()
    presentation = web_projection(report)
    assert report['connectionState'] == presentation['state'] == 'refreshing'
    assert '正在检查' in presentation['description']
    assert presentation['connected'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('error,state,caption', [
    ('disconnected', 'disconnected', '连接检查失败'),
    ('user_disabled', 'disabled', '已停用'),
    ('sdk_missing', 'sdk_missing', '尚未安装'),
    ('not_running', 'not_running', '尚未运行'),
    ('unsafe_home', 'unsafe_configuration', '未通过安全检查'),
])
async def test_confirmed_failure_has_actionable_recovery(backend, tmp_path, error, state, caption):
    peer = MetadataPeer()
    peer.error = error
    runtime = ARLRuntime(lambda: tmp_path.resolve(), settings_client_factory=lambda _: peer)
    report = await backend.arl_status(runtime)
    presentation = web_projection(report)
    assert report['connectionState'] == presentation['state'] == state
    assert presentation['connected'] is False
    assert caption in presentation['description']
    assert presentation['recovery']
    assert report['taskReadiness'] == 'unverified'


@pytest.mark.parametrize('report,expected', [
    (None, 'unknown'),
    ({'status': 'ready', 'ready': True}, 'unknown'),
    ({'status': 'ready', 'ready': True, 'metadataFreshness': 'unknown'}, 'unknown'),
    ({'status': 'ready', 'ready': True, 'metadataFreshness': 'unavailable'}, 'unknown'),
    ({'status': 'user_disabled', 'ready': True, 'metadataFreshness': 'current'}, 'disabled'),
    ({'status': 'ready', 'ready': True, 'metadataFreshness': 'current', 'refreshPending': True}, 'refreshing'),
    ({'status': 'starting', 'ready': False, 'metadataFreshness': 'current'}, 'starting'),
    ({'status': 'ready', 'ready': True, 'metadataFreshness': 'last_known'}, 'stale'),
])
def test_legacy_conflicting_or_incomplete_reports_do_not_claim_health(report, expected):
    presentation = web_projection(report)
    assert presentation['state'] == expected
    assert presentation['connected'] is False
    assert presentation['taskReadiness'] == 'unverified'
    assert '已就绪' not in presentation['description']


@pytest.mark.asyncio
async def test_last_known_without_pending_does_not_claim_an_active_check(backend):
    class HistoricalRuntime:
        async def status(self, **kwargs):
            assert kwargs == {'start': False, 'timeout': 3}
            return {'status': 'ready', 'ready': True, 'metadata_freshness': 'last_known',
                    'metadata_refresh_pending': False}

    report = await backend.arl_status(HistoricalRuntime())
    presentation = web_projection(report)
    assert report['connectionState'] == presentation['state'] == 'stale'
    assert presentation['connected'] is False
    assert presentation['label'] == '上次记录'
    assert '当前连接待核对' in presentation['description']
    assert '正在' not in presentation['description']


@pytest.mark.asyncio
async def test_native_and_web_use_the_same_actual_backend_responses(backend, tmp_path):
    if shutil.which('xcrun') is None:
        pytest.skip('macOS Swift compiler unavailable for native metadata consumer')
    peer = MetadataPeer()
    runtime = ARLRuntime(lambda: tmp_path.resolve(), settings_client_factory=lambda _: peer)
    reports = [await backend.arl_status(runtime)]
    class HistoricalRuntime:
        async def status(self, **_kwargs):
            return {'status': 'ready', 'ready': True, 'metadata_freshness': 'last_known',
                    'metadata_refresh_pending': False}
    reports.append(await backend.arl_status(HistoricalRuntime()))
    assert runtime._busy.acquire(blocking=False)
    try:
        reports.append(await backend.arl_status(runtime))
    finally:
        runtime._busy.release()
    cold = ARLRuntime(lambda: tmp_path.resolve(), settings_client_factory=lambda _: peer)
    assert cold._busy.acquire(blocking=False)
    try:
        reports.append(await backend.arl_status(cold))
    finally:
        cold._busy.release()
    for error in ('disconnected', 'user_disabled', 'sdk_missing', 'not_running', 'unsafe_home'):
        peer.error = error
        reports.append(await backend.arl_status(runtime))
    binary = tmp_path / 'ARLStatusConsumerProbe'
    subprocess.run([
        'xcrun', 'swiftc', '-module-cache-path', str(tmp_path / 'swift-cache'),
        str(ROOT / 'native/FridayDesktop/Models/DesktopStatus.swift'),
        str(ROOT / 'native/FridayDesktop/tests/ARLStatusConsumerProbe.swift'), '-o', str(binary),
    ], capture_output=True, text=True, timeout=60, check=True)
    result = subprocess.run([str(binary), '--project'], input=json.dumps(reports),
                            text=True, capture_output=True, timeout=10, check=True)
    native = json.loads(result.stdout)
    for report, projection in zip(reports, native, strict=True):
        web = web_projection(report)
        assert projection == {key: web[key] for key in ('state', 'description', 'recovery')}
        assert projection['state'] == report['connectionState']
