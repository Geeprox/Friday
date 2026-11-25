"""桌面后台与独立米家局域网进程之间的有界管道。"""

import asyncio
import json
import os
import re
import time

FAMILY = re.compile(r'[a-f0-9]{64}')
DID = re.compile(r'(?:[0-9]{1,32}|blt\.[0-9]\.[a-z0-9]{1,64})')
STATUS_TTL = 12.0
# 给 SDK 的常规重连与首轮目录请求留出一个观察周期。
DIRECTORY_GRACE = 20.0
DIRECTORY_RETRY_INITIAL = 30.0
DIRECTORY_RETRY_MAX = 600.0


class MijiaLanClient:
    def __init__(self, current_family):
        self.current_family = current_family
        self.task = None
        self.process = None
        self.family = None
        self.routes = frozenset()
        self.connected = False
        self.push = 0
        self.directory_devices = None
        self.directory_family = None
        self.directory_recovering = False
        self.directory_recovery_attempts = 0
        self.updated_at = None
        self.sequence = 0
        self.io = asyncio.Lock()

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._watch(), name='friday-mijia-lan')

    def ready(self, family, did=None):
        return (self.family == family == self.current_family() and self.process is not None
                and self.process.returncode is None and self.connected
                and self.updated_at is not None and 0 <= time.monotonic() - self.updated_at <= STATUS_TTL
                and (did is None or did in self.routes))

    def status(self):
        family = self.current_family()
        live = self.ready(family)
        same_scope = family is not None and family == self.directory_family
        if live:
            directory_state = 'unknown' if self.directory_devices is None else (
                'available' if self.directory_devices else 'missing')
        else:
            directory_state = 'recovering' if same_scope and self.directory_recovering else 'unavailable'
        return {'connected': live, 'control_devices': len(self.routes) if live else 0,
                'push_capable': self.push if live else 0, 'directory_state': directory_state,
                'directory_devices': self.directory_devices if live else None,
                'directory_recovery_attempts': self.directory_recovery_attempts if same_scope else 0}

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
        self.directory_devices = None
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

    async def _call(self, request, family, *, expected_process=None):
        async with self.io:
            proc = self.process
            if (proc is None or proc.returncode is not None or self.family != family
                    or family != self.current_family()):
                raise ValueError('mijia_lan_unavailable')
            # 等候管道期间可能切换路由或重建工作进程，旧请求不能随恢复一起派发。
            if expected_process is not None and (proc is not expected_process
                    or not self.ready(family, request.get('did'))):
                raise ValueError('mijia_lan_unavailable')
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

    async def request(self, request, *, family):
        process = self.process
        if not self.ready(family, request.get('did')):
            raise ValueError('mijia_lan_unavailable')
        read_only = request.get('cover') == 'read' or ('cover' not in request and 'power' not in request)
        async with asyncio.timeout(1.6 if read_only else 5):
            reply = await self._call(request, family, expected_process=process)
        if 'error' in reply:
            # 本地候选失效后，让下一次用户操作重新选择仍可用的渠道。
            if self.process is process:
                self.routes = self.routes.difference({request.get('did')})
            raise ValueError('mijia_lan_unavailable')
        return reply

    async def _watch(self):
        missing_since = None
        retry_at, retry_delay = 0., DIRECTORY_RETRY_INITIAL
        try:
            while True:
                family = self.current_family()
                if family != self.directory_family:
                    self.directory_family, missing_since = family, None
                    retry_at, retry_delay = 0., DIRECTORY_RETRY_INITIAL
                    self.directory_recovering = False
                    self.directory_recovery_attempts = 0
                if not isinstance(family, str) or not FAMILY.fullmatch(family):
                    if self.process is not None:
                        await self._close_process()
                    await asyncio.sleep(2)
                    continue
                try:
                    if self.family != family or self.process is None or self.process.returncode is not None:
                        missing_since = None
                        await self._close_process()
                        if family != self.current_family():
                            continue
                        await self._open(family)
                    # 健康查询不占用正在等待设备回执的管道，也不因操作繁忙重启进程。
                    if self.io.locked():
                        await asyncio.sleep(.25)
                        continue
                    async with asyncio.timeout(2.0):
                        row = await self._call({'routes': True}, family)
                    ids = row.get('routes')
                    directory = row.get('directory_devices')
                    if (not isinstance(ids, list) or len(ids) > 512
                            or not all(isinstance(did, str) and DID.fullmatch(did) for did in ids)
                            or len(set(ids)) != len(ids)
                            or type(row.get('push')) is not int or not 0 <= row['push'] <= len(ids)
                            or type(row.get('connected')) is not bool
                            or (directory is not None and
                                (type(directory) is not int or not len(ids) <= directory <= 512))):
                        raise ValueError('mijia_lan_unavailable')
                    now = time.monotonic()
                    if self.updated_at is None or not 0 <= now - self.updated_at <= STATUS_TTL:
                        missing_since = None
                    self.routes = frozenset(ids)
                    self.connected, self.push = row['connected'], row['push']
                    self.directory_devices, self.updated_at = directory, now
                    if self.connected:
                        self.directory_recovering = False
                    if self.connected and directory == 0:
                        missing_since = now if missing_since is None else missing_since
                        if now - missing_since >= DIRECTORY_GRACE and now >= retry_at:
                            # 复用本家庭的连接与目录发现入口。恢复时所有可控路由均为空。
                            self.directory_recovering = True
                            self.directory_recovery_attempts += 1
                            retry_at, retry_delay = now + retry_delay, min(retry_delay * 2, DIRECTORY_RETRY_MAX)
                            await self._close_process()
                    else:
                        missing_since = None
                        if self.connected and directory:
                            retry_at, retry_delay = 0., DIRECTORY_RETRY_INITIAL
                    await asyncio.sleep(3)
                except (OSError, ValueError, TimeoutError):
                    missing_since = None
                    await self._close_process()
                    await asyncio.sleep(8)
        finally:
            self.directory_recovering = False
            await self._close_process()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
