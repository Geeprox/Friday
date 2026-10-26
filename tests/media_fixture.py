"""SIMULATED H264/Opus frames, generated with installed standard encoders."""
from array import array
from fractions import Fraction
from math import sin

import av


def packets():
    video = av.CodecContext.create("libx264", "w")
    video.width, video.height, video.pix_fmt = 160, 120, "yuv420p"
    video.time_base, video.framerate = Fraction(1, 20), Fraction(20, 1)
    video.thread_count = 1
    video.options = {"preset": "ultrafast", "tune": "zerolatency", "g": "20"}
    images = []
    for index in range(40):
        frame = av.VideoFrame(160, 120, "yuv420p")
        for plane_index, plane in enumerate(frame.planes):
            plane.update(bytes([16 + index if plane_index == 0 else 128]) * plane.buffer_size)
        frame.pts = index
        images.extend((bytes(packet), bool(packet.is_keyframe)) for packet in video.encode(frame))
    images.extend((bytes(packet), bool(packet.is_keyframe)) for packet in video.encode(None))
    audio = av.CodecContext.create("libopus", "w")
    audio.sample_rate, audio.layout, audio.format = 48000, "mono", "flt"
    audio.time_base = Fraction(1, 48000)
    audio.thread_count = 1
    sounds = []
    for index in range(100):
        frame = av.AudioFrame(format="flt", layout="mono", samples=960)
        frame.planes[0].update(array('f', (.05 * sin(sample * .04) for sample in range(960))).tobytes())
        frame.sample_rate, frame.pts = 48000, index * 960
        sounds.extend(bytes(packet) for packet in audio.encode(frame))
    sounds.extend(bytes(packet) for packet in audio.encode(None))
    return images, sounds
