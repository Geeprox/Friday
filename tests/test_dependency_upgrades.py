"""Offline dependency transactions; no real containers, accounts or devices."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / 'scripts/setup-dependencies.py'
    spec = importlib.util.spec_from_file_location('dependency_upgrade_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    directory = tmp_path / 'data/deployment'
    existing = directory / 'homeassistant/custom_components/xiaomi_home'
    existing.mkdir(parents=True)
    (existing / 'manifest.json').write_text(json.dumps({'domain': 'xiaomi_home', 'version': 'v0.4.3'}))
    (existing / 'original.py').write_text('VERSION = "old"\n')
    credentials = directory / 'homeassistant/.storage/account-fixture'
    credentials.parent.mkdir()
    credentials.write_text('synthetic-local-state')
    cache = tmp_path / 'data/upstream' / ('ha-xiaomi-home-' + module.XIAOMI_HOME_VERSION)
    (cache / '.git').mkdir(parents=True)
    component = cache / 'custom_components/xiaomi_home'
    component.mkdir(parents=True)
    (component / 'manifest.json').write_text(json.dumps({'domain': 'xiaomi_home', 'version': module.XIAOMI_HOME_VERSION}))
    (component / 'updated.py').write_text('VERSION = "new"\n')
    fixture = SimpleNamespace(directory=directory, existing=existing, credentials=credentials,
                              component=component, running=False, mounted=directory / 'homeassistant',
                              commit=module.XIAOMI_HOME_COMMIT, dirty='', calls=[])
    def run(argv, **_):
        fixture.calls.append(argv)
        if argv[0] == 'git':
            if 'rev-parse' in argv:
                return SimpleNamespace(stdout=fixture.commit + '\n', returncode=0)
            if 'status' in argv:
                return SimpleNamespace(stdout=fixture.dirty, returncode=0)
            raise AssertionError('No clone/network is permitted in this fixture')
        assert argv[:2] == ['fixture-docker', 'inspect']
        return SimpleNamespace(stdout=json.dumps([{'Id': 'fixture-container', 'State': {'Running': fixture.running},
                               'Mounts': [{'Destination': '/config', 'Type': 'bind', 'Source': str(fixture.mounted)}]}]),
                               returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    return module, fixture


def test_existing_install_requires_explicit_upgrade_without_touching_source(setup):
    module, fixture = setup
    with pytest.raises(ValueError, match='explicit_offline_upgrade'):
        module.install_xiaomi(fixture.directory)
    assert fixture.calls == []
    assert (fixture.existing / 'original.py').is_file()


def test_running_container_is_refused_before_any_component_change(setup):
    module, fixture = setup
    fixture.running = True
    with pytest.raises(ValueError, match='stop_home_assistant'):
        module.install_xiaomi(fixture.directory, upgrade=True, container='fixture-container', docker='fixture-docker')
    assert (fixture.existing / 'original.py').is_file()
    assert not (fixture.directory / 'integration-backups').exists()


def test_wrong_container_mount_is_refused(setup):
    module, fixture = setup
    fixture.mounted = fixture.directory / 'another-config'
    with pytest.raises(ValueError, match='mount_mismatch'):
        module.install_xiaomi(fixture.directory, upgrade=True, container='fixture-container', docker='fixture-docker')
    assert (fixture.existing / 'original.py').is_file()


def test_upgrade_keeps_account_state_and_preserves_previous_component(setup):
    module, fixture = setup
    result = module.install_xiaomi(fixture.directory, upgrade=True, container='fixture-container', docker='fixture-docker')
    assert result['version'] == 'v0.5.0' and result['previous_version'] == 'v0.4.3'
    assert result['configuration_retained'] and not result['account_login_required']
    assert result['services_started'] is False
    assert (fixture.existing / 'updated.py').is_file()
    assert (Path(result['backup']) / 'original.py').is_file()
    assert fixture.credentials.read_text() == 'synthetic-local-state'
    assert sum(argv[0] == 'fixture-docker' for argv in fixture.calls) == 2


def test_failed_activation_restores_original_component(setup, monkeypatch):
    module, fixture = setup
    rename = Path.rename
    def fail_new_install(path, destination):
        if path.name == 'xiaomi_home' and path.parent.name.startswith('.xiaomi-stage-'):
            raise OSError('synthetic activation failure')
        return rename(path, destination)
    monkeypatch.setattr(Path, 'rename', fail_new_install)
    with pytest.raises(OSError, match='synthetic activation'):
        module.install_xiaomi(fixture.directory, upgrade=True, container='fixture-container', docker='fixture-docker')
    assert (fixture.existing / 'original.py').is_file()
    assert not (fixture.existing / 'updated.py').exists()
    assert fixture.credentials.read_text() == 'synthetic-local-state'


@pytest.mark.parametrize('failure', ['commit', 'dirty', 'manifest', 'symlink'])
def test_unreviewed_source_cannot_replace_existing_component(setup, failure):
    module, fixture = setup
    if failure == 'commit':
        fixture.commit = '0' * 40
    elif failure == 'dirty':
        fixture.dirty = ' M component.py'
    elif failure == 'manifest':
        (fixture.component / 'manifest.json').write_text('{"domain":"different","version":"v0.5.0"}')
    else:
        (fixture.component / 'linked.py').symlink_to(fixture.credentials)
    with pytest.raises(ValueError, match='source_verification_failed'):
        module.install_xiaomi(fixture.directory, upgrade=True, container='fixture-container', docker='fixture-docker')
    assert (fixture.existing / 'original.py').is_file()


def test_private_directory_cannot_follow_symlink(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / 'scripts/setup-dependencies.py'
    spec = importlib.util.spec_from_file_location('dependency_path_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'data').symlink_to(outside)
    with pytest.raises(ValueError, match='private_data'):
        module.install_xiaomi(tmp_path / 'data/deployment')
    assert not list(outside.iterdir())
