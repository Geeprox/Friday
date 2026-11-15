"""以合成录音与 OpenAI 回执验证 KTV 歌词发现边界。"""

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from homeplane.intelligence import ktv_lyrics
from homeplane.intelligence.ktv_lyrics import KtvLyricsRecovery
from homeplane.ktv_source_api import LocalSourceAPI
from tests.test_intelligence_providers import runtime


def test_web_lookup_matches_official_recording_then_gets_provider_timecodes(tmp_path):
    requested = []
    received = []
    urls = [
        "https://untrusted.invalid/song?id=95",
        "https://music.163.com/song?id=61",
        "https://music.163.com/#/song?id=62",
    ]

    def handler(request):
        requested.append(json.loads(request.content))
        return httpx.Response(200, json={
            "status": "completed",
            "usage": {"input_tokens": 54, "output_tokens": 7},
            "output": [
                {"type": "web_search_call", "action": {"type": "search", "sources": [{"url": url} for url in urls]}},
                {"type": "message", "content": [{"type": "output_text", "text": "任意生成歌词都不能被采用"}]},
            ],
        })

    def rpc(operation, *, id, cookie):
        received.append((operation, id))
        assert cookie == ""
        if operation == "detail":
            return {"body": {"songs": [{"id": int(id), "name": "合成歌曲" if id != "61" else "合成歌曲（现场）",
                     "ar": [{"name": "合成歌手", "id": 3}], "dt": 180000, "al": {"id": 4}, "isrc": "CNA123456789"}]}}
        assert operation == "lyrics"
        raw = "[00:05.00]合成一句\n[00:10.00]合成二句\n[00:15.00]合成三句" if id == "62" else ""
        return {"body": {"lrc": {"lyric": raw}}}

    source = LocalSourceAPI(tmp_path / "source", tmp_path / "provider", rpc=rpc)
    owner = runtime(tmp_path / "ai", handler)
    job = {"song_id": "60", "title": "PRIVATE queue title", "audio_origin": "song",
           "requester": {"nickname": "PRIVATE guest"},
           "media": {"original": {"duration_seconds": 180.0}}}
    queue = SimpleNamespace(get=lambda _: job, jobs_dir=tmp_path)
    recovery = KtvLyricsRecovery(owner, source, queue, lambda _: {"status": "unavailable", "lines": []})
    try:
        result = recovery("synthetic-job")
        assert result["status"] == "ready"
        assert [(row["time_ms"], row["text"]) for row in result["lines"]] == [
            (5000, "合成一句"), (10000, "合成二句"), (15000, "合成三句"),
        ]
        assert received == [("lyrics", "60"), ("detail", "60"), ("detail", "61"), ("detail", "62"), ("lyrics", "62")]
        assert requested[0]["tools"][0]["filters"]["allowed_domains"] == ["music.163.com", "y.music.163.com"]
        assert requested[0]["store"] is False and "PRIVATE" not in requested[0]["input"]
        assert owner.status()["usage"]["used"]["text_tokens"] == 61
        job["audio_origin"] = "cloud"
        assert recovery("synthetic-job")["status"] == "unavailable"
        assert len(requested) == 1
    finally:
        source.close()


@pytest.mark.parametrize("selection_is_cited", [True, False])
def test_conflicting_timelines_need_an_api_choice_with_a_catalogue_citation(tmp_path, selection_is_cited):
    requests = []
    lyrics = {
        "81": {"status": "ready", "lines": [{"time_ms": at, "text": "合成歌词"} for at in (1000, 2000, 3000)]},
        "82": {"status": "ready", "lines": [{"time_ms": at, "text": "合成歌词"} for at in (5000, 7000, 9300)]},
    }
    unavailable = {"status": "unavailable", "lines": []}

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            selected = ["81", "82"]
        else:
            assert json.loads(body["input"])["candidate_song_ids"] == ["81", "82"]
            selected = ["82" if selection_is_cited else "81"]
        return httpx.Response(200, json={
            "status": "completed",
            "usage": {"input_tokens": 40, "output_tokens": 10},
            "output": [{"type": "web_search_call", "action": {
                "type": "search", "sources": [{"url": f"https://music.163.com/song?id={i}"} for i in selected],
            }}, {"type": "message", "content": [{"type": "output_text", "text": '{"song_id":"82"}'}]}],
        })

    def recording(song_id):
        return {"id": song_id, "title": "合成录音", "duration_ms": 12000, "artists": ["合成歌手"],
                "artist_ids": ("44",), "album": "10", "isrc": "CNA123456789"}

    source = SimpleNamespace(public_lyric_recording=recording,
                             public_timed_lyrics=lambda song_id, _: lyrics.get(song_id, unavailable))
    job = {"song_id": "80", "audio_origin": "song", "media": {"original": {"duration_seconds": 12.0}}}
    queue = SimpleNamespace(get=lambda _: job, jobs_dir=tmp_path)
    recovery = KtvLyricsRecovery(runtime(tmp_path / "ai", handler), source, queue, lambda _: unavailable)
    result = recovery("synthetic-job")
    assert len(requests) == 2
    assert result["lines"] == (lyrics["82"]["lines"] if selection_is_cited else [])


def test_nas_archive_lyric_recovery_retries_after_an_empty_result(tmp_path, monkeypatch):
    epoch = [1800000000]
    monkeypatch.setattr(ktv_lyrics, "time", SimpleNamespace(time=lambda: epoch[0], monotonic=time.monotonic))
    requests = []
    unavailable = {"status": "unavailable", "lines": []}
    timed = {"status": "ready", "lines": [{"time_ms": at, "text": "合成歌词"} for at in (1000, 2000, 3000)]}

    def handler(request):
        requests.append(json.loads(request.content))
        sources = [] if len(requests) == 1 else [{"url": "https://music.163.com/song?id=91"}]
        return httpx.Response(200, json={"status": "completed", "usage": {"input_tokens": 40, "output_tokens": 8},
            "output": [{"type": "web_search_call", "action": {"type": "search", "sources": sources}}]})

    def recording(song_id):
        return {"id": song_id, "title": "合成录音", "duration_ms": 12000, "artists": ["合成歌手"],
                "artist_ids": ("44",), "album": "10", "isrc": "CNA123456789"}

    source = SimpleNamespace(public_lyric_recording=recording,
                             public_timed_lyrics=lambda song_id, _: timed if song_id == "91" else unavailable)
    recovery = KtvLyricsRecovery(runtime(tmp_path / "ai", handler), source, None, None)
    task = {"song_id": "90", "source": "song", "duration_ms": 12000, "title": "PRIVATE archive name"}
    assert recovery(task) == unavailable
    assert recovery(task) == unavailable
    assert len(requests) == 1
    epoch[0] += 60
    assert recovery(task) == timed
    assert len(requests) == 2 and "PRIVATE" not in requests[-1]["input"]
