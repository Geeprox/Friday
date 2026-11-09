"""Asynchronous Room facade. Native state belongs to one killable child."""

import asyncio
import base64
import json
import secrets
import sys
import threading
import time
import weakref

from .local_child import LocalChild, owned
from .room_protocol import (
    HAND_FIELDS,
    MAX_JPEG,
    MAX_REPLY,
    MAX_REQUEST,
    PERSON_FIELDS,
    layout_result,
    metadata,
    packet,
)

_SLOTS = threading.BoundedSemaphore(8)
_SESSIONS = weakref.WeakSet()


class RoomSession:
    def __init__(self, folder, *, scope=None, command=None, startup_timeout=4, timeout=0.6, retry_delay=2):
        self.command = tuple(
            command or (sys.executable, "-B", "-m", "homeplane.embodied.room_worker", str(folder))
        )
        self.scope, self.startup_timeout, self.timeout, self.retry_delay = (
            scope,
            startup_timeout,
            timeout,
            retry_delay,
        )
        self.owner = self.operation = self.finisher = None
        self.closed, self.ready = False, False
        self.epoch, self.sequence, self.failures = 0, 0, 0
        self.nonce = secrets.token_hex(16)
        self.retry_at = 0
        self.frame = None
        self.command_until = 0
        self.reset_pending = False
        self.registration = None
        self.reg_info = {"status": "checking"}
        self.scene_visible = False
        self.draft = None
        self.reason = None
        _SESSIONS.add(self)

    def status(self):
        return dict(
            state="ready" if self.ready else "closed" if self.closed else "unavailable",
            reason=self.reason,
            cleanup_pending=self.finisher is not None and self.owner is not None and self.owner.slot,
        )

    def expect_motion(self, now, seconds=1.5):
        self.epoch += 1
        self.command_until = now + seconds
        self.registration = None
        self.reg_info = {"status": "moving"}
        self.draft = None

    def reset_seats(self):
        self.epoch += 1
        self.reset_pending = True
        self.draft = None

    async def _exchange(self, action, value, frame):
        self.sequence += 1
        process = self.owner.process
        process.stdin.write(
            packet(
                dict(v=1, nonce=self.nonce, sequence=self.sequence, action=action, frame=frame, value=value),
                MAX_REQUEST,
            )
        )
        await process.stdin.drain()
        raw = await process.stdout.readline()
        if not raw or len(raw) > MAX_REPLY or not raw.endswith(b"\n"):
            raise ValueError("room_protocol_invalid")
        reply = json.loads(raw)
        if (
            not isinstance(reply, dict)
            or set(reply) != {"v", "nonce", "sequence", "action", "frame", "result"}
            or type(reply["v"]) is not int
            or reply["v"] != 1
            or type(reply["sequence"]) is not int
            or reply["sequence"] != self.sequence
            or reply["nonce"] != self.nonce
            or reply["action"] != action
            or reply["frame"] != frame
        ):
            raise ValueError("room_protocol_invalid")
        return reply["result"]

    async def _perform(self, action, value, frame, picture, current):
        if not self.ready:
            self.owner = LocalChild(self.command, _SLOTS, limit=MAX_REPLY)
            self.nonce, self.sequence = secrets.token_hex(16), 0
            async with asyncio.timeout(self.startup_timeout):
                await self.owner.start()
                if await self._exchange("open", dict(jpeg=base64.b64encode(picture).decode()), frame) != {}:
                    raise ValueError("room_protocol_invalid")
            self.ready = True
        if not current():
            return None
        async with asyncio.timeout(self.timeout):
            return await self._exchange(action, value, frame)

    def _retire(self, reason, operation=None):
        self.ready = False
        self.reason = reason
        self.registration = None
        self.reg_info = {"status": "unavailable"}
        self.scene_visible = False
        self.draft = None
        self.frame = None
        self.failures = min(5, self.failures + 1)
        self.retry_at = time.monotonic() + min(30, self.retry_delay * 2 ** (self.failures - 1))
        if operation is not None:
            operation.cancel()
        if self.finisher is not None and not self.finisher.done():
            return
        owner = self.owner

        async def finish():
            if operation is not None:
                await asyncio.gather(operation, return_exceptions=True)
            if owner is not None:
                if not await owner.close() and owner.reaper is not None:
                    await asyncio.shield(owner.reaper)

        self.finisher = owned(asyncio.create_task(finish()))

    async def _call(self, action, value, frame, picture, current, active):
        if (
            self.closed
            or self.operation is not None
            or time.monotonic() < self.retry_at
            or self.finisher is not None
            and not self.finisher.done()
            or not current()
        ):
            return None
        epoch = self.epoch
        original = current

        def current():
            return epoch == self.epoch and original()

        active = active or current
        if not isinstance(picture, bytes) or not 0 < len(picture) <= MAX_JPEG:
            return None
        operation = self.operation = owned(
            asyncio.create_task(self._perform(action, value, frame, picture, current))
        )
        try:
            while not operation.done():
                await asyncio.wait({operation}, timeout=0.04)
                if (
                    self.closed
                    or epoch != self.epoch
                    or not active()
                    or (self.ready and not operation.done() and not current())
                ):
                    raise ValueError("room_source_retired")
            result = await operation
            if self.closed or epoch != self.epoch or not active():
                raise ValueError("room_source_retired")
            if not current():
                if result is not None:
                    self._retire("room_source_retired", operation)
                return None
            return result
        except asyncio.CancelledError:
            self._retire("room_cancelled", operation)
            if not asyncio.current_task().cancelling():
                return None
            raise
        except Exception as error:
            reason = (
                "room_timeout"
                if isinstance(error, TimeoutError)
                else "room_source_retired"
                if str(error) == "room_source_retired"
                else "room_unavailable"
            )
            self._retire(reason, operation)
            return None
        finally:
            self.operation = None

    async def register(self, picture, masks, now, *, current=lambda: True, active=None):
        frame = secrets.token_hex(16)
        result = await self._call(
            "register",
            dict(
                jpeg=base64.b64encode(picture).decode(),
                masks=masks,
                now=now,
                motion_until=self.command_until,
                reset=self.reset_pending,
            ),
            frame,
            picture,
            current,
            active,
        )
        if result is None:
            return False
        try:
            result = metadata(result)
        except ValueError:
            self._retire("room_protocol_invalid")
            return False
        # Tiny matrix representation only; feature extraction stays in the child.
        import numpy as np

        self.registration = (
            np.asarray(result["matrix"], dtype=float) if result["matrix"] is not None else None
        )
        self.reg_info = dict(result["info"])
        self.command_until = result["command_until"]
        self.scene_visible = result["visible"]
        self.frame = frame
        self.reset_pending = False
        self.reason = None
        return True

    async def update(self, picture, people, hands, now, width, height, objects, *, current=lambda: True):
        if not self.ready or self.frame is None:
            return None
        value = dict(people=people, hands=hands, now=now, width=width, height=height, objects=objects)
        result = await self._call("layout", value, self.frame, picture, current, None)
        if result is None:
            return None
        try:
            result = layout_result(result, people, hands)
        except ValueError:
            self._retire("room_protocol_invalid")
            return None
        for original, reply, fields in [(a, b, PERSON_FIELDS) for a, b in zip(people, result["people"])] + [
            (a, b, HAND_FIELDS) for a, b in zip(hands, result["hands"])
        ]:
            for field in fields:
                original.pop(field, None)
                if field in reply:
                    original[field] = reply[field]
        self.draft = result["draft"]
        self.failures = 0
        return result["layout"]

    def retire(self):
        self.closed = True
        self.epoch += 1
        self._retire("room_closed", self.operation)

    async def close(self):
        self.retire()
        if self.finisher is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.finisher), 1)
            except TimeoutError:
                return False
        return True


async def close_rooms(scope):
    return await asyncio.gather(
        *(room.close() for room in list(_SESSIONS) if room.scope is scope), return_exceptions=True
    )
