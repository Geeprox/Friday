"""Production sampler bounds work and retains the camera's arrival evidence."""
import asyncio
import base64
import threading
import time

import cv2
import numpy as np

from homeplane.embodied.frame_sampling import LatestFrameEncoder, encode_frame
from homeplane.embodied.vision_cadence import MotionSample, VisionCadence


def test_burst_replaces_pending_frame_without_blocking_control_loop():
    async def run():
        entered, release = threading.Event(), threading.Event()
        encoded, output = [], []
        def encode(packet, **kwargs):
            encoded.append(packet[0])
            if len(encoded) == 1:
                entered.set()
                release.wait(1)
            return {"received_at": packet[1]}
        sampler = LatestFrameEncoder(output.append, encode=encode)
        task = asyncio.create_task(sampler.run())
        sampler.offer(0, time.time(), time.time()*1000)
        try:
            for _ in range(100):
                if entered.is_set():break
                await asyncio.sleep(.002)
            assert entered.is_set()
            for n in range(1, 101):sampler.offer(n, time.time(), time.time()*1000)
            assert sampler.latest[0] == 100 and sampler.replaced == 99
            release.set()
            await asyncio.sleep(.25)
            assert encoded == [0, 100]
            assert len(output) == 2
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert sampler.latest is None
    asyncio.run(run())


def test_stale_and_future_inputs_never_encode():
    async def run():
        encoded = []
        sampler = LatestFrameEncoder(lambda _:None, encode=lambda packet, **_:encoded.append(packet))
        task = asyncio.create_task(sampler.run())
        try:
            for stamp in (time.time()-2, time.time()+2):
                sampler.offer(None, stamp, stamp*1000)
                await asyncio.sleep(.02)
            assert not encoded
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_cancellation_discards_inflight_encoding_result():
    async def run():
        entered, release = threading.Event(), threading.Event()
        output = []
        def encode(*args, **kwargs):
            entered.set()
            release.wait(1)
            return {"jpeg":"discarded"}
        sampler = LatestFrameEncoder(output.append, encode=encode)
        task = asyncio.create_task(sampler.run())
        sampler.offer(None, time.time(), time.time()*1000)
        try:
            for _ in range(100):
                if entered.is_set():break
                await asyncio.sleep(.002)
            assert entered.is_set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:release.set()
        await asyncio.sleep(.02)
        assert output == [] and sampler.latest is None
    asyncio.run(run())


def test_encoder_preserves_native_face_size_and_original_arrival():
    frame = np.full((2160,3840,3), 96, dtype=np.uint8)
    received = time.time()-.1
    result = encode_frame((frame, received, received*1000+8), face=True, quality=3)
    def shape(key):return cv2.imdecode(np.frombuffer(base64.b64decode(result[key]),np.uint8),1).shape[:2]
    assert shape('jpeg') == (1080,1920)
    assert shape('face_jpeg') == (2160,3840)
    assert shape('preview') == (270,480)
    assert result['received_at'] == result['preview_at'] == received
    assert result['host_decode_ms'] == 8
    assert 'face_jpeg' not in encode_frame((frame, received, received*1000),face=False,quality=3)


def test_empty_view_skips_inference_but_detects_local_motion_and_refreshes_regularly():
    frame = np.full((480, 960, 3), 60, dtype=np.uint8)
    moved = frame.copy()
    moved[251:286, 142:177] = 105
    motion, cadence = MotionSample(), VisionCadence()
    processed = []
    for index in range(101):
        now = index / 5
        pixels = moved if index >= 70 else frame
        quiet = motion.quiet_ms(pixels, now)
        if cadence.due({'scene_quiet_ms': quiet}, now, force=False):
            processed.append(index)
            cadence.observed(now, clear=True)
    during_quiet = [n for n in processed if 25 <= n < 70]
    assert 8 <= len(during_quiet) <= 16
    assert all(b - a <= 4 for a, b in zip(during_quiet, during_quiet[1:]))
    assert all(n in processed for n in range(70, 91))
    # 断流后的首帧不能沿用旧的静止窗口。
    assert motion.quiet_ms(moved, 22) == 0


def test_activity_safety_or_lost_evidence_gets_full_inference():
    cadence = VisionCadence()
    still = {'scene_quiet_ms': 10000}
    cadence.observed(10, clear=True)
    assert not cadence.due(still, 10.2, force=False)
    assert cadence.due(still, 10.2, force=True)
    assert cadence.due({}, 10.2, force=False)
    cadence.observed(10.3, clear=False)
    for now in (10.4, 11, 12, 13, 14.2):
        assert cadence.due(still, now, force=False)
        cadence.observed(now, clear=True)
    assert not cadence.due(still, 14.4, force=False)
    cadence.reset()
    assert cadence.due(still, 14.4, force=False)
