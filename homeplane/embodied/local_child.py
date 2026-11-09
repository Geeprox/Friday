"""Own a local child's handle and slot through cancellation and actual reap."""

import asyncio

_OWNED = set()


def owned(task):
    _OWNED.add(task)

    def done(finished):
        _OWNED.discard(finished)
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(done)
    return task


class LocalChild:
    def __init__(self, command, slots, *, limit=65536, reap_timeout=1,
                 terminate_timeout=.2, cwd=None, env=None):
        self.command, self.slots = tuple(command), slots
        self.limit, self.reap_timeout = limit, reap_timeout
        self.terminate_timeout = terminate_timeout
        self.options = {key: value for key, value in {"cwd": cwd, "env": env}.items() if value is not None}
        self.process = self.spawn = self.reaper = None
        self.slot, self.closed = False, False

    def spawn_options(self):
        return self.options

    async def start(self):
        if self.closed:
            raise ValueError("child_closed")
        if self.process is not None:
            return self.process
        if self.spawn is None:
            if not self.slots.acquire(blocking=False):
                raise ValueError("child_busy")
            self.slot = True
            self.spawn = owned(
                asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *self.command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        limit=self.limit,
                        **self.spawn_options(),
                    )
                )
            )
        self.process = await asyncio.shield(self.spawn)
        return self.process

    def retire(self):
        if self.slot and (self.reaper is None or self.reaper.done()):
            self.reaper = owned(asyncio.create_task(self._reap()))

    def reaped(self):
        pass

    async def _reap(self):
        process = drain = None
        reaped = False
        try:
            try:
                process = self.process or await asyncio.shield(self.spawn)
            except Exception:
                reaped = True
                return
            self.process = process

            async def discard_output():
                while await process.stdout.read(16384):
                    pass

            drain = owned(asyncio.create_task(discard_output()))
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), self.terminate_timeout)
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
            if reaped:
                try:
                    if process is not None and process.stdin is not None:
                        process.stdin.close()
                finally:
                    self.process = self.spawn = None
                    if self.slot:
                        self.slot = False
                        self.slots.release()
                    self.reaped()

    async def close(self):
        self.closed = True
        self.retire()
        if self.reaper is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.reaper), self.reap_timeout)
            except (TimeoutError, OSError):
                return False
        return not self.slot
