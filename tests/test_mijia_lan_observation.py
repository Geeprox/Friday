"""在没有真实设备的情况下验证中枢订阅、断线重连和旧回包的边界。"""

import asyncio
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from homeplane.embodied import mijia_lan, mijia_lan_observation


DID = "100000001"
GROUP = "fixture"
GRANT = "c" * 64
REQUEST = dict(did=DID, siid=2, piid=4, grant=GRANT, read="illuminance_observation")
OCCUPANCY = dict(REQUEST, piid=5, read="occupancy_observation")
CONTACT = dict(REQUEST, piid=5, read="contact_observation")


class Gateway:
    group_id = GROUP

    def __init__(self):
        self.online = True
        self.connected = False
        self.handler = None
        self.events = []
        self.requests = []
        self.removed = 0

    def _on_connect(self, _client, _userdata, _flags, code, _properties):
        self.connected = code == 0

    def _on_disconnect(self, *_args):
        self.connected = False

    def _on_message(self, _client, _userdata, msg):
        self.events.append((copy_context(), self.handler, msg.event))

    def flush_events(self):
        for context, handler, event in self.events:
            if handler is not None:
                context.run(handler, event)
        self.events.clear()

    def receive(self, value, *, retain=False, dup=False, did=DID, piid=4):
        change = SimpleNamespace(siid=2, piid=piid, value=value)
        msg = SimpleNamespace(topic=f"fixture/appMsg/notify/iot/fixture/property/2.{piid}",
                              retain=retain, dup=dup, event=SimpleNamespace(did=did, changes=[change]))
        self._on_message(None, None, msg)

    def local_device(self, _did):
        return {"group_id": GROUP}

    def can_push(self, _did):
        return self.online and self.connected

    async def sub_device_props_async(self, did, handler):
        assert did == DID and self.handler is None
        self.handler = handler
        ack = asyncio.get_running_loop().create_future()
        self.requests.append(ack)
        await ack

    def unsub_device_props(self, did):
        assert did == DID
        self.handler = None
        self.removed += 1

    def props_sub_groups(self, did):
        assert did == DID
        return {GROUP} if self.handler is not None else set()


@pytest.fixture
def feed(monkeypatch):
    clock = SimpleNamespace(now=10.0, current=True)
    timing = SimpleNamespace(monotonic=lambda: clock.now, time=lambda: 1000.0 + clock.now)
    monkeypatch.setattr(mijia_lan_observation, "time", timing)
    monkeypatch.setattr(mijia_lan, "time", timing)
    connections = {}
    gateway = mijia_lan.retrying_directory_client(
        Gateway, lambda: clock.current, (OSError,), OSError, connections
    )()
    gateway._on_connect(None, None, None, 0, None)
    stream = mijia_lan_observation.SensorNotifications(
        gateway, {DID}, GRANT, GROUP, lambda: clock.current, connections, (OSError,)
    )
    return clock, gateway, stream


async def acknowledge(stream, gateway):
    assert await stream.snapshot(REQUEST) == {"state": "subscribing"}
    await asyncio.sleep(0)
    ack = gateway.requests[-1]
    ack.set_result(None)
    await asyncio.sleep(0)
    awaiting = await stream.snapshot(REQUEST)
    assert awaiting["state"] == "awaiting_notification"
    return awaiting["subscription"]


@pytest.mark.asyncio
async def test_observation_requires_current_ack_and_uses_wire_arrival_time(feed):
    clock, gateway, stream = feed
    try:
        assert await stream.snapshot(REQUEST) == {"state": "subscribing"}
        await asyncio.sleep(0)
        gateway.receive(9)
        subscribed = await acknowledge(stream, gateway)
        gateway.receive(99, retain=True)
        gateway.receive(99, dup=True)
        gateway.receive(99, piid=5)
        gateway.receive(99, did="100000002")
        gateway.flush_events()
        assert await stream.snapshot(REQUEST) == {"state": "awaiting_notification", "subscription": subscribed}
        clock.now = 13
        gateway.receive(22)
        clock.now = 16
        gateway.flush_events()
        row = await stream.snapshot(REQUEST)
        assert row == dict(state="observed", subscription=subscribed, received_at=1013.0,
                           age_seconds=3.0, sequence=1, value=22)
        clock.now = 17
        assert await stream.snapshot(REQUEST) == dict(row, age_seconds=4.0)
        assert len(gateway.requests) == 1
        gateway.receive(True)
        gateway.flush_events()
        assert (await stream.snapshot(REQUEST))["state"] == "awaiting_notification"
    finally:
        await stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("sensor_request", "value"), [(OCCUPANCY, 3), (CONTACT, False)])
