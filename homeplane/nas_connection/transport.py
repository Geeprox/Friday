"""Persistent direct client in a killable worker. No inherited credentials or shell."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Protocol

from .contracts import NASConnectionConfig, NASConnectionError, NASCredentials

MAX_FRAME = 2 * 1024 * 1024


class Transport(Protocol):
    async def connect(self, config: NASConnectionConfig, credentials: NASCredentials) -> dict: ...
    async def request(self, operation: str, payload: dict) -> dict: ...
    async def close(self) -> None: ...


class WorkerTransport:
    def __init__(self, *, python_executable=sys.executable):
        self.process = None
        self.sequence = 0
        self.python_executable = python_executable

    async def connect(self, config, credentials):
        # Executing the script by absolute path avoids PYTHONPATH/sitecustomize inheritance.
        command = [self.python_executable, "-I", "-B", str(Path(__file__).with_name("worker.py"))]
        environment = {k: os.environ[k] for k in ("LANG", "LC_ALL", "SYSTEMROOT") if k in os.environ}
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=environment, limit=MAX_FRAME,
            )
            return await self.request("connect", {"config": config.model_dump(),
                                                   "credentials": credentials.payload()})
        except BaseException:
            await self.close()
            raise

    async def request(self, operation, payload):
        process = self.process
        if process is None or process.returncode is not None:
            raise NASConnectionError("nas_unavailable")
        self.sequence += 1
        frame = json.dumps({"id": self.sequence, "op": operation, "payload": payload}).encode() + b"\n"
        if len(frame) > 32768:
            raise NASConnectionError("nas_budget_exceeded")
        try:
            process.stdin.write(frame)
            await process.stdin.drain()
            data = await process.stdout.readline()
            if not data or len(data) > MAX_FRAME:
                raise NASConnectionError("nas_transport_failed")
            reply = json.loads(data)
            if reply.get("id") != self.sequence:
                raise NASConnectionError("nas_transport_failed")
            if "error" in reply:
                raise NASConnectionError(reply["error"])
            if not isinstance(reply.get("result"), dict):
                raise NASConnectionError("nas_transport_failed")
            return reply["result"]
        except (OSError, ValueError, asyncio.IncompleteReadError):
            raise NASConnectionError("nas_transport_failed") from None

    async def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is None:
            # Closing stdin requests normal adapter teardown, then terminate/kill if hung.
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 0.2)
            except TimeoutError:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 0.2)
                except TimeoutError:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
