"""Isolated local MP4 protection. Never emits source pixels or writes the source."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from fractions import Fraction

from .archive_privacy import FrameProtection, LocalDetector
from .archive_worker import open_beneath, root_handle, signature

MAX_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_SECONDS = 600
MAX_FRAMES = 36000


def current_source(root_fd, path, expected, original_fd):
    fd = open_beneath(root_fd, path)
    try:
        if signature(os.fstat(fd)) != expected or signature(os.fstat(original_fd)) != expected:
            raise ValueError("privacy_source_changed")
    finally:
        os.close(fd)


def preview_window(value):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"start_seconds", "duration_seconds"}
        or any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) for k in value)
        or not 0 <= value["start_seconds"] < MAX_SECONDS
        or not 0 < value["duration_seconds"] <= 30
        or value["start_seconds"] + value["duration_seconds"] > MAX_SECONDS
    ):
        raise ValueError("privacy_invalid_window")
    return value


def transcode(source_fd, target, detector, *, checkpoint=lambda: None, window=None):
    import av

    av.logging.set_level(av.logging.PANIC)
    protection = FrameProtection(detector)
    window = preview_window(window)
    start = window["start_seconds"] if window is not None else 0
    end = start + window["duration_seconds"] if window is not None else MAX_SECONDS
    with os.fdopen(os.dup(source_fd), "rb") as source:
        with av.open(
            source,
            mode="r",
            format="mov",
            options={
                "enable_drefs": "0",
                "use_absolute_path": "0",
                "protocol_whitelist": "file,pipe,fd",
                "probesize": "1048576",
                "analyzeduration": "1000000",
            },
        ) as incoming:
            videos = list(incoming.streams.video)
            if len(videos) != 1 or len(incoming.streams.audio) > 1 or len(incoming.streams) > 4:
                raise ValueError("privacy_invalid_media")
            video = videos[0]
            video.codec_context.thread_count = 2
            width, height = video.width, video.height
            rate = video.average_rate
            if not (
                0 < width <= 4096
                and 0 < height <= 4096
                and width * height <= 4096 * 2160
                and rate is not None
                and 0 < rate <= 60
            ):
                raise ValueError("privacy_media_budget")
            scale = min(1.0, 1280 / width, 720 / height)
            out_width, out_height = (
                max(2, round(width * scale) // 2 * 2),
                max(2, round(height * scale) // 2 * 2),
            )
            if start:
                # Decode from the preceding keyframe; no skipped source frame
                # is ever encoded or returned to the viewer.
                incoming.seek(round(start * av.time_base), backward=True, any_frame=False)
            with av.open(
                target,
                mode="w",
                format="mp4",
                options={"movflags": "+frag_keyframe+empty_moov+default_base_moof"},
            ) as outgoing:
                output_video = outgoing.add_stream("libx264", rate=rate)
                output_video.width, output_video.height = out_width, out_height
                output_video.pix_fmt = "yuv420p"
                output_video.options = {
                    "crf": "23",
                    "preset": "veryfast",
                    "threads": "2",
                    "bf": "0",
                    "g": str(max(1, round(float(rate)))),
                }
                output_video.time_base = Fraction(1, 90000)
                output_audio = {}
                for audio in incoming.streams.audio:
                    if audio.codec_context.name not in {"aac", "mp3", "alac", "opus", "ac3", "mp2"}:
                        raise ValueError("privacy_audio_unavailable")
                    output_audio[audio.index] = outgoing.add_stream_from_template(audio)
                frames, decoded, last_time, finished = 0, 0, -1.0, False
                started = checked = time.monotonic()
                for packet in incoming.demux():
                    if time.monotonic() - started > 170 or target.tell() > MAX_BYTES:
                        raise ValueError("privacy_media_budget")
                    if time.monotonic() - checked >= 1:
                        checkpoint()
                        checked = time.monotonic()
                    if packet.stream.index == video.index:
                        for frame in packet.decode():
                            decoded += 1
                            if frame.pts is None or frame.time_base is None:
                                raise ValueError("privacy_invalid_timeline")
                            timestamp = float(frame.pts * frame.time_base)
                            if (
                                not 0 <= timestamp <= MAX_SECONDS
                                or timestamp < last_time
                                or decoded > MAX_FRAMES
                            ):
                                raise ValueError("privacy_media_budget")
                            last_time = timestamp
                            if timestamp < start:
                                continue
                            if timestamp >= end:
                                finished = True
                                break
                            frames += 1
                            image = frame.reformat(
                                width=out_width, height=out_height, format="bgr24"
                            ).to_ndarray()
                            protected = protection.apply(image, timestamp)
                            # The encoder receives ONLY the newly allocated protected
                            # frame. The decoded source is never an encode fallback.
                            rendered = av.VideoFrame.from_ndarray(protected, format="bgr24")
                            rendered.pts, rendered.time_base = round((timestamp - start) * 90000), Fraction(1, 90000)
                            for encoded in output_video.encode(rendered):
                                outgoing.mux(encoded)
                    elif packet.stream.index in output_audio and packet.dts is not None:
                        if packet.pts is None or packet.time_base is None:
                            continue
                        audio_time = float(packet.pts * packet.time_base)
                        if not start <= audio_time < end or float(packet.dts * packet.time_base) < start:
                            continue
                        shift = round(Fraction(str(start)) / packet.time_base)
                        packet.pts -= shift
                        packet.dts -= shift
                        packet.stream = output_audio[packet.stream.index]
                        outgoing.mux(packet)
                    if finished:
                        break
                if not frames:
                    raise ValueError("privacy_invalid_media")
                for encoded in output_video.encode():
                    outgoing.mux(encoded)
            checkpoint()
            return {
                "frames_processed": frames,
                "width": out_width,
                "height": out_height,
                "uncertain_frames": protection.uncertain_frames,
            }


def render(root, cache, request, *, detector_factory=LocalDetector):
    if (
        not isinstance(request, dict)
        or set(request) not in ({"path", "signature", "output", "model"}, {"path", "signature", "output", "model", "window"})
        or not isinstance(request["path"], str)
        or len(request["path"].encode()) > 1024
        or not re.fullmatch(r"[a-f0-9]{64}", request["signature"])
        or not re.fullmatch(r"[a-f0-9]{32}\.mp4", request["output"])
        or (
            request["model"] is not None
            and (
                not isinstance(request["model"], str)
                or not os.path.isabs(request["model"])
                or len(request["model"].encode()) > 4096
            )
        )
    ):
        raise ValueError("privacy_invalid_job")
    window = preview_window(request.get("window"))
    source_root, cache_root = root_handle(root), root_handle(cache)
    source_fd = output_fd = None
    created = False
    try:
        info = os.fstat(cache_root)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("privacy_private_cache_required")
        source_fd = open_beneath(source_root, request["path"])
        before = os.fstat(source_fd)
        if signature(before) != request["signature"] or not 0 < before.st_size <= MAX_SOURCE_BYTES:
            raise ValueError("privacy_source_changed")
        detector, degraded = None, "detector_unconfigured"
        if request["model"] is not None:
            try:
                detector = detector_factory(request["model"])
                degraded = "none"
            except Exception:
                # An explicitly configured but unusable detector never releases
                # source pixels. No model downloader is called by this process.
                degraded = "detector_unavailable"
        output_fd = os.open(
            request["output"], os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=cache_root
        )
        created = True
        with os.fdopen(os.dup(output_fd), "w+b", buffering=0) as target:
            rendered = transcode(
                source_fd,
                target,
                detector,
                window=window,
                checkpoint=lambda: current_source(
                    source_root, request["path"], request["signature"], source_fd
                ),
            )
            target.flush()
            os.fsync(target.fileno())
        current_source(source_root, request["path"], request["signature"], source_fd)
        info = os.fstat(output_fd)
        if not 0 < info.st_size <= MAX_BYTES:
            raise ValueError("privacy_media_budget")
        if degraded == "none" and rendered["uncertain_frames"]:
            degraded = "analysis_uncertain"
        return {
            "status": "protected",
            "size_bytes": info.st_size,
            "signature": signature(info),
            "privacy": "local_person_mosaic" if detector is not None else "local_full_frame_mosaic",
            "privacy_degraded_reason": degraded,
            "original_modified": False,
            "original_copied": False,
        }
    except BaseException:
        if created:
            os.unlink(request["output"], dir_fd=cache_root)
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if output_fd is not None:
            os.close(output_fd)
        os.close(source_root)
        os.close(cache_root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--cache", required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise ValueError("privacy_invalid_job")
        result = render(args.root, args.cache, json.loads(raw))
    except BaseException:
        # Fixed outcome only; no private filename, exception, frame or detection.
        result = {"status": "unavailable", "code": "archive_privacy_unavailable"}
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
