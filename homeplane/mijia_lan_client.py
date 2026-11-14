"""桌面后台与独立米家局域网进程之间的有界管道。"""

import asyncio
import json
import os
import re
import time

FAMILY = re.compile(r'[a-f0-9]{64}')
DID = re.compile(r'(?:[0-9]{1,32}|blt\.[0-9]\.[a-z0-9]{1,64})')


class MijiaLanClient:
    def __init__(self, current_family):
        self.current_family = current_family
        self.task = None
        self.process = None
        self.family = None
        self.routes = frozenset()
        self.connected = False
        self.push = 0
        self.updated_at = None
        self.sequence = 0
        self.io = asyncio.Lock()

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._watch(), name='friday-mijia-lan')

    def ready(self, family, did=None):
        return (self.family == family == self.current_family() and self.process is not None
                and self.process.returncode is None and self.connected
                and self.updated_at is not None and 0 <= time.monotonic() - self.updated_at <= 12
                and (did is None or did in self.routes))

    def status(self):
        live = self.ready(self.current_family())
        return {'connected': live, 'control_devices': len(self.routes) if live else 0,
                'push_capable': self.push if live else 0}

    async def _open(self, family):
        from .embodied.paths import DATA, PROJECT, PYTHON

        self.process = await asyncio.create_subprocess_exec(
            str(PYTHON), '-B', '-m', 'homeplane.embodied.mijia_lan', cwd=PROJECT,
            env=dict(os.environ, PYTHONPATH=str(PROJECT), FRIDAY_EMBODIED_DATA=str(DATA)),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=64 * 1024,
        )
        async with asyncio.timeout(20):
            reply = json.loads(await self.process.stdout.readline())
        if reply != {'ready': True, 'family': family}:
            raise ValueError('mijia_lan_unavailable')
        self.family = family

    async def _close_process(self):
        proc, self.process = self.process, None
        self.family = self.updated_at = None
        self.routes, self.connected, self.push = frozenset(), False, 0
        if proc is None or proc.returncode is not None:
            return
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    async def _call(self, request, family, *, guard=None):
        async with self.io:
            proc = self.process
            if (proc is None or proc.returncode is not None or self.family != family
                    or family != self.current_family()):
                raise ValueError('mijia_lan_unavailable')
            if guard is not None and not guard():
                raise ValueError('mijia_permission_changed')
            self.sequence += 1
            ident = self.sequence
            try:
                proc.stdin.write(json.dumps({**request, 'id': ident, 'family': family}).encode() + b'\n')
                await proc.stdin.drain()
                # 若上一条读取被上层超时取消，它的迟到回包只按编号丢弃。
                for _ in range(16):
                    raw = await proc.stdout.readline()
                    if not raw or len(raw) > 64 * 1024:
                        break
                    reply = json.loads(raw)
                    if reply.get('id') == ident:
                        return reply
                    if type(reply.get('id')) is not int or reply['id'] >= ident:
                        break
            except (OSError, ValueError, AttributeError):
                pass
            self.connected = False
            raise ValueError('mijia_lan_unavailable')

    async def request(self, request, *, family, guard=None):
        if not self.ready(family, request.get('did')):
            raise ValueError('mijia_lan_unavailable')
        read_only = request.get('cover') == 'read' or not {'cover', 'power', 'value'}.intersection(request)
        async with asyncio.timeout(1.6 if read_only else 5):
            reply = await self._call(request, family, guard=guard)
        if 'error' in reply:
            if reply['error'] in ('mijia_property_unsupported', 'mijia_permission_changed'):
                raise ValueError(reply['error'])
            # 本地候选失效后，让下一次用户操作重新选择仍可用的渠道。
            self.routes = self.routes.difference({request.get('did')})
            raise ValueError('mijia_lan_unavailable')
        return reply

    async def _watch(self):
        try:
            while True:
                family = self.current_family()
                if not isinstance(family, str) or not FAMILY.fullmatch(family):
                    if self.process is not None:
                        await self._close_process()
                    await asyncio.sleep(2)
                    continue
                try:
                    if self.family != family or self.process is None or self.process.returncode is not None:
                        await self._close_process()
                        await self._open(family)
                    # 健康查询不占用正在等待设备回执的管道，也不因操作繁忙重启进程。
                    if self.io.locked():
                        await asyncio.sleep(.25)
                        continue
                    async with asyncio.timeout(2.0):
                        row = await self._call({'routes': True}, family)
                    ids = row.get('routes')
                    if (not isinstance(ids, list) or len(ids) > 512
                            or not all(isinstance(did, str) and DID.fullmatch(did) for did in ids)
                            or len(set(ids)) != len(ids)
                            or type(row.get('push')) is not int or not 0 <= row['push'] <= len(ids)
                            or type(row.get('connected')) is not bool):
                        raise ValueError('mijia_lan_unavailable')
                    self.routes = frozenset(ids)
                    self.connected, self.push, self.updated_at = row['connected'], row['push'], time.monotonic()
                    await asyncio.sleep(3)
                except (OSError, ValueError, TimeoutError):
                    await self._close_process()
                    await asyncio.sleep(8)
        finally:
            await self._close_process()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
