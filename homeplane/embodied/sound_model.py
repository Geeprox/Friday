"""Optional Apple SoundAnalysis child; camera PCM stays in bounded memory.

Constructing this adapter or asking for readiness never starts the classifier.
Identifiers below are requested vocabulary, not a claim that the host supports
them. Only exact identifiers returned by the running native request are used.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import platform
from pathlib import Path

from .paths import DATA, PRIVATE
from .review_child import ReviewChild
from .temporal_model import host_budget

SOUND = DATA / 'bin/friday-sound'
INFANT_CRY_LABEL = 'baby_crying'
IDENTIFIERS = {
    'crying': (INFANT_CRY_LABEL, 'crying', 'sobbing'),
    'scream': ('screaming',),
    'glass_break': ('glass_breaking',),
    'alarm': ('smoke_detector', 'fire_alarm'),
}
MESSAGES = {
    'crying': '听到疑似哭声，请查看附近画面。',
    'scream': '听到疑似尖叫声，请查看附近画面。',
    'glass_break': '听到疑似玻璃破碎声，请查看附近画面。',
    'alarm': '听到疑似警报声，请查看附近画面。',
}
WINDOW_BYTES = 64000  # Two seconds, mono16kHz, signed16-bit little endian.
START_TIMEOUT = 8


def sound_readiness(binary=SOUND):
    binary = Path(binary)
    reason = None
    try:
        mode = binary.stat()
        if (binary.is_symlink() or not binary.is_file() or mode.st_uid != os.getuid()
                or mode.st_mode & 0o022 or not os.access(binary, os.X_OK)):
            reason = 'sound_runtime_missing'
    except OSError:
        reason = 'sound_runtime_missing'
    if platform.system() != 'Darwin':reason = 'macos_required'
    return dict(available=reason is None, reason=reason, model='apple.soundanalysis.version1',
                labels_verified=False, audio_persisted=False, directional_audio=False,
                automatic_announcement=False, cloud_fallback=False)


def catalog(labels):
    if (not isinstance(labels, list) or not 0 < len(labels) <= 1024
            or any(not isinstance(x, str) or not 0 < len(x) <= 80 for x in labels)
            or len(set(labels)) != len(labels)):
        raise ValueError('sound_protocol_invalid')
    return {kind: tuple(x for x in wanted if x in labels)
            for kind, wanted in IDENTIFIERS.items() if any(x in labels for x in wanted)}


def sound_result(reply, supported):
    """Strict known-vocabulary evidence. Thresholds are trial gates, not accuracy."""
    windows = reply.get('windows') if isinstance(reply, dict) else None
    if not isinstance(windows, list) or not 1 <= len(windows) <= 4:
        raise ValueError('sound_protocol_invalid')
    candidates = []
    for window in windows:
        if not isinstance(window, dict):raise ValueError('sound_protocol_invalid')
        start, duration, competing = (window.get(k) for k in ('offset', 'duration', 'competing_confidence'))
        if (any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, duration, competing))
                or not 0 <= start < 2 or not 1.5 <= duration <= 2.001
                or start+duration > 2.001 or not 0 <= competing <= 1):
            raise ValueError('sound_protocol_invalid')
        scores = window.get('scores')
        known = {identifier for identifiers in supported.values() for identifier in identifiers}
        if (not isinstance(scores, dict) or set(scores) != known
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                       for v in scores.values())):
            raise ValueError('sound_protocol_invalid')
        ranked = sorted(((max(scores[x] for x in names), kind) for kind, names in supported.items()),
                        reverse=True)
        if not ranked:raise ValueError('sound_labels_unavailable')
        confidence, kind = ranked[0]
        rival = max(competing, ranked[1][0] if len(ranked) > 1 else 0)
        infant_score = scores.get(INFANT_CRY_LABEL, 0)
        cry_rival = max((scores.get(name, 0) for name in IDENTIFIERS['crying'][1:]), default=0)
        infant_cry = (kind == 'crying' and infant_score >= .85
                      and infant_score - max(rival, cry_rival) >= .15)
        candidates.append((kind, confidence, infant_cry) if confidence >= .85 and confidence-rival >= .15 else None)
    if any(x is None for x in candidates) or len({x[0] for x in candidates}) != 1:return None
    result = dict(kind=candidates[0][0], confidence=min(x[1] for x in candidates))
    if all(x[2] for x in candidates):
        result['infant_cry'] = True
    return result


class AppleSoundProcess:
    def __init__(self, binary=SOUND, private=PRIVATE, budget=host_budget):
        self.binary, self.private = Path(binary), Path(private)
        self.budget = budget
        self.child = None
        self.supported = {}
        self.sequence = 0
        self.lock = asyncio.Lock()

    @property
    def process(self):
        return self.child.process if self.child else None

    @property
    def lease(self):
        return self.child.lease if self.child else None

    def readiness(self):
        return dict(sound_readiness(self.binary), labels_verified=bool(self.supported),
                    supported_types=list(self.supported),
                    infant_cry_supported=(INFANT_CRY_LABEL in self.supported.get('crying', ())
                                          if self.supported else None))

    async def _start(self):
        if self.child and self.child.closed:
            if self.child.slot:
                raise ValueError('sound_model_busy')
            self.child = None
        if self.process and self.process.returncode is None:return
        if self.child:
            await self.close()
            if self.child:
                raise ValueError('sound_model_busy')
        ready = self.readiness()
        if not ready['available']:raise ValueError(ready['reason'])
        self.child = ReviewChild(
            (str(self.binary), '--camera-pcm'), self.private, 'sound-analysis.lock',
            busy='sound_model_busy', unavailable='sound_runtime_missing',
            env={**os.environ, 'FRIDAY_SOUND_OWNER_PID': str(os.getpid())},
        )
        try:
            async with asyncio.timeout(START_TIMEOUT):
                process = await self.child.start()
                ready = json.loads(await process.stdout.readline())
            if (not isinstance(ready, dict) or ready.get('type') != 'ready'
                    or type(ready.get('version')) is not int or ready['version'] != 1
                    or type(ready.get('sample_rate')) is not int or ready['sample_rate'] != 16000):
                raise ValueError('sound_model_unavailable')
            self.supported = catalog(ready.get('known_classifications'))
            if not self.supported:raise ValueError('sound_labels_unavailable')
        except BaseException:
            await self.close()
            raise

    async def analyze(self, pcm):
        if not isinstance(pcm, bytes) or len(pcm) != WINDOW_BYTES:
            raise ValueError('sound_window_invalid')
        async with self.lock:
            try:
                budget = await asyncio.to_thread(self.budget)
                if (budget.free_percent is None or budget.total_bytes < 4*1024**3
                        or budget.total_bytes*budget.free_percent/100 < 1024**3):
                    raise ValueError('sound_memory_pressure')
                if budget.load > max(1, budget.cores)*1.5:raise ValueError('sound_cpu_busy')
                await self._start()
                self.sequence += 1
                request = dict(id=self.sequence, pcm=base64.b64encode(pcm).decode(),
                               identifiers=[x for names in self.supported.values() for x in names])
                self.process.stdin.write((json.dumps(request)+'\n').encode())
                await asyncio.wait_for(self.process.stdin.drain(), 1)
                reply = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 6))
                if (not isinstance(reply, dict) or type(reply.get('version')) is not int
                        or reply['version'] != 1 or reply.get('type') != 'result'
                        or type(reply.get('id')) is not int or reply['id'] != self.sequence):
                    raise ValueError('sound_protocol_invalid')
                return sound_result(reply, self.supported)
            except BaseException:
                if not self.child or not self.child.closed:
                    await self.close()
                raise

    async def close(self):
        self.supported = {}
        child = self.child
        if child:
            await child.close()
            if self.child is child and not child.slot:
                self.child = None
