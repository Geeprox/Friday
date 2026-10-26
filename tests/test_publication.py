"""Publication boundaries and capability resolution use only synthetic inputs."""
import importlib.util
from pathlib import Path

import pytest

from homeplane.embodied.miot_spec import cover_binding, properties


def script(name):
    path = Path(__file__).parents[1] / 'scripts' / name
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def spec_for(service, offset=0):
    def prop(i, name, access, **extra):
        return {'iid': i + offset, 'type': 'urn:miot-spec-v2:property:' + name + ':00000001',
                'access': access, **extra}
    return {'services': [{'iid': service, 'properties': [
        prop(1, 'current-position', ['read'], **{'value-range': [0, 100, 1]}),
        prop(2, 'target-position', ['read', 'write'], **{'value-range': [0, 100, 1]}),
        prop(3, 'fault', ['read']),
        prop(4, 'motor-control', ['write'], **{'value-list': [
            {'value': 8, 'description': 'Open'}, {'value': 3, 'description': 'Close'},
            {'value': 5, 'description': 'Stop'}]}),
    ]}]}


def config(spec):
    return {'miot_covers': {'imported': {'did': 'SYNTHETIC-DID', 'name': 'Imported shade',
                                        'room': 'Imported area', 'model': 'any-vendor.any-model', 'spec': spec}}}


@pytest.mark.parametrize('service,offset', [(7, 0), (29, 18)])
def test_different_models_and_property_ids_resolve_from_imported_spec(service, offset):
    result = cover_binding(config(spec_for(service, offset)))
    assert result['refs']['current-position'] == (service, 1 + offset)
    assert result['refs']['motor-control'] == (service, 4 + offset)
    assert result['commands'] == {'open': 8, 'close': 3, 'stop': 5}
    assert result['name'] == 'Imported shade' and result['room'] == 'Imported area'


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'not_writable', 'unknown_values', 'range'])
def test_unknown_or_ambiguous_cover_specs_cannot_grant_control(change):
    spec = spec_for(17)
    props = spec['services'][0]['properties']
    if change == 'missing':
        props.pop()
    elif change == 'duplicate':
        props.append(dict(props[-1]))
    elif change == 'not_writable':
        props[-1]['access'] = ['read']
    elif change == 'unknown_values':
        props[-1]['value-list'][0]['description'] = 'unverified vendor action'
    else:
        props[0]['value-range'] = [0, 255, 1]
    with pytest.raises(ValueError):
        cover_binding(config(spec))


def test_multiple_discovered_covers_require_selection():
    value = config(spec_for(3))
    value['miot_covers']['another'] = value['miot_covers']['imported']
    with pytest.raises(ValueError):
        cover_binding(value)
    value['selected_cover'] = 'another'
    assert cover_binding(value)['did'] == 'SYNTHETIC-DID'


@pytest.mark.parametrize('value', [None, [], {'services': False}, {'services': [None]}])
def test_malformed_vendor_specs_are_rejected(value):
    with pytest.raises(ValueError):
        properties(value)


def test_publication_scanner_checks_private_files_and_never_prints_match(tmp_path):
    audit = script('audit_publication.py')
    (tmp_path / 'README.md').write_text('A minimal generic project')
    assert audit.scan(tmp_path)['findings'] == []
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/private.json').write_text('{"private": "SYNTHETIC-HOUSEHOLD"}')
    result = audit.scan(tmp_path, ['SYNTHETIC-HOUSEHOLD'])
    assert result['findings'] == [{'path': 'data/private.json', 'rule': 'private_file'}]
    assert 'SYNTHETIC-HOUSEHOLD' not in str(result)


def test_publication_scanner_rejects_symlinks_and_unreviewed_media(tmp_path):
    audit = script('audit_publication.py')
    (tmp_path / 'real.txt').write_text('synthetic')
    (tmp_path / 'link.txt').symlink_to(tmp_path / 'real.txt')
    (tmp_path / 'capture.mp4').write_bytes(b'NOT-REAL-MEDIA')
    assert {f['rule'] for f in audit.scan(tmp_path)['findings']} == {'symlink', 'private_file'}


def test_personal_paths_are_checked_but_api_home_routes_are_not(tmp_path):
    audit = script('audit_publication.py')
    # Construct the synthetic path so the scanner can also audit its own tests.
    private_path = '/' + 'Users' + '/' + 'synthetic-owner' + '/private'
    (tmp_path / 'app.py').write_text("route='/api/home/layout'\npath=" + repr(private_path))
    findings = audit.scan(tmp_path)['findings']
    assert findings == [{'path': 'app.py', 'line': 2, 'rule': 'personal_home_path'}]


def test_dependency_prepare_is_private_and_never_starts_services(tmp_path, monkeypatch):
    setup = script('setup-dependencies.py')
    monkeypatch.setattr(setup, 'ROOT', tmp_path)
    directory = tmp_path / 'data/deployment'
    assert setup.prepare(directory) == {'prepared': True, 'services_started': False}
    assert (directory / 'compose.yaml').stat().st_mode & 0o777 == 0o600
    assert (directory / '.env').stat().st_mode & 0o777 == 0o600
    value = (directory / 'compose.yaml').read_text()
    assert 'network_mode: host' in value and 'privileged' not in value
    with pytest.raises(FileExistsError):
        setup.prepare(directory)
    assert not setup.plan()['services_started']


def test_dependency_prepare_cannot_follow_a_private_data_symlink(tmp_path, monkeypatch):
    setup = script('setup-dependencies.py')
    monkeypatch.setattr(setup, 'ROOT', tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'data').symlink_to(outside)
    with pytest.raises(ValueError):
        setup.prepare(tmp_path / 'data/deployment')
    assert not list(outside.iterdir())


@pytest.mark.asyncio
async def test_new_simulator_product_pages_do_not_require_a_live_adapter(tmp_path):
    import httpx
    from homeplane.api import create_app
    from homeplane.simulator import Simulator
    app = create_app(Simulator(), db_path=tmp_path/'traces.sqlite', settings_path=tmp_path/'settings.json')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as client:
            overview = await client.get('/api/home/overview')
            assert overview.status_code == 200
            assert overview.json()['simulated'] is True and overview.json()['connected'] is False
            connections = await client.get('/api/home/connections')
            assert connections.status_code == 200
            assert connections.json()['home']['transport'] == 'simulated'
            assert connections.json()['home']['simulated'] is True
            assert (await client.get('/api/home/layout')).json() is None
