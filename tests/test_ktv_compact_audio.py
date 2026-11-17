"""真实 AAC 编解码和合成 NAS 归档，不使用账户、歌曲或音频设备。"""
import copy
import hashlib
import json
import shutil
import subprocess
import threading
import uuid
import wave
from functools import partial
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from homeplane.ktv_archive import MediaArchive
from homeplane.ktv_audio import pack_playback, source_bitrate
from homeplane.ktv_service import JobRequest, Queue, create_app, transcode_pcm
from integrations.ktv_public.audio_format import compact_spec, verify_compact
from integrations.ktv_public.nas_server import Refusal, Store

ROOT = Path(__file__).resolve().parents[1]
FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="需要真实 FFmpeg 编解码器")


def source_wav(path, rate=48000, frames=146051):
    time = np.arange(frames) / rate
    # 多音与断续包络可识别零延迟相关峰，避免纯音周期造成歧义。
    signal = sum(np.sin(2 * np.pi * hz * time) for hz in (331, 977, 2141, 3577)) * .12
    signal *= np.where((time * 3).astype(int) % 3, .7, .2)
    pcm = (np.column_stack((signal, signal * .8)) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(pcm.tobytes())
    return pcm


def package(source, folder, backing=None):
    return pack_playback(source, backing or source, folder, threading.Event(), ffmpeg=FFMPEG, ffprobe=FFPROBE)


@pytest.fixture
def encoded(tmp_path):
    source = tmp_path / "source.wav"
    source_wav(source)
    folder = tmp_path / "encoded"
    folder.mkdir()
    media = package(source, folder)
    return folder, media, source


def test_lossy_source_bitrate_is_preserved_and_lossless_uses_256k(tmp_path):
    source = tmp_path / "source.wav"
    source_wav(source, rate=44100)
    mp3 = tmp_path / "source.mp3"
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", str(source), "-c:a", "libmp3lame",
                    "-b:a", "160k", str(mp3)], check=True)
    assert source_bitrate(mp3, FFPROBE) == 160000
    assert source_bitrate(source, FFPROBE) == 256000
    target = tmp_path / "lossy"
    target.mkdir()
    media = package(mp3, target)
    assert all(item["bitrate"] == 160000 for item in media.values())
    assert media["original"]["frames"] == media["instrumental"]["frames"]
    assert media["original"]["sha256"] == media["instrumental"]["sha256"]
    assert sum(item["bytes"] for item in media.values()) < media["original"]["frames"] * 8 * .2


def test_compact_container_rejects_tampered_timeline_and_implicit_trim(encoded):
    folder, media, _ = encoded
    spec = media["original"]
    assert compact_spec(spec)
    verify_compact(folder / spec["filename"], spec)
    for patch in ({"frames": spec["frames"] + 1}, {"encoder_delay": 0}, {"encoder_padding": 1024},
                  {"bitrate": 0}, {"sample_rate": 44100}):
        with pytest.raises(ValueError):
            verify_compact(folder / spec["filename"], {**spec, **patch})
    raw = bytearray((folder / spec["filename"]).read_bytes())
    location = raw.find(b"mdhd")
    raw[location + 23] ^= 1
    changed = folder / "bad.m4a"
    changed.write_bytes(raw)
    with pytest.raises(ValueError):
        verify_compact(changed, spec)


def test_cancelled_pair_is_not_published(tmp_path):
    source = tmp_path / "source.wav"
    source_wav(source)
    folder = tmp_path / "cancel"
    folder.mkdir()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        pack_playback(source, source, folder, cancel, ffmpeg=FFMPEG, ffprobe=FFPROBE)
    assert list(folder.iterdir()) == []


