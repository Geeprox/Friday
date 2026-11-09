"""Bounded local count-ins from an existing separated vocal stem and matched lyrics.

No separation, microphone capture or network request is started here. Energy is
only used to confirm a quiet-to-vocal transition close to an existing LRC line;
it is not transcription, word alignment or a measured singing-latency clock.
"""
import json
import os
from pathlib import Path
import threading
import wave
from contextlib import ExitStack

_lock = threading.Lock()
_cache = {}


def vocal_entries(levels, starts, *, hop_ms=20):
    import numpy as np

    energy = np.asarray(levels, dtype=float)
    if energy.size < 10 or not np.isfinite(energy).all() or (energy < 0).any():
        return []
    threshold = max(0.003, float(np.percentile(energy, 90)) * 0.12)
    active = energy >= threshold
    # Short gaps inside a sung phrase are breaths, not another entrance.
    sounding = np.flatnonzero(active)
    for left, right in zip(sounding[:-1], sounding[1:]):
        if (right - left) * hop_ms <= 200:
            active[left:right] = True
    edges = np.diff(np.r_[False, active, False].astype(int))
    beginnings, endings = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    previous_end = 0
    entries = []
    for begin, end in zip(beginnings, endings):
        if (end - begin) * hop_ms < 180:
            continue
        onset = int(begin * hop_ms)
        if (begin - previous_end) * hop_ms >= 5000:
            nearest = min(starts, key=lambda value: abs(value - onset), default=None)
            if nearest is not None and abs(nearest - onset) <= 1500:
                entries.append(onset)
        previous_end = end
    # A residual can retain a small amount of accompaniment. In that case an
    # adaptive local floor can confirm an LRC entrance even without digital
    # silence. Require a sustained threefold rise near that exact lyric only.
    for index, start in enumerate(starts):
        if start < 6000 or (index > 0 and start - starts[index - 1] < 10000):
            continue
        floor = energy[max(0, int((start - 6000) / hop_ms)):int((start - 2000) / hop_ms)]
        if len(floor) < 100:
            continue
        local = max(threshold, float(np.median(floor)) * 3)
        if float(np.percentile(floor, 90)) >= local or local > float(np.percentile(energy, 90)):
            continue
        left, right = max(0, int((start - 1500) / hop_ms)), min(len(energy), int((start + 1500) / hop_ms))
        for begin in range(left, max(left, right - 10)):
            if np.count_nonzero(energy[begin:begin + 10] >= local) >= 8:
                onset = int(begin * hop_ms)
                if not any(abs(value - onset) <= 1500 for value in entries):
                    entries.append(onset)
                break
    return sorted(set(entries))


def with_count_ins(result: dict, preparation: Path, *, original: Path | None = None, instrumental: Path | None = None) -> dict:
    """Add optional cue metadata; keep usable lyrics if a stem is unavailable."""
    from .ktv_search import is_lyric_credit
    response = dict(result)
    response["lines"] = [row for row in result.get("lines", [])[:1000] if not is_lyric_credit(row.get("text", ""))]
    response.update(cue_source="lyrics", entry_ms=[])
    try:
        lines = response["lines"]
        starts = tuple(int(row["time_ms"]) for row in lines[:1000])
        if not starts:
            return response
        paths, basis = [], "vocal_activity"
        if preparation.is_file() and preparation.stat().st_size <= 65536:
            report = json.loads(preparation.read_text())
            if report.get("status") == "ready" and report.get("model_verified") is True and report.get("simulated") is False:
                candidate = Path(report["vocals_path"])
                if candidate.is_file():
                    paths = [candidate]
        # Restored jobs may no longer have the separation report. Their exact,
        # aligned original/accompaniment pair provides a vocal residual instead.
        if not paths and original is not None and instrumental is not None:
            paths, basis = [original, instrumental], "vocal_residual"
        if not paths:
            return response
        identities = []
        for stem in paths:
            info = stem.stat()
            if (not stem.is_absolute() or stem.resolve() != stem or not stem.is_file()
                    or info.st_uid != os.getuid() or not 44 < info.st_size <= 700 * 1024**2):
                return response
            identities.append((str(stem), info.st_mtime_ns, info.st_size))
        key = (tuple(identities), starts)
        with _lock:
            if key not in _cache:
                import numpy as np
                with ExitStack() as stack:
                    readers = [stack.enter_context(wave.open(str(path), "rb")) for path in paths]
                    audio = readers[0]
                    rate, channels = audio.getframerate(), audio.getnchannels()
                    if (audio.getsampwidth() != 2 or channels not in (1, 2)
                            or not 8000 <= rate <= 96000 or audio.getnframes() / rate > 1800
                            or any(reader.getparams() != audio.getparams() for reader in readers[1:])):
                        return response
                    hop = round(rate * 0.02)
                    levels = []
                    while block := audio.readframes(hop):
                        samples = np.frombuffer(block, dtype="<i2").astype(np.float32) / 32768
                        if len(readers) == 2:
                            samples -= np.frombuffer(readers[1].readframes(hop), dtype="<i2").astype(np.float32) / 32768
                        levels.append(float(np.sqrt(np.mean(samples * samples))))
                if len(_cache) >= 80:
                    _cache.clear()
                _cache[key] = vocal_entries(levels, starts, hop_ms=hop * 1000 / rate)
            detected = _cache[key]
            if detected:
                # Keep lyric-timed entrances where leakage or quiet vocals leave
                # the energy estimate uncertain. Do not invent a global offset.
                entries = [start for i, start in enumerate(starts)
                           if start > 0 and (i == 0 or start - starts[i - 1] >= 10000)]
                for onset in detected:
                    entries = [value for value in entries if abs(value - onset) > 1500]
                    entries.append(onset)
                response.update(cue_source=basis, entry_ms=sorted(set(entries)), cue_audio_count=len(detected))
    except (OSError, ValueError, KeyError, TypeError, wave.Error, ImportError, EOFError):
        pass
    return response
