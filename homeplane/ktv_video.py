"""Optional, bounded MV acquisition and measured musical alignment. No playback or capture."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .ktv_download import canonical_song_id

MAX_VIDEO = 128 * 1024 * 1024
SAMPLE_RATE = 8000


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def metadata(path, params):
    if path not in {"/api/v3/song/detail", "/api/v1/mv/detail", "/api/song/enhance/play/mv/url"}:
        raise ValueError("invalid_metadata_endpoint")
    data = bytearray()
    with httpx.stream("GET", "https://music.163.com" + path, params=params,
                      headers={"Referer": "https://music.163.com/"}, timeout=8,
                      follow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("mv_unavailable")
        for block in response.iter_bytes():
            if len(data) + len(block) > 1048576:
                raise ValueError("metadata_budget_exceeded")
            data.extend(block)
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("code") != 200:
        raise ValueError("mv_unavailable")
    return value


def cdn_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("invalid_mv_url")
    url = urlsplit(value)
    host = url.hostname or ""
    if (url.scheme not in {"http", "https"} or url.username or url.password or url.fragment
            or url.port not in {None, 80, 443}
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or not any(host.endswith(suffix) for suffix in (".vod.126.net", ".music.126.net"))):
        raise ValueError("invalid_mv_url")
    # The provider sometimes advertises HTTP. Fetch only the same official CDN over HTTPS.
    return urlunsplit(("https", host, url.path, url.query, ""))


def download(url, size, output, event):
    if type(size) is not int or not 0 < size <= MAX_VIDEO:
        raise ValueError("video_budget_exceeded")
    deadline, total = time.monotonic() + 50, 0
    try:
        with httpx.stream("GET", cdn_url(url), timeout=8, follow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError("mv_unavailable")
            if response.headers.get("content-length") and int(response.headers["content-length"]) != size:
                raise ValueError("video_size_changed")
            with output.open("xb") as stream:
                output.chmod(0o600)
                for block in response.iter_bytes(65536):
                    if event.is_set() or time.monotonic() > deadline or total + len(block) > size:
                        raise ValueError("video_download_cancelled")
                    stream.write(block)
                    total += len(block)
        if total != size:
            raise ValueError("video_size_changed")
    except Exception:
        output.unlink(missing_ok=True)
        raise


def command(args, event, *, timeout=45, output=None):
    from .ktv_service import run_bounded
    if output is None:
        run_bounded(args, event, timeout=timeout)
        return
    # ffprobe output is bounded by the fixed fields and local validated input.
    value = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout, check=True)
    if event.is_set() or len(value.stdout) > 65536:
        raise ValueError("video_probe_cancelled")
    return json.loads(value.stdout)


def normalize_video(source, output, event, *, ffmpeg, ffprobe, duration):
    """Bounded TV rendition with short GOP and a synthetic silent audio clock."""
    original = command([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=duration:format=duration", "-of", "json", str(source)],
                       event, output=True, timeout=10)
    # An AV source can have a longer audio tail than its picture track.
    picture_duration = float(original.get("streams", [{}])[0].get("duration", original["format"]["duration"]))
    bitrate = max(128000, min(2400000, int(MAX_VIDEO * 8 * .85 / duration)))
    command([ffmpeg, "-nostdin", "-v", "error", "-n", "-i", str(source),
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0",
             "-t", str(picture_duration), "-vf", "setpts=PTS-STARTPTS,scale=w='min(1280,iw)':h=-2", "-r", "30", "-fps_mode", "cfr",
             "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "21",
             "-profile:v", "main", "-pix_fmt", "yuv420p", "-bf", "0", "-g", "30",
             "-keyint_min", "30", "-sc_threshold", "0", "-maxrate", str(bitrate),
             "-bufsize", str(bitrate * 2), "-c:a", "aac", "-b:a", "32k", "-shortest", "-map_metadata", "-1",
             "-movflags", "+faststart", str(output)], event, timeout=240)
    info = command([ffprobe, "-v", "error", "-show_entries",
                    "stream=codec_type,codec_name,has_b_frames:format=duration", "-of", "json", str(output)],
                   event, output=True, timeout=10)
    streams = info.get("streams", [])
    pictures = [item for item in streams if item.get("codec_type") == "video"]
    clocks = [item for item in streams if item.get("codec_type") == "audio"]
    if (len(streams) != 2 or len(pictures) != 1 or len(clocks) != 1
            or pictures[0].get("codec_name") != "h264" or pictures[0].get("has_b_frames") != 0
            or clocks[0].get("codec_name") != "aac"
            or abs(float(info.get("format", {}).get("duration", 0)) - picture_duration) > .15
            or not 0 < output.stat().st_size <= MAX_VIDEO):
        output.unlink(missing_ok=True)
        raise ValueError("unsupported_mv")
    return {"duration_seconds": float(info["format"]["duration"]), "encoding": "h264_mp4_silent_clock"}


def align_samples(song, video, *, sample_rate=SAMPLE_RATE):
    """Require matching waveform windows across the song, not merely matching durations."""
    import numpy as np
    from scipy import signal
    window = 12 * sample_rate
    end = min(len(song), len(video)) / sample_rate - 25
    if end <= 20:
        raise ValueError("insufficient_alignment")
    windows = []
    for start in np.linspace(15, end, 7):
        at = int(start * sample_rate)
        x = song[at:at + window].astype(np.float64)
        lo, hi = max(0, at - 12 * sample_rate), min(len(video), at + window + 12 * sample_rate)
        y = video[lo:hi].astype(np.float64)
        if len(x) != window or len(y) < window:
            continue
        x -= x.mean()
        y -= y.mean()
        if np.sqrt(np.mean(x * x)) < .001:
            continue
        cross = signal.correlate(y, x, mode="valid", method="fft")
        cumulative = np.concatenate(([0.0], np.cumsum(y * y)))
        normal = np.sqrt(np.maximum((cumulative[window:] - cumulative[:-window]) * np.sum(x * x), 1e-20))
        scores = cross / normal
        peak = int(np.argmax(scores))
        windows.append((float(scores[peak]), (lo + peak - at) * 1000 / sample_rate))
    if len(windows) < 5:
        raise ValueError("insufficient_alignment")
    offsets = [item[1] for item in windows]
    if min(item[0] for item in windows) < .94 or max(offsets) - min(offsets) > 25:
        raise ValueError("different_mv_arrangement")
    return {"video_offset_ms": float(np.median(offsets)), "method": "multi_window_waveform_correlation",
            "score_min": min(item[0] for item in windows), "window_count": len(windows),
            "offset_drift_ms": max(offsets) - min(offsets)}


def prepare_video(song_id: str, original: Path, folder: Path, event):
    """Only an aligned, video-only H.264 MP4 is published; optional failures are silent fallbacks."""
    import numpy as np
    song_id = canonical_song_id(song_id)
    if (folder / "mv-source.json").is_file():
        return prepare_same_mv(song_id, original, folder, event)
    if folder.resolve() != folder or original.resolve() != original or not original.is_relative_to(folder):
        raise ValueError("unsafe_video_workspace")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("video_runtime_unavailable")
    source, output = folder / ".mv-source.mp4", folder / "video.mp4"
    samples = [folder / ".song.f32", folder / ".mv.f32"]
    try:
        songs = metadata("/api/v3/song/detail", {"c": json.dumps([{"id": int(song_id)}])}).get("songs", [])
        if len(songs) != 1 or str(songs[0].get("id")) != song_id:
            raise ValueError("song_identity_changed")
        mv_id = songs[0].get("mv")
        if type(mv_id) is not int or mv_id <= 0:
            raise ValueError("no_mv")
        detail = metadata("/api/v1/mv/detail", {"id": mv_id}).get("data", {})
        if detail.get("id") != mv_id:
            raise ValueError("mv_identity_changed")
        options = [int(v["br"]) for v in detail.get("brs", []) if isinstance(v, dict)
                   and type(v.get("br")) is int and 240 <= v["br"] <= 720
                   and isinstance(v.get("size"), (int, float)) and 0 < v["size"] <= MAX_VIDEO]
        if not options:
            raise ValueError("mv_unavailable")
        media = metadata("/api/song/enhance/play/mv/url", {"id": mv_id, "r": max(options)}).get("data", {})
        if media.get("id") != mv_id or media.get("r") not in options:
            raise ValueError("mv_identity_changed")
        download(media.get("url"), media.get("size"), source, event)
        info = command([ffprobe, "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height:format=duration", "-of", "json", str(source)],
                       event, output=True, timeout=10)
        streams = info.get("streams", [])
        pictures = [s for s in streams if s.get("codec_type") == "video"]
        duration = float(info.get("format", {}).get("duration", 0))
        if (len(pictures) != 1 or pictures[0].get("codec_name") != "h264"
                or not 0 < pictures[0].get("width", 0) <= 1920 or not 0 < pictures[0].get("height", 0) <= 1080
                or not 0 < duration <= 1800 or len([s for s in streams if s.get("codec_type") == "audio"]) != 1):
            raise ValueError("unsupported_mv")
        command([ffmpeg, "-nostdin", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
                 "-f", "null", "-"], event, timeout=60)
        for input_path, pcm in zip((original, source), samples):
            command([ffmpeg, "-nostdin", "-v", "error", "-n", "-i", str(input_path), "-map", "0:a:0",
                     "-t", "1800", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", str(pcm)], event)
            if pcm.stat().st_size > 1800 * SAMPLE_RATE * 4:
                raise ValueError("alignment_budget_exceeded")
        alignment = align_samples(*(np.fromfile(p, dtype="<f4") for p in samples))
        delivery = normalize_video(source, output, event, ffmpeg=ffmpeg, ffprobe=ffprobe, duration=duration)
        if not 0 < output.stat().st_size <= MAX_VIDEO:
            raise ValueError("video_budget_exceeded")
        output.chmod(0o600)
        return {"status": "ready", "encoding": delivery["encoding"], "filename": "video.mp4",
                "bytes": output.stat().st_size, "sha256": digest(output), "duration_seconds": delivery["duration_seconds"],
                "source_audio_sha256": digest(original), "mv_id": str(mv_id), "delivery_revision": 3, **alignment}
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        source.unlink(missing_ok=True)
        for path in samples:
            path.unlink(missing_ok=True)


def extract_mv_audio(source, output, event, *, ffmpeg, ffprobe):
    """Preserve the picture timeline, including silent introductions and edited sections."""
    import math
    info = command([ffprobe, '-v', 'error', '-show_entries',
        'stream=codec_type,codec_name,width,height,duration,start_time:format=duration', '-of', 'json', str(source)],
        event, output=True, timeout=10)
    pictures = [s for s in info.get('streams', []) if s.get('codec_type') == 'video']
    tracks = [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']
    if len(pictures) != 1 or len(tracks) != 1 or pictures[0].get('codec_name') != 'h264':
        raise ValueError('unsupported_mv')
    duration = float(pictures[0].get('duration', info.get('format', {}).get('duration', 0)))
    offset = float(tracks[0].get('start_time', 0)) - float(pictures[0].get('start_time', 0))
    if not math.isfinite(duration) or not 1 < duration <= 1800 or not math.isfinite(offset) or abs(offset)>60:
        raise ValueError('unsupported_mv')
    filters = ['asetpts=PTS-STARTPTS']
    if offset < 0:
        filters += ['atrim=start='+str(-offset), 'asetpts=PTS-STARTPTS']
    elif offset > 0:
        filters += ['adelay='+str(round(offset*1000))+':all=1']
    filters += ['apad', 'atrim=duration='+str(duration)]
    command([ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode', '-n', '-i', str(source),
        '-map', '0:a:0', '-af', ','.join(filters), '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le',
        '-map_metadata', '-1', str(output)], event, timeout=60)
    return duration


def acquire_mv_source(song_id, folder, event, *, mv_id=None):
    """Try an exact full MV first; no song-download quota or paid MV operation."""
    from .ktv_download import _write
    song_id = canonical_song_id(song_id)
    source, audio = folder/'.mv-source.mp4', folder/'.mv-audio.wav'
    receipt = folder/'mv-source.json'
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise ValueError('video_runtime_unavailable')
    try:
        if mv_id is None:
            songs = metadata('/api/v3/song/detail', {'c':json.dumps([{'id':int(song_id)}])}).get('songs', [])
            if len(songs)!=1 or str(songs[0].get('id'))!=song_id:
                raise ValueError('song_identity_changed')
            mv_id = songs[0].get('mv')
        else:
            mv_id = int(canonical_song_id(mv_id))
        if type(mv_id) is not int or mv_id<=0:
            raise ValueError('no_mv')
        detail = metadata('/api/v1/mv/detail', {'id':mv_id}).get('data', {})
        if detail.get('id')!=mv_id:
            raise ValueError('mv_identity_changed')
        options = [v['br'] for v in detail.get('brs', []) if isinstance(v,dict) and type(v.get('br')) is int
                   and 240<=v['br']<=720 and type(v.get('size')) in {int,float} and 0<v['size']<=MAX_VIDEO]
        if not options:
            raise ValueError('mv_unavailable')
        media = metadata('/api/song/enhance/play/mv/url', {'id':mv_id,'r':max(options)}).get('data', {})
        if media.get('id')!=mv_id or media.get('r') not in options or media.get('freeTrialInfo'):
            raise ValueError('mv_identity_changed')
        download(media.get('url'), media.get('size'), source, event)
        duration = extract_mv_audio(source,audio,event,ffmpeg=ffmpeg,ffprobe=ffprobe)
        expected = detail.get('duration')
        if type(expected) not in {int,float} or expected<=0 or abs(duration-expected/1000)>max(1.5,duration*.01):
            raise ValueError('incomplete_mv')
        audio.chmod(0o600)
        _write(receipt, {'song_id':song_id,'mv_id':str(mv_id),'duration_seconds':duration,
            'mv_sha256':digest(source),'source_sha256':digest(audio),'source_bytes':audio.stat().st_size,
            'full_decode_verified':True,'audio_origin':'mv','identity_basis':'exact_mv_id_full_decode'})
        return audio
    except Exception:
        for path in (source,audio,receipt):path.unlink(missing_ok=True)
        raise


def prepare_same_mv(song_id, original, folder, event):
    """Bind the video to the exact MV audio used by the separator, with zero timeline offset."""
    from .ktv_service import transcode_pcm
    record = json.loads((folder/'mv-source.json').read_text())
    source, output = folder/'.mv-source.mp4', folder/'video.mp4'
    if record.get('song_id')!=song_id or record.get('mv_sha256')!=digest(source):
        raise ValueError('mv_identity_changed')
    # Reproduce the final PCM contract and compare, rather than claiming a waveform match.
    proof = folder/'.mv-proof.wav'
    try:
        transcode_pcm(folder/'.mv-audio.wav',proof,event,ffmpeg=shutil.which('ffmpeg'))
        if digest(proof)!=digest(original):
            raise ValueError('source_changed')
        delivery=normalize_video(source,output,event,ffmpeg=shutil.which('ffmpeg'),ffprobe=shutil.which('ffprobe'),duration=record['duration_seconds'])
        return {'status':'ready','encoding':delivery['encoding'],'filename':'video.mp4','bytes':output.stat().st_size,
            'sha256':digest(output),'duration_seconds':delivery['duration_seconds'],'source_audio_sha256':digest(original),
            'video_offset_ms':0,'method':'same_mv_audio','derived_from_video_sha256':record['mv_sha256'],
            'mv_id':record['mv_id'],'delivery_revision':3}
    finally:
        proof.unlink(missing_ok=True)
