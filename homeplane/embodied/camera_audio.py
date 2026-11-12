"""Private camera PCM framing and admission; no audio capture, models or storage.

The installed SDK produces flattened mono s16 at16kHz. Every exported200ms
packet retains its oldest input receipt time and a capture-bound sequence.
"""

import base64
from collections import deque
from dataclasses import dataclass
import json
import math
import re

from .camera_provenance import bind_capture_packet, camera_source_context

PCM_BYTES = 6400
SAMPLE_RATE = 16000
MAX_AGE = 1.5
MAX_GAP = .6
VOICE_WINDOW_SECONDS = 1.3


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def fresh(received, decoded, wall):
    return (all(number(x) and x >= 0 for x in (received, decoded, wall))
            and 0 <= wall-received <= MAX_AGE and received <= decoded <= wall)


def offer_voice_window(writer, *, now, captured, enabled, attention):
    """仅在控制管道可写时提交本帧授权，后续由新鲜帧续期。"""
    if writer.is_closing() or writer.transport.get_write_buffer_size():
        return
    deadline = now
    if number(captured) and 0 <= now - captured < MAX_AGE:
        deadline = min(now + VOICE_WINDOW_SECONDS, captured + MAX_AGE)
    command = dict(action='voice_window', expires_mono=deadline,
                   enabled=enabled and deadline > now, attention=attention and deadline > now)
    try:
        writer.write((json.dumps(command, separators=(',', ':')) + '\n').encode())
    except (OSError, RuntimeError):
        # 管道关闭交由采集生命周期处理，视频观察可以继续结束本帧。
        return


def voice_window_deadlines(command, now):
    """积压命令只能使用采集帧原有的剩余授权时间。"""
    deadline = command.get('expires_mono')
    if not number(deadline) or not now < deadline <= now + VOICE_WINDOW_SECONDS:
        return 0.0, 0.0
    return (deadline if command.get('enabled') is True else 0.0,
            deadline if command.get('attention') is True else 0.0)


@dataclass(frozen=True)
class AdmittedAudio:
    packet: dict | None
    reset: bool
    diagnostics: dict
    received_mono: float | None = None


class PCMFramer:
    def __init__(self, selected, capture_session):
        if (camera_source_context(selected) is None or not isinstance(capture_session, str)
                or not re.fullmatch(r'[a-f0-9]{32}', capture_session)):
            raise ValueError('invalid_audio_source')
        self.selected = selected
        self.session = capture_session
        self.sequence = 0
        self.latest_receipt = None
        self.epoch = -1
        self.reset()

    def reset(self):
        self.pending = bytearray()
        self.parts = deque()
        self.last_received = None
        self.last_arrival = None
        self.samples = 0
        self.epoch += 1

    def push(self, raw, *, received, decoded, wall, mono):
        if (not isinstance(raw, bytes) or not 0 < len(raw) <= 32000 or len(raw) % 2
                or not fresh(received, decoded, wall) or not number(mono) or mono < 0):
            self.reset()
            return []
        if self.latest_receipt is not None and received < self.latest_receipt:
            self.reset()
            return []
        self.latest_receipt = received
        if (self.last_arrival is not None and not 0 <= mono-self.last_arrival <= MAX_GAP
                or self.last_received is not None and not 0 <= received-self.last_received <= MAX_GAP
                or self.parts and wall-self.parts[0][1] > MAX_AGE):
            self.reset()
        self.pending.extend(raw)
        self.parts.append([len(raw), received, decoded])
        self.last_received, self.last_arrival = received, mono
        packets = []
        while len(self.pending) >= PCM_BYTES:
            chunk = bytes(self.pending[:PCM_BYTES])
            del self.pending[:PCM_BYTES]
            remaining = PCM_BYTES
            receipts, decoded_times = [], []
            while remaining:
                part = self.parts[0]
                take = min(remaining, part[0])
                receipts.append(part[1]); decoded_times.append(part[2])
                part[0] -= take; remaining -= take
                if not part[0]:self.parts.popleft()
            self.sequence += 1
            packet = dict(type='audio', pcm=base64.b64encode(chunk).decode(), sample_rate=SAMPLE_RATE,
                channels=1, sample_format='s16le', received_at=min(receipts), decoded_at=max(decoded_times),
                capture_session=self.session, audio_epoch=self.epoch, audio_sequence=self.sequence,
                sample_start=self.samples)
            self.samples += PCM_BYTES // 2
            packets.append(bind_capture_packet(packet, self.selected))
        return packets


