"""Owned, two-phase local calibration save; caller cancellation cannot split it."""

import asyncio
import base64
import json
import secrets
import sys
import threading
import time

from .calibration_store import MAX_BUNDLE, CalibrationStore, encode, manual_config
from .local_child import LocalChild, owned

_SLOT = threading.BoundedSemaphore(1)


class CalibrationSaver:
    def __init__(self, folder, invalidate, *, timeout=8, command=None):
        self.folder, self.invalidate = folder, invalidate
        self.timeout = timeout
        self.command = tuple(
            command or (sys.executable, "-B", "-m", "homeplane.embodied.calibration_worker", str(folder))
        )
        self.task = self.owner = self.finisher = None
        self.closed = False
        self.cleanup_pending = None

    async def save(self, config, reference, *, current=lambda: True):
        if self.closed:
            raise ValueError("calibration_unavailable")
        if self.task is not None and not self.task.done():
            raise ValueError("calibration_busy")
        if self.cleanup_pending is not None:
            raise ValueError("calibration_unavailable")
        config = manual_config(json.loads(json.dumps(config, allow_nan=False)))
        encode(config, reference)
        self.task = owned(asyncio.create_task(self._save(config, reference, current)))
        try:
            return await asyncio.wait_for(asyncio.shield(self.task), self.timeout + 2)
        except TimeoutError:
            raise ValueError("calibration_unavailable") from None

    async def _save(self, config, reference, current):
        owner = self.owner = LocalChild(self.command, _SLOT, limit=4096)
        nonce = secrets.token_hex(16)
        began = time.time()
        possible_commit = False
        expected_payload, expected = encode(config, reference)

        async def reply(kind):
            raw = await owner.process.stdout.readline()
            if not raw or len(raw) > 4096 or not raw.endswith(b"\n"):
                raise ValueError("calibration_unavailable")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("nonce") != nonce:
                raise ValueError("calibration_unavailable")
            if value.get("type") == "error":
                raise ValueError(
                    "calibration_invalid"
                    if value.get("reason") == "calibration_invalid"
                    else "calibration_unavailable"
                )
            if (
                set(value) != {"type", "nonce", "revision"}
                or value["type"] != kind
                or not isinstance(value["revision"], str)
                or len(value["revision"]) != 64
                or any(c not in "abcdef0123456789" for c in value["revision"])
            ):
                raise ValueError("calibration_unavailable")
            return value["revision"]

        try:
            async with asyncio.timeout(self.timeout):
                if self.closed or not current():
                    raise ValueError("calibration_source_changed")
                await owner.start()
                request = dict(
                    v=1, nonce=nonce, config=config, reference_jpeg=base64.b64encode(reference).decode()
                )
                payload = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode()
                if len(payload) > MAX_BUNDLE:
                    raise ValueError("calibration_invalid")
                owner.process.stdin.write(payload)
                await owner.process.stdin.drain()
                revision = await reply("prepared")
                if revision != expected:
                    raise ValueError("calibration_unavailable")
                # No await separates source admission from sending the commit.
                if self.closed or not current():
                    raise ValueError("calibration_source_changed")
                possible_commit = True
                owner.process.stdin.write(
                    (json.dumps(dict(action="commit", nonce=nonce, revision=revision)) + "\n").encode()
                )
                await owner.process.stdin.drain()
                if await reply("saved") != revision:
                    raise ValueError("calibration_unavailable")
                return dict(saved=True, metric=False, revision=revision)
        except Exception as error:
            reason = str(error)
            if reason == "child_busy":
                reason = "calibration_busy"
            if reason not in {"calibration_invalid", "calibration_source_changed", "calibration_busy"}:
                reason = "calibration_unavailable"
            raise ValueError(reason) from None
        finally:

            async def finish():
                try:
                    if not await owner.close() and owner.reaper is not None:
                        await asyncio.shield(owner.reaper)
                    if possible_commit:
                        self.cleanup_pending = nonce
                        try:
                            self.invalidate()
                        finally:
                            await asyncio.to_thread(
                                CalibrationStore(self.folder).cleanup, nonce, expected_payload, began
                            )
                            self.cleanup_pending = None
                except Exception:
                    raise ValueError("calibration_unavailable") from None

            self.finisher = owned(asyncio.create_task(finish()))
            await asyncio.shield(self.finisher)

    async def close(self):
        first = not self.closed
        self.closed = True
        if self.task is not None:
            if first and not self.task.done():
                self.task.cancel()
            await asyncio.gather(asyncio.shield(self.task), return_exceptions=True)
        if self.finisher is not None:
            await asyncio.shield(self.finisher)
