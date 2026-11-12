"""One owned output per event loop; revocation never releases unfinished work."""

import asyncio
import os
import secrets
import stat
import weakref
from contextlib import contextmanager

from .local_child import owned

_ACTIVE = weakref.WeakKeyDictionary()


@contextmanager
def output_lock(private):
    import fcntl

    descriptor = os.open(private / "speech-output.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size != 0
        ):
            raise ValueError("speech_output_lock_unavailable")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class SpeechExpired(ValueError):
    def __init__(self):
        super().__init__("speech_request_expired")


class SpeechGuard:
    def __init__(self, current=lambda: True, *, revalidate=None):
        self.current = current
        self.revalidate = revalidate
        self.revoked = self.dispatched = False
        self.bindings = {}

    def check(self):
        try:
            valid = not self.revoked and self.current() is True
        except Exception:
            valid = False
        if not valid:
            raise SpeechExpired()

    async def checkpoint(self):
        """Refresh external ownership at send boundaries without blocking the loop."""
        self.check()
        if self.revalidate is not None:
            try:
                valid = await self.revalidate()
            except Exception:
                valid = False
            if valid is not True:
                self.revoked = True
                raise SpeechExpired()
        self.check()

    def bind(self, key, value):
        self.check()
        previous = self.bindings.setdefault(key, value)
        if not secrets.compare_digest(previous, value):
            raise SpeechExpired()

    def dispatch(self):
        self.check()
        self.dispatched = True

    def receipt(self):
        return dict(status="unconfirmed" if self.dispatched else "expired", physical_audio_verified=False)


async def finish_cancelled(task):
    if not task.done() and not task.cancelling():
        task.cancel()
    await asyncio.shield(asyncio.gather(task, return_exceptions=True))


async def run_output(perform, current, *, timeout=90, revalidate=None):
    loop = asyncio.get_running_loop()
    previous = _ACTIVE.get(loop)
    if previous is not None and not previous.done():
        return dict(status="busy", physical_audio_verified=False)
    guard = SpeechGuard(current, revalidate=revalidate)
    guard.check()
    work = owned(asyncio.create_task(perform(guard)))
    _ACTIVE[loop] = work

    def release(finished):
        if _ACTIVE.get(loop) is finished:
            del _ACTIVE[loop]

    work.add_done_callback(release)
    deadline = loop.time() + timeout
    try:
        while not work.done():
            await asyncio.wait({work}, timeout=0.05)
            try:
                guard.check()
                if loop.time() >= deadline:
                    raise SpeechExpired()
            except SpeechExpired:
                guard.revoked = True
                await finish_cancelled(work)
                return guard.receipt()
        result = await work
        await guard.checkpoint()
        return result
    except SpeechExpired:
        return guard.receipt()
    except asyncio.CancelledError:
        guard.revoked = True
        await finish_cancelled(work)
        if asyncio.current_task().cancelling():
            raise
        return guard.receipt()
    finally:
        guard.revoked = True