class AudioIngress:
    def __init__(self, selected, capture_session):
        self.context = camera_source_context(selected)
        if (self.context is None or not isinstance(capture_session, str)
                or not re.fullmatch(r'[a-f0-9]{32}', capture_session)):
            raise ValueError('invalid_audio_source')
        self.session = capture_session
        self.frame_at = None
        self.frame_stamp = None
        self.epoch = None
        self.sequence = None
        self.sample_end = None
        self.received = None
        self.arrival = None
        self.needs_reset = True
        self.accepted = self.dropped = self.gaps = 0
        self.reason = 'waiting_video'

    def status(self):
        return dict(status='ready' if self.reason is None else 'waiting', reason=self.reason,
                    accepted_packets=self.accepted, dropped_packets=self.dropped,
                    discontinuities=self.gaps, audio_persisted=False, directional_audio=False,
                    age_basis='local_sdk_receive')

    def reject(self, reason):
        self.dropped += 1
        self.needs_reset = True
        self.reason = reason
        return AdmittedAudio(None, True, self.status())

    def observe_frame(self, packet, *, wall, mono):
        if (not isinstance(packet, dict) or packet.get('type') != 'frame'
                or packet.get('capture_session') != self.session
                or packet.get('camera_source_context') != self.context
                or not number(mono) or mono < 0 or not number(wall)
                or not number(packet.get('received_at'))
                or not 0 <= wall-packet['received_at'] <= MAX_AGE):
            self.frame_at = None
            self.needs_reset = True
            return False
        stamp = packet['received_at']
        if self.frame_stamp is not None and stamp <= self.frame_stamp:
            return False
        self.frame_stamp = stamp
        self.frame_at = mono-(wall-stamp)
        return True

    def accept(self, packet, *, wall, mono):
        if (self.frame_at is None or not number(mono) or not 0 <= mono-self.frame_at <= MAX_AGE):
            return self.reject('video_stale')
        if (not isinstance(packet, dict) or packet.get('type') != 'audio'
                or packet.get('capture_session') != self.session
                or packet.get('camera_source_context') != self.context):
            return self.reject('source_mismatch')
        if (type(packet.get('sample_rate')) is not int or packet['sample_rate'] != SAMPLE_RATE
                or type(packet.get('channels')) is not int or packet['channels'] != 1
                or packet.get('sample_format') != 's16le'):
            return self.reject('format_invalid')
        if not fresh(packet.get('received_at'), packet.get('decoded_at'), wall):
            return self.reject('audio_stale')
        epoch, sequence, start = (packet.get(k) for k in ('audio_epoch', 'audio_sequence', 'sample_start'))
        if (any(type(x) is not int or not 0 <= x < 2**63 for x in (epoch, sequence, start))
                or sequence == 0 or start % (PCM_BYTES // 2)):
            return self.reject('sequence_invalid')
        if self.sequence is not None and (sequence <= self.sequence or epoch < self.epoch):
            return self.reject('sequence_replayed')
        if (self.epoch == epoch and self.sample_end is not None and start < self.sample_end
                or self.received is not None and packet['received_at'] < self.received):
            return self.reject('sequence_replayed')
        encoded = packet.get('pcm')
        if not isinstance(encoded, str) or len(encoded) > 9000:
            return self.reject('format_invalid')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return self.reject('format_invalid')
        if len(raw) != PCM_BYTES:
            return self.reject('format_invalid')
        changed = (self.needs_reset or self.sequence is None or epoch != self.epoch
                   or sequence != self.sequence+1 or start != self.sample_end
                   or self.arrival is not None and not 0 <= mono-self.arrival <= MAX_GAP
                   or self.received is not None and packet['received_at']-self.received > MAX_GAP)
        if changed:self.gaps += 1
        self.epoch, self.sequence, self.sample_end = epoch, sequence, start+PCM_BYTES//2
        self.received, self.arrival = packet['received_at'], mono
        self.needs_reset = False
        self.accepted += 1
        self.reason = None
        return AdmittedAudio(dict(packet), changed, self.status(), mono-(wall-packet['received_at']))


async def deliver_audio(service, admission):
    if not isinstance(admission, AdmittedAudio):
        service.reset_audio()
        service.audio_input_at = None
        return False
    service.state['audio_input'] = admission.diagnostics
    service.audio_input_at = admission.received_mono
    if admission.reset:service.reset_audio()
    if admission.packet is None:return False
    await service.audio(admission.packet)
    return True
