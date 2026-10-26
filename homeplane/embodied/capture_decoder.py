"""Capture-process-only decoder: keep the GOP, sample before pixel conversion.

SDK audio, queue, stop/join and native ownership are inherited unchanged. There
is no second camera connection or extra codec thread. No SDK file is modified.
"""
import collections
import sys
import time


def install_decoder(camera_module, *, clock=time.monotonic, wall=time.time, on_packet=None, relay_enabled=lambda:True):
    import av
    from av.codec.hwaccel import HWAccel
    from miot.types import MIoTCameraCodec

    base = camera_module.MIoTMediaDecoder
    if getattr(base, '_friday_sampled', False):
        return base

    class SampledDecoder(base):
        _friday_sampled = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._friday_last_pixels = -100.0
            self._friday_decoded = collections.deque(maxlen=80)
            self._friday_software = sys.platform != 'darwin' or not self._enable_hw_accel
            self._friday_errors = 0
            self._friday_pixel_errors = 0
            self._friday_sequence = 0
            self._friday_source_sequence = 0
            self._friday_pending_video = 0
            self._friday_receipts = collections.OrderedDict()

        def _friday_fallback(self):
            if not self._friday_software:
                self._friday_software = True
                self._video_decoder = None
                self._friday_receipts.clear()

        def _on_video_callback(self, packet):
            codec = {MIoTCameraCodec.VIDEO_H264:'h264', MIoTCameraCodec.VIDEO_H265:'hevc'}.get(packet.codec_id)
            if codec is None:
                return
            if packet.channel == 0:self._friday_source_sequence += 1
            if self._video_decoder is None:
                try:
                    hw = None if self._friday_software else HWAccel('videotoolbox', allow_software_fallback=True)
                    self._video_decoder = av.CodecContext.create(codec, 'r', hwaccel=hw)
                except Exception:
                    self._friday_software = True
                    self._video_decoder = av.CodecContext.create(codec, 'r')
                self._video_decoder.thread_count = 1
            try:
                # Never drop compressed frames: later frames may reference them.
                self._friday_sequence += 1
                encoded = av.Packet(packet.data)
                encoded.pts = self._friday_sequence
                self._friday_receipts[encoded.pts] = (packet.timestamp,packet.channel,packet.recv_unix_ms)
                while len(self._friday_receipts)>128:self._friday_receipts.popitem(last=False)
                frames = self._video_decoder.decode(encoded)
                self._friday_errors = 0
            except Exception:
                self._friday_errors += 1
                if self._friday_errors >= 3:self._friday_fallback()
                # No packet replay, no invented fresh timestamp; await recovery.
                return
            if on_packet is not None and relay_enabled() and packet.channel == 0:
                # Relay the compressed access unit. TV decoding never changes
                # the recognition sample cadence or original arrival timestamp.
                import base64
                if len(packet.data) <= 2*1024*1024 and self._friday_pending_video < 4:
                    value=dict(type='video',codec=codec,
                        source_sequence=self._friday_source_sequence,
                        data=base64.b64encode(packet.data).decode(),
                        width=self._video_decoder.width,height=self._video_decoder.height,
                        received_at=packet.recv_unix_ms/1000)
                    self._friday_pending_video += 1
                    def deliver(value=value):
                        try:on_packet(value)
                        finally:self._friday_pending_video -= 1
                    self._main_loop.call_soon_threadsafe(deliver)
            now = clock()
            self._friday_decoded.extend([now] * len(frames))
            # A decoder may delay or reorder frames. Bind each output to its
            # original input PTS, never the packet which happened to release it.
            eligible=[]
            for frame in frames:
                receipt=self._friday_receipts.pop(frame.pts,None)
                if receipt is not None and 0<=wall()*1000-receipt[2]<=500:
                    eligible.append((frame,receipt))
            if (not eligible or not self._video_frame_callback
                    or now-self._friday_last_pixels < .2-1e-6):
                return
            frame,receipt=max(eligible,key=lambda item:item[1][2])
            self._friday_last_pixels = now
            decoded_at = int(wall()*1000)
            # Convert only the latest decoded frame on this same decoder thread.
            # Already uint8; astype would make another unnecessary full-4K copy.
            try:
                bgr = frame.to_ndarray(format='bgr24', threads=1)
                self._friday_pixel_errors=0
            except Exception:
                self._friday_pixel_errors+=1
                if self._friday_pixel_errors>=3:self._friday_fallback()
                return
            callback = self._video_frame_callback(bgr, receipt[0], receipt[1], receipt[2], decoded_at)
            self._main_loop.call_soon_threadsafe(self._main_loop.create_task, callback)

        def friday_status(self):
            times = list(self._friday_decoded)
            fps = (len(times)-1)/(times[-1]-times[0]) if len(times)>1 and times[-1]>times[0] else 0
            return dict(hardware_decoding=bool(self._video_decoder and self._video_decoder.is_hwaccel),
                        decoded_fps=round(fps,1))

    camera_module.MIoTMediaDecoder = SampledDecoder
    return SampledDecoder
