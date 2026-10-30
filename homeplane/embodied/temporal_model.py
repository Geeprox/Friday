"""One bounded, offline MLX owner. No model imports in perception or Console."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .paths import MODELS, PRIVATE, PROJECT

MODEL_ID = "mlx-community/Qwen3.5-2B-4bit"
REVISION = "674aaa7240b91e8012fcad5d791b7dfe5ba90207"
WEIGHT_BYTES = 1722271785
WEIGHT_SHA256 = "713fe7e5d3c3965f7106b0d0ee17615f7869c23c8d327996df8c1196fbcf07d5"
MODEL = MODELS / "qwen3.5-2b-4bit"
GIB = 1024**3
MEMORY_LIMIT = 3584 * 1024**2
EVENTS = ("none", "unclear", "possible_fall", "face_obscured", "rapid_movement")


class VisualResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event: Literal["none", "unclear", "possible_fall", "face_obscured", "rapid_movement"]
    observation: str = Field(min_length=1, max_length=240)


class WorkerReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    result: VisualResult
    peak_mlx_bytes: int = Field(ge=0, le=MEMORY_LIMIT)


def model_readiness(model=MODEL):
    """Metadata only; never imports MLX, downloads, or evaluates weights."""
    model = Path(model)
    reason = None
    try:
        source = json.loads((model / "friday-source.json").read_text())
        if (
            not isinstance(source, dict)
            or model.is_symlink()
            or (model / "model.safetensors").is_symlink()
            or source.get("repository") != MODEL_ID
            or source.get("revision") != REVISION
            or source.get("weight_sha256") != WEIGHT_SHA256
            or (model / "model.safetensors").stat().st_size != WEIGHT_BYTES
            or not all((model / name).is_file() for name in ("config.json", "tokenizer.json"))
        ):
            reason = "model_pin_mismatch"
    except (OSError, ValueError, TypeError):
        reason = "model_not_installed"
    if reason is None and (platform.system() != "Darwin" or platform.machine() != "arm64"):
        reason = "apple_silicon_required"
    if reason is None and importlib.util.find_spec("mlx_vlm") is None:
        reason = "local_runtime_missing"
    return dict(
        available=reason is None,
        reason=reason,
        model=MODEL_ID,
        mode="bounded_window_review",
        native_streaming=False,
        max_frames=4,
        sample_interval_seconds=1,
        min_request_interval_seconds=6,
        max_image_edge=256,
        mlx_memory_limit_mib=3584,
        parallel_requests=1,
        idle_exit_seconds=60,
        automatic_announcement=False,
        seek="optional_not_loaded",
        cloud_fallback=False,
    )


@dataclass(frozen=True)
class HostBudget:
    total_bytes: int
    free_percent: int | None
    load: float = 0
    cores: int = 1

    def reason(self, *, resident=False):
        if self.total_bytes < 12 * GIB:
            return "memory_capacity"
        if self.free_percent is None:
            return "memory_unknown"
        reserve = 3 * GIB if resident else 5 * GIB
        if self.total_bytes * self.free_percent / 100 < reserve:
            return "memory_pressure"
        if self.load > max(1, self.cores) * 1.5:
            return "cpu_busy"
        return None


def host_budget():
    """Read-only host pressure. Failure defers inference instead of guessing."""
    try:
        total = int(
            subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=2, stderr=subprocess.DEVNULL)
        )
        text = subprocess.check_output(
            ["memory_pressure", "-Q"], text=True, timeout=2, stderr=subprocess.DEVNULL
        )
        match = re.search(r"System-wide memory free percentage:\s*(\d+)%", text)
        percent = int(match[1]) if match else None
        return HostBudget(total, percent, os.getloadavg()[0], os.cpu_count() or 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return HostBudget(0, None)


class MlxWindowProcess:
    """One child and one file lease; cancellation releases GPU ownership."""

    def __init__(self, model=MODEL, private=PRIVATE, *, budget=host_budget):
        self.model, self.private, self.budget = Path(model), Path(private), budget
        self.process = self.lease = self.idle_task = None
        self.lock = asyncio.Lock()
        self.last_used = 0
        self.peak_mlx_bytes = 0

    async def _start(self):
        if self.process is not None and self.process.returncode is None:
            return
        ready = model_readiness(self.model)
        if not ready["available"]:
            raise ValueError(ready["reason"])
        import fcntl

        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.private / "temporal-model.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ValueError("model_busy") from None
        self.lease = fd
        environment = dict(
            os.environ,
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            OMP_NUM_THREADS="2",
            VECLIB_MAXIMUM_THREADS="2",
        )
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-m",
                "homeplane.embodied.temporal_worker",
                "--model",
                str(self.model),
                cwd=PROJECT,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=8192,
            )
            self.last_used = time.monotonic()
            self.idle_task = asyncio.create_task(self._idle())
        except BaseException:
            await self.close()
            raise

    async def _idle(self):
        try:
            while self.process is not None:
                await asyncio.sleep(15)
                if not self.lock.locked() and time.monotonic() - self.last_used >= 60:
                    async with self.lock:
                        await self.close()
                    return
        except asyncio.CancelledError:
            pass

    async def analyze(self, frames):
        async with self.lock:
            try:
                if self.process is not None and self.process.returncode is not None:
                    await self.close()
                reason = (await asyncio.to_thread(self.budget)).reason(resident=self.process is not None)
                if reason:
                    raise ValueError(reason)
                await self._start()
                request = dict(
                    frames=[
                        dict(t=round(stamp - frames[0][0], 3), jpeg=base64.b64encode(jpeg).decode())
                        for stamp, jpeg in frames
                    ]
                )
                payload = json.dumps(request).encode() + b"\n"
                if len(payload) > 3 * 1024**2:
                    raise ValueError("window_too_large")

                async def exchange():
                    self.process.stdin.write(payload)
                    await self.process.stdin.drain()
                    return await self.process.stdout.readline()

                data = await asyncio.wait_for(exchange(), 12)
                if not data:
                    raise ValueError("model_unavailable")
                self.last_used = time.monotonic()
                reply = WorkerReply.model_validate_json(data)
                self.peak_mlx_bytes = reply.peak_mlx_bytes
                return reply.result
            except asyncio.CancelledError:
                await self.close()
                raise
            except (OSError, ValueError, asyncio.TimeoutError) as error:
                await self.close()
                code = str(error)
                raise ValueError(
                    code
                    if code
                    in {
                        "memory_pressure",
                        "memory_unknown",
                        "memory_capacity",
                        "cpu_busy",
                        "model_busy",
                        "model_not_installed",
                        "model_pin_mismatch",
                        "local_runtime_missing",
                    }
                    else "model_review_unavailable"
                ) from None

    async def close(self):
        idle, self.idle_task = self.idle_task, None
        if idle and idle is not asyncio.current_task():
            idle.cancel()
        process, self.process = self.process, None
        lease, self.lease = self.lease, None
        try:
            if process and process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 2)
                except (ProcessLookupError, asyncio.TimeoutError):
                    pass
        finally:
            try:
                if process and process.returncode is None:
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        pass
            finally:
                if lease is not None:
                    os.close(lease)
        if idle and idle is not asyncio.current_task():
            await asyncio.gather(idle, return_exceptions=True)
