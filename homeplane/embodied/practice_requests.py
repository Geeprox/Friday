"""持有互动体验请求；开始回执丢失后仍可按原请求取消。"""

import asyncio
import time
import weakref

import httpx


def valid_id(value, length=64):
    return isinstance(value, str) and len(value) == length and all(c in '0123456789abcdef' for c in value)


async def join_owned(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return task.result()


class PracticeRequests:
    def __init__(self, owner, *, clock=time.monotonic, wall=time.time):
        self.owner = weakref.proxy(owner)
        self.clock, self.wall = clock, wall
        self.entries, self.current = {}, None
        self.retiring = None
        self.closing = False

    def _ended(self, entry):
        entry['cancelled'] = True
        if self.current is entry:
            self.current = None
            self.owner.practice_deadline = 0.0
            self.owner.practice_token = None
        return dict(phase='ended', remaining=0, progress=0.0, request_id=entry['id'])

    def _prune(self):
        now = self.clock()
        if self.current and self.current['lease_end'] <= now:
            self._ended(self.current)
        self.entries = {key:entry for key,entry in self.entries.items()
                        if entry['expires'] > now or any(not task.done() for task in entry['tasks'])}

    def _reserve(self, request_id):
        self._prune()
        if len(self.entries) >= 128:
            raise ValueError('practice_request_capacity')
        entry = dict(id=request_id, expires=self.clock()+120, lease_end=self.clock()+90,
                     cancelled=False, sent=False, scope=None, process=self.owner.process,
                     gesture=None, tasks=[], start_task=None, stop_task=None)
        self.entries[request_id] = entry
        return entry

    def _task(self, entry, coroutine):
        task = asyncio.create_task(coroutine)
        # 调用方关闭后，原请求仍持有结果与清理责任；异常不遗留为无人回收任务。
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        entry['tasks'].append(task)
        return task

    def _running(self, entry):
        process = entry['process']
        return process is not None and process is self.owner.process and process.returncode is None

    async def _scope(self, entry):
        if not self._running(entry):
            raise ValueError('practice_recognition_not_running')
        result = await self.owner.request('/api/practice/capabilities', timeout=1)
        if (not self._running(entry) or not isinstance(result, dict)
                or result.get('scoped_requests') != 1 or not valid_id(result.get('scope'), 32)):
            raise ValueError('practice_protocol_unavailable')
        entry['scope'] = result['scope']

    def _reply(self, entry, result):
        if (not isinstance(result, dict) or result.get('request_id') != entry['id']
                or result.get('scope') != entry['scope']):
            raise ValueError('practice_reply_unconfirmed')
        return result

    async def _start(self, entry):
        try:
            await self._scope(entry)
            if entry['cancelled'] or self.closing:
                return self._ended(entry)
            if not self._running(entry) or self.clock() >= entry['lease_end']:
                raise ValueError('practice_source_changed')
            entry['sent'] = True
            result = self._reply(entry, await self.owner.request('/api/practice/start', dict(
                gesture=entry['gesture'], request_id=entry['id'], expected_scope=entry['scope'],
                not_after=self.clock()+5, lease_deadline=entry['lease_end'])))
            if entry['cancelled'] or self.current is not entry:
                return dict(phase='ended', remaining=0, progress=0.0, request_id=entry['id'])
            if result.get('phase') in {'blocked', 'ended'}:
                self._ended(entry)
            elif valid_id(result.get('token'), 32):
                self.owner.practice_token = result['token']
            else:
                raise ValueError('practice_reply_unconfirmed')
            return result
        except asyncio.CancelledError:
            if not entry['sent']:
                self._ended(entry)
            raise
        except (httpx.HTTPError, OSError, ValueError):
            if not entry['sent']:
                self._ended(entry)
            # 已发送而未取得可信回执时保留原门槛，直到明确取消或期限结束。
            raise ValueError('practice_unavailable') from None

    async def _stop(self, entry):
        entry['cancelled'] = True
        if entry['start_task'] is not None and not entry['sent']:
            return self._ended(entry)
        process = entry['process']
        if process is None or process.returncode is not None:
            return self._ended(entry)
        try:
            if not self._running(entry):
                raise ValueError('practice_source_changed')
            if entry['scope'] is None:
                await self._scope(entry)
            result = self._reply(entry, await self.owner.request('/api/practice/stop', dict(
                request_id=entry['id'], expected_scope=entry['scope'])))
            if result.get('phase') != 'ended':
                raise ValueError('practice_reply_unconfirmed')
            return self._ended(entry)
        except (httpx.HTTPError, OSError, ValueError):
            raise ValueError('practice_stop_unconfirmed') from None

    async def call(self, operation, payload):
        request_id = payload.get('request_id')
        if not valid_id(request_id) or operation not in {'start','stop','status'}:
            raise ValueError('practice_invalid_request')
        if self.closing:
            raise ValueError('practice_closed')
        self._prune()
        entry = self.entries.get(request_id)
        if operation == 'start':
            if self.closing or self.owner.closed:
                raise ValueError('practice_closed')
            if entry is not None:
                if entry['cancelled']:
                    return self._ended(entry)
                if entry['gesture'] != payload.get('gesture'):
                    raise ValueError('practice_request_conflict')
                return await asyncio.shield(entry['start_task'])
            issued = payload.get('issued_at_ms')
            # 本机页面与宿主共用墙钟；时间只限制迟到意图，不授予任何权限。
            if type(issued) is not int or not -1 <= self.wall()-issued/1000 <= 10:
                raise ValueError('practice_request_expired')
            if self.clock() < self.owner.practice_deadline:
                raise ValueError('practice_already_active')
            if self.owner.process is None or self.owner.process.returncode is not None:
                raise ValueError('practice_recognition_not_running')
            entry = self._reserve(request_id)
            entry['gesture'] = payload.get('gesture')
            self.current = entry
            self.owner.practice_deadline = entry['lease_end']
            self.owner.practice_token = None
            entry['start_task'] = self._task(entry, self._start(entry))
            return await asyncio.shield(entry['start_task'])
        if operation == 'stop':
            entry = entry or self._reserve(request_id)
            previous = entry['stop_task']
            if previous is None or previous.done() and (previous.cancelled() or previous.exception() is not None):
                entry['stop_task'] = self._task(entry, self._stop(entry))
            return await asyncio.shield(entry['stop_task'])
        if entry is None:
            return dict(phase='ended', remaining=0, progress=0.0, request_id=request_id)
        if entry['start_task'] is None or not entry['sent']:
            return self._ended(entry) if entry['cancelled'] else dict(phase='waiting', remaining=90, progress=0.0, request_id=request_id)
        if not self._running(entry):
            raise ValueError('practice_source_changed')
        try:
            result = self._reply(entry, await self.owner.request('/api/practice/status', dict(
                request_id=request_id, expected_scope=entry['scope'])))
        except (httpx.HTTPError, OSError, ValueError):
            raise ValueError('practice_unavailable') from None
        if result.get('phase') == 'ended':
            self._ended(entry)
        return result

    async def _retire(self):
        tasks = []
        for entry in self.entries.values():
            entry['cancelled'] = True
            for task in entry['tasks']:
                if not task.done():
                    task.cancel()
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def retire(self):
        self.closing = True
        if self.retiring is None or self.retiring.done():
            self.retiring = asyncio.create_task(self._retire())
        await join_owned(self.retiring)

    def worker_stopped(self):
        if self.current:
            self._ended(self.current)
        self.closing = False