def test_queue_ready_media_and_local_archive_restore_are_compact_without_duplicate_source(tmp_path):
    source = tmp_path / "source.wav"
    source_wav(source, rate=44100)
    queue = Queue(tmp_path / "runtime", acquire=lambda *args: source, separate=lambda *args: source,
                  transcode=partial(transcode_pcm, ffmpeg=FFMPEG),
                  pack_audio=partial(pack_playback, ffmpeg=FFMPEG, ffprobe=FFPROBE), simulated=True)
    key = "s" * 43
    try:
        requested = queue.submit(JobRequest(song_id="123", title="合成双轨", request_id=str(uuid.uuid4()), playback_encoding="aac_lc_m4a"))
        result = queue.futures[requested["id"]].result(10)
        assert result["state"] == "ready", result
        folder = queue.jobs_dir / result["id"]
        assert {p.name for p in folder.iterdir()} == {"state.json", "original.m4a", "instrumental.m4a"}
        with TestClient(create_app(queue, key)) as client:
            for spec in result["media"].values():
                response = client.get(spec["url"], headers={"X-KTV-Key": key})
                assert response.status_code == 200 and response.headers["content-type"] == "audio/mp4"
                assert hashlib.sha256(response.content).hexdigest() == spec["sha256"]
        mount = tmp_path / "nas"
        root = mount / "archive"
        root.mkdir(parents=True)
        config = tmp_path / "archive.json"
        config.write_text(json.dumps({"mount": str(mount), "root": str(root)}))
        config.chmod(0o600)
        archive = MediaArchive(queue, config, mounted=lambda path: True)
        archived = archive.archive(queue.jobs[result["id"]])
        assert archived["state"] == "archived"
        entry = archive.entries["123"]
        assert set(entry["files"]) == {"original", "instrumental"}
        assert all(item["file"].endswith(".m4a") for item in entry["files"].values())
        target = tmp_path / "restored"
        target.mkdir()
        restored = archive.restore_job("123", target, queue.jobs[result["id"]]["binding"])
        assert restored["cache_hit"]
        assert set(restored["media"]) == {"original", "instrumental"}
        assert archive.verify_prepared("123", job=queue.jobs[result["id"]])
        for item in restored["media"].values():
            assert hashlib.sha256((target / item["filename"]).read_bytes()).hexdigest() == item["sha256"]
    finally:
        queue.close()


def test_nas_archives_one_content_blob_and_reuses_the_compressed_timeline(encoded, tmp_path, monkeypatch):
    from test_ktv_coordinator import fake_cache, manifest

    folder, media, _ = encoded
    store = Store(tmp_path / "nas", "https://example.test")
    monkeypatch.setattr(store.coordinator.cache_pool, "submit", lambda *args: None)
    try:
        payload, _, instance, party, room = manifest(store)
        payload["job"]["media"] = {
            kind: {**{k: v for k, v in spec.items() if k != "filename"}, "url": payload["job"]["media"][kind]["url"]}
            for kind, spec in media.items()
        }
        frames = media["original"]["frames"]
        payload["timeline"] = {"sample_rate": 48000, "frames": frames, "duration_ms": round(frames / 48)}
        store.coordinator.manifest(payload)
        job = store.coordinator.jobs()["jobs"][0]
        assert job["media"]["original"]["encoding"] == "aac_lc_m4a"
        assert job["timeline"]["frames"] == frames
        observed = []
        data = {kind: (folder / spec["filename"]).read_bytes() for kind, spec in media.items()}
        fake_cache(monkeypatch, data, observed)
        row = store.coordinator._row(payload["id"])
        for kind, spec in store.coordinator._specs(row).items():
            store.coordinator._cache_one(row, kind, spec)
        assert len([p for p in store.coordinator.media_root.iterdir() if len(p.name) == 64]) == 1
        path, stored = store.coordinator.media_file(payload["id"], "original")
        assert path.read_bytes() == data["original"] and stored["frames"] == frames
        # 重发同一版本不重复归档，改变单轨时间线不能混入旧的版本。
        store.coordinator.manifest(payload)
        broken = copy.deepcopy(payload)
        broken["job"]["media"]["instrumental"]["frames"] += 1
        with pytest.raises(Refusal):
            store.coordinator.manifest(broken)
        assert store.coordinator.media_file(payload["id"], "instrumental")[0] == path
    finally:
        store.coordinator.close()


