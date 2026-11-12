"""Source contracts with fake account replies and synthetic local audio only."""
import hashlib
import json
import threading
import wave
from pathlib import Path

import httpx
import pytest

from homeplane.ktv_source_api import APIAudioAdapter, LocalSourceAPI, source_url


def logged_api(tmp_path, rpc):
    api = LocalSourceAPI(tmp_path / "account", tmp_path / "provider", rpc=rpc)
    api.session_path.write_text(json.dumps({"cookie": "MUSIC_U=synthetic-test-only"}))
    api.session_path.chmod(0o600)
    return api


def test_qr_state_and_credentials_are_owner_only(tmp_path):
    replies = {"qr": {"body": {"data": {"unikey": "synthetic-test-key"}}},
               "poll": {"body": {"code": 803}, "cookie": ["MUSIC_U=synthetic; Path=/; HttpOnly"]}}
    api = LocalSourceAPI(tmp_path / "account", tmp_path / "provider", rpc=lambda op, **kw: replies[op])
    assert api.begin_login()["state"] == "waiting_scan"
    assert api.qr().startswith(b"\x89PNG")
    assert api.poll_login()["logged_in"]
    assert not api.session_path.stat().st_mode & 0o077
    assert "synthetic" not in json.dumps(api.status())
    assert "HttpOnly" not in api._session()
    assert api.poll_login()["logged_in"]


@pytest.mark.parametrize("change,code", [({"freeTrialInfo": {"end": 30}}, "source_rights_denied"),
    ({"url": None}, "source_rights_denied"), ({"id": 2}, "download_identity_mismatch"),
    ({"md5": "invalid"}, "source_file_invalid"), ({"code": 403}, "source_rights_denied")])
def test_rights_trial_and_identity_are_not_bypassed(tmp_path, change, code):
    item = {"id": 1, "code": 200, "size": 2000, "md5": "0" * 32,
            "url": "https://m.test.music.126.net/source"}
    item.update(change)
    def rpc(op, **kw):
        return {"body": {"code": 200, **({"songs": [{"id": 1, "dt": 2000}]} if op == "detail" else {"data": item})}}
    with pytest.raises(ValueError, match=code):
        logged_api(tmp_path, rpc).source("1")


@pytest.mark.parametrize("url", ["https://example.org/song", "https://music.126.net/song",
    "https://x.music.126.net@127.0.0.1/song", "file:///tmp/song", "https://x.music.126.net:8080/song"])
def test_source_destination_is_fixed_official_cdn(url):
    with pytest.raises(ValueError, match="source_file_invalid"):
        source_url(url)


def test_no_login_never_invokes_download_or_client(tmp_path):
    api = LocalSourceAPI(tmp_path / "account", tmp_path / "provider", rpc=lambda *a, **k: pytest.fail("no request"))
    with pytest.raises(ValueError, match="source_login_required"):
        APIAudioAdapter(api, tmp_path / "audio", enabled=True).acquire("1")


def test_complete_transfer_decode_cache_and_tamper(tmp_path, monkeypatch):
    import subprocess
    pcm = tmp_path / "synthetic.wav"
    with wave.open(str(pcm), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(b"\0" * 48000 * 4 * 2)
    flac = tmp_path / "synthetic.flac"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(pcm), str(flac)], check=True)
    raw = flac.read_bytes()
    class API:
        calls = 0
        def source(self, song):
            self.calls += 1
            return {"id": song, "size": len(raw), "md5": hashlib.md5(raw).hexdigest(),
                    "url": "https://m.test.music.126.net/song", "duration_seconds": 2}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw)))
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: client.stream(method, url))
    api, stages = API(), []
    adapter = APIAudioAdapter(api, tmp_path / "audio", enabled=True)
    result = adapter.acquire("1", cancel=threading.Event(), progress=stages.append)
    assert result["full_decode_verified"] and result["exact_song_id_verified"]
    assert result["source_bytes"] == len(raw) and api.calls == 1
    assert {p["phase"] for p in stages} >= {"source_authorizing", "source_downloading", "decoding"}
    assert adapter.acquire("1")["cache_hit"] and api.calls == 1
    Path(result["source_path"]).write_bytes(b"damaged")
    with pytest.raises(ValueError, match="source_file_invalid"):
        adapter.acquire("1")


def test_source_quality_defaults_to_mp3_and_is_bounded(tmp_path):
    api = LocalSourceAPI(tmp_path / 'account', tmp_path / 'provider')
    assert api.settings() == {'level':'standard'}
    assert api.configure({'level':'exhigh'}) == {'level':'exhigh'}
    assert LocalSourceAPI(tmp_path / 'account', tmp_path / 'provider').settings()['level'] == 'exhigh'
    assert not (api.root / 'settings.json').stat().st_mode & 0o077
    with pytest.raises(ValueError):
        api.configure({'level':'lossless'})
    with pytest.raises(ValueError):
        api.configure({'level':'standard','url':'https://example.test'})


