import io
import wave

import pytest

from friday.camera import SpeechSegments

np = pytest.importorskip("numpy")


def test_server_gate_silence_speech_and_overlong_tail():
    silence = np.zeros(320, dtype="<i2").tobytes()
    speech = np.full(320, 3000, dtype="<i2").tobytes()
    gate = SpeechSegments()
    assert all(gate.feed(silence) is None for _ in range(100))
    outputs = [wav for chunk in [speech] * 40 + [silence] * 40 if (wav := gate.feed(chunk))]
    assert len(outputs) == 1
    with wave.open(io.BytesIO(outputs[0])) as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getnframes() < 6 * 16000
    gate = SpeechSegments()
    assert all(gate.feed(chunk) is None for chunk in [speech] * 400 + [silence] * 40)


def test_speech_gate_can_recover_after_overflow():
    silence = np.zeros(320, dtype="<i2").tobytes()
    speech = np.full(320, 3000, dtype="<i2").tobytes()
    gate = SpeechSegments()
    for chunk in [speech] * 400 + [silence] * 40:
        gate.feed(chunk)
    outputs = [wav for chunk in [speech] * 40 + [silence] * 40 if (wav := gate.feed(chunk))]
    assert len(outputs) == 1
