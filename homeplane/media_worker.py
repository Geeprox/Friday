"""Fixed local stream client and isolated standard decoder. Never loads a model.

Wire framing verified against installed Miloco2026.9.11. Audio has no source
timestamp; video clock provenance is not guaranteed. Receipt overlap is not AV sync.
"""
from __future__ import annotations

import asyncio
import json
import resource
import struct
import sys
import time
from urllib.parse import urlencode

from .media_contracts import MediaConnection, MediaProbeResult, MediaStreamResult

MAX_PACKET = 1024 * 1024
MAX_BYTES = 12 * 1024 * 1024


class MediaError(Exception):
    def __init__(self, code):
        self.code = code


class StreamDecoder:
    def __init__(self, kind):
        import av
        self.av, self.kind = av, kind
        self.result = MediaStreamResult()
        self.decoder = self.resampler = None
        self.meta = None
        self.last_timestamp = None
        self.buffered_bytes = 0

    def initialize(self, meta):
        codec = meta.get("codec")
        if self.kind == "video":
            if codec != "h264" or meta.get("container") != "annexb":
                raise MediaError("unsupported_codec")
            signature = (codec, "annexb")
        else:
            rate, channels = meta.get("sampleRate"), meta.get("numberOfChannels")
            if codec not in {"opus", "g711a", "g711u"} or type(rate) is not int or type(channels) is not int:
                raise MediaError("unsupported_codec")
            if channels not in (1, 2) or rate not in (8000, 16000, 48000) or (codec == "opus" and rate != 48000):
                raise MediaError("unsupported_codec")
            signature = (codec, rate, channels)
        if self.meta is not None:
            if signature != self.meta:
                raise MediaError("codec_changed")
            return
        self.meta = signature
        name = {"g711a": "pcm_alaw", "g711u": "pcm_mulaw"}.get(codec, codec)
        self.decoder = self.av.CodecContext.create(name, "r")
        self.decoder.thread_count = 1
        if self.kind == "video":
            self.decoder.options = {"max_pixels": str(3840 * 2160)}
            self.decoder.open()
        self.result.codec = codec
        if self.kind == "audio":
            self.decoder.sample_rate = rate
            self.decoder.layout = "mono" if channels == 1 else "stereo"
            if codec == "opus":
                # RFC7845 identification header; this supplies codec setup,
                # never an inferred source-clock anchor or sample timestamp.
                self.decoder.extradata = b"OpusHead" + struct.pack("<BBHIhB", 1, channels, 0, 48000, 0, 0)
            self.resampler = self.av.AudioResampler(format="s16", layout="mono", rate=16000)

    def frame(self, frame, elapsed_ms):
        if self.result.decoded_frames >= 4096:
            raise MediaError("input_budget")
        if self.kind == "video":
            if not (0 < frame.width <= 4096 and 0 < frame.height <= 4096 and frame.width * frame.height <= 3840 * 2160):
                raise MediaError("input_budget")
            if self.result.width is not None and (frame.width, frame.height) != (self.result.width, self.result.height):
                raise MediaError("codec_changed")
            self.result.width, self.result.height = frame.width, frame.height
        else:
            if frame.sample_rate not in (8000, 16000, 48000) or len(frame.layout.channels) not in (1, 2) or not 0 < frame.samples <= 5760:
                raise MediaError("decode_failed")
            self.result.sample_rate, self.result.channels = frame.sample_rate, len(frame.layout.channels)
            samples = sum(output.samples for output in self.resampler.resample(frame))
            if self.result.pcm_16k_samples + samples > 320000:
                raise MediaError("input_budget")
            self.result.pcm_16k_samples += samples
            self.result.pcm_format = "mono_s16_16000"
        self.result.decoded_frames += 1
        self.result.first_decode_ms = self.result.first_decode_ms if self.result.first_decode_ms is not None else elapsed_ms
        self.result.last_decode_ms = elapsed_ms

    def feed(self, message, elapsed_ms):
        if self.decoder is None:
            raise MediaError("missing_init")
        if not message or len(message) > MAX_PACKET or self.result.packets >= 4096 or self.result.encoded_bytes + len(message) > MAX_BYTES:
            raise MediaError("input_budget")
        self.result.packets += 1
        self.result.encoded_bytes += len(message)
        try:
            if self.kind == "video":
                if len(message) <= 16 or message[0] not in (0, 1) or message[1:8] != b"\0" * 7:
                    raise MediaError("malformed_packet")
                timestamp = struct.unpack(">Q", message[8:16])[0]
                self.result.video_timestamps_seen += 1
                if timestamp >= 9_000_000_000_000_000 or (self.last_timestamp is not None and timestamp < self.last_timestamp):
                    self.result.video_clock_discontinuities += 1
                self.last_timestamp = timestamp
                self.buffered_bytes += len(message) - 16
                packets = self.decoder.parse(message[16:])
                if packets:
                    self.buffered_bytes = 0
                if self.buffered_bytes > MAX_PACKET:
                    raise MediaError("input_budget")
            else:
                packets = [self.av.Packet(message)]
            for packet in packets:
                for frame in self.decoder.decode(packet):
                    self.frame(frame, elapsed_ms)
        except self.av.error.FFmpegError:
            raise MediaError("decode_failed") from None


