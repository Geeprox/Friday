"""用隔离的子进程管道验证直连目录丢失、恢复及旧请求失效。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from homeplane import mijia_lan_client
from homeplane.embodied.mijia_lan import route_snapshot
from homeplane.mijia_lan_client import MijiaLanClient

FAMILY = 'a' * 64
DID = '100001'


class WorkerPipe:
    def __init__(self, health):
        self.health = health
        self.packets = []
        self.stdin = self.stdout = self
        self.returncode = None
        self.closed = False

    def write(self, packet):
        assert not self.closed
        self.packets.append(json.loads(packet))

    async def drain(self):
        pass

    async def readline(self):
        request = self.packets[-1]
        reply = self.health() if request.get('routes') else {'value': True}
        return json.dumps({'id': request['id'], **reply}).encode()

    def close(self):
        self.closed = True

    async def wait(self):
        assert self.closed
        self.returncode = 0


class EndObservation(Exception):
    pass


async def observe(monkeypatch, health, until, *, skip=None):
    now = 0.
    opened = {}
    statuses = {}
    client = MijiaLanClient(lambda: FAMILY)

    async def advance(delay):
        nonlocal now
        assert delay == 3
        statuses[now] = client.status()
        if now >= until:
            raise EndObservation
        now = skip[1] if skip and now == skip[0] else now + delay

    async def open_worker(family):
        assert family == FAMILY
        index = len(opened)
        client.process = opened[now] = WorkerPipe(lambda: health(index, now))
        client.family = family

    monkeypatch.setattr(client, '_open', open_worker)
    monkeypatch.setattr(mijia_lan_client, 'time', SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(mijia_lan_client, 'asyncio',
                        SimpleNamespace(sleep=advance, timeout=asyncio.timeout, wait_for=asyncio.wait_for))
    with pytest.raises(EndObservation):
        await client._watch()
    for worker in opened.values():
        assert worker.closed
        assert all(set(p) == {'routes', 'id', 'family'} for p in worker.packets)
    return opened, statuses


def health_packet(directory, routes=(), *, connected=True):
    return dict(connected=connected, directory_devices=directory, routes=list(routes), push=len(routes))


def test_directory_keeps_offline_rows_and_respects_family():
    devices = {
        DID: {'group_id': 'home', 'online': True},
        '100002': {'group_id': 'home', 'online': False},
        '100003': {'group_id': 'home', 'online': True},
        '100004': {'group_id': 'other', 'online': True},
        '100005': {'group_id': 'home', 'online': True},
    }
    hub = SimpleNamespace(is_ready=True, local_device=devices.get,
                          can_control=lambda did: devices[did]['online'],
                          in_local_cooldown=lambda did: did == '100003',
                          can_push=lambda did: did == DID)
    allowed = set(devices).difference({'100005'}) | {'100006'}
    assert route_snapshot(hub, allowed, 'home') == health_packet(3, [DID])


@pytest.mark.asyncio
async def test_connected_empty_directory_reopens_only_its_worker(monkeypatch):
    def health(index, now):
        if index == 0:
            return health_packet(0)
        # 新进程先连上中枢，随后目录恢复；第二次故障重新使用初始重试间隔。
        if index == 1:
            return health_packet(2, [DID]) if now < 30 else health_packet(0)
        if index == 2:
            return health_packet(0)
        return health_packet(2, [DID])

    opened, statuses = await observe(monkeypatch, health, until=90)
    assert list(opened) == [0, 24, 54, 84]
    assert statuses[18]['directory_state'] == 'missing'
    assert statuses[21]['directory_state'] == 'recovering'
    assert statuses[24] == dict(connected=True, directory_state='available', directory_devices=2,
                               control_devices=1, push_capable=1, directory_recovery_attempts=1)
    assert statuses[84] == {**statuses[24], 'directory_recovery_attempts': 3}


@pytest.mark.asyncio
async def test_directory_recovery_backs_off_and_rechecks_after_observation_gap(monkeypatch):
    opened, statuses = await observe(monkeypatch, lambda *_: health_packet(0), until=1656, skip=(15, 100))
    assert list(opened) == [0, 124, 154, 214, 334, 574, 1054, 1654]
    assert statuses[100]['directory_state'] == 'missing'
    assert statuses[121]['directory_state'] == 'recovering'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('packet', 'state'),
    [(health_packet(3), 'available'),
     (dict(connected=True, routes=[], push=0), 'unknown'),
     (health_packet(0, connected=False), 'unavailable')],
)
async def test_no_restart_for_offline_devices_older_helper_or_disconnected_gateway(monkeypatch, packet, state):
    opened, statuses = await observe(monkeypatch, lambda *_: packet, until=36)
    assert list(opened) == [0]
    assert statuses[36]['control_devices'] == 0
    assert statuses[36]['directory_recovery_attempts'] == 0
    assert statuses[36]['directory_state'] == state


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['process', 'route', 'age'])
async def test_queued_request_rechecks_current_process_and_route(monkeypatch, change):
    now = 10.
    monkeypatch.setattr(mijia_lan_client, 'time', SimpleNamespace(monotonic=lambda: now))
    client = MijiaLanClient(lambda: FAMILY)
    original, replacement = (WorkerPipe(lambda: health_packet(1, [DID])) for _ in range(2))
    client.process, client.family = original, FAMILY
    client.connected, client.routes, client.updated_at = True, frozenset({DID}), now
    operation = {'did': DID, 'power': True}

    async with client.io:
        queued = asyncio.create_task(client.request(operation, family=FAMILY))
        await asyncio.sleep(0)
        if change == 'process':
            client.process = replacement
        elif change == 'route':
            client.routes = frozenset()
        else:
            now += 13
    with pytest.raises(ValueError, match='mijia_lan_unavailable'):
        await queued
    assert original.packets == replacement.packets == []
    client.process, client.routes, client.updated_at = replacement, frozenset({DID}), now
    assert (await client.request(operation, family=FAMILY))['value'] is True
    assert len(replacement.packets) == 1
