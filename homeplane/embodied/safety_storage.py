"""One bounded storage worker; cancellation never releases a running DB slot."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor


class SafetyStorageBusy(RuntimeError):
    pass


class SafetyStorageFull(RuntimeError):
    pass


class SafetyIO:
    def __init__(self, *, max_pending=16, acquire_timeout=2.5, thread_name_prefix='friday-safety-store'):
        self.max_pending = max_pending
        self.acquire_timeout = acquire_timeout
        self.pending = 0
        self.closed = False
        self.active = None
        self.lock = asyncio.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name_prefix)

    async def call(self, operation, *args, **kwargs):
        if self.closed or self.pending >= self.max_pending:
            raise SafetyStorageBusy('safety_storage_busy')
        self.pending += 1
        acquired = False
        try:
            try:
                async with asyncio.timeout(self.acquire_timeout):
                    await self.lock.acquire()
                    acquired = True
            except TimeoutError:
                raise SafetyStorageBusy('safety_storage_busy') from None
            if self.closed:
                raise SafetyStorageBusy('safety_storage_closed')
            future = self.active = self.executor.submit(operation, *args, **kwargs)
            wrapped = asyncio.wrap_future(future)
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(wrapped)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    # Keep ownership until the actual thread operation finishes,
                    # including repeated cancellation of the awaiting owner.
                    if wrapped.cancelled():
                        if future.cancelled():
                            raise
                        wrapped = asyncio.wrap_future(future)
                except BaseException:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            if acquired:
                self.active = None
                self.lock.release()
            self.pending -= 1

    async def drain(self):
        async with self.lock:
            pass

    async def close(self):
        self.closed = True
        await self.drain()
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