def test_actual_codec_output_uses_the_same_tv_pcm_trimmer(encoded, tmp_path):
    folder, media, _ = encoded
    # 已有 JDK 可被系统发现，也可复用当前工作区的固定 Android 工具链。
    javac = shutil.which("javac")
    java = shutil.which("java")
    candidates = [parent / ".artifacts/embodied-toolchain/jdk" for parent in (ROOT, *ROOT.parents)]
    for candidate in candidates:
        if (candidate / "bin/javac").is_file():
            javac, java = str(candidate / "bin/javac"), str(candidate / "bin/java")
            break
    if not javac or not java:
        pytest.skip("需要已有 JDK 验证电视 PCM 裁切器")
    classes = tmp_path / "classes"
    classes.mkdir()
    code = ROOT / "native/KTVTV/android/src/local/friday/ktv"
    subprocess.run([javac, "--release", "8", "-d", str(classes),
                    str(code / "CompactPcmWriter.java"), str(code / "MediaTimeline.java"), str(code / "WavSource.java"),
                    str(ROOT / "native/KTVTV/tests/CompactPcmTest.java")], check=True, capture_output=True)
    for kind, spec in media.items():
        raw = tmp_path / (kind + ".pcm")
        out = tmp_path / (kind + ".wav")
        subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", str(folder / spec["filename"]),
                        "-f", "s16le", str(raw)], check=True)
        subprocess.run([java, "-cp", str(classes), "local.friday.ktv.CompactPcmTest",
                        str(raw), str(out), str(spec["frames"])], check=True, capture_output=True)
        with wave.open(str(out), "rb") as pcm:
            assert pcm.getnframes() == spec["frames"]
            decoded = np.frombuffer(pcm.readframes(pcm.getnframes()), "<i2").reshape(-1, 2).astype(float)[:, 0]
        with wave.open(str(folder / ".playback-original.wav"), "rb") as reference:
            expected = np.frombuffer(reference.readframes(reference.getnframes()), "<i2").reshape(-1, 2).astype(float)[:, 0]
        span = slice(2048, min(len(expected), 96000) - 2048)
        errors = {lag: np.mean((expected[span] - decoded[2048 + lag:span.stop + lag]) ** 2) for lag in range(-20, 21)}
        assert min(errors, key=errors.get) == 0, errors
        assert np.corrcoef(expected[span], decoded[span])[0, 1] > .99


def test_phone_guide_uses_the_same_cropped_timeline(encoded, tmp_path):
    from types import SimpleNamespace
    from homeplane.ktv_guide import GuidePublisher

    folder, media, _ = encoded
    queue = SimpleNamespace(jobs_dir=folder.parent, get=lambda job_id: {"media": media})
    guide = GuidePublisher(queue, local=True)
    guide.destination = tmp_path / "phone-guide"
    try:
        result = guide._prepare(folder.name)
        for spec in (result, result["reference"]):
            pcm = subprocess.check_output([FFMPEG, "-nostdin", "-v", "error", "-i",
                                           str(guide.destination / (spec["sha256"] + ".mp3")), "-f", "s16le", "-"])
            assert len(pcm) // 4 == media["original"]["frames"]
            assert spec["duration_ms"] == round(media["original"]["frames"] / 48)
    finally:
        guide.close()


def test_unknown_and_older_tv_keep_wav_tasks(tmp_path, monkeypatch):
    from test_ktv_coordinator import started

    store = Store(tmp_path / "nas-negotiation", "https://example.test")
    monkeypatch.setattr(store.coordinator.cache_pool, "submit", lambda *args: None)
    try:
        instance, _ = started(store)
        assert store.poll()["playback_encoding"] == "pcm_s16le_wav"
        report = {"instance": instance, "playback": True, "audio": False, "media": True, "protocol": 1}
        store.coordinator.tv_readiness(report)
        assert store.poll()["playback_encoding"] == "pcm_s16le_wav"
        store.coordinator.tv_readiness({**report, "audio_formats": ["pcm_s16le_wav", "aac_lc_m4a"]})
        assert store.poll()["playback_encoding"] == "aac_lc_m4a"
        store.coordinator.tv_readiness(report)
        assert store.poll()["playback_encoding"] == "pcm_s16le_wav"
    finally:
        store.coordinator.close()


