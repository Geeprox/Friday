import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeplane.embodied.miot_spec import camera_can_take_motor, camera_power_writable, camera_properties

from homeplane.embodied import camera_interaction, mijia_inventory
from homeplane.embodied.mijia_inventory import connect_camera, home_scope, public_inventory, read_inventory
from homeplane.embodied.service import EmbodiedService


def house(name, dids):
    return SimpleNamespace(home_id=name, dids=dids, room_list={})


def fixture():
    camera = SimpleNamespace(did='synthetic-new', urn='urn:miot-spec-v2:device:camera:00:fixture.camera.one:1',
                             model='fixture.camera.one', name='新摄像头', online=True, room_id='test-room',
                             room_name='测试房间', home_id='test-home', sub_devices={})
    camera.model_dump = lambda **_: {'did': camera.did, 'token': 'synthetic-device-secret'}
    config = dict(version=1, enabled=True, main_origin='http://127.0.0.1:8740', viewer_port=8741,
                  cameras=[{'id': 'cam_1111111111', 'device': {'did': 'synthetic-registered'}}])
    home = house('test-home', ['synthetic-registered', camera.did])
    transport = SimpleNamespace(
        get_homes_async=AsyncMock(return_value={home.home_id: home, 'other-home': house('other-home', ['other'])}),
        get_devices_async=AsyncMock(return_value={camera.did: camera}),
        deinit_async=AsyncMock(),
    )
    login = SimpleNamespace(current=AsyncMock(return_value={'cloud_server': 'cn', 'access_token': 'synthetic-grant'}),
                            http=lambda **_: transport)
    return config, home, login, transport


@pytest.mark.asyncio
async def test_mijia_spec_preserves_supported_controls_without_human_close_up(monkeypatch):
    import httpx

    def prop(iid, name, access, kind='bool'):
        return dict(iid=iid, type=f'urn:miot-spec-v2:property:{name}:00000001', access=access, format=kind)

    def service(iid, name, *properties):
        return dict(iid=iid, type=f'urn:miot-spec-v2:service:{name}:00000001', properties=list(properties))

    urn = 'urn:miot-spec-v2:device:camera:00:fixture.camera.one:1'
    read, write = ['read', 'notify'], ['read', 'write', 'notify']
    raw = {'type': urn, 'services': [
        service(2, 'camera-control', prop(1, 'on', write), prop(2, 'ptz-camera', read),
                prop(3, 'human-tracking', write), prop(4, 'vendor-private', read)),
        service(3, 'cruise', prop(1, 'cruise-switch', write)),
        service(4, 'ai-detection', prop(1, 'pet-tracking', write)),
        service(5, 'camera-fav-area', prop(1, 'fav-area', read, 'string'),
                prop(2, 'active-fav-area', write, 'uint8')),
        service(6, 'indicator-light', prop(1, 'on', write)),
    ]}

    async def response_bytes():
        yield json.dumps(raw).encode()

    @asynccontextmanager
    async def stream(method, url, *, params):
        assert (method, url, params) == ('GET', 'https://miot-spec.org/miot-spec-v2/instance', {'type': urn})
        yield SimpleNamespace(raise_for_status=lambda: None, aiter_bytes=response_bytes)

    @asynccontextmanager
    async def client(**_):
        yield SimpleNamespace(stream=stream)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    spec = await mijia_inventory.camera_spec(SimpleNamespace(urn=urn))
    camera = dict(spec=spec, mijia_enrolled=True, ptz_supported=True)
    assert len(spec['services']) == 4
    assert camera_power_writable(camera) and camera_can_take_motor(camera)
    assert 'human_close_up' not in camera_properties(camera)
    assert len(spec['services'][0]['properties']) == 3


