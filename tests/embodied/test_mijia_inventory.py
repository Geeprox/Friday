import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
    report = await connect_camera(row['id'], login, tmp_path, config, {}, read_spec)
    assert report['state'] == 'connected'
    saved = json.loads(path.read_text())
    assert saved['cameras'][0] == config['cameras'][0]
    camera = saved['cameras'][1]
    assert camera['preview_only'] is True and not camera['allow_open'] and not camera['takeover']
    assert camera['device']['did'] == row['identity'].removeprefix('cn_')
    assert await connect_camera(row['id'], login, tmp_path, config, {}, read_spec) == report
    assert len(json.loads(path.read_text())['cameras']) == 2

    from homeplane.embodied import service as service_module
    monkeypatch.setattr(service_module, 'configuration', lambda: saved)
    service = EmbodiedService()
    try:
        service.discovery_report = {'state': 'ready', 'devices': rows, 'region': 'cn'}
        assert service.mijia_discovery()['devices'] == []
    finally:
        await service.close()


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
