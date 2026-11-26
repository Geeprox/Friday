"""合成的米家目录、网关回复和本机 API。"""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from homeplane import mijia_control
from homeplane.api import create_app
from homeplane.embodied.mijia_lan import property_operation
from homeplane.mijia_lan_client import MijiaLanClient
from tests.test_mijia_lan_recovery import WorkerPipe


DEVICE = "100000001"
CANDIDATE = "a" * 64
FAMILY = "b" * 64
GRANT = "c" * 64
QUERY = dict(did=DEVICE, siid=2, piid=4, grant=GRANT, read="illuminance")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "mixed_write", "scope", "timeout", "invalid"])
async def test_worker_illuminance_only_reads_current_home(fault):
    current = True

    async def value(*args):
        nonlocal current
        assert args == (DEVICE, 2, 4)
        if fault == "scope":
            current = False
        if fault == "timeout":
            raise TimeoutError()
        return {"code": 0, "value": True if fault == "invalid" else 25.0}

    gateway = SimpleNamespace(local_device=lambda _: {"group_id": "fixture"}, can_control=lambda _: True,
                              in_local_cooldown=lambda _: False, get_prop_async=AsyncMock(side_effect=value),
                              set_prop_async=AsyncMock(), note_local_failure=Mock())
    request = dict(QUERY, power="on") if fault == "mixed_write" else QUERY
    if fault == "none":
        assert await property_operation(gateway, request, GRANT, "fixture", {DEVICE}, lambda: current) == {"value": 25.0}
    else:
        with pytest.raises(TimeoutError if fault == "timeout" else ValueError):
            await property_operation(gateway, request, GRANT, "fixture", {DEVICE}, lambda: current)
    assert gateway.get_prop_async.await_count == (0 if fault == "mixed_write" else 1)
    gateway.set_prop_async.assert_not_awaited()
    gateway.note_local_failure.assert_not_called()


@pytest.mark.asyncio
async def test_optional_illuminance_keeps_control_route_and_does_not_queue():
    client = MijiaLanClient(lambda: FAMILY)
    client.family = FAMILY
    client.process = SimpleNamespace(returncode=None)
    client.connected, client.routes, client.updated_at = True, frozenset({DEVICE}), time.monotonic()
    client._call = AsyncMock(return_value={"error": "mijia_lan_unavailable"})
    async with client.io:
        with pytest.raises(ValueError, match="mijia_lan_unavailable"):
            await client.request(QUERY, family=FAMILY)
    client._call.assert_not_awaited()
    with pytest.raises(ValueError, match="mijia_lan_unavailable"):
        await client.request(QUERY, family=FAMILY)
    client._call.assert_awaited_once()
    assert client.ready(FAMILY, DEVICE)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["lux", "door", "camera", "changed", "foreign", "remote", "wrong_origin"])
async def test_loopback_illuminance_readback_uses_registered_native_capability(tmp_path, monkeypatch, case):
    model = "urn:miot-spec-v2:device:sensor:0000A001:synthetic"
    identity = "cn_" + DEVICE
    definition = dict(iid=4, type="urn:miot-spec-v2:property:illumination:00000001", access=["read", "notify"])
    if case == "door":
        definition["value-list"] = [{"value": 1, "description": "Weak"}, {"value": 2, "description": "Strong"}]
    else:
        definition.update(unit="lux", **{"value-range": [0, 10000, 1]})
    spec = dict(type=model, services=[dict(iid=2, properties=[definition])])
    catalog = dict(state="ready", family=FAMILY, grant=GRANT, models={identity: {"urn": model}},
                   devices=[dict(id=CANDIDATE, identity=identity, kind="camera" if case == "camera" else "device")])

    async def read(*args):
        assert args == (DEVICE, 2, 4)
        if case == "changed":
            app.state.embodied.discovery_report = dict(catalog, family="d" * 64)
        return {"code": 0, "value": 2 if case == "door" else 25.0}

    gateway = SimpleNamespace(local_device=lambda _: {"group_id": "fixture"}, can_control=lambda _: True,
                              in_local_cooldown=lambda _: False, get_prop_async=AsyncMock(side_effect=read),
                              set_prop_async=AsyncMock())

    class SensorPipe(WorkerPipe):
        async def readline(self):
            packet = self.packets[-1].copy()
            request_id = packet.pop("id")
            assert packet.pop("family") == FAMILY and packet == QUERY
            response = await property_operation(gateway, packet, GRANT, "fixture", {DEVICE}, lambda: True)
            return json.dumps(dict(id=request_id, **response)).encode()

    pipe = SensorPipe(lambda: {})
    lan = MijiaLanClient(lambda: app.state.embodied.discovery_report["family"])
    lan.process, lan.family = pipe, FAMILY
    lan.connected, lan.routes, lan.updated_at = True, frozenset({DEVICE}), time.monotonic()
    original = mijia_control.MijiaPower
    monkeypatch.setattr(mijia_control, "MijiaPower", lambda service: original(
        service, spec=AsyncMock(return_value=spec), lan=lan))
    app = create_app(db_path=tmp_path / "trace.sqlite", settings_path=tmp_path / "settings.json",
                     background_enabled=True)
    app.state.embodied.discovery_report = catalog
    app.state.embodied.discovery_checked_at = time.monotonic()
    peer = "192.0.2.2" if case == "remote" else "127.0.0.1"
    headers = {"origin": "https://synthetic.invalid"} if case == "wrong_origin" else {}
    target = "f" * 64 if case == "foreign" else CANDIDATE
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 45321)),
                                 base_url="http://127.0.0.1:8740") as client:
        result = await client.get("/api/embodied/mijia-lan/illuminance/" + target, headers=headers)
    if case in {"lux", "door"}:
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        row = result.json()
        assert row["status"] == "reported" and row["source"] == target and row["transport"] == "mijia_lan"
        assert row["received_at"] and row["measured_at"] is None
        assert (row["kind"], row["unit"], row["value"]) == (
            ("numeric", "lux", 25.0) if case == "lux" else ("category", None, "strong")
        )
    elif case in {"remote", "wrong_origin"}:
        assert result.status_code == 403
    else:
        assert result.status_code == 503
    assert len(pipe.packets) == gateway.get_prop_async.await_count == (
        1 if case in {"lux", "door", "changed"} else 0
    )
    gateway.set_prop_async.assert_not_awaited()
