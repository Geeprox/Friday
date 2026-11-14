"""Fresh setup uses only temporary settings; no HA, camera, audio or model starts."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from homeplane.ha import HAReadOnlyAdapter
from homeplane.local_camera_home import LocalCameraHome
from homeplane.onboarding import SetupConfiguration, setup_report
from homeplane.settings import SettingsService

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'http://127.0.0.1:8740'


def test_setup_inventory_is_passive_and_does_not_expose_camera_id(tmp_path):
    settings = SettingsService(tmp_path / 'settings.json')
    settings.update({'ha_url': 'http://home.example:8123', 'camera_id': 'SYNTHETIC-PRIVATE-ID'})
    report = setup_report(settings, {'transport': 'simulated', 'simulated': True})
    assert not report['home_readable'] and report['simulated']
    assert report['camera_id_configured'] is True
    assert 'SYNTHETIC-PRIVATE-ID' not in str(report)
    assert report['device_writes'] == 'unchanged'
    with pytest.raises(ValueError):
        SetupConfiguration.model_validate({'ha_url': 'http://home.example', 'token': 'not-a-real-secret'})


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv('FRIDAY_HOME', str(tmp_path))
    monkeypatch.setenv('FRIDAY_SETTINGS', str(tmp_path / 'settings.json'))
    monkeypatch.setenv('FRIDAY_EMBODIED_DATA', str(tmp_path / 'embodied'))
    spec = importlib.util.spec_from_file_location('setup_test_backend', ROOT / 'native/FridayDesktop/backend/desktop_backend.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from homeplane import ha_setup
    monkeypatch.setattr(ha_setup, 'CREDENTIAL_PATH', tmp_path / 'oauth.json')
    backend = module.DesktopService()
    assert backend.inner is None
    return backend, module


@pytest.mark.asyncio
async def test_fresh_desktop_setup_is_reachable_without_real_adapter(fresh):
    backend, _ = fresh
    async with httpx.AsyncClient(transport=httpx.ASGITransport(backend), base_url=ORIGIN) as client:
        assert (await client.get('/')).headers['location'] == '/desktop/setup'
        page = await client.get('/desktop/setup')
        assert page.status_code == 200 and 'connection-setup-page.js' in page.text
        for name in ['style.css', 'interface.css', 'connection-setup.css', 'connection-setup.js', 'connection-setup-page.js']:
            assert (await client.get('/desktop/setup/assets/' + name)).status_code == 200
        assert (await client.get('/desktop/setup/assets/settings.json')).status_code == 404
        report = (await client.get('/api/settings/setup')).json()
        assert report['desktop'] and report['ha_url'] is None and not report['home_readable']
        assert backend.inner is None


@pytest.mark.asyncio
async def test_setup_configure_validates_origin_and_preserves_unrelated_values(fresh):
    backend, module = fresh
    backend.setup_settings().update({'nas_path': '/synthetic/storage'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(backend), base_url=ORIGIN) as client:
        payload = {'ha_url': 'http://synthetic-ha.example:8123', 'ha_instance': 'SYNTHETIC'}
        for origin in [None, 'https://cross-site.example']:
            headers = {'Origin': origin} if origin else {}
            assert (await client.post('/api/settings/setup', json=payload, headers=headers)).status_code == 403
        response = await client.post('/api/settings/setup', json=payload, headers={'Origin': ORIGIN})
        assert response.status_code == 200 and response.json()['ha_url'] == payload['ha_url']
        assert backend.setup_settings().config.nas_path == '/synthetic/storage'
        auth = await client.post('/api/settings/ha/connect', json={}, headers={'Origin': ORIGIN})
        assert auth.status_code == 200
        assert auth.json()['authorization_url'].startswith(payload['ha_url'] + '/auth/authorize?')
        for bad in [{'ha_url': 'http://user:password@example.invalid'}, {'ha_url': 'http://home.example/?token=x'}, {'device_writes': True}]:
            assert (await client.post('/api/settings/setup', json=bad, headers={'Origin': ORIGIN})).status_code == 400
        assert (await client.post('/api/settings/setup', content=b'x' * 2049, headers={'Origin': ORIGIN})).status_code == 413
        assert backend.inner is None and module.config().ha_url == payload['ha_url']


@pytest.mark.asyncio
async def test_discovery_is_explicit_and_does_not_select_multiple_instances(fresh):
    backend, _ = fresh
    calls = []
    async def scan():
        calls.append(True)
        return [{'instance_id': 'SYNTHETIC-A', 'addresses': ['http://one.example:8123']},
                {'instance_id': 'SYNTHETIC-B', 'addresses': ['http://two.example:8123']}]
    backend.setup_discovery.scanner = scan
    async with httpx.AsyncClient(transport=httpx.ASGITransport(backend), base_url=ORIGIN) as client:
        await client.get('/api/settings/setup')
        assert not calls
        response = (await client.get('/api/settings/ha/discover')).json()
        assert response['selection_required'] is True and len(calls) == 1
        assert backend.setup_settings().config.ha_url is None


@pytest.mark.asyncio
async def test_changed_address_is_not_reported_as_readable(fresh):
    backend, _ = fresh
    backend.setup_settings().update({'ha_url': 'http://new.example'})
    backend.inner = SimpleNamespace(state=SimpleNamespace(
        home_experience=SimpleNamespace(connection_health=lambda: {'transport': 'online'}),
        core=SimpleNamespace(adapter=SimpleNamespace(url='http://old.example'),actions=SimpleNamespace(policy_report=lambda:{'enabled':False})),
        embodied=SimpleNamespace(configured=lambda: False)))
    report = await backend.setup_status()
    assert b'"home_readable":false' in report.body


@pytest.mark.asyncio
async def test_mijia_source_requires_local_origin_and_preserves_active_ha_until_readback(fresh, monkeypatch):
    backend, module = fresh
    backend.persistent = True
    backend.setup_settings().update({'ha_url': 'http://synthetic-ha.example:8123'})
    module.STATE.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr('homeplane.embodied.service.mijia_runtime_configured', lambda path: True)
    old = HAReadOnlyAdapter('http://synthetic-ha.example:8123', 'synthetic-token', 'fixture')
    old.health = lambda: {'mode': 'ha-read-only', 'transport': 'online', 'authentication_available': True,
                          'observation_gap': False}
    missing_targets = set()
    power = SimpleNamespace(qualify_source=AsyncMock(return_value=None), connected_targets=lambda: (None, {}),
                            unqualified_references=lambda _targets: missing_targets,
                            uses_lan_home=lambda source: source is not old, ready_count=lambda: 1)
    rules = [SimpleNamespace(enabled=True)]
    core = SimpleNamespace(adapter=old, actions=SimpleNamespace(mijia_power_home=power,
                           policy_report=lambda: {'enabled': False}))
    embodied = SimpleNamespace(configured=lambda: True, discovery_report={'state': 'ready'})

    async def attach_mijia(*, current, commit):
        assert current()
        commit()
        core.adapter = LocalCameraHome()
        return old, None

    backend.connection = 'connected'
    backend.adapter = old
    backend.inner = SimpleNamespace(state=SimpleNamespace(core=core, embodied=embodied,
        home_experience=SimpleNamespace(connection_health=lambda: core.adapter.health()),
        core_rules=SimpleNamespace(records=lambda: rules), attach_mijia=attach_mijia))
    monkeypatch.setattr(module, 'real_adapter', AsyncMock(side_effect=AssertionError('HA must not be contacted')))
    transport = httpx.ASGITransport(backend)
    path = '/desktop/home-source'
    command = {'source': 'mijia-lan'}
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        assert (await client.post(path, json=command)).status_code == 403
        blocked = await client.post(path, json=command, headers={'Origin': ORIGIN})
        assert blocked.json()['code'] == 'ha_rules_in_use'
        assert power.qualify_source.await_count == 0
        rules.clear()
        missing_targets.add('synthetic-target')
        blocked = await client.post(path, json=command, headers={'Origin': ORIGIN})
        assert blocked.json()['code'] == 'mijia_interaction_targets_missing'
        missing_targets.clear()
        missing = await client.post(path, json=command, headers={'Origin': ORIGIN})
        assert missing.json()['code'] == 'mijia_readback_required'
        assert module.home_source() is None and backend.adapter is old
        power.qualify_source.return_value = lambda: True
        result = await client.post(path, json=command, headers={'Origin': ORIGIN})
        assert result.is_success and result.json()['source'] == 'mijia-lan'
        report = (await client.get('/api/settings/setup')).json()
        assert report['active_source'] == 'mijia-lan' and report['home_readable']
        assert report['mijia']['ready'] == 1
        assert backend.inner.state.embodied is embodied
    assert json.loads((module.STATE / 'home-source.json').read_text()) == command
    assert backend.setup_settings().config.ha_url == 'http://synthetic-ha.example:8123'
    assert (await backend.connect())['homeSource'] == 'mijia-lan'
    assert not module.real_adapter.await_count


@pytest.mark.asyncio
async def test_saved_mijia_choice_skips_ha_on_cold_boot(fresh, monkeypatch):
    backend, module = fresh
    backend.persistent = True
    module.STATE.mkdir(parents=True, exist_ok=True)
    backend.setup_settings().update({'ha_url': 'http://synthetic-ha.example:8123'})
    module.save_home_source('mijia-lan')
    monkeypatch.setattr(module, 'real_adapter', AsyncMock(side_effect=AssertionError('HA must not be contacted')))
    monkeypatch.setattr(module, 'local_people_catalog', lambda: None)
    monkeypatch.setattr(module, 'prepare_private_workspace', lambda: None)
    from homeplane.embodied.service import EmbodiedService

    monkeypatch.setattr(EmbodiedService, 'configured', lambda _: False)
    monkeypatch.setattr(EmbodiedService, 'start_mijia_discovery', lambda _: None)
    try:
        report = await backend._connect()
        assert report['connection'] == 'connected' and report['homeSource'] == 'mijia-lan'
        assert report['providerReads'] == [] and report['failure'] is None
        assert isinstance(backend.adapter, LocalCameraHome)
        assert isinstance(backend.inner.state.core.adapter, LocalCameraHome)
        assert not module.real_adapter.await_count
    finally:
        await backend.close()
