"""在本机识别语音，单独的唤醒词仅返回确认事件。"""

import base64
import contextlib
import json
import os
import signal
import sys
import time
import warnings

import numpy as np

from homeplane.embodied.intent import address, transcript_quality
from homeplane.embodied.local_asr import LocalASR
from homeplane.embodied.voice_preferences import read_preferences

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
warnings.filterwarnings("ignore")


def emit(x):
    print(json.dumps(x, ensure_ascii=False, separators=(",", ":")), flush=True)


def normalize_audio(samples):
    rms = float(np.sqrt(np.mean(samples * samples)))
    gain = min(24.0, 0.08 / max(rms, 1e-7))
    return np.clip(samples * gain, -0.98, 0.98), rms, gain


def main():
    asr = LocalASR(os.environ.get("FRIDAY_ASR", "sensevoice"))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Load/warm only the existing local checkpoint. Silence never becomes a command.
    with contextlib.redirect_stdout(sys.stderr):
        asr.transcribe(np.zeros(16000, dtype=np.float32))
    emit({"type": "ready", "asr": asr.label, "audio_persisted": False})
    for line in sys.stdin:
        request = {}
        try:
            if len(line) > 400000:
                raise ValueError("input_too_large")
            request = json.loads(line)
            began = time.monotonic()
            if request.get("op") != "transcribe":
                raise ValueError("operation_not_allowed")
            policy=read_preferences()
            if request.get('voice_revision',policy.revision)!=policy.revision:
                raise ValueError('voice_preferences_changed')
            pcm = base64.b64decode(request["pcm"], validate=True)
            if not 6400 <= len(pcm) <= 256000:
                raise ValueError("invalid_audio_duration")
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            samples, rms, gain = normalize_audio(samples)
            with contextlib.redirect_stdout(sys.stderr):
                text, quality = asr.transcribe(samples)
            valid, quality_reason = transcript_quality(text, quality, wake_word=policy.wake_word)
            wake_only = request.get("wake_only") is True
            route = (address(text, not wake_only and request.get("attending") is True, wake_word=policy.wake_word)
                     if valid else {"admitted": False, "reason": quality_reason})
            latency = round((time.monotonic() - began) * 1000, 1)
            if route.get("reason") == "waiting_instruction" and route.get("wake_word") is True:
                emit(dict(id=request["id"], type="wake", voice_revision=policy.revision, latency_ms=latency))
                continue
            if valid and wake_only:
                quality_reason = "wake_word_required"
                route = {"admitted": False, "reason": quality_reason}
            emit(dict(id=request["id"], type="transcript",
                      text=text if route["admitted"] else "", address=route,
                      asr_quality=dict(quality, filter=quality_reason), voice_revision=policy.revision,
                      signal={"input_rms": round(rms, 4), "gain": round(gain, 2)}, latency_ms=latency))
        except Exception as e:
            emit(
                {
                    "id": request.get("id"),
                    "type": "error",
                    "error_class": type(e).__name__,
                    "code": getattr(e, "code", None),
                }
            )


if __name__ == "__main__":
    main()
