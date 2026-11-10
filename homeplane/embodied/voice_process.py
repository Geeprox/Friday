"""Own a voice pipe through startup, serialized requests, failure and actual reap.

Starting is nonblocking for the camera loop. A retired generation never accepts
another request, and its local child slot remains held until it actually exits.
This module imports no speech model or device SDK.
"""

import asyncio
import json
import threading
import time

from .local_child import LocalChild, owned


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
    def __init__(self, command, *, startup_timeout=60, request_timeout=60, retry_delay=30):
        self.command = tuple(command)
        self.startup_timeout, self.request_timeout = startup_timeout, request_timeout
        self.retry_delay, self.retry_at = retry_delay, 0
        self.child = self.reader = None
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
        self.phase, self.metadata = 'loading_local_models', {}
        self.reader = owned(asyncio.create_task(self._read(child)))

    def _fail_pending(self):
        for future in self.futures.values():
            if not future.done():
                future.set_exception(RuntimeError('voice_worker_closed'))

    def stop(self, phase='stopped'):
        self.phase, self.metadata = phase, {}
        self.retry_at = time.monotonic() + self.retry_delay if phase == 'unavailable' else 0
        self._fail_pending()
        if self.reader and not self.reader.done():
            self.reader.cancel()
        if self.child:
            self.child.retire()

    async def _read(self, child):
        try:
            async with asyncio.timeout(self.startup_timeout):
                process = await child.start()
                initial = _reply(await process.stdout.readline())
                if (initial.get('type') != 'ready' or not isinstance(initial.get('asr'), str)
                        or not 1 <= len(initial['asr']) <= 128
                        or initial.get('intelligence') is not None and not isinstance(initial['intelligence'], dict)):
                    raise ValueError('invalid_voice_ready')
            self.metadata = dict(asr_model=initial['asr'], intelligence=initial.get('intelligence'))
            self.phase = 'ready'
            while line := await process.stdout.readline():
                item = _reply(line)
                seq = item.get('id')
                if type(seq) is not int or seq <= 0 or item.get('type') not in {'transcript', 'task_result', 'error'}:
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
            if self.child is child:
                if self.phase != 'stopped':
                    self.phase = 'unavailable'
                    self.retry_at = time.monotonic() + self.retry_delay
                self.metadata = {}
                self._fail_pending()
            child.retire()

    async def request(self, op, **payload):
        expected = {'transcribe': 'transcript', 'delegate': 'task_result'}.get(op)
        if expected is None:
            raise ValueError('operation_not_allowed')
        child = future = seq = None
        sent = False
        try:
            # Queueing, pipe backpressure and the response share one deadline.
            async with asyncio.timeout(self.request_timeout):
                async with self.lock:
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
                    if result['type'] not in {expected, 'error'}:
                        raise ValueError('invalid_voice_reply')
                    return result
        except BaseException:
            if sent and self.child is child:
                self.stop('unavailable')
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
