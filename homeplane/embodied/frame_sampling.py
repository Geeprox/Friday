"""One latest decoded frame, bounded encoding cadence, original arrival times."""
import asyncio
import base64
import time

import cv2


def offer_frame(queue, packet, *, now=None):
    """Keep one whole, recent face packet until consumed; never mix frames.

    A latest-only body queue can repeatedly overwrite the lower-cadence native
    face frame. Give that whole packet at most half a second of priority. Errors
    always revoke it, and newer face packets still replace older ones.
    """
    if queue.full():
        pending = queue.get_nowait()
        at = time.time() if now is None else now
        if (pending.get('type') == 'error'
                or (pending.get('type') == packet.get('type') == 'frame'
                and pending.get('face_jpeg') and not packet.get('face_jpeg')
                and 0 <= at - pending.get('received_at', -100) <= .5)):
            packet = pending
    queue.put_nowait(packet)


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
