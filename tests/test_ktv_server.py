"""Source-selection contracts with synthetic paths; no network, downloads or models."""

import threading
from types import SimpleNamespace

import pytest

from scripts.ktv_server import acquire_source


@pytest.mark.parametrize("preference,expected", [("auto", "song"), ("song", "song"),
                                                  ("cloud", "cloud"), ("mv", "mv"),
                                                  ("mv_direct", "mv_direct")])
def test_acquisition_does_not_substitute_mv_audio_for_a_song(tmp_path, monkeypatch, preference, expected):
    from homeplane import ktv_video

    folder = tmp_path / "job"
    calls = []
    queue = SimpleNamespace(jobs={folder.name: {"source_preference": preference}},
                            lock=threading.RLock(), report_source=lambda *a: None)

    def adapter(name):
        def acquire(song_id, **kwargs):
            calls.append((name, song_id))
            return {"status": "ready", "source_path": str(tmp_path / (name + ".wav"))}
        return SimpleNamespace(acquire=acquire)

    def mv(song_id, folder, event, *, mv_id=None):
        calls.append(("mv", song_id, mv_id))
        return tmp_path / "mv.wav"

    monkeypatch.setattr(ktv_video, "acquire_mv_source", mv)
    source = acquire_source(queue, adapter("song"), adapter("cloud"), "123", folder, threading.Event())
    assert queue.jobs[folder.name]["audio_origin"] == expected
    if expected in {"mv", "mv_direct"}:
        assert source == tmp_path / "mv.wav" and calls == [("mv", "123", "123" if expected == "mv_direct" else None)]
    else:
        assert source == tmp_path / (expected + ".wav") and calls == [(expected, "123")]


@pytest.mark.parametrize("preference", ["auto", "song"])
def test_unavailable_song_never_falls_back_to_mv(tmp_path, monkeypatch, preference):
    from homeplane import ktv_video

    queue = SimpleNamespace(jobs={"job": {"source_preference": preference}},
                            lock=threading.RLock(), report_source=lambda *a: None)
    unavailable = SimpleNamespace(acquire=lambda *a, **k: {"status": "unavailable", "code": "source_rights_denied"})
    monkeypatch.setattr(ktv_video, "acquire_mv_source", lambda *a, **k: pytest.fail("MV substituted for unavailable song"))
    with pytest.raises(ValueError, match="source_rights_denied"):
        acquire_source(queue, unavailable, unavailable, "123", tmp_path / "job", threading.Event())