def test_cloud_catalog_is_bounded_metadata_and_cloud_url_requires_uploaded_identity(tmp_path):
    calls = []
    upload = {'songId': 123, 'songName': '测试云盘歌', 'simpleSong': {'dt': 2000, 'ar': [{'name': '歌手'}]},
              'fileSize': 2000, 'privateCloud': {'md5': 'a'*32, 'userId': 'never-export'}}
    item = {'id': 123, 'size': 2000, 'md5': 'a'*32, 'code': 200,
            'url': 'https://m.test.music.126.net/cloud', 'freeTrialInfo': None}
    def rpc(op, **kw):
        calls.append(op)
        if op == 'cloud':return {'body': {'code': 200, 'data': [upload], 'hasMore': False}}
        if op == 'cloud_detail':return {'body': {'code': 200, 'data': [{k:v for k,v in upload.items() if k!='privateCloud'}]}}
        assert op == 'cloud_url'
        return {'body': {'code': 200, 'data': [item]}}
    api = logged_api(tmp_path, rpc)
    rows = api.cloud_catalog()
    assert rows == [{'song_id': '123', 'title': '测试云盘歌', 'artists': ['歌手'], 'source': 'cloud'}]
    assert 'never-export' not in str(rows)
    assert api.cloud_source('123')['requested_level'] == 'cloud_original'
    item['md5'] = 'b'*32
    with pytest.raises(ValueError, match='download_identity_mismatch'):api.cloud_source('123')
    assert 'download' not in calls and 'detail' not in calls


def test_cloud_adapter_never_reuses_public_source_manifest(tmp_path):
    root = tmp_path / 'cache'
    folder = root / '123';folder.mkdir(parents=True);root.chmod(0o700);folder.chmod(0o700)
    audio = folder / 'source.audio';audio.write_bytes(b'public-source')
    (folder / 'manifest.json').write_text(json.dumps({'song_id':'123','source':'song','source_sha256':hashlib.sha256(audio.read_bytes()).hexdigest(),'full_decode_verified':True}))
    class API:
        def cloud_source(self, song):pytest.fail('tampered cache must fail first')
    with pytest.raises(ValueError, match='source_file_invalid'):
        APIAudioAdapter(API(), root, enabled=True, source='cloud').acquire('123')


def test_cloud_lyrics_are_bound_to_uploaded_content_and_reject_placeholders(tmp_path, monkeypatch):
    lrc = '[00:10.00]合成第一句\n[00:20.00]合成第二句\n[00:30.00]合成第三句'
    calls = []
    def rpc(op, **kw):
        calls.append((op, kw.get('id')))
        return {'body': {'code': 200, 'lrc': {'lyric': lrc}}}
    api = logged_api(tmp_path, rpc)
    owned = {'md5': 'a'*32, 'title': '合成歌', 'artists': ['合成歌手'], 'duration_ms': 60000}
    path = api.root/'cloud-owned.json';path.write_text(json.dumps({'songs': {'123': owned}}))
    assert len(api.cloud_lyrics('123')['lines']) == 3
    assert len(api.cloud_lyrics('123')['lines']) == 3 and len(calls) == 1
    saved = api.root/'cloud-lyrics/123.json'
    assert saved.stat().st_mode & 0o077 == 0
    owned['md5'] = 'b'*32;path.write_text(json.dumps({'songs': {'123': owned}}))
    api.cloud_lyrics('123');assert len(calls) == 2
    api.close()


def test_cloud_lyric_fallback_requires_title_artist_and_duration(tmp_path, monkeypatch):
    monkeypatch.setattr('homeplane.ktv_search.search_songs', lambda q: [
        {'song_id': '1', 'title': '合成歌', 'artists': ['翻唱者']},
        {'song_id': '2', 'title': '合成歌 (Live)', 'artists': ['合成歌手']},
        {'song_id': '3', 'title': '合成歌', 'artists': ['合成歌手']},
        {'song_id': '4', 'title': '合成歌', 'artists': ['合成歌手']}])
    calls = []
    def rpc(op, **kw):
        identity = kw['id'];calls.append((op, identity))
        if op == 'detail':return {'body': {'songs': [{'id': int(identity), 'dt': 90000 if identity == '3' else 60500}]}}
        return {'body': {'uncollected': identity == '123', 'lrc': {'lyric': '[00:00]暂无歌词' if identity == '123' else '[00:10]第一句\n[00:20]第二句\n[00:30]第三句'}}}
    api = logged_api(tmp_path, rpc)
    (api.root/'cloud-owned.json').write_text(json.dumps({'songs': {'123': {'md5': 'a'*32, 'title': '合成歌', 'artists': ['合成歌手'], 'duration_ms': 60000}}}))
    assert api.cloud_lyrics('123')['status'] == 'ready'
    assert calls == [('lyrics', '123'), ('detail', '3'), ('detail', '4'), ('lyrics', '4')]
    assert json.loads((api.root/'cloud-lyrics/123.json').read_text())['basis'] == 'exact_title_artist_duration'
    api.close()


def test_cloud_featured_credit_matches_primary_artist_but_not_a_cover(tmp_path, monkeypatch):
    from homeplane.ktv_source_api import _same_lyric_identity
    owned={'title':'合成歌曲','artists':['原唱 (Feat. 朋友)']}
    assert _same_lyric_identity(owned,{'title':'合成歌曲','artists':['原唱']})
    assert not _same_lyric_identity(owned,{'title':'合成歌曲','artists':['翻唱者']})
    assert not _same_lyric_identity(owned,{'title':'合成歌曲 (Live)','artists':['原唱']})


def test_filename_lyric_matching_keeps_artist_and_version_constraints():
    from homeplane.ktv_source_api import _same_lyric_identity
    owned = {'title': '01. 原唱 - 合成歌曲.flac', 'artists': ['原唱']}
    assert _same_lyric_identity(owned, {'title': '合成歌曲', 'artists': ['原唱']})
    assert not _same_lyric_identity(owned, {'title': '合成歌曲', 'artists': ['翻唱者']})
    assert not _same_lyric_identity(owned, {'title': '合成歌曲 (Live)', 'artists': ['原唱']})
