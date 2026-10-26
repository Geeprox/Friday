"""Bounded, in-memory Annex-B fan-out. No decode, transcode, disk or camera SDK.

Each subscriber starts at a random-access unit with codec parameter sets. A
slow subscriber is disconnected instead of dropping a reference frame. Source
epochs revoke every subscriber on privacy closure or reconnect.
"""
import asyncio
import base64
from collections import deque
import math
import re
import struct
import time

START = re.compile(b'\x00\x00(?:\x00)?\x01')
HEADER = struct.Struct('>IBHHQQ')
MAX_PACKET = 2 * 1024 * 1024
MAX_GOP = 8 * 1024 * 1024


def units(data, codec):
    starts = list(START.finditer(data))
    for i, start in enumerate(starts):
        stop = starts[i+1].start() if i+1 < len(starts) else len(data)
        payload = data[start.end():stop]
        if payload:
            yield (payload[0] & 31 if codec == 'h264' else (payload[0] >> 1) & 63), b'\x00\x00\x00\x01'+payload


class VideoRelay:
    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.epoch = 0
        self.sequence = 0
        self.records = deque()
        self.bytes = 0
        self.codec = None
        self.parameters = {}
        self.size = None
        self.subscribers = 0
        self.source_sequence = None
        self.changed = asyncio.Event()

    def clear(self):
        self.discontinuity()
        self.parameters.clear()
        self.codec = self.size = None

    def discontinuity(self):
        """Revoke decoders after a reference gap, retaining same-format headers."""
        self.epoch += 1
        self.records.clear()
        self.bytes = 0
        self.source_sequence = None
        self.changed.set()

    def offer(self, packet):
        source_sequence=packet.get('source_sequence')
        if type(source_sequence) is not int or source_sequence<1:
            self.clear();return
        if self.source_sequence is not None and source_sequence!=self.source_sequence+1:
            self.discontinuity()
        self.source_sequence=source_sequence
        codec, at = packet.get('codec'), packet.get('received_at')
        w, h = packet.get('width'), packet.get('height')
        if (codec not in {'h264','hevc'} or type(at) not in (int,float)
                or not math.isfinite(at) or not 0 <= self.clock()-at <= 1.5
                or type(w) is not int or type(h) is not int
                or not 16 <= w <= 8192 or not 16 <= h <= 4320):self.clear();return
        value = packet.get('data')
        if not isinstance(value,str) or len(value) > MAX_PACKET*4//3+4:self.discontinuity();return
        try:data=base64.b64decode(value,validate=True)
        except (ValueError,TypeError):self.discontinuity();return
        if not data or len(data)>MAX_PACKET:self.discontinuity();return
        if self.codec != codec or self.size != (w,h):
            self.clear()
            self.source_sequence=source_sequence
            self.codec,self.size=codec,(w,h)
        parts=list(units(data,codec))
        if not parts:self.discontinuity();return
        required={7,8} if codec=='h264' else {32,33,34}
        keys={5} if codec=='h264' else {16,17,18,19,20,21}
        for kind,nal in parts:
            if kind in required:
                if len(nal)>65536:self.clear();return
                self.parameters[kind]=nal
        key=any(kind in keys for kind,_ in parts)
        if key:
            self.records.clear();self.bytes=0
            if not required.issubset(self.parameters):self.clear();return
            data=b''.join(self.parameters[k] for k in sorted(required))+data
        elif not self.records:return
        if len(data)>MAX_PACKET:self.clear();return
        if self.bytes+len(data)>MAX_GOP or len(self.records)>=160:
            self.records.clear();self.bytes=0;self.changed.set();return
        self.sequence+=1
        record=HEADER.pack(len(data),1 if codec=='h264' else 2,w,h,int(at*1000),self.sequence)+data
        self.records.append((self.sequence,at,record));self.bytes+=len(data)
        self.changed.set()

    async def stream(self):
        if self.subscribers>=2:raise ValueError('viewer_capacity')
        self.subscribers+=1
        epoch=self.epoch;cursor=None
        try:
            while epoch==self.epoch:
                self.changed.clear()
                if self.records:
                    if self.clock()-self.records[-1][1]>2:
                        self.records.clear();self.bytes=0;cursor=None
                if self.records:
                    if cursor is not None and cursor<self.records[0][0]-1:return
                    available=[r for r in self.records if cursor is None or r[0]>cursor]
                    for sequence,at,record in available:
                        if epoch!=self.epoch:return
                        yield record
                        cursor=sequence
                try:await asyncio.wait_for(self.changed.wait(),2)
                except TimeoutError:return
        finally:self.subscribers-=1
