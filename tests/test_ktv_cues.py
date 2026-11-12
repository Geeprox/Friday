import json
import wave

import numpy as np

from homeplane.ktv_cues import vocal_entries, with_count_ins


def test_vocal_entries_require_silence_sustained_voice_and_matching_lyric():
    levels = np.zeros(3000)
    levels[500:800] = 0.2  # Singing begins at ten seconds.
    levels[650:654] = 0  # A short breath is not an interlude.
    levels[1100:1104] = 0.5  # A click is not singing.
    levels[1500:1700] = 0.2
    levels[1800:1900] = 0.2  # Too short a pause to count in again.
    assert vocal_entries(levels, [10200, 30000, 36000]) == [10000, 30000]
    assert vocal_entries(levels, [14000, 35000]) == []
    assert vocal_entries(np.zeros(1000), [10000]) == []


def test_local_leakage_floor_needs_a_sustained_rise_at_a_known_entrance():
    levels = np.full(1500, 0.03)
    levels[500:800] = 0.15
    assert abs(vocal_entries(levels, [10000])[0] - 10000) <= 100
    assert vocal_entries(levels, [20000]) == []


def test_unavailable_or_unverified_stem_preserves_lyrics(tmp_path):
    lyrics = {"status": "ready", "lines": [{"time_ms": 10000, "text": "Synthetic"}]}
    preparation = tmp_path / "preparation.json"
    assert with_count_ins(lyrics, preparation)["cue_source"] == "lyrics"
    preparation.write_text(json.dumps({"status": "ready", "model_verified": False}))
    assert with_count_ins(lyrics, preparation)["lines"] == lyrics["lines"]
    assert "cue_source" not in lyrics


def test_existing_pcm_stem_can_supply_private_local_count_in(tmp_path):
    stem = tmp_path / "vocals.wav"
    samples = np.zeros(16000 * 12, dtype="<i2")
    samples[160000:] = 6000
    with wave.open(str(stem), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(samples.tobytes())
    preparation = tmp_path / "preparation.json"
    preparation.write_text(json.dumps({"status": "ready", "model_verified": True,
                                      "simulated": False, "vocals_path": str(stem)}))
    result = with_count_ins({"status": "ready", "lines": [{"time_ms": 10100, "text": "Synthetic"}]}, preparation)
    assert result["cue_source"] == "vocal_activity"
    assert result["entry_ms"] == [10000]
    assert str(tmp_path) not in json.dumps(result)


def test_restored_pair_count_in_and_credits(tmp_path):
    original, instrumental = tmp_path / "original.wav", tmp_path / "instrumental.wav"
    backing = np.full(16000 * 12, 1000, dtype="<i2")
    vocal = backing.copy()
    vocal[160000:] += 5000
    for path, samples in [(original, vocal), (instrumental, backing)]:
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(samples.tobytes())
    result = with_count_ins({"lines": [{"time_ms": 1000, "text": "录音工程师 : Synthetic"},
                                      {"time_ms": 10100, "text": "Synthetic lyric"}]},
                            tmp_path / "missing.json", original=original, instrumental=instrumental)
    assert result["cue_source"] == "vocal_residual"
    assert result["entry_ms"] == [10000]
    assert len(result["lines"]) == 1