@pytest.mark.asyncio
async def test_discovery_uses_configured_home_and_does_not_register_until_confirmation(tmp_path, monkeypatch):
    config, home, login, transport = fixture()
    path = tmp_path / 'config.json'
    before = json.dumps(config, ensure_ascii=False)
    path.write_text(before)
    path.chmod(0o600)
    region, candidates = await read_inventory(login, config, {'home_id': 'old-import'})
    transport.get_devices_async.assert_awaited_once_with(home_infos=[home], fetch_share_home=True)
    rows = public_inventory(region, candidates)
    row, = rows
    assert row['kind'] == 'camera' and row['name'] == '新摄像头'
    assert 'synthetic-device-secret' not in json.dumps(rows)
    assert path.read_text() == before
    spec = {'services': []}
    read_spec = AsyncMock(return_value=spec)
    read_ptz = AsyncMock(return_value=True)
    report = await connect_camera(row['id'], login, tmp_path, config, {}, read_spec, read_ptz)
    assert report['state'] == 'connected'
    saved = json.loads(path.read_text())
    assert saved['cameras'][0] == config['cameras'][0]
    camera = saved['cameras'][1]
    assert camera['mijia_enrolled'] is True and camera['ptz_supported'] is True
    assert not camera.get('preview_only') and not camera['allow_open'] and not camera['takeover']
    assert camera_interaction.allowed(camera['id'], directory=tmp_path)
    assert camera['device']['did'] == row['identity'].removeprefix('cn_')
    assert await connect_camera(row['id'], login, tmp_path, saved, {}, read_spec, read_ptz) == report
    assert len(json.loads(path.read_text())['cameras']) == 2

    from homeplane.embodied import service as service_module
    monkeypatch.setattr(service_module, 'configuration', lambda: saved)
    service = EmbodiedService()
    try:
        service.discovery_report = {'state': 'ready', 'devices': rows, 'region': 'cn'}
        assert service.mijia_discovery()['devices'] == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_previously_connected_camera_keeps_identity_and_gets_interaction(tmp_path, monkeypatch):
    config, _, login, devices = fixture()
    camera = next(iter((await devices.get_devices_async()).values()))
    legacy = dict(id='cam_2222222222', device={'did': camera.did}, region='cn',
                  room='自定义房间', preview_only=True, allow_open=False, takeover=False)
    config['cameras'].append(legacy)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setattr(mijia_inventory, 'camera_spec', AsyncMock(return_value={'services': []}))
    monkeypatch.setattr(mijia_inventory, 'ptz_readback', AsyncMock(return_value=False))

    result = await mijia_inventory.upgrade_registered_cameras(login, tmp_path, config, {})

    assert result == {'state': 'ready', 'upgraded': [legacy['id']]}
    saved = json.loads(path.read_text())
    upgraded = saved['cameras'][1]
    assert upgraded['id'] == legacy['id'] and upgraded['room'] == legacy['room']
    assert upgraded['mijia_enrolled'] is True and not upgraded['allow_open']
    assert 'preview_only' not in upgraded
    assert camera_interaction.allowed(legacy['id'], directory=tmp_path) is True


def test_camera_interaction_defaults_on_and_preserves_user_preference(tmp_path, monkeypatch):
    camera_id = 'cam_2222222222'
    folder = tmp_path / 'cameras' / camera_id
    folder.mkdir(parents=True)
    monkeypatch.setattr(camera_interaction, 'camera_storage', lambda _: folder)
    assert camera_interaction.allowed(camera_id, directory=tmp_path)
    assert camera_interaction.save(camera_id, False, 0).revision == 1
    assert camera_interaction.allowed(camera_id, directory=tmp_path) is False
    with pytest.raises(ValueError, match='camera_interaction_changed'):
        camera_interaction.save(camera_id, True, 0)
    assert camera_interaction.save(camera_id, True, 1).revision == 2
    assert camera_interaction.allowed(camera_id, directory=tmp_path)


def test_discovery_rejects_ambiguous_homes():
    config, home, _, _ = fixture()
    with pytest.raises(ValueError, match='home_selection_required'):
        home_scope([home, house('duplicate', home.dids)], config, {})


@pytest.mark.asyncio
async def test_shared_device_deduplicates_by_identity_and_preview_only_rejects_writes(monkeypatch):
    import homeplane.embodied.service as service_module

    config, _, _, _ = fixture()
    monkeypatch.setattr(service_module, 'configuration', lambda: config)
    monkeypatch.setattr(service_module, 'camera', lambda _: {'preview_only': True})
    service = EmbodiedService()
    try:
        service.discovery_report = {'state': 'ready', 'region': 'cn', 'devices': [
            {'id': 'ref1', 'identity': 'cn_same', 'name': '重复开关', 'room': '', 'kind': 'device', 'online': True},
            {'id': 'ref2', 'identity': 'cn_new', 'name': '新灯', 'room': '', 'kind': 'device', 'online': False},
        ]}
        rows = service.mijia_discovery({'cn_same'})['devices']
        assert [row['id'] for row in rows] == ['ref2']
        assert 'identity' not in rows[0]
        with pytest.raises(ValueError, match='camera_control_unavailable'):
            await service.camera_control('test', direction='left')
        with pytest.raises(ValueError, match='camera_power_unavailable'):
            await service.camera_power('test', enabled=True)
    finally:
        await service.close()
