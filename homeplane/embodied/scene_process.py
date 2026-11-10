"""Lazy scene subprocess with source leases, deadlines and owned child reaping.

Construction/status never import or start a model. Camera bytes stay in bounded
anonymous pipes. A timed-out child holds the sole slot until it is truly reaped.
"""

import asyncio
import base64
import json
import secrets
import sys
import threading
import time
from pathlib import Path

from .scene_protocol import MAX_JPEG, MAX_REPLY, REASONS, scene_result, source_context

_SLOT = threading.BoundedSemaphore(1)
_OWNED = set()


def owned(task):
    _OWNED.add(task)

    def done(finished):
        _OWNED.discard(finished)
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(done)
    return task


class SceneProcess:
    def __init__(self, model, *, startup_timeout=12, frame_timeout=.8,
                 retry_delay=2, reap_timeout=1, command=None):
        self.command = tuple(command or (sys.executable, '-B', '-m',
                                        'homeplane.embodied.scene_worker', str(Path(model))))
        self.startup_timeout, self.frame_timeout = startup_timeout, frame_timeout
        self.retry_delay, self.reap_timeout = retry_delay, reap_timeout
        self.process = self.spawn = self.operation = self.reaper = None
        self.slot, self.closed, self.ready = False, False, False
        self.lock = asyncio.Lock()
        self.nonce, self.sequence = secrets.token_hex(16), 0
        self.retry_at = 0
        self.failures = 0
        self.state, self.reason = 'idle', None

    def status(self):
        return dict(state=self.state, reason=self.reason,
                    cleanup_pending=self.slot and not self.ready and self.reaper is not None)

    async def _exchange(self, action, value, context=None):
        self.sequence += 1
        packet = dict(v=1, nonce=self.nonce, sequence=self.sequence,
                      action=action, value=value, context=context)
        self.process.stdin.write((json.dumps(packet, separators=(',', ':'), allow_nan=False) + '\n').encode())
        await self.process.stdin.drain()
        try:
            raw = await self.process.stdout.readline()
        except ValueError:
            raise ValueError('scene_protocol_invalid') from None
        if not raw or len(raw) > MAX_REPLY or not raw.endswith(b'\n'):
            raise ValueError('scene_protocol_invalid')
        try:
            reply = json.loads(raw)
        except ValueError:
            raise ValueError('scene_protocol_invalid') from None
        if (not isinstance(reply, dict)
                or set(reply) != {'v', 'nonce', 'sequence', 'action', 'context', 'ok', 'result', 'reason'}
                or type(reply['v']) is not int or reply['v'] != 1
                or type(reply['sequence']) is not int or reply['sequence'] != self.sequence
                or reply['nonce'] != self.nonce or reply['action'] != action
                or reply['context'] != context or type(reply['ok']) is not bool):
            raise ValueError('scene_protocol_invalid')
        if context is not None:
            source_context(reply['context'])
        if not reply['ok']:
            raise ValueError(reply['reason'] if reply['reason'] in REASONS else 'scene_protocol_invalid')
        if reply['reason'] is not None:
            raise ValueError('scene_protocol_invalid')
        if action == 'init':
            if reply['result'] != {}:
                raise ValueError('scene_protocol_invalid')
            return None
        return scene_result(reply['result'])

    async def _perform(self, jpeg, context, current, active):
        if self.process is None:
            if not _SLOT.acquire(blocking=False):
                raise ValueError('scene_model_busy')
            self.slot = True
            self.nonce, self.sequence = secrets.token_hex(16), 0
            self.state, self.reason = 'starting', None
            self.spawn = owned(asyncio.create_task(asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=MAX_REPLY)))
            async with asyncio.timeout(self.startup_timeout):
                # Cancellation cannot discard a child handle still being created.
                self.process = await asyncio.shield(self.spawn)
                await self._exchange('init', {})
            self.ready = True
            self.state = 'ready'
        if not active() or self.closed:
            raise ValueError('scene_source_retired')
        # Model startup may outlive the triggering frame. Keep the warmed model
        # for the next fresh frame while the same live source still owns it.
        if not current():
            return None
        self.state = 'detecting'
        async with asyncio.timeout(self.frame_timeout):
            return await self._exchange('frame', dict(jpeg=base64.b64encode(jpeg).decode()), context)

    def _retire(self, reason):
        self.ready = False
        self.reason = reason
        if reason not in {'scene_closed', 'scene_cancelled', 'scene_source_retired', 'scene_model_busy'}:
            self.failures = min(5, self.failures + 1)
        delay = min(30, self.retry_delay * 2**max(0, self.failures - 1))
        self.retry_at = time.monotonic() + delay
        if self.reaper is not None and not self.reaper.done():
            return
        if not self.slot:
            self.state = 'closed' if self.closed else 'backoff'
            return
        self.state = 'cleanup_pending'
        self.reaper = owned(asyncio.create_task(self._reap()))

    async def _reap(self):
        process = None
        drain = None
        reaped = False
        try:
            try:
                process = self.process or await asyncio.shield(self.spawn)
            except Exception:
                reaped = True
                return  # Spawn failure has no child to reap.
            self.process = process

            async def discard_output():
                while await process.stdout.read(16384):
                    pass

            # Keep a filled stdout pipe from delaying subprocess.wait() after exit.
            drain = owned(asyncio.create_task(discard_output()))
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), .2)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            reaped = True
        finally:
            if drain:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            # A cancelled/unreaped native child must never free the slot.
            if reaped:
                if process is not None and process.stdin is not None:
                    process.stdin.close()
                self.process = self.spawn = None
                if self.slot:
                    self.slot = False
                    _SLOT.release()
                self.state = 'closed' if self.closed else 'backoff'

    async def analyze(self, jpeg, *, context, current=lambda: True, active=None):
        active = active or current
        if (self.closed or self.lock.locked() or time.monotonic() < self.retry_at
                or self.slot and not self.ready
                or self.reaper is not None and not self.reaper.done() or not current()):
            return None
        try:
            context = source_context(context)
            if not isinstance(jpeg, bytes) or not 0 < len(jpeg) <= MAX_JPEG:
                raise ValueError('scene_frame_invalid')
        except ValueError:
            self.reason = 'scene_frame_invalid'
            return None
        async with self.lock:
            operation = self.operation = owned(asyncio.create_task(self._perform(jpeg, context, current, active)))
            try:
                while not operation.done():
                    await asyncio.wait({operation}, timeout=.04)
                    if not active() or self.closed or (
                            not operation.done() and self.state == 'detecting' and not current()):
                        raise ValueError('scene_source_retired')
                result = await operation
                if not active() or self.closed:
                    raise ValueError('scene_source_retired')
                self.state, self.reason = 'ready', None
                self.failures = 0
                return result if current() else None
            except asyncio.CancelledError:
                operation.cancel()
                self._retire('scene_cancelled')
                raise
            except Exception as error:
                reason = str(error) if isinstance(error, ValueError) else 'scene_worker_unavailable'
                known = REASONS | {'scene_model_busy', 'scene_source_retired'}
                if isinstance(error, TimeoutError):
                    reason = 'scene_timeout'
                elif reason not in known:
                    reason = 'scene_worker_unavailable'
                operation.cancel()
                self._retire(reason)
                return None
            finally:
                self.operation = None

    async def close(self):
        self.closed = True
        if self.operation is not None:
            self.operation.cancel()
        self._retire('scene_closed')
        if self.reaper is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.reaper), self.reap_timeout)
            except TimeoutError:
                return False
            except Exception:
                self.reason = 'scene_cleanup_unavailable'
                return False
        return not self.slot
