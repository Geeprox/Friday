"""Synthetic SDK/session only; no credentials, network, cameras or physical moves."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from homeplane.embodied import ptz_control, miot_spec


class Session:
    def __init__(self):
        self.token, self.calls, self.error = "SYNTHETIC-grant-A", 0, None

    async def current(self):
        self.calls += 1
        if self.error:
            raise self.error
        return {"access_token": self.token, "cloud_server": "cn"}


class SDK:
    def __init__(self, **options):
        self.options = options
        self.rows = [dict(did="SYNTHETIC-camera", siid=2, piid=1, code=0, value=False),
                     dict(did="SYNTHETIC-camera", siid=2, piid=2, code=0, value=1)]
        self.writes, self.closed, self.active = [], False, False
        self.entered = self.release = None

    async def get_props_async(self, parameters):
        assert {p.did for p in parameters} == {"SYNTHETIC-camera"}
        self.active = True
        try:
            if self.entered:
                self.entered.set()
                await self.release.wait()
            return self.rows
        finally:
            self.active = False

    async def set_prop_async(self, parameter):
        assert not self.closed
        self.writes.append(parameter)
        return {"code": 0}

    async def deinit_async(self):
        assert not self.active, "Never close an in-flight SDK during grant renewal"
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    parent, types = ModuleType("miot"), ModuleType("miot.types")
    parent.__path__ = []
    types.MIoTGetPropertyParam = types.MIoTSetPropertyParam = SimpleNamespace
    monkeypatch.setitem(sys.modules, "miot", parent)
    monkeypatch.setitem(sys.modules, "miot.types", types)
    monkeypatch.setattr(ptz_control, "camera", lambda _: {"device": {"did": "SYNTHETIC-camera"}, "region": "cn"})
    monkeypatch.setattr(miot_spec, "camera_properties", lambda _: {"cruise": (2, 1), "active_preset": (2, 2)})
    session, clients = Session(), []

    def factory(**options):
        client = SDK(**options)
        clients.append(client)
        return client

    owner = ptz_control.Transport(session=session, http_factory=factory)
    return owner, session, clients


async def test_lazy_saved_grant_renewal_keeps_one_write_and_replaces_old_client(transport):
    owner, session, clients = transport
    assert not clients and session.calls == 0  # Constructor does not read login or connect.
    assert await owner.read() == {"cruise": False, "active_preset": 1}
    session.token = "SYNTHETIC-grant-B"
    result = await owner.write("active_preset", 2)
    assert result == {"code": 0, "property": "active_preset", "value": 2}
    assert len(clients) == 2 and clients[0].closed and not clients[0].writes
    assert len(clients[1].writes) == 1
    assert clients[1].options["access_token"] == session.token
    assert clients[1].writes[0].did == "SYNTHETIC-camera"
    await owner.close()
    assert clients[1].closed


@pytest.mark.parametrize("kind", ["wrong_device", "duplicate_property"])
async def test_readback_requires_one_matching_device_property(transport, kind):
    owner, _, clients = transport
    await owner.read()
    if kind == "wrong_device":
        clients[0].rows = [dict(row, did="SYNTHETIC-other-camera") for row in clients[0].rows]
    else:
        clients[0].rows.append(dict(clients[0].rows[0], value=True))
    with pytest.raises(RuntimeError, match="incomplete_camera_readback"):
        await owner.read()
    assert not clients[0].writes
    await owner.close()


async def test_other_device_rows_cannot_overwrite_valid_local_state(transport):
    owner, _, clients = transport
    await owner.read()
    clients[0].rows.append(dict(clients[0].rows[0], did="SYNTHETIC-other-camera", value=True))
    assert (await owner.read())["cruise"] is False
    await owner.close()


async def test_renewal_failure_never_attempts_or_replays_a_write(transport):
    owner, session, clients = transport
    session.error = ValueError("mijia_authorization_required")
    with pytest.raises(ValueError, match="authorization_required"):
        await owner.write("cruise", True)
    assert not clients
    await owner.close()
    assert not clients
    with pytest.raises(RuntimeError, match="transport_closed"):
        await owner.read()


async def test_client_rotation_waits_for_inflight_read(transport):
    owner, session, clients = transport
    await owner.read()
    clients[0].entered, clients[0].release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(owner.read())
    await asyncio.wait_for(clients[0].entered.wait(), 1)
    session.token = "SYNTHETIC-grant-B"
    write = asyncio.create_task(owner.write("active_preset", 2))
    await asyncio.sleep(0)
    assert len(clients) == 1 and not clients[0].closed
    clients[0].release.set()
    await asyncio.gather(pending, write)
    assert len(clients) == 2 and clients[0].closed and len(clients[1].writes) == 1
    await owner.close()
