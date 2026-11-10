"""持有单个照片子进程及其清理责任，不阻塞家庭控制事件循环。"""

import asyncio
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

from homeplane.media_probe import read_worker_rss, stop_process

from .photo_codec import PhotoCodecError
from .photo_contracts import MAX_RENDER_BYTES, PhotoWorkRequest, PhotoWorkResult

RSS_LIMIT = 900 * 1024 * 1024
PHOTO_CODES = frozenset({
    "photo_original_changed", "photo_dependency_missing", "photo_color_profile_unsupported",
    "photo_color_depth_unsupported", "photo_pixel_limit", "photo_animation_unsupported",
    "photo_output_limit", "photo_output_unverified", "photo_decode_failed", "photo_protocol_invalid",
    "photo_processing_failed",
    "photo_source_image_limit",
})


async def _join_owned(task):
    """重复取消不转交给清理任务；清理完成前不释放名额。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return task.result()


class PhotoRunner:
    def __init__(self, *, launcher=None, timeout=15, rss_reader=None):
        self.launcher = launcher or self._launch
        self.timeout = timeout
        self.rss_reader = rss_reader or read_worker_rss
        self.active = None
        self.process = None
        self.closed = False

    async def _launch(self):
        script = Path(__file__).with_name("photo_worker.py")
        environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-I", str(script), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, env=environment, limit=65536,
        )

    async def render(self, payload, request: PhotoWorkRequest):
        if self.closed:
            raise PhotoCodecError("photo_renderer_closed")
        if self.active is not None:
            raise PhotoCodecError("photo_renderer_busy")
        if len(payload) != request.input_bytes:
            raise PhotoCodecError("photo_original_changed")
        task = asyncio.create_task(self._run(payload, request))
        self.active = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            try:
                await _join_owned(task)
            except (Exception, asyncio.CancelledError):
                pass
            raise
        finally:
            if task.done() and self.active is task:
                self.active = None

    async def _run(self, payload, request):
        process = None
        launch = asyncio.create_task(self.launcher())
        tasks = []
        try:
            async with asyncio.timeout(self.timeout):
                process = await asyncio.shield(launch)
                self.process = process

                async def memory_guard():
                    while process.returncode is None:
                        if await self.rss_reader(process) > RSS_LIMIT:
                            raise PhotoCodecError("photo_memory_limit")
                        await asyncio.sleep(.2)

                io = asyncio.create_task(self._exchange(process, payload, request))
                memory = asyncio.create_task(memory_guard())
                tasks = [io, memory]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if memory in done:
                    await memory
                return await io
        except TimeoutError:
            raise PhotoCodecError("photo_timeout") from None
        except PhotoCodecError:
            raise
        except (OSError, ValueError, asyncio.IncompleteReadError):
            raise PhotoCodecError("photo_worker_failed") from None
        finally:
            cleanup = asyncio.create_task(self._cleanup(launch, process, tasks))
            await _join_owned(cleanup)

    async def _cleanup(self, launch, process, tasks):
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is None:
            try:
                process = await asyncio.shield(launch)
            except (Exception, asyncio.CancelledError):
                return
        try:
            await stop_process(process, grace=.25)
        finally:
            if self.process is process:
                self.process = None

    async def _exchange(self, process, payload, request):
        header = request.model_dump_json().encode()
        process.stdin.write(struct.pack(">I", len(header)) + header)
        for offset in range(0, len(payload), 1048576):
            process.stdin.write(memoryview(payload)[offset:offset + 1048576])
            await process.stdin.drain()
        process.stdin.close()
        count = struct.unpack(">I", await process.stdout.readexactly(4))[0]
        if not 0 < count <= 4096:
            raise PhotoCodecError("photo_protocol_invalid")
        response = json.loads(await process.stdout.readexactly(count))
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise PhotoCodecError("photo_protocol_invalid")
        if response["ok"] is False:
            code = response.get("code")
            raise PhotoCodecError(code if code in PHOTO_CODES else "photo_processing_failed")
        if set(response) != {"ok", "result"}:
            raise PhotoCodecError("photo_protocol_invalid")
        result = PhotoWorkResult.model_validate(response["result"])
        if (result.original_sha256 != request.original_sha256 or result.output_format != request.output_format
                or request.max_edge and max(result.width, result.height) > request.max_edge):
            raise PhotoCodecError("photo_output_unverified")
        remaining, parts, digest = result.size_bytes, [], hashlib.sha256()
        if remaining > MAX_RENDER_BYTES:
            raise PhotoCodecError("photo_output_limit")
        while remaining:
            chunk = await process.stdout.readexactly(min(1048576, remaining))
            digest.update(chunk)
            parts.append(chunk)
            remaining -= len(chunk)
            await asyncio.sleep(0)
        if await process.stdout.read(1):
            raise PhotoCodecError("photo_protocol_invalid")
        await process.wait()
        if process.returncode != 0 or digest.hexdigest() != result.output_sha256:
            raise PhotoCodecError("photo_output_unverified")
        encoded = b"".join(parts)
        magic = b"\x89PNG\r\n\x1a\n" if result.output_format == "PNG" else b"\xff\xd8"
        if not encoded.startswith(magic):
            raise PhotoCodecError("photo_output_unverified")
        return result, encoded

    async def close(self):
        self.closed = True
        if self.active is not None:
            task = self.active
            task.cancel()
            try:
                await _join_owned(task)
            except (Exception, asyncio.CancelledError):
                pass
            if self.active is task:
                self.active = None
