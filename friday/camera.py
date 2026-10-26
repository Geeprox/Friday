"""Miloco camera source for unattended capture; frames/audio remain in bounded memory."""

import asyncio
import base64
import io
import json
import os
import struct
import time
import wave
from collections import deque
from uuid import uuid4

import websockets


class SpeechSegments:
    """Bounded PCM energy segmentation, not speaker identification."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.chunks = []
        self.voiced = 0
        self.silence = 0
        self.samples = 0
        self.overflow = False
        self.preroll = deque(maxlen=10)

    def feed(self, pcm):
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2")
        seconds = len(samples) / 16000
        loud = len(samples) and float(np.sqrt(np.mean((samples.astype(np.float32) / 32768) ** 2))) > 0.015
        self.preroll.append(pcm)
        if loud and not self.chunks:
            self.chunks = list(self.preroll)
            self.samples = sum(len(c) // 2 for c in self.chunks)
        elif self.chunks:
            self.chunks.append(pcm)
            self.samples += len(samples)
        if loud:
            self.voiced += seconds
            self.silence = 0
        else:
            self.silence += seconds
        if self.samples > 6 * 16000:
            self.overflow = True
            self.chunks = []
        if self.silence >= 0.65 and (self.chunks or self.overflow):
            data = b"".join(self.chunks) if self.voiced >= 0.25 and not self.overflow else None
            self.reset()
            if data:
                output = io.BytesIO()
                with wave.open(output, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(data)
                return output.getvalue()
        return None


class MilocoCapture:
    def __init__(self, settings, on_media, on_failure, journal):
        self.settings, self.on_media, self.on_failure, self.journal = settings, on_media, on_failure, journal
        self.task = None
        self.inference = None
        self.frames = deque(maxlen=8)
        self.preview = None
        self.last_video = self.last_audio = 0
        self.status = "stopped"
        self.error = None
        self.session = str(uuid4())
        self.level = 0

    async def start(self):
        if self.task and not self.task.done():
            raise ValueError("Camera is already running")
        if not self.settings.camera_did:
            raise ValueError("Configure a Miloco camera first")
        import av  # Fail before accepting the request if native decoder is missing.

        self.av = av
        self.frames.clear()
        self.preview = None
        self.error = None
        self.session = str(uuid4())
        self.status = "connecting"
        self.last_video = self.last_audio = time.monotonic()
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        tasks = [t for t in (self.task, self.inference) if t and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.task = self.inference = None
        self.frames.clear()
        self.preview = None
        self.status = "stopped"
        self.level = 0

    async def run(self):
        tasks = [asyncio.create_task(self.stream(kind)) for kind in ("video", "audio")]
        tasks.append(asyncio.create_task(self.watchdog()))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise ConnectionError("Camera connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status = "error"
            self.error = type(exc).__name__
            self.journal.record("camera", "capture_failed", error_type=self.error)
            self.on_failure()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.inference and not self.inference.done():
                self.inference.cancel()
            self.frames.clear()
            self.preview = None
            self.level = 0

    async def watchdog(self):
        while True:
            await asyncio.sleep(2)
            limit = 30 if self.status == "connecting" else 12
            if time.monotonic() - min(self.last_video, self.last_audio) > limit:
                raise TimeoutError("Camera audio/video stalled")

    async def stream(self, kind):
        from urllib.parse import urlencode

        url = self.settings.miloco_url.rstrip("/").replace("http:", "ws:", 1).replace("https:", "wss:", 1)
        url += (
            "/api/miot/ws/"
            + kind
            + "_stream?"
            + urlencode({"camera_id": self.settings.camera_did, "channel": 0})
        )
        token = os.environ.get(self.settings.miloco_token_env, "")
        async with websockets.connect(
            url,
            additional_headers={"Authorization": "Bearer " + token},
            proxy=None,
            open_timeout=10,
            max_size=2_000_000,
        ) as ws:
            decoder = self.av.CodecContext.create("h264", "r") if kind == "video" else None
            resampler = self.av.AudioResampler(format="s16", layout="mono", rate=16000)
            gate = SpeechSegments()
            last_frame = 0
            async for message in ws:
                if isinstance(message, str):
                    meta = json.loads(message)
                    if meta.get("type") == "error":
                        raise ConnectionError("Miloco stream error")
                    if kind == "audio" and meta.get("type") == "init":
                        codec = {"opus": "opus", "g711a": "pcm_alaw", "g711u": "pcm_mulaw"}.get(
                            meta.get("codec")
                        )
                        if not codec:
                            raise ValueError("Unsupported audio codec")
                        decoder = self.av.CodecContext.create(codec, "r")
                        decoder.sample_rate = int(meta.get("sampleRate", 48000))
                        channels = int(meta.get("numberOfChannels", 1))
                        if channels not in (1, 2):
                            raise ValueError("Unsupported audio channels")
                        decoder.layout = "mono" if channels == 1 else "stereo"
                        if codec == "opus":
                            decoder.extradata = b"OpusHead" + struct.pack(
                                "<BBHIhB", 1, channels, 0, 48000, 0, 0
                            )
                    continue
                if decoder is None:
                    raise ValueError("Missing codec initialization")
                if kind == "video":
                    if len(message) <= 16:
                        raise ValueError("Invalid video frame")
                    packets = decoder.parse(message[16:])
                    for packet in packets:
                        for frame in decoder.decode(packet):
                            self.last_video = time.monotonic()
                            if self.last_video - last_frame < 0.4:
                                continue
                            last_frame = self.last_video
                            image = frame.to_image()
                            image.thumbnail((960, 540))
                            output = io.BytesIO()
                            image.save(output, format="JPEG", quality=65)
                            self.preview = output.getvalue()
                            self.frames.append((time.time(), base64.b64encode(self.preview).decode()))
                else:
                    for frame in decoder.decode(self.av.Packet(message)):
                        self.last_audio = time.monotonic()
                        self.status = (
                            ("processing" if self.inference and not self.inference.done() else "listening")
                            if self.preview
                            else "connecting"
                        )
                        for pcm in resampler.resample(frame):
                            samples = pcm.to_ndarray()
                            self.level = min(
                                1.0, float((samples.astype("float32") ** 2).mean() ** 0.5) / 32768 * 8
                            )
                            if self.inference and not self.inference.done():
                                gate.reset()
                                continue
                            wav = gate.feed(samples.tobytes())
                            if wav:
                                fresh = [image for at, image in self.frames if time.time() - at < 3.2]
                                if len(fresh) >= 2 and time.time() - self.frames[-1][0] < 1:
                                    self.inference = asyncio.create_task(self.submit(wav, fresh[::2][-4:]))
                                else:
                                    gate.reset()

    async def submit(self, wav, frames):
        self.status = "processing"
        try:
            await self.on_media(str(uuid4()), self.session, time.time(), wav, frames)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.journal.record("perception", "failure", error_type=type(exc).__name__)
        finally:
            if self.status != "error":
                self.status = "listening"
