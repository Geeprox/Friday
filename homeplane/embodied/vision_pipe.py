"""限制原生识别交换，并独立于摄像头采集管理识别子进程。"""

import asyncio
import json
import math
import time


PIPE_LIMIT = 6_000_000


def _nonfinite(_):
    raise ValueError('invalid_vision_reply')


async def request_vision(process, payload, *, timeout=12):
    if not isinstance(payload, dict) or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('invalid_vision_request')
    try:
        encoded = (json.dumps(payload, allow_nan=False, separators=(',', ':'))+'\n').encode()
    except (ValueError, TypeError):
        raise ValueError('invalid_vision_request') from None
    if len(encoded) > PIPE_LIMIT:
        raise ValueError('vision_request_too_large')
    # drain() also waits when the child stops reading. Giving only readline()
    # a deadline leaves an unbounded request before that timeout ever starts.
    async with asyncio.timeout(timeout):
        process.stdin.write(encoded)
        await process.stdin.drain()
        line = await process.stdout.readline()
    if not line:
        raise RuntimeError('vision_stream_closed')
    if len(line) > PIPE_LIMIT or not line.endswith(b'\n'):
        raise ValueError('invalid_vision_reply')
    try:
        reply = json.loads(line, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_vision_reply') from None
    if not isinstance(reply, dict):
        raise ValueError('invalid_vision_reply')
    if reply.get('error'):
        raise RuntimeError('vision_request_failed')
    if any(not isinstance(reply.get(key), list) for key in ('boxes', 'poses')) or any(
            key in reply and not isinstance(reply[key], list) for key in ('hands', 'faces')):
        raise ValueError('invalid_vision_reply')
    return reply


class VisionProcess:
    """每路识别串行处理。出错的管道确认退出后才允许替换。"""

    def __init__(self, executable):
        self.executable = str(executable)
        self.process = None
        self.failures = 0
        self.retry_at = 0.0

    async def detect(self, payload, *, on_failure):
        if time.monotonic() < self.retry_at:
            return None
        if self.failures and not await self._retire_failed():
            self.retry_at = time.monotonic() + 1
            return None
        try:
            if self.process is None:
                self.process = await asyncio.create_subprocess_exec(
                    self.executable, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=PIPE_LIMIT,
                )
            reply = await request_vision(self.process, payload)
        except (OSError, RuntimeError, ValueError) as error:
            self.failures += 1
            self.retry_at = time.monotonic() + 2 ** min(self.failures - 1, 3)
            try:
                on_failure(type(error).__name__)
            finally:
                await self._retire_failed()
            return None
        self.failures = 0
        return reply

    async def _retire_failed(self):
        try:
            await self.close()
        except OSError:
            return False
        return True

    async def close(self):
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(process.wait(), 3)
        self.process = None
