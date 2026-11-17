"""双轨 AAC 播放成品，固定采样时间线与显式首尾裁切。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import wave
from pathlib import Path

from integrations.ktv_public.audio_format import AAC_ENCODING, verify_compact

RATE = 48000
LOSSLESS_BITRATE = 256000
LOSSLESS_CODECS = {"flac", "alac", "wavpack", "ape", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"}


def audio_suffix(spec):
    return ".m4a" if spec.get("encoding") == AAC_ENCODING else ".wav"


def source_bitrate(source, ffprobe):
    value = json.loads(subprocess.check_output([
        ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=codec_name,bit_rate:format=duration,size,bit_rate", "-of", "json", str(source),
    ], timeout=30))
    tracks = value.get("streams", [])
    if len(tracks) != 1:
        raise ValueError("invalid_audio")
    track = tracks[0]
    codec = track.get("codec_name", "")
    if codec in LOSSLESS_CODECS or codec.startswith("pcm_"):
        return LOSSLESS_BITRATE
    source_rate = track.get("bit_rate") or value.get("format", {}).get("bit_rate")
    if source_rate is None:
        container = value.get("format", {})
        source_rate = float(container["size"]) * 8 / float(container["duration"])
    bitrate = round(float(source_rate) / 1000) * 1000
    if not math.isfinite(bitrate) or not 8000 <= bitrate <= 1024000:
        raise ValueError("invalid_audio")
    return bitrate


def pack_playback(source, instrumental, folder, event, *, ffmpeg, ffprobe, bitrate=None):
    from .ktv_service import MAX_DURATION_SECONDS, run_bounded, transcode_pcm

    bitrate = source_bitrate(source, ffprobe) if bitrate is None else bitrate
    if type(bitrate) is not int or not 8000 <= bitrate <= 1024000:
        raise ValueError("invalid_audio")
    normalized = [folder / ".playback-original.wav", folder / ".playback-instrumental.wav"]
    partials, published = [], []
    complete = False
    try:
        for original, output in zip((source, instrumental), normalized):
            transcode_pcm(original, output, event, ffmpeg=ffmpeg)
        with wave.open(str(normalized[0]), "rb") as audio:
            frames = audio.getnframes()
        with wave.open(str(normalized[1]), "rb") as audio:
            backing_frames = audio.getnframes()
        if not 0 < frames <= MAX_DURATION_SECONDS * RATE or abs(backing_frames - frames) > 4800:
            raise ValueError("invalid_audio")
        result = {}
        for kind, input_path in zip(("original", "instrumental"), normalized):
            part = folder / ("." + kind + ".partial.m4a")
            partials.append(part)
            part.unlink(missing_ok=True)
            # 禁用容器隐式裁切。电视按契约统一去掉编码器的 1024 帧延迟。
            run_bounded([ffmpeg, "-nostdin", "-v", "error", "-n", "-i", str(input_path),
                         "-map", "0:a:0", "-af", f"apad,atrim=end_sample={frames}",
                         "-ar", "48000", "-ac", "2", "-c:a", "aac", "-profile:a", "aac_low",
                         "-b:a", str(bitrate), "-map_metadata", "-1", "-use_editlist", "0",
                         "-movflags", "+faststart", str(part)], event, timeout=180)
            with part.open("rb") as encoded:
                checksum = hashlib.file_digest(encoded, "sha256").hexdigest()
            spec = {"filename": kind + ".m4a", "bytes": part.stat().st_size,
                    "sha256": checksum,
                    "duration_seconds": frames / RATE, "sample_rate": RATE, "channels": 2,
                    "encoding": AAC_ENCODING, "frames": frames, "encoder_delay": 1024,
                    "encoder_padding": (-(frames + 1024)) % 1024, "bitrate": bitrate}
            verify_compact(part, spec)
            result[kind] = spec
        if event.is_set():
            raise ValueError("cancelled")
        # 全部轨道验证后才发布文件，state.json 最后切换为 ready。
        for kind, part in zip(("original", "instrumental"), partials):
            output = folder / (kind + ".m4a")
            part.chmod(0o600)
            os.replace(part, output)
            published.append(output)
        complete = True
        return result
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in ([normalized[1]] if complete else normalized) + partials:
            path.unlink(missing_ok=True)