def test_stock_migration_is_read_only_by_default_and_protects_active_songs(tmp_path, monkeypatch):
    from test_ktv_coordinator import fake_cache, manifest
    from scripts.ktv_compact_playback import plan, prepare, apply
    from scripts import ktv_compact_playback as migration

    store = Store(tmp_path / "nas-migration", "https://example.test")
    monkeypatch.setattr(store.coordinator.cache_pool, "submit", lambda *args: None)
    try:
        payload, data, _, _, room = manifest(store)
        store.coordinator.manifest(payload)
        observed = []
        fake_cache(monkeypatch, data, observed)
        row = store.coordinator._row(payload["id"])
        for kind, spec in store.coordinator._specs(row).items():
            store.coordinator._cache_one(row, kind, spec)
        with store.connection() as db:
            db.execute("INSERT INTO managed_editions SELECT song_id,source,?,timeline,job,lyrics,updated "
                       "FROM managed_editions WHERE song_id='123'", ("f" * 64,))
        before = (store.db.read_bytes(), {p.name: p.read_bytes() for p in store.coordinator.media_root.iterdir()})
        preview = plan(store.root)
        assert preview["items"][0]["protected"]
        assert (store.db.read_bytes(), {p.name: p.read_bytes() for p in store.coordinator.media_root.iterdir()}) == before
        proof = tmp_path / "physical-acceptance.json"
        proof.write_text(json.dumps({"audio_encoding": "aac_lc_m4a", "physical_tv": True,
                                     "original_backing_switch": True, "lyrics_in_sync": True}))
        stage = tmp_path / "stage"
        prepare(store.root, stage)
        assert list(stage.iterdir()) == []
        with store.connection() as db:
            db.execute("UPDATE rooms SET closed=1 WHERE id=?", (room["id"],))
        legacy = tmp_path / "legacy"
        (legacy / "blobs").mkdir(parents=True)
        (legacy / "catalog").mkdir()
        specs = payload["job"]["media"]
        files = {}
        for kind, spec in specs.items():
            name = spec["sha256"] + ".wav"
            (legacy / "blobs" / name).write_bytes(data[kind])
            files[kind] = {"file": name, "sha256": spec["sha256"], "bytes": spec["bytes"]}
        untouched = tmp_path / "personal-original.wav"
        untouched.write_bytes(data["original"])
        raw_source = b"synthetic Friday-managed downloaded source"
        raw_sha = hashlib.sha256(raw_source).hexdigest()
        raw_entry = {"file": raw_sha + ".audio", "sha256": raw_sha, "bytes": len(raw_source)}
        (legacy / "blobs" / raw_entry["file"]).write_bytes(raw_source)
        files["source"] = raw_entry
        from homeplane.ktv_service import MODEL_REVISION
        catalog = {"song_id": "123", "files": files, "media": specs, "binding": MODEL_REVISION,
                   "retained": [files["original"]], "originals": [files["instrumental"]],
                   "source_metadata": {"bitrate": 128000}}
        (legacy / "catalog/123.json").write_text(json.dumps(catalog))
        prepare(store.root, stage, legacy)
        staged = list(stage.glob("*/prepared.json"))
        assert len(staged) == 2
        first, second = [json.loads(path.read_text()) for path in staged]
        assert first["encoding_revision"] == second["encoding_revision"] == 1
        for kind in ("original", "instrumental"):
            left = staged[0].parent / first["media"][kind]["filename"]
            right = staged[1].parent / second["media"][kind]["filename"]
            assert left.stat().st_ino == right.stat().st_ino
        with store.connection() as db:
            db.execute("UPDATE rooms SET closed=0 WHERE id=?", (room["id"],))
        assert apply(store.root, stage, proof, legacy)["completed"] == 0
        with store.connection() as db:
            db.execute("UPDATE rooms SET closed=1 WHERE id=?", (room["id"],))
        real_atomic = migration.atomic
        def interrupted(path, value):
            if path.name == "applied.json":
                raise OSError("synthetic_receipt_interruption")
            real_atomic(path, value)
        monkeypatch.setattr(migration, "atomic", interrupted)
        with pytest.raises(OSError, match="synthetic_receipt"):
            apply(store.root, stage, proof, legacy)
        monkeypatch.setattr(migration, "atomic", real_atomic)
        result = apply(store.root, stage, proof, legacy)
        assert result["completed"] == 2 and result["removed_bytes"] == 2 * sum(len(v) for v in data.values()) + len(raw_source)
        assert apply(store.root, stage, proof, legacy)["completed"] == 2
        assert untouched.read_bytes() == data["original"]
        converted = json.loads((legacy / "catalog/123.json").read_text())
        assert converted["retained"] == converted["originals"] == []
        assert all(item["file"].endswith(".m4a") for item in converted["files"].values())
        restored = tmp_path / "legacy-restored"
        restored.mkdir()
        archive_config = tmp_path / "legacy-restore.json"
        archive_config.write_text(json.dumps({"mount": str(tmp_path), "root": str(legacy)}))
        archive_config.chmod(0o600)
        from types import SimpleNamespace
        archive = MediaArchive(SimpleNamespace(root=tmp_path / "runtime", jobs={}), archive_config, mounted=lambda path: True)
        job = archive.restore_job("123", restored, MODEL_REVISION)
        assert job and job["cache_hit"]
        for kind, spec in job["media"].items():
            blob = legacy / "blobs" / converted["files"][kind]["file"]
            canonical = store.coordinator.media_root / spec["sha256"]
            assert blob.stat().st_ino == canonical.stat().st_ino
            assert checksum_file(restored / spec["filename"]) == spec["sha256"]
        with store.connection() as db:
            edition = db.execute("SELECT job FROM managed_editions WHERE song_id='123'").fetchone()
        assert all(item["encoding"] == "aac_lc_m4a" for item in json.loads(edition[0])["media"].values())
        assert all(not (store.coordinator.media_root / spec["sha256"]).exists() for spec in payload["job"]["media"].values())
    finally:
        store.coordinator.close()


def checksum_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