async def receive(config, kind, started):
    import websockets
    decoder = StreamDecoder(kind)
    origin = config.origin.replace("http:", "ws:", 1).replace("https:", "wss:", 1)
    url = origin + "/api/miot/ws/" + kind + "_stream?" + urlencode({"camera_id": config.camera_id, "channel": 0})
    try:
        async with asyncio.timeout(config.seconds):
            async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + config.token.get_secret_value()},
                    proxy=None, open_timeout=3, close_timeout=.5, max_queue=2, max_size=MAX_PACKET,
                    compression=None) as stream:
                async for message in stream:
                    if isinstance(message, bytes):
                        decoder.feed(message, (time.monotonic() - started) * 1000)
                    else:
                        if len(message) > 2048:
                            raise MediaError("input_budget")
                        meta = json.loads(message)
                        if not isinstance(meta, dict):
                            raise MediaError("protocol_error")
                        if meta.get("type") == "error":
                            raise MediaError("stream_error")
                        if meta.get("type") != "init":
                            raise MediaError("protocol_error")
                        decoder.initialize(meta)
                raise MediaError("stream_closed")
    except TimeoutError:
        if not decoder.result.decoded_frames:
            decoder.result.failure = "no_decoded_frames"
    except MediaError as exc:
        decoder.result.failure = exc.code
    except (OSError, ValueError, websockets.exceptions.WebSocketException):
        decoder.result.failure = "stream_closed"
    decoder.result.status = "failed" if decoder.result.failure else "decoded"
    return decoder.result


async def probe(config):
    import httpx
    result = MediaProbeResult(status="failed")
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3) as client:
            async with client.stream("GET", config.origin + "/api/miot/status",
                    headers={"Authorization": "Bearer " + config.token.get_secret_value()}) as response:
                if response.status_code in (401, 403):
                    raise MediaError("service_login_required")
                if response.status_code != 200:
                    raise MediaError("service_unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 65536:
                        raise MediaError("input_budget")
                    body.extend(chunk)
                data = json.loads(body)
                if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), dict):
                    raise MediaError("protocol_error")
                if data["data"].get("is_bound") is not True:
                    raise MediaError("account_login_required")
        started = time.monotonic()
        result.video, result.audio = await asyncio.gather(receive(config, "video", started), receive(config, "audio", started))
        result.status = "decoded_sync_unknown" if result.video.status == result.audio.status == "decoded" else "failed"
        if result.status != "failed":
            result.reception_overlap_ms = max(0.0, min(result.video.last_decode_ms, result.audio.last_decode_ms) - max(result.video.first_decode_ms, result.audio.first_decode_ms))
    except MediaError as exc:
        result.failure = exc.code
    except (httpx.HTTPError, OSError, ValueError):
        result.failure = "service_unavailable"
    return result


def main():
    # No core dump containing raw frames/credentials, no worker file writes.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    try:
        data = sys.stdin.buffer.read(16385)
        if len(data) > 16384:
            raise ValueError("request_budget")
        config = MediaConnection.model_validate_json(data)
        result = asyncio.run(probe(config))
    except ImportError:
        result = MediaProbeResult(status="failed", failure="worker_unavailable")
    except Exception:
        # No raw exception, identifiers, token or encoded media on stdout.
        result = MediaProbeResult(status="failed", failure="worker_failed")
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result.worker_peak_rss_bytes = usage if sys.platform == "darwin" else usage * 1024
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
