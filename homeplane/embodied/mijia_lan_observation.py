"""本机光照诊断所需的局域网订阅与读回。"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import math
import secrets
import time

from .mijia_power import property_request


# Paho 的线程把收包上下文传入 SDK 排队的 asyncio 回调。
property_receipt = ContextVar('mijia_lan_property_receipt', default=None)
OBSERVATION_IDLE = 45.0


@dataclass
class Observation:
    connection: object
    prop: tuple
    observed_at: float
    token: str
    task: asyncio.Task | None = None
    confirmed_at: float | None = None
    failed: bool = False
    invalidated: bool = False
    sequence: int = 0
    sample: tuple | None = None


class IlluminanceNotifications:
    def __init__(self, manager, allowed, grant, group, scope_current, connections, subscribe_errors):
        self.manager, self.allowed, self.grant, self.group = manager, allowed, grant, group
        self.scope_current, self.connections, self.subscribe_errors = scope_current, connections, subscribe_errors
        self.observations = {}
        self.lock = asyncio.Lock()

    def _source(self, did):
        if (self.manager.can_push(did)
                and (self.manager.local_device(did) or {}).get('group_id') == self.group):
            return self.connections.get(self.group)
        return None

    async def _remove(self, did, observation):
        del self.observations[did]
        if observation.task is not None and not observation.task.done():
            observation.task.cancel()
            await asyncio.gather(observation.task, return_exceptions=True)
        self.manager.unsub_device_props(did)

    async def expire(self):
        now = time.monotonic()
        async with self.lock:
            for did, observation in tuple(self.observations.items()):
                if (now - observation.observed_at > OBSERVATION_IDLE or observation.invalidated
                        or self._source(did) is not observation.connection):
                    await self._remove(did, observation)

    async def directory_changed(self, _added, _removed):
        # 设备可能在下一次后台健康读回前迅速恢复，先让旧样本失效。
        for did, observation in self.observations.items():
            if self._source(did) is not observation.connection:
                observation.invalidated = True
        await self.expire()

    async def close(self):
        async with self.lock:
            for did, observation in tuple(self.observations.items()):
                await self._remove(did, observation)

    def _accept(self, did, observation, event):
        receipt = property_receipt.get()
        if (observation.invalidated or receipt is None or receipt[0] is not observation.connection
                or observation.confirmed_at is None or receipt[1] < observation.confirmed_at
                or self.observations.get(did) is not observation or self._source(did) is not observation.connection
                or event.did != did or len(event.changes) != 1):
            return
        change = event.changes[0]
        if (change.siid, change.piid) != observation.prop or not self.scope_current():
            return
        value = change.value
        if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
            observation.sample = None
            return
        observation.sequence += 1
        observation.sample = (receipt[1], receipt[2], value)

    async def _subscribe(self, did, observation):
        try:
            await self.manager.sub_device_props_async(did, lambda event: self._accept(did, observation, event))
        except self.subscribe_errors:
            observation.failed = True
        else:
            if self._source(did) is observation.connection and self.scope_current():
                observation.confirmed_at = time.monotonic()
            else:
                observation.failed = True

    async def snapshot(self, request):
        if set(request) != {'did', 'siid', 'piid', 'grant', 'read'}:
            raise ValueError('mijia_invalid_binding')
        did, siid, piid, _ = property_request({k: v for k, v in request.items() if k != 'read'})
        if request['grant'] != self.grant or did not in self.allowed or not self.scope_current():
            raise ValueError('mijia_lan_unavailable')
        async with self.lock:
            return await self._observe(did, (siid, piid))

    async def _observe(self, did, prop):
        source = self._source(did)
        observation = self.observations.get(did)
        if observation is not None and (observation.connection is not source or observation.prop != prop
                                        or observation.failed or observation.invalidated):
            failed_on_current_connection = observation.failed and observation.connection is source
            await self._remove(did, observation)
            if failed_on_current_connection:
                return {'state': 'unavailable'}
            source = self._source(did)
            observation = None
        if source is None or not self.scope_current():
            return {'state': 'unavailable'}
        now = time.monotonic()
        if observation is None:
            observation = Observation(source, prop, now, secrets.token_hex(16))
            self.observations[did] = observation
            observation.task = asyncio.create_task(self._subscribe(did, observation))
        observation.observed_at = now
        if observation.confirmed_at is None:
            return {'state': 'subscribing'}
        row = {'state': 'awaiting_notification', 'subscription': observation.token}
        if observation.sample is not None:
            received_mono, received_at, value = observation.sample
            row.update(state='observed', received_at=received_at, age_seconds=now - received_mono,
                       sequence=observation.sequence, value=value)
        # 回包时也复查连接，避免把刚断线的最后一条样本继续交给上层。
        return row if self._source(did) is source and not observation.invalidated else {'state': 'unavailable'}
