"""Bound one request over an already-owned native vision process.

No process/model is started here. A failed or cancelled exchange invalidates
the stream: the owner must reap it instead of accepting a late reply as a new
frame. Both camera owners exit their source and do that in their finally block.
"""

import asyncio
import json
import math


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
    if len(line) > PIPE_LIMIT:
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
