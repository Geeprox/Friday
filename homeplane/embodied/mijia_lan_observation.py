"""本机传感器诊断所需的局域网订阅与读回。"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import secrets
import time

from ..mijia_lan_client import SENSOR_OBSERVATIONS, valid_sensor_value
from .mijia_power import property_request


# Paho 的线程把收包上下文传入 SDK 排队的 asyncio 回调。
property_receipt = ContextVar('mijia_lan_property_receipt', default=None)
OBSERVATION_IDLE = 45.0


@dataclass
class PropertyConnection:
    received_packets: int = 0


@dataclass
class PropertyObservation:
    prop: tuple[int, int]
    observed_at: float
    token: str
    last_packet: int
    sequence: int = 0
    sample: tuple[float, float, int | float | bool] | None = None


@dataclass
class DeviceSubscription:
    connection: PropertyConnection
    properties: dict[str, PropertyObservation] = field(default_factory=dict)
    task: asyncio.Task | None = None
    confirmed_after_packet: int | None = None
    failed: bool = False
    invalidated: bool = False


class SensorNotifications:
    def __init__(self, manager, allowed, grant, group, scope_current, connections, subscribe_errors):
        self.manager, self.allowed, self.grant, self.group = manager, allowed, grant, group
        self.scope_current, self.connections, self.subscribe_errors = scope_current, connections, subscribe_errors
        self.subscriptions: dict[str, DeviceSubscription] = {}
        self.lock = asyncio.Lock()

    def _source(self, did):
        if (self.manager.can_push(did)
                and (self.manager.local_device(did) or {}).get('group_id') == self.group):
            return self.connections.get(self.group)
        return None

    def _confirmed_registration(self, did):
        return self.manager.props_sub_groups(did) == {self.group}

    def _current_subscription(self, did, subscription):
        return (not subscription.invalidated and self._source(did) is subscription.connection
                and (subscription.confirmed_after_packet is None or self._confirmed_registration(did)))

    async def _remove(self, did, subscription):
        del self.subscriptions[did]
        if subscription.task is not None and not subscription.task.done():
            subscription.task.cancel()
            await asyncio.gather(subscription.task, return_exceptions=True)
        self.manager.unsub_device_props(did)

    def _expire_properties(self, subscription, now):
        for signal, observation in tuple(subscription.properties.items()):
            if now - observation.observed_at > OBSERVATION_IDLE:
                del subscription.properties[signal]

    async def expire(self):
        now = time.monotonic()
        async with self.lock:
            for did, subscription in tuple(self.subscriptions.items()):
                self._expire_properties(subscription, now)
                if not subscription.properties or not self._current_subscription(did, subscription):
                    await self._remove(did, subscription)

    async def directory_changed(self, _added, _removed):
        # 设备可能在下一次后台健康读回前迅速恢复，先让旧样本失效。
        for did, subscription in self.subscriptions.items():
            if self._source(did) is not subscription.connection:
                subscription.invalidated = True
        await self.expire()

    async def close(self):
        async with self.lock:
            for did, subscription in tuple(self.subscriptions.items()):
                await self._remove(did, subscription)

    def _accept(self, did, subscription, event):
        receipt = property_receipt.get()
        if (receipt is None or receipt[0] is not subscription.connection
                or subscription.confirmed_after_packet is None or receipt[1] <= subscription.confirmed_after_packet
                or self.subscriptions.get(did) is not subscription or not self._current_subscription(did, subscription)
                or event.did != did or len(event.changes) != 1 or not self.scope_current()):
            return
        _, packet, received_mono, received_at = receipt
        change = event.changes[0]
        for signal, observation in subscription.properties.items():
            if ((change.siid, change.piid) != observation.prop
                    or received_mono - observation.observed_at > OBSERVATION_IDLE
                    or packet <= observation.last_packet):
                continue
            observation.last_packet = packet
            if valid_sensor_value(signal, change.value):
                observation.sequence += 1
                observation.sample = (received_mono, received_at, change.value)
            else:
                observation.sample = None

    async def _subscribe(self, did, subscription):
        try:
            await self.manager.sub_device_props_async(did, lambda event: self._accept(did, subscription, event))
        except self.subscribe_errors:
            subscription.failed = True
        else:
            if (self._current_subscription(did, subscription) and self._confirmed_registration(did)
                    and self.scope_current()):
                subscription.confirmed_after_packet = subscription.connection.received_packets
            else:
                subscription.failed = True

    async def snapshot(self, request):
        signal = SENSOR_OBSERVATIONS.get(request.get('read'))
        if signal is None or set(request) != {'did', 'siid', 'piid', 'grant', 'read'}:
            raise ValueError('mijia_invalid_binding')
        did, siid, piid, _ = property_request({k: v for k, v in request.items() if k != 'read'})
        if request['grant'] != self.grant or did not in self.allowed or not self.scope_current():
            raise ValueError('mijia_lan_unavailable')
        async with self.lock:
            return await self._observe(did, signal, (siid, piid))

    async def _observe(self, did, signal, prop):
        source = self._source(did)
        subscription = self.subscriptions.get(did)
        if subscription is not None:
            self._expire_properties(subscription, time.monotonic())
            if (not subscription.properties or subscription.failed
                    or not self._current_subscription(did, subscription)):
                failed_on_current_connection = subscription.failed and subscription.connection is source
                await self._remove(did, subscription)
                if failed_on_current_connection:
                    return {'state': 'unavailable'}
                source = self._source(did)
                subscription = None
        if source is None or not self.scope_current():
            return {'state': 'unavailable'}
        now = time.monotonic()
        if subscription is None:
            subscription = DeviceSubscription(source)
            self.subscriptions[did] = subscription
            subscription.task = asyncio.create_task(self._subscribe(did, subscription))
        observation = subscription.properties.get(signal)
        if observation is None or observation.prop != prop:
            observation = PropertyObservation(prop, now, secrets.token_hex(16), source.received_packets)
            subscription.properties[signal] = observation
        observation.observed_at = now
        if subscription.confirmed_after_packet is None:
            return {'state': 'subscribing'}
        row = {'state': 'awaiting_notification', 'subscription': observation.token}
        if observation.sample is not None:
            received_mono, received_at, value = observation.sample
            row.update(state='observed', received_at=received_at, age_seconds=now - received_mono,
                       sequence=observation.sequence, value=value)
        # 回包时也复查连接，避免把刚断线的最后一条样本继续交给上层。
        return row if self._current_subscription(did, subscription) else {'state': 'unavailable'}
