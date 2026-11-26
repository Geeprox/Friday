"""按原始到达时间编码并选择镜头样本。"""
import asyncio
import base64
import time

import cv2

CAPTURE_IDLE_TIMEOUT = 75
# 父进程留出采集进程判定超时并通知读端的余量。
CAPTURE_RECEIVE_TIMEOUT = CAPTURE_IDLE_TIMEOUT + 10


class PendingFrames:
    """单个消费者可取最新整帧，或尚在半秒优先窗口内的完整人脸帧。"""

    def __init__(self, *, clock=time.time):
        self._clock = clock
        self._available = asyncio.Event()
        self._latest = None
        self._face = None

    def offer(self, packet):
        if self._latest is not None and self._latest.get('type') == 'error':
            return
        self._latest = packet
        if packet.get('type') != 'frame':
            self._face = None
        elif packet.get('face_jpeg'):
            self._face = packet
        elif self._face is not None and any(
            packet.get(key) != self._face.get(key)
            for key in ('capture_session', 'camera_source_context', '_motion_generation')
        ):
            self._face = None
        self._available.set()

    def empty(self):
        return self._latest is None

    async def wait(self):
        await self._available.wait()

    async def get(self):
        await self.wait()
        return self.get_nowait()

    def get_nowait(self):
        if self.empty():
            raise asyncio.QueueEmpty
        latest, face = self._latest, self._face
        self._face = None
        if face is not None and face is not latest:
            now = self._clock()
            if 0 <= now - face.get('received_at', -100) <= .5:
                # 取走人脸帧时保留更新的帧，供下一次识别选取。
                return face
        self._latest = None
        self._available.clear()
        return latest


def encode_frame(packet, *, face, quality):
    frame, received_at, decoded_at = packet
    began = time.time()
    height, width = frame.shape[:2]
    native_size = [width, height]
    result = {}
    if face:
        ok, data = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if ok:result['face_jpeg'] = base64.b64encode(data).decode()
    if width > 1920 or height > 1080:
        scale = min(1920/width, 1080/height)
        frame = cv2.resize(frame, (round(width*scale), round(height*scale)))
    ok, data = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
    if not ok:return None
    height, width = frame.shape[:2]
    ok, preview = cv2.imencode('.jpg', cv2.resize(frame, (480,270)), [cv2.IMWRITE_JPEG_QUALITY,65])
    if ok:result.update(preview=base64.b64encode(preview).decode(),preview_at=received_at)
    result.update(type='frame',jpeg=base64.b64encode(data).decode(),stream_quality=quality,
                  width=width,height=height,native_size=native_size,received_at=received_at,
                  capture_encode_ms=round((time.time()-began)*1000,1),
                  host_decode_ms=round(decoded_at-received_at*1000))
    return result


class LatestFrameEncoder:
    def __init__(self, emit, *, quality=3, face_enabled=lambda:True, encode=encode_frame):
        self.emit, self.quality, self.face_enabled, self.encode = emit, quality, face_enabled, encode
        self.latest = None
        self.available = asyncio.Event()
        self.last_encoded = self.last_face = -100.0
        self.offered = self.replaced = self.sent = 0

    def offer(self, frame, received_at, decoded_at):
        self.offered += 1
        if self.latest is not None:self.replaced += 1
        # The SDK owns a distinct BGR ndarray for each decoded frame. Retaining
        # this reference avoids a full 4K copy; downstream never mutates it.
        self.latest = (frame, received_at, decoded_at)
        self.available.set()

    async def run(self):
        try:
            while True:
                await self.available.wait()
                await asyncio.sleep(max(0, .2-(time.monotonic()-self.last_encoded)))
                packet, self.latest = self.latest, None
                self.available.clear()
                if packet is None or not 0 <= time.time()-packet[1] <= .5:continue
                now = time.monotonic()
                face = self.face_enabled() and now-self.last_face >= .5
                self.last_encoded = now
                if face:self.last_face = now
                result = await asyncio.to_thread(self.encode, packet, face=face, quality=self.quality)
                if result is not None and 0 <= time.time()-packet[1] <= 1:
                    self.sent += 1
                    result['sampling'] = dict(offered=self.offered,replaced=self.replaced,sent=self.sent)
                    self.emit(result)
        finally:
            self.latest = None
