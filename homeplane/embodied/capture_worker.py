"""Bounded living-room video capture through the official local Miloco SDK."""

import asyncio
import base64
import collections
import json
import logging
import os
import re
import sys
import time
from types import SimpleNamespace

import cv2
import miot.camera as camera_sdk
from miot.camera import MIoTCamera
from miot.types import MIoTCameraInfo, MIoTCameraStatus

from homeplane.embodied.local_motor import LocalMotor
from homeplane.embodied.frame_sampling import LatestFrameEncoder
from homeplane.embodied.capture_decoder import install_decoder
from homeplane.embodied.paths import PRIVATE
from homeplane.embodied.paths import camera as registered_camera

OUTPUT = os.dup(1)
NULL = os.open(os.devnull, os.O_WRONLY)
os.dup2(NULL, 1)
os.dup2(NULL, 2)
logging.disable(logging.CRITICAL)
os.umask(0o077)
owner = None


def emit(value):
    data = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    while data:
        data = data[os.write(OUTPUT, data) :]


class ObservedCamera(MIoTCamera):
    """Classify native transport diagnostics; discard the original log text."""

    _last_diagnostic = 0.0

    def _on_miot_camera_log(self, level, msg):
        now = time.monotonic()
        if now - self._last_diagnostic < 1:
            return
        text = msg.decode("utf-8", errors="replace").lower()
        categories = [
            x
            for x in ("timeout", "failed", "error", "tls", "http", "auth", "lan", "p2p", "relay", "connect")
            if x in text
        ]
        if not categories:
            return
        self._last_diagnostic = now
        permitted = {-1, -2, -3, -4, -20, -100, -101, -102, -103, -104, -110, 0, 200, 401, 403}
        codes = [
            int(n)
            for n in re.findall(r"(?:err(?:or)?|ret|result|code)\s*[:=]\s*(-?\d{1,6})(?!\d)", text)
            if int(n) in permitted
        ]
        emit(
            {"type": "diagnostic", "stage": "native_transport", "categories": categories, "codes": codes[:4]}
        )


