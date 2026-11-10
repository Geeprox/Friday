import base64
import hashlib
import json
import struct
import subprocess
import threading
import zlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from homeplane import ktv_download as download

REAL_RUN = download._run


@pytest.fixture
def environment(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir(mode=0o700)
    adapter = download.NetEaseDownloadAdapter(music, tmp_path / "cache")
    original = subprocess.run

    def synthetic_ncm(name="misleading-name.ncm", song_id="123456", duration=2000):
        metadata = {"musicId": song_id, "duration": duration, "musicName": "SYNTHETIC TRACK",
                    "artist": [["SYNTHETIC ARTIST", 1]], "format": "mp3"}
        encrypted = original(
            ["/usr/bin/openssl", "enc", "-e", "-aes-128-ecb", "-nosalt", "-K", download.NCM_METADATA_KEY.hex()],
            input=b"music:" + json.dumps(metadata).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, check=True,
        ).stdout
        blob = bytes(value ^ 0x63 for value in b"163 key(Don't modify):" + base64.b64encode(encrypted))
        path = music / name
        path.write_bytes(b"CTENFDAM\0\0" + struct.pack("<I", 16) + b"x" * 16 + struct.pack("<I", len(blob)) + blob + b"SYNTHETIC_AUDIO")
        return path

    # The mocked runner stands only for decoder/independent validators. Metadata
    # AES and its exact song identity are still parsed from synthetic NCM bytes.
    calls = []

    def runner(argv, cancel, deadline, directory, *, capture=False):
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return b"ncmdump version 1.5.1\n"
        if argv[1:] == ["-version"]:
            return (Path(argv[0]).name + " version 9.0.2 fixture\n").encode()
        if Path(argv[0]).name == "ncmdump":
            (Path(argv[-1]) / "source.mp3").write_bytes(b"SYNTHETIC_DECODED_AUDIO")
            return b""
        if Path(argv[0]).name == "ffprobe":
            return json.dumps({"streams": [{"codec_type": "audio", "codec_name": "mp3", "channels": 2,
                                              "sample_rate": "44100"}], "format": {"duration": "2.0"}}).encode()
        if Path(argv[0]).name == "ffmpeg":
            return b""
        pytest.fail("unexpected subprocess")

    monkeypatch.setattr(download, "_run", runner)
    return adapter, synthetic_ncm, calls


def test_construct_and_empty_catalog_never_launch_or_create_cache(environment, monkeypatch):
    adapter, _, _ = environment
    request = Mock(side_effect=AssertionError("catalog must not open client"))
    monkeypatch.setattr(adapter, "_request", request)
    assert not adapter.cache_dir.exists()
    assert adapter.catalog() == []
    assert not adapter.cache_dir.exists()
    assert adapter.last_catalog_report["status"] == "ready"
    result = adapter.acquire("123456")
    assert result["status"] == "waiting_download" and result["request_sent"] is False
    assert result["source_path"] is None and result["code"] == "download_unavailable"
    request.assert_not_called()


def test_catalog_uses_embedded_identity_not_filename_and_contains_no_paths(environment):
    adapter, make, _ = environment
    make("wrong-title-and-999999.ncm")
    catalog = adapter.catalog()
    assert catalog == [{"song_id": "123456", "title": "SYNTHETIC TRACK", "artists": ["SYNTHETIC ARTIST"], "duration_seconds": 2.0}]
    assert str(adapter.download_dir) not in json.dumps(catalog)
    assert adapter.last_catalog_report["source_audio_verified"] is False
    wrong = adapter.acquire("999999")
    assert wrong["status"] == "waiting_download" and wrong["download_confirmed"] is False


def test_decode_stages_copy_checks_identity_and_both_validators_then_content_bound_cache(environment):
    adapter, make, calls = environment
    source = make()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    first = adapter.acquire("123456", timeout=5)
    assert first["status"] == "ready" and first["exact_song_id_verified"] is True
    assert first["full_decode_verified"] is True and first["cache_hit"] is False
    assert first["source_sha256"] == hashlib.sha256(Path(first["source_path"]).read_bytes()).hexdigest()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    decoder = next(argv for argv in calls if Path(argv[0]).name == "ncmdump" and "-o" in argv)
    assert "-m" not in decoder and "--remove" not in decoder and Path(decoder[1]) != source
    assert any(Path(argv[0]).name == "ffprobe" and "-show_entries" in argv for argv in calls)
    assert any(Path(argv[0]).name == "ffprobe" and "-select_streams" in argv
               and argv[argv.index("-select_streams") + 1] == "a" for argv in calls)
    assert any(Path(argv[0]).name == "ffmpeg" and "-xerror" in argv and "null" in argv for argv in calls)
    assert list((adapter.cache_dir / "jobs").iterdir()) == []
    calls.clear()
    second = adapter.acquire("123456", timeout=2)
    assert second["status"] == "ready" and second["cache_hit"] is True and calls == []


def test_corrupt_cache_never_returns_old_ready_result_or_overwrites_evidence(environment):
    adapter, make, _ = environment
    make()
    result = adapter.acquire("123456", timeout=5)
    audio = Path(result["source_path"])
    audio.write_bytes(b"CORRUPTED")
    failed = adapter.acquire("123456", timeout=5)
    assert failed["status"] == "unavailable" and failed["source_path"] is None
    assert failed["code"] == "decode_failed" and audio.read_bytes() == b"CORRUPTED"


def test_multiple_matching_downloads_fail_closed(environment):
    adapter, make, _ = environment
    make("one.ncm")
    make("two.ncm")
    result = adapter.acquire("123456")
    assert result["code"] == "download_identity_mismatch" and result["source_path"] is None


def test_pre_cancelled_acquire_never_opens_client_or_creates_cache(environment, monkeypatch):
    adapter, _, _ = environment
    adapter.allow_launch = True
    monkeypatch.setattr(adapter, "_request", Mock(side_effect=AssertionError("cancelled must not launch")))
    cancel = threading.Event()
    cancel.set()
    result = adapter.acquire("123456", cancel=cancel)
    assert result["status"] == "cancelled" and result["code"] == "download_cancelled"
    assert not adapter.cache_dir.exists()


def test_one_explicit_request_then_watch_exact_new_download_without_playing(environment, monkeypatch):
    adapter, make, _ = environment
    adapter.allow_launch = True
    requested = []

    def request(song_id, *_):
        requested.append(song_id)
        make("arrived.ncm", song_id)

    monkeypatch.setattr(adapter, "_request", request)
    result = adapter.acquire("123456", timeout=5)
    assert result["status"] == "ready" and result["request_sent"] is True
    assert requested == ["123456"]
    assert json.loads(base64.b64decode(download.download_uri("123456").removeprefix("orpheus://"))) == {
        "cmd": "download", "type": "song", "id": "123456"
    }


def test_accepted_uri_is_not_download_confirmation_or_implicitly_retried(environment, monkeypatch):
    adapter, _, _ = environment
    adapter.allow_launch = True
    request = Mock()
    monkeypatch.setattr(adapter, "_request", request)
    result = adapter.acquire("123456", timeout=0.03)
    assert result["code"] == "download_timeout" and result["request_sent"] is True
    assert result["download_confirmed"] is False and result["source_path"] is None
    request.assert_called_once()


def test_catalog_entry_and_metadata_budgets_are_visible(environment, monkeypatch):
    adapter, make, _ = environment
    make("one.ncm", "1")
    make("two.ncm", "2")
    make("three.ncm", "3")
    monkeypatch.setattr(download, "MAX_FILES", 2)
    assert len(adapter.catalog()) == 2 and adapter.last_catalog_report["truncated"] is True
    monkeypatch.setattr(download, "MAX_FILES", 256)
    one = next(adapter.download_dir.iterdir())
    size = download.read_ncm_metadata(one)["metadata_bytes"]
    monkeypatch.setattr(download, "MAX_CATALOG_METADATA_BYTES", size)
    assert len(adapter.catalog()) == 1 and adapter.last_catalog_report["truncated"] is True


def test_no_symlink_sources_and_no_unbounded_ncm_metadata(environment):
    adapter, make, _ = environment
    source = make()
    alias = adapter.download_dir / "alias.ncm"
    alias.symlink_to(source)
    with pytest.raises(download.DownloadError, match="source_changed"):
        download.read_ncm_metadata(alias)
    bad = adapter.download_dir / "bad.ncm"
    bad.write_bytes(b"CTENFDAM\0\0" + struct.pack("<I", download.MAX_KEY_BYTES + 1) + b"ignored")
    with pytest.raises(download.DownloadError, match="decode_failed"):
        download.read_ncm_metadata(bad)


def test_decoder_version_mismatch_never_validates_audio(environment, monkeypatch):
    adapter, make, _ = environment
    make()
    calls = []

    def runner(argv, *_args, **_kwargs):
        calls.append(argv)
        return b"ncmdump version UNREVIEWED\n"

    monkeypatch.setattr(download, "_run", runner)
    result = adapter.acquire("123456", timeout=5)
    assert result["code"] == "decode_failed" and result["detail_code"] == "decoder_version_mismatch"
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["0", "123;open", "１２３", "9223372036854775808", True, None])
def test_non_numeric_or_out_of_range_song_ids_never_launch(environment, value):
    adapter, _, calls = environment
    result = adapter.acquire(value)
    assert result["code"] == "invalid_request" and calls == []