async def test_shared_subscription_keeps_independent_properties_and_start_times(feed, sensor_request, value):
    clock, gateway, stream = feed
    try:
        light_subscription = await acknowledge(stream, gateway)
        gateway.receive(14)
        gateway.receive(value, piid=5)
        sensor = await stream.snapshot(sensor_request)
        assert sensor["state"] == "awaiting_notification"
        assert sensor["subscription"] != light_subscription
        gateway.flush_events()
        assert await stream.snapshot(sensor_request) == sensor
        assert (await stream.snapshot(REQUEST))["value"] == 14

        clock.now += 1
        gateway.receive(value, piid=5)
        gateway.receive(15)
        gateway.flush_events()
        assert (await stream.snapshot(sensor_request)) == dict(
            sensor, state="observed", sequence=1, value=value, received_at=1011.0, age_seconds=0.0)
        light = await stream.snapshot(REQUEST)
        assert (light["subscription"], light["sequence"], light["value"]) == (light_subscription, 2, 15)
        assert len(gateway.requests) == 1
        assert gateway.removed == 0
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_late_callback_cannot_replace_or_clear_a_newer_packet_even_at_same_clock_time(feed):
    _, gateway, stream = feed
    try:
        await acknowledge(stream, gateway)
        gateway.receive(10)
        gateway.receive(True)
        gateway.receive(11)
        gateway.events.reverse()
        gateway.flush_events()
        light = await stream.snapshot(REQUEST)
        assert (light["sequence"], light["value"]) == (1, 11)

        gateway.receive(12)
        gateway.receive(True)
        gateway.events.reverse()
        gateway.flush_events()
        assert (await stream.snapshot(REQUEST))["state"] == "awaiting_notification"
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_idle_property_expires_without_disturbing_other_property_or_replaying_old_sample(feed):
    clock, gateway, stream = feed
    try:
        light_subscription = await acknowledge(stream, gateway)
        first = await stream.snapshot(OCCUPANCY)
        gateway.receive(1, piid=5)
        gateway.flush_events()
        assert (await stream.snapshot(OCCUPANCY))["value"] == 1
        clock.now += 40
        gateway.receive(16)
        gateway.flush_events()
        assert (await stream.snapshot(REQUEST))["value"] == 16

        gateway.receive(0, piid=5)
        clock.now += 6
        await stream.expire()
        again = await stream.snapshot(OCCUPANCY)
        gateway.flush_events()
        assert again["subscription"] != first["subscription"]
        assert await stream.snapshot(OCCUPANCY) == again
        assert (await stream.snapshot(REQUEST))["subscription"] == light_subscription
        assert len(gateway.requests) == 1 and gateway.removed == 0
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_registry_loss_after_confirmed_subscription_discards_the_old_sample(feed):
    _, gateway, stream = feed
    try:
        old = await acknowledge(stream, gateway)
        gateway.receive(10)
        gateway.flush_events()
        assert (await stream.snapshot(REQUEST))["value"] == 10
        gateway.receive(11)
        # 固定版 SDK 可能因重连前的迟到回包撤下订阅表。
        gateway.handler = None
        new = await acknowledge(stream, gateway)
        assert old != new
        gateway.flush_events()
        assert await stream.snapshot(REQUEST) == {"state": "awaiting_notification", "subscription": new}
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_reconnect_even_between_polls_requires_another_ack_and_new_packet(feed):
    clock, gateway, stream = feed
    try:
        old = await acknowledge(stream, gateway)
        previous_occupancy = await stream.snapshot(OCCUPANCY)
        gateway.receive(10)
        gateway.receive(1, piid=5)
        gateway.flush_events()
        assert (await stream.snapshot(REQUEST))["state"] == "observed"
        assert (await stream.snapshot(OCCUPANCY))["value"] == 1
        gateway.receive(11)
        gateway.receive(2, piid=5)
        gateway._on_disconnect(None, None, None, 0, None)
        gateway._on_connect(None, None, None, 0, None)
        clock.now = 20
        assert await stream.snapshot(REQUEST) == {"state": "subscribing"}
        assert gateway.removed == 1
        new = await acknowledge(stream, gateway)
        assert old != new and len(gateway.requests) == 2
        next_occupancy = await stream.snapshot(OCCUPANCY)
        assert next_occupancy["subscription"] != previous_occupancy["subscription"]
        gateway.flush_events()
        assert await stream.snapshot(REQUEST) == {"state": "awaiting_notification", "subscription": new}
        assert await stream.snapshot(OCCUPANCY) == next_occupancy
        gateway.receive(24)
        gateway.flush_events()
        assert (await stream.snapshot(REQUEST))["value"] == 24
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_device_directory_loss_invalidates_even_if_device_is_back_before_next_read(feed):
    _, gateway, stream = feed
    try:
        old = await acknowledge(stream, gateway)
        gateway.receive(10)
        gateway.flush_events()
        gateway.receive(11)
        async with stream.lock:
            gateway.online = False
            offline = asyncio.create_task(stream.directory_changed([], []))
            await asyncio.sleep(0)
            gateway.online = True
        await offline
        new = await acknowledge(stream, gateway)
        assert old != new
        gateway.flush_events()
        assert await stream.snapshot(REQUEST) == {"state": "awaiting_notification", "subscription": new}
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_failed_subscription_can_recover_without_acting_on_old_or_out_of_scope_data(feed):
    clock, gateway, stream = feed
    try:
        wrong = dict(REQUEST, grant="d" * 64)
        with pytest.raises(ValueError):
            await stream.snapshot(wrong)
        with pytest.raises(ValueError):
            await stream.snapshot(dict(REQUEST, power="on"))
        assert not gateway.requests
        assert await stream.snapshot(REQUEST) == {"state": "subscribing"}
        await asyncio.sleep(0)
        gateway.requests[0].set_exception(OSError())
        await asyncio.sleep(0)
        assert await stream.snapshot(REQUEST) == {"state": "unavailable"}
        await acknowledge(stream, gateway)
        gateway.receive(23)
        clock.current = False
        gateway.flush_events()
        with pytest.raises(ValueError):
            await stream.snapshot(REQUEST)
        clock.current = True
        assert (await stream.snapshot(REQUEST))["state"] == "awaiting_notification"
        clock.now += mijia_lan_observation.OBSERVATION_IDLE + 1
        await stream.expire()
        assert gateway.handler is None
    finally:
        await stream.close()
