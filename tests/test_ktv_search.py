import json
from contextlib import contextmanager

import pytest

from homeplane import ktv_search


class Response:
    status_code = 200
    def __init__(self, data):
        self.data = data
    def iter_bytes(self):
        yield self.data


def install(monkeypatch, data, calls):
    ktv_search._cache.clear()
    @contextmanager
    def stream(method, url, **kwargs):
        calls.append((method, url, kwargs))
        yield Response(data)
    monkeypatch.setattr(ktv_search.httpx, "stream", stream)


def test_metadata_search_fixed_host_no_credentials_and_cache(monkeypatch):
    calls = []
    data = {"code": 200, "result": {"songs": [{"id": 123, "name": "Synthetic song",
                                                "artists": [{"name": "Synthetic artist"}]}]}}
    install(monkeypatch, json.dumps(data).encode(), calls)
    value = ktv_search.search_songs("Synthetic")
    assert value[0]["song_id"] == "123"
    assert value[0]["download_permission_confirmed"] is False
    assert calls[0][1] == "https://music.163.com/api/search/get/web"
    assert calls[0][2]["follow_redirects"] is False
    assert set(calls[0][2]["headers"]) == {"Referer"}
    assert ktv_search.search_songs("Synthetic") == value and len(calls) == 1


def test_search_metadata_is_not_used_as_download_authority(monkeypatch):
    calls = []
    install(monkeypatch, json.dumps({"code": 200, "result": {"songs": [
        {"id": "../../etc/passwd", "name": "bad"}, {"id": 123, "name": "control\ncharacter"},
        {"id": 124, "name": "Synthetic", "audio_url": "https://example.com/audio"}]}}).encode(), calls)
    value = ktv_search.search_songs("test")
    assert [s["song_id"] for s in value] == ["124"]
    assert "audio_url" not in value[0]


def test_search_response_bounded(monkeypatch):
    install(monkeypatch, b"x" * (1024 * 1024 + 1), [])
    with pytest.raises(ValueError, match="search_response_exceeded"):
        ktv_search.search_songs("test")


def test_invalid_query_never_requests(monkeypatch):
    calls = []
    install(monkeypatch, b"", calls)
    for query in ("", " " * 3, "x" * 121, None):
        with pytest.raises(ValueError):
            ktv_search.search_songs(query)
    assert not calls


def test_lrc_multi_timestamp_offset_and_budget():
    value = "[offset:100]\n[00:01.25][00:03.125]Synthetic lyric\n[99:00]too late\n[00:04.0]\n"
    assert ktv_search.parse_lrc(value) == [{"time_ms": 1150, "text": "Synthetic lyric"},
                                          {"time_ms": 3025, "text": "Synthetic lyric"}]
    with pytest.raises(ValueError, match="lyrics_budget_exceeded"):
        ktv_search.parse_lrc("x" * 65537)


def test_credit_roles_do_not_become_vocal_entrances():
    roles = ["录音师", "录音室", "混音工程师", "混音录音室", "女声", "和声编写", "合声", "弦乐编写", "弦乐团", "编程",
             "Production Coordination", "Arrangement, Keyboards & Programming", "Recording / Mixing Engineer",
             "Background Vocals", "Guitar and Bass", "Mastered by", "OP", "SP"]
    value = "\n".join("[00:01]" + role + " : Synthetic" for role in roles)
    value += "\n[00:15.54]Synthetic lyric"
    assert ktv_search.parse_lrc(value) == [{"time_ms": 15540, "text": "Synthetic lyric"}]
    for lyric in ("Love: a synthetic lyric", "I say: stay", "Bass in my heart", "Production of a dream: tonight"):
        assert not ktv_search.is_lyric_credit(lyric)
    for credit in ("Strings Arranged & Conducted by Synthetic", "Recorded at Synthetic Studio",
                   "Engineered by Synthetic", "Vocals Recorded at Synthetic Studio", "Mixed by Synthetic"):
        assert ktv_search.is_lyric_credit(credit)


def test_lyrics_fixed_endpoint_and_cache(monkeypatch):
    ktv_search._lyrics.clear()
    calls = []
    install(monkeypatch, json.dumps({"lrc": {"lyric": "[00:01.00]Synthetic lyric"}}).encode(), calls)
    first = ktv_search.song_lyrics("123")
    assert first["status"] == "ready" and first["lines"][0]["time_ms"] == 1000
    assert calls[0][1] == "https://music.163.com/api/song/lyric"
    assert ktv_search.song_lyrics("123") == first and len(calls) == 1


def test_credit_timestamps_do_not_replace_the_first_sung_line_countdown():
    text='[00:00.00]作词：合成作者\n[00:01.00]作 曲 : 合成作者\n[00:03.00]编曲：合成作者\n[00:20.00]第一句合成歌词\n[00:25.00]第二句合成歌词'
    assert ktv_search.parse_lrc(text)==[{'time_ms':20000,'text':'第一句合成歌词'},{'time_ms':25000,'text':'第二句合成歌词'}]