def test_real_mp3_attached_cover_is_not_counted_as_a_second_audio_stream(environment, monkeypatch, tmp_path):
    adapter, make, _ = environment
    if not adapter.ffmpeg.is_file() or not adapter.ffprobe.is_file():
        pytest.skip("pinned local FFmpeg tools unavailable")
    make()
    cover = tmp_path / "synthetic-cover.png"

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

    cover.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
                      + chunk(b"IDAT", zlib.compress((b"\0" + b"\x20\x50\x90" * 16) * 16)) + chunk(b"IEND", b""))
    audio = tmp_path / "synthetic-with-cover.mp3"
    subprocess.run([str(adapter.ffmpeg), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-i", str(cover), "-t", "2", "-map", "0:a", "-map", "1:v",
                    "-c:a", "libmp3lame", "-q:a", "8", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
                    "-id3v2_version", "3", str(audio)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=15, check=True)
    full = json.loads(subprocess.run([str(adapter.ffprobe), "-v", "error", "-show_streams", "-of", "json", str(audio)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True).stdout)
    assert [stream["codec_type"] for stream in full["streams"]] == ["audio", "video"]
    assert full["streams"][1]["disposition"]["attached_pic"] == 1

    def runner(argv, cancel, deadline, directory, *, capture=False):
        if Path(argv[0]).name == "ncmdump":
            if argv[1:] == ["--version"]:
                return b"ncmdump version 1.5.1\n"
            (Path(argv[-1]) / "source.mp3").write_bytes(audio.read_bytes())
            return b""
        return REAL_RUN(argv, cancel, deadline, directory, capture=capture)

    monkeypatch.setattr(download, "_run", runner)
    result = adapter.acquire("123456", timeout=20)
    assert result["status"] == "ready" and result["full_decode_verified"] is True


def test_multiple_audio_streams_still_rejected(environment, monkeypatch):
    adapter, make, _ = environment
    make()
    original = download._run

    def runner(argv, *args, **kwargs):
        if Path(argv[0]).name == "ffprobe" and "-show_entries" in argv:
            stream = {"codec_type": "audio", "codec_name": "mp3", "channels": 2, "sample_rate": "44100"}
            return json.dumps({"streams": [stream, stream], "format": {"duration": "2.0"}}).encode()
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(download, "_run", runner)
    result = adapter.acquire("123456", timeout=5)
    assert result["code"] == "decode_failed" and result["detail_code"] == "decoded_audio_or_duration_mismatch"


def test_cache_capacity_includes_all_files_and_never_evicts_or_changes_source(environment, monkeypatch):
    adapter, make, calls = environment
    source = make()
    original = source.read_bytes()
    adapter.cache_dir.mkdir(mode=0o700)
    existing = adapter.cache_dir / "preserve-existing.bin"
    existing.write_bytes(b"PRESERVE")
    monkeypatch.setattr(download, "MAX_CACHE_BYTES", existing.stat().st_size + download.CACHE_METADATA_RESERVE - 1)
    result = adapter.acquire("123456", timeout=5)
    assert result["code"] == "download_unavailable" and result["detail_code"] == "cache_capacity_exceeded"
    assert existing.read_bytes() == b"PRESERVE" and source.read_bytes() == original
    assert list(adapter.cache_dir.iterdir()) == [existing] and calls == []


def test_cache_capacity_reserves_staged_source_and_decoded_output_before_copy(environment, monkeypatch):
    adapter, make, calls = environment
    source = make()
    original = source.read_bytes()
    monkeypatch.setattr(download, "MAX_CACHE_BYTES", download.CACHE_METADATA_RESERVE + 2 * source.stat().st_size
                        + download.MAX_METADATA_BYTES - 1)
    result = adapter.acquire("123456", timeout=5)
    assert result["code"] == "download_unavailable" and result["detail_code"] == "cache_capacity_exceeded"
    assert not (adapter.cache_dir / "jobs").exists() and source.read_bytes() == original and calls == []
