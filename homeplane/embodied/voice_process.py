"""Own a voice pipe through startup, serialized requests, failure and actual reap.

Starting is nonblocking for the camera loop. A retired generation never accepts
another request, and its local child slot remains held until it actually exits.
This module imports no speech model or device SDK.
"""

import asyncio
import json
import math
import threading
import time

from .local_child import LocalChild, owned

REPLIES = {
    'transcribe': {'transcript', 'wake', 'error'},
    'delegate': {'task_result', 'ignored', 'error'},
}


def _nonfinite(_):
    raise ValueError('invalid_voice_reply')


def _reply(line):
    if not line or len(line) > 100000:
        raise ValueError('invalid_voice_reply')
    value = json.loads(line, parse_constant=_nonfinite)
    if not isinstance(value, dict):
        raise ValueError('invalid_voice_reply')
    return value


class VoiceProcess:
    def __init__(self, command, *, operation='transcribe', startup_timeout=60, request_timeout=60, retry_delay=30):
        self.command = tuple(command)
        self.operation = operation
        self.reply_types = REPLIES[operation]
        self.startup_timeout, self.request_timeout = startup_timeout, request_timeout
        self.retry_delay, self.retry_at = retry_delay, 0
        self.child = self.reader = None
        self.started = asyncio.Event()
        self.phase = 'not_started'
        self.metadata = {}
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.futures = {}
        self.slots = threading.BoundedSemaphore(1)

    @property
    def ready(self):
        process = self.child.process if self.child else None
        return bool(self.phase == 'ready' and process and process.returncode is None
                    and self.reader and not self.reader.done())

    @property
    def status(self):
        return 'unavailable' if self.phase == 'ready' and not self.ready else self.phase

    def ensure_started(self):
        if self.reader and not self.reader.done():
            if self.child.process and self.child.process.returncode is not None:
                self.stop('unavailable')
            return
        if self.child and self.child.slot or time.monotonic() < self.retry_at:
            return
        child = LocalChild(self.command, self.slots, limit=100000)
        self.child = child
        self.started = asyncio.Event()
        self.phase, self.metadata = 'loading_local_models', {}
        self.reader = owned(asyncio.create_task(self._read(child, self.started)))

    def _fail_pending(self):
        for future in self.futures.values():
            if not future.done():
                future.set_exception(RuntimeError('voice_worker_closed'))

    def stop(self, phase='stopped', *, retry_delay=None):
        self.phase, self.metadata = phase, {}
        delay = self.retry_delay if retry_delay is None else retry_delay
        self.retry_at = time.monotonic() + delay if phase == 'unavailable' else 0
        self._fail_pending()
        self.started.set()
        if self.reader and not self.reader.done():
            self.reader.cancel()
        if self.child:
            self.child.retire()

    async def _read(self, child, started):
        try:
            async with asyncio.timeout(self.startup_timeout):
                process = await child.start()
                initial = _reply(await process.stdout.readline())
                asr = initial.get('asr')
                if (initial.get('type') != 'ready'
                        or (self.operation == 'transcribe' and (not isinstance(asr, str) or not 1 <= len(asr) <= 128))
                        or (self.operation == 'delegate' and set(initial) != {'type'})
                        or initial.get('intelligence') is not None and not isinstance(initial['intelligence'], dict)):
                    raise ValueError('invalid_voice_ready')
            self.metadata = dict(asr_model=asr, intelligence=initial.get('intelligence'))
            self.phase = 'ready'
            started.set()
            while line := await process.stdout.readline():
                item = _reply(line)
                seq = item.get('id')
                if type(seq) is not int or seq <= 0 or item.get('type') not in self.reply_types:
                    raise ValueError('invalid_voice_reply')
                future = self.futures.get(seq)
                if future and not future.done():
                    future.set_result(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Model errors and decoded content never enter lifecycle diagnostics.
            pass
        finally:
            started.set()
            if self.child is child:
                if self.phase not in {'stopped', 'unavailable'}:
                    self.phase = 'unavailable'
                    self.retry_at = time.monotonic() + self.retry_delay
                self.metadata = {}
                self._fail_pending()
            child.retire()

    async def request(self, op, *, guard=None, deadline=None, **payload):
        if op != self.operation:
            raise ValueError('operation_not_allowed')
        child = future = seq = None
        sent = False
        try:
            # Queueing, pipe backpressure and the response share one deadline.
            if deadline is not None and not time.monotonic() < deadline:
                raise TimeoutError('voice_request_expired')
            timeout = self.request_timeout if deadline is None else min(self.request_timeout, deadline - time.monotonic())
            async with asyncio.timeout(timeout):
                async with self.lock:
                    if guard is not None and guard() is not True:
                        return {'type': 'error', 'code': 'voice_context_changed'}
                    if deadline is not None and not time.monotonic() < deadline:
                        raise TimeoutError('voice_request_expired')
                    if not self.ready:
                        raise RuntimeError('voice_not_ready')
                    child = self.child
                    process = child.process
                    self.sequence += 1
                    seq = self.sequence
                    encoded = (json.dumps(dict(payload, op=op, id=seq), allow_nan=False) + '\n').encode()
                    if len(encoded) > 400000:
                        raise ValueError('voice_request_too_large')
                    future = asyncio.get_running_loop().create_future()
                    self.futures[seq] = future
                    sent = True
                    process.stdin.write(encoded)
                    await process.stdin.drain()
                    result = await future
                    if self.child is not child or not self.ready:
                        raise RuntimeError('voice_worker_closed')
                    if result['type'] == 'wake' and not (
                        set(result) == {'id', 'type', 'voice_revision', 'latency_ms'}
                        and type(result['voice_revision']) is int
                        and result['voice_revision'] == payload.get('voice_revision')
                        and type(result['latency_ms']) in {int, float}
                        and 0 <= result['latency_ms'] < math.inf
                    ):
                        raise ValueError('invalid_voice_reply')
                    return result
        except BaseException as error:
            if sent and self.child is child:
                # 单次识别超时后仍等实际回收，下一条语音可尽快恢复。
                retry = 1 if op == 'transcribe' and isinstance(error, TimeoutError) else None
                self.stop('unavailable', retry_delay=retry)
            raise
        finally:
            if seq is not None:
                self.futures.pop(seq, None)
            if future is not None:
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

    async def close(self):
        self.stop()
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)
        return await self.child.close() if self.child else True

    async def start_for_request(self):
        """仅按需启动文字子进程，等待就绪期间仍由原所有者管理进程。"""
        self.ensure_started()
        reader = self.reader
        if reader is None:
            raise RuntimeError('voice_not_ready')
        async with asyncio.timeout(self.startup_timeout):
            await self.started.wait()
        if self.reader is not reader or not self.ready:
            raise RuntimeError('voice_not_ready')
