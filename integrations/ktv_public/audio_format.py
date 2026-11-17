"""压缩双轨的纯标准库契约，Mac 与 Python 3.8 NAS 共用。"""
import math
import struct

AAC_ENCODING = "aac_lc_m4a"
AAC_FIELDS = {"frames", "encoder_delay", "encoder_padding", "bitrate"}
MAX_FRAMES = 86400000


def compact_spec(spec, frames=None):
    if spec.get("encoding") != AAC_ENCODING:
        return False
    count = spec.get("frames")
    delay, padding = spec.get("encoder_delay"), spec.get("encoder_padding")
    rate = spec.get("bitrate")
    return (type(count) is int and 1 <= count <= MAX_FRAMES
            and (frames is None or frames == count)
            and spec.get("sample_rate") == 48000 and spec.get("channels") == 2
            and type(delay) is int and delay == 1024
            and type(padding) is int and padding == (-(count + delay)) % 1024
            and type(rate) is int and 8000 <= rate <= 1024000
            and type(spec.get("duration_seconds")) in (int, float)
            and math.isfinite(spec["duration_seconds"])
            and abs(spec["duration_seconds"] - count / 48000) < .000001)


def _boxes(stream, start, end):
    position = start
    count = 0
    while position < end:
        stream.seek(position)
        raw = stream.read(8)
        if len(raw) != 8:
            raise ValueError("invalid_compact_container")
        size, name = struct.unpack(">I4s", raw)
        header = 8
        if size == 1:
            raw = stream.read(8)
            if len(raw) != 8:
                raise ValueError("invalid_compact_container")
            size, header = struct.unpack(">Q", raw)[0], 16
        if size < header or position + size > end or count >= 10000:
            raise ValueError("invalid_compact_container")
        yield name, position + header, position + size
        position += size
        count += 1
    if position != end:
        raise ValueError("invalid_compact_container")


def _one(boxes, name):
    matching = [item for item in boxes if item[0] == name]
    if len(matching) != 1:
        raise ValueError("invalid_compact_container")
    return matching[0][1:]


def _read(stream, box, limit=4194304):
    start, end = box
    if end - start > limit:
        raise ValueError("invalid_compact_container")
    stream.seek(start)
    raw = stream.read(end - start)
    if len(raw) != end - start:
        raise ValueError("invalid_compact_container")
    return raw


def verify_compact(path, spec):
    """验证单音轨 AAC LC，无隐式 edit list，首尾裁切以签名清单为准。"""
    if not compact_spec(spec):
        raise ValueError("invalid_compact_contract")
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        if size != spec["bytes"]:
            raise ValueError("invalid_compact_container")
        roots = list(_boxes(stream, 0, size))
        brand = _read(stream, _one(roots, b"ftyp"), 512)
        if not any(b in brand for b in (b"M4A ", b"isom", b"mp42")):
            raise ValueError("invalid_compact_container")
        moov = list(_boxes(stream, *_one(roots, b"moov")))
        track = list(_boxes(stream, *_one(moov, b"trak")))
        if any(name == b"edts" for name, _, _ in track):
            raise ValueError("implicit_audio_trim")
        mdia = list(_boxes(stream, *_one(track, b"mdia")))
        header = _read(stream, _one(mdia, b"mdhd"), 64)
        if len(header) < 24 or header[0] != 0:
            raise ValueError("invalid_compact_container")
        timescale, duration = struct.unpack_from(">II", header, 12)
        if timescale != 48000 or duration != spec["frames"] + spec["encoder_delay"]:
            raise ValueError("compact_timeline_mismatch")
        handler = _read(stream, _one(mdia, b"hdlr"), 512)
        if handler[8:12] != b"soun":
            raise ValueError("invalid_compact_codec")
        minf = list(_boxes(stream, *_one(mdia, b"minf")))
        table = list(_boxes(stream, *_one(minf, b"stbl")))
        sample = _read(stream, _one(table, b"stsd"), 4096)
        # AudioSampleEntry 的声道、采样率与 esds 的 AAC LC 配置必须一致。
        if (len(sample) < 56 or sample[4:8] != b"\0\0\0\1" or sample[12:16] != b"mp4a"
                or struct.unpack_from(">H", sample, 32)[0] != 2
                or struct.unpack_from(">I", sample, 40)[0] != 48000 << 16
                or b"esds" not in sample or b"\x11\x90" not in sample):
            raise ValueError("invalid_compact_codec")
        samples = _read(stream, _one(table, b"stsz"))
        if len(samples) < 12:
            raise ValueError("invalid_compact_container")
        fixed, count = struct.unpack_from(">II", samples, 4)
        expected = (spec["frames"] + 1024 + spec["encoder_padding"]) // 1024
        if count != expected or count > 84377 or fixed or len(samples) != 12 + count * 4:
            raise ValueError("compact_timeline_mismatch")
        sizes = struct.unpack_from(">" + "I" * count, samples, 12)
        if not all(1 <= length <= 16384 for length in sizes):
            raise ValueError("invalid_compact_container")
        mdat = _one(roots, b"mdat")
        if sum(sizes) != mdat[1] - mdat[0]:
            raise ValueError("invalid_compact_container")