async def main():
    global owner
    reader = asyncio.StreamReader()
    pipe, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    request = json.loads(await reader.readline())
    if request.get("source") != "miloco":
        raise ValueError("source_not_allowed")
    quality = int(os.environ.get("FRIDAY_CAMERA_QUALITY", "3"))
    if quality not in (2, 3):
        raise ValueError("quality_not_allowed")
    selected = registered_camera(request.get("camera"))
    device = selected["device"]
    login = json.loads((PRIVATE / "miot-session.json").read_text())
    info = MIoTCameraInfo(**device, channel_count=1, camera_status=MIoTCameraStatus.DISCONNECTED)
    owner = ObservedCamera(cloud_server="cn", access_token=login["access_token"])
    parent_pid = os.getppid()
    started = time.monotonic()
    last = 0.0
    attention_until = 0.0
    audio_until = 0.0
    video_until = 0.0
    audio_pending = bytearray()
    audio_observed = False
    raw_times = collections.deque(maxlen=80)
    decode_times = collections.deque(maxlen=80)

    def rate(values):
        return (
            round((len(values) - 1) / (values[-1] - values[0]), 1)
            if len(values) > 1 and values[-1] > values[0]
            else 0
        )

    async def raw_packet(did, data, timestamp, sequence, channel):
        if channel == 0:
            if not raw_times:
                emit({"type": "diagnostic", "stage": "first_video_packet"})
            raw_times.append(time.monotonic())

    cv2.setNumThreads(1)

    def encoded(packet):
        packet.update(raw_fps=rate(raw_times), decoded_fps=rate(decode_times))
        decoders = getattr(owner_camera[0], '_decoders', []) if owner_camera else []
        if decoders and hasattr(decoders[0], 'friday_status'):
            packet.update(decoders[0].friday_status())
        emit(packet)

    sampler = LatestFrameEncoder(encoded, quality=quality,
        face_enabled=lambda: time.monotonic()<attention_until or request.get('identity') is True)
    sampler_task = asyncio.create_task(sampler.run())

    async def decoded(did, frame, timestamp, channel, recv_unix_ms, decoded_unix_ms):
        nonlocal last
        now = time.monotonic()
        if not decode_times:
            emit({"type": "diagnostic", "stage": "first_decoded_frame"})
        decode_times.append(now)
        if channel != 0:return
        last = now
        sampler.offer(frame, recv_unix_ms/1000, decoded_unix_ms)

    async def decoded_audio(did, pcm, timestamp, channel, recv_unix_ms, decoded_unix_ms):
        nonlocal audio_observed
        if not audio_observed:
            emit({"type": "diagnostic", "stage": "first_decoded_audio"})
            audio_observed = True
        if channel != 0 or time.monotonic() > audio_until:
            audio_pending.clear()
            return
        audio_pending.extend(pcm.astype("<i2", copy=False).tobytes())
        while len(audio_pending) >= 6400:
            chunk = bytes(audio_pending[:6400])
            del audio_pending[:6400]
            emit(
                {
                    "type": "audio",
                    "pcm": base64.b64encode(chunk).decode(),
                    "sample_rate": 16000,
                    "received_at": recv_unix_ms / 1000,
                }
            )

    owner_camera = []
    try:
        # Consume the SDK's full-rate decoded frames; suppress its unused PIL/JPEG
        # preview conversion after the first frame. This leaves installed SDK files intact.
        install_decoder(camera_sdk, on_packet=emit,relay_enabled=lambda:time.monotonic()<video_until)
        await owner.init_async(frame_interval=3600000, enable_hw_accel=True)
        emit({"type": "diagnostic", "stage": "native_initialized"})
        camera = await owner.create_camera_async(
            camera_info=info, frame_interval=3600000, enable_hw_accel=True
        )
        owner_camera.append(camera)
        emit({"type": "diagnostic", "stage": "camera_created"})

        async def status_changed(did, status):
            emit(
                {"type": "diagnostic", "stage": "native_status_" + str(getattr(status, "name", status))[:40]}
            )

        await camera.register_status_changed_async(status_changed)
        await camera.register_decode_video_frame_async(decoded, channel=0)
        await camera.register_raw_video_async(raw_packet, channel=0)
        await camera.register_decode_audio_frame_async(decoded_audio, channel=0)
        await camera.start_async(
            qualities=[SimpleNamespace(value=quality)],
            enable_audio=request.get("audio") is True,
            enable_reconnect=True,
            enable_record=False,
        )
        emit({"type": "diagnostic", "stage": "camera_start_returned"})
        motor = LocalMotor(camera)

        async def controls():
            nonlocal audio_until, attention_until, video_until
            while raw := await reader.readline():
                command = {}
                try:
                    command = json.loads(raw)
                    if command.get('action')=='video_window':
                        video_until=time.monotonic()+1.5 if command.get('enabled') is True else 0.0
                        continue
                    if command.get("action") == "voice_window":
                        audio_until = time.monotonic() + 1.3 if command.get("enabled") is True else 0.0
                        attention_until = time.monotonic() + 1.3 if command.get("attention") is True else 0.0
                        if not command.get("enabled"):
                            audio_pending.clear()
                        continue
                    if command.get("action") != "motor_step":
                        continue
                    if request.get("motor") is not True:
                        raise ValueError("motor_not_owned")
                    result = await asyncio.to_thread(motor.step, command.get("direction"))
                    emit(dict(result, type="motor_result", request_id=command.get("request_id")))
                except Exception as e:
                    emit(
                        {
                            "type": "motor_result",
                            "request_id": command.get("request_id") if isinstance(command, dict) else None,
                            "error_class": type(e).__name__,
                        }
                    )

        control_task = asyncio.create_task(controls())
        while time.monotonic() - started < 86400 and os.getppid() == parent_pid:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if sampler_task.done():
                await sampler_task
            # Freshness pauses consumers immediately. Keep the SDK session long
            # enough for its local reconnect; don't discard it after only 8s.
            if (not last and now - started > 75) or (last and now - last > 75):
                emit({"type": "error", "reason": "source_unavailable"})
                break
    except Exception as error:
        emit({"type": "error", "reason": "source_unavailable", "error_class": type(error).__name__})
    finally:
        sampler_task.cancel()
        await asyncio.gather(sampler_task, return_exceptions=True)
        if "control_task" in locals():
            control_task.cancel()
            await asyncio.gather(control_task, return_exceptions=True)
        pipe.close()
        try:
            await asyncio.wait_for(owner.deinit_async(), 4)
        except BaseException:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException as error:
        emit(
            {"type": "error", "reason": "capture_initialization_failed", "error_class": type(error).__name__}
        )
    os._exit(0)
