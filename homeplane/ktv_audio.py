"""双轨 AAC 播放成品，固定采样时间线与显式首尾裁切。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

from integrations.ktv_public.audio_format import AAC_ENCODING, MAX_DURATION_SECONDS, verify_compact

from .ktv_files import safe_file
from .ktv_process import run_bounded

RATE = 48000
LOSSLESS_BITRATE = 256000
LOSSLESS_CODECS = {"flac", "alac", "wavpack", "ape", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"}
FLOAT_STEREO = "aformat=sample_fmts=flt:sample_rates=48000:channel_layouts=stereo"
TRACKS = ("original", "instrumental")
PILOT_TRUE_PEAK_DBTP = -1.0


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


def transcode_pcm(source: Path, output: Path, event: threading.Event, *, ffmpeg: str, gain_db: float = 0):
    # 用标准库 wave 重打包以移除额外 RIFF 块，保持电视端的 PCM 格式。
    if not math.isfinite(gain_db) or gain_db > 0:
        raise ValueError("invalid_audio")
    filters = []
    if gain_db:
        filters = ["-af", f"{FLOAT_STEREO},volume={gain_db:.2f}dB:precision=float"]
    intermediate = output.with_suffix(".partial.wav")
    try:
        run_bounded(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                *filters,
                "-t",
                str(MAX_DURATION_SECONDS + 1),
                "-map_metadata",
                "-1",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(intermediate),
            ],
            event,
            timeout=180,
        )
        safe_file(intermediate)
        with wave.open(str(intermediate), "rb") as reader, wave.open(str(output), "wb") as writer:
            writer.setparams(reader.getparams())
            while chunk := reader.readframes(8192):
                if event.is_set():
                    raise ValueError("cancelled")
                writer.writeframesraw(chunk)
        output.chmod(0o600)
    finally:
        intermediate.unlink(missing_ok=True)


def _true_peak(path, folder, event, *, ffmpeg, spec=None):
    filters = [FLOAT_STEREO]
    input_options = []
    if spec is not None:
        input_options = ["-ignore_editlist", "1"]
        delay = spec["encoder_delay"]
        filters.append(f"atrim=start_sample={delay}:end_sample={delay + spec['frames']}")
    filters.append("loudnorm=I=-14:TP=-1:LRA=11:print_format=json")
    # 只读取输入真峰值。过滤器输出进入 null，不参与交付音轨。
    with tempfile.TemporaryFile(dir=folder) as log:
        run_bounded([ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-v", "info", *input_options,
                     "-i", str(path), "-map", "0:a:0", "-t", str(MAX_DURATION_SECONDS + 1),
                     "-af", ",".join(filters), "-f", "null", "-"], event, timeout=180, stderr=log)
        end = log.seek(0, os.SEEK_END)
        log.seek(max(0, end - 8192))
        summary = re.search(rb'\{\s*"input_i"[^{}]+\}', log.read())
    try:
        peak = float(json.loads(summary.group())["input_tp"]) if summary else math.nan
    except (ValueError, KeyError, TypeError):
        raise ValueError("transcode_failed") from None
    if peak == -math.inf:
        return None
    if not math.isfinite(peak):
        raise ValueError("transcode_failed")
    return peak


def _encode_pair(inputs, normalized, partials, event, *, ffmpeg, bitrate, gain_db):
    for source, output in zip(inputs, normalized):
        transcode_pcm(source, output, event, ffmpeg=ffmpeg, gain_db=gain_db)
    with wave.open(str(normalized[0]), "rb") as audio:
        frames = audio.getnframes()
    with wave.open(str(normalized[1]), "rb") as audio:
        backing_frames = audio.getnframes()
    if not 0 < frames <= MAX_DURATION_SECONDS * RATE or abs(backing_frames - frames) > 4800:
        raise ValueError("invalid_audio")
    result = {}
    for kind, input_path, part in zip(TRACKS, normalized, partials):
        part.unlink(missing_ok=True)
        # 禁用容器隐式裁切。电视按契约统一去掉编码器的 1024 帧延迟。
        run_bounded([ffmpeg, "-nostdin", "-v", "error", "-n", "-i", str(input_path),
                     "-map", "0:a:0", "-af", f"apad,atrim=end_sample={frames}",
                     "-ar", "48000", "-ac", "2", "-c:a", "aac", "-profile:a", "aac_low",
                     "-b:a", str(bitrate), "-map_metadata", "-1", "-use_editlist", "0",
                     "-movflags", "+faststart", str(part)], event, timeout=180)
        with part.open("rb") as encoded:
            checksum = hashlib.file_digest(encoded, "sha256").hexdigest()
        spec = {"filename": kind + ".m4a", "bytes": part.stat().st_size, "sha256": checksum,
                "duration_seconds": frames / RATE, "sample_rate": RATE, "channels": 2,
                "encoding": AAC_ENCODING, "frames": frames, "encoder_delay": 1024,
                "encoder_padding": (-(frames + 1024)) % 1024, "bitrate": bitrate}
        verify_compact(part, spec)
        result[kind] = spec
    return result


def _pack_playback(source, instrumental, folder, event, *, ffmpeg, ffprobe, bitrate, protect_peaks):
    if event.is_set():
        raise ValueError("cancelled")
    bitrate = source_bitrate(source, ffprobe) if bitrate is None else bitrate
    if type(bitrate) is not int or not 8000 <= bitrate <= 1024000:
        raise ValueError("invalid_audio")
    inputs = (source, instrumental)
    normalized = [folder / f".playback-{kind}.wav" for kind in TRACKS]
    partials = [folder / f".{kind}.partial.m4a" for kind in TRACKS]
    gain_db = 0.0
    evidence = {}
    published = []
    complete = False
    try:
        if protect_peaks:
            input_peaks = {kind: _true_peak(path, folder, event, ffmpeg=ffmpeg) for kind, path in zip(TRACKS, inputs)}
            peak = max((p for p in input_peaks.values() if p is not None), default=-math.inf)
            gain_db = round(min(0, PILOT_TRUE_PEAK_DBTP - peak), 2)
            evidence = {"target_dbtp": PILOT_TRUE_PEAK_DBTP, "input_dbtp": input_peaks, "attempts": []}
        for _ in range(3 if protect_peaks else 1):
            result = _encode_pair(inputs, normalized, partials, event, ffmpeg=ffmpeg, bitrate=bitrate, gain_db=gain_db)
            if not protect_peaks:
                break
            decoded = {kind: _true_peak(part, folder, event, ffmpeg=ffmpeg, spec=result[kind])
                       for kind, part in zip(TRACKS, partials)}
            evidence["attempts"].append({"gain_db": gain_db, "decoded_dbtp": decoded})
            peak = max((p for p in decoded.values() if p is not None), default=-math.inf)
            if peak <= PILOT_TRUE_PEAK_DBTP:
                break
            # AAC 可能产生新的峰值。每次从未量化的输入重做双轨，预留少量编码波动余量。
            gain_db = round(gain_db + PILOT_TRUE_PEAK_DBTP - peak - 0.3, 2)
        else:
            raise ValueError("transcode_failed")
        if event.is_set():
            raise ValueError("cancelled")
        # 全部轨道验证后才发布文件，state.json 最后切换为 ready。
        for kind, part in zip(TRACKS, partials):
            output = folder / (kind + ".m4a")
            part.chmod(0o600)
            os.replace(part, output)
            published.append(output)
        complete = True
        return result, evidence
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in ([normalized[1]] if complete else normalized) + partials:
            path.unlink(missing_ok=True)


def pack_playback(source, instrumental, folder, event, *, ffmpeg, ffprobe, bitrate=None):
    result, _ = _pack_playback(source, instrumental, folder, event, ffmpeg=ffmpeg, ffprobe=ffprobe,
                               bitrate=bitrate, protect_peaks=False)
    return result


def pack_peak_safe_playback(source, instrumental, folder, event, *, ffmpeg, ffprobe, bitrate=None):
    """在新建目录准备双轨试听候选，另行返回峰值回读。"""
    folder.mkdir(mode=0o700)
    return _pack_playback(source, instrumental, folder, event, ffmpeg=ffmpeg, ffprobe=ffprobe,
                          bitrate=bitrate, protect_peaks=True)
