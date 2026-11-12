"""Temporary fake NAS and synthetic PCM only. No mounted household data or models."""
import hashlib
import json
import shutil
import uuid
import wave

import pytest

from homeplane.ktv_archive import MediaArchive
from homeplane.ktv_cache import CacheManager
from homeplane.ktv_service import JobRequest, Queue


def setup_archive(tmp_path):
    source = tmp_path / "synthetic.wav"
    with wave.open(str(source), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(b"\0" * 1920)
    calls = []
    def acquire(*args):
        calls.append("acquire")
        return source
    queue = Queue(tmp_path / "runtime", acquire=acquire, separate=lambda *a: source,
                  transcode=lambda src, out, event: shutil.copyfile(src, out), simulated=True)
    job = queue.submit(JobRequest(song_id="1", title="合成曲目", request_id=str(uuid.uuid4())))
    queue.futures[job["id"]].result(3)
    raw = queue.root / "api-downloads/1"
    raw.mkdir(parents=True)
    shutil.copyfile(source, raw / "source.audio")
    (raw / "manifest.json").write_text(json.dumps({"song_id": "1", "full_decode_verified": True,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "source_bytes": source.stat().st_size,
        "duration_seconds": .01}))
    mount = tmp_path / "nas"
    root = mount / "private/ktv"
    root.mkdir(parents=True)
    config = tmp_path / "archive.json"
    config.write_text(json.dumps({"mount": str(mount), "root": str(root)}))
    config.chmod(0o600)
    archive = MediaArchive(queue, config, mounted=lambda path: True)
    queue.archive = archive
    prepared = tmp_path / "stems"
    prepared.mkdir()
    manager = CacheManager(queue, prepared)
    manager.archive = archive
    archive.cache_manager = manager
    return queue, archive, manager, job, calls


def test_archive_verified_and_local_clear_restores_without_provider(tmp_path):
    queue, archive, manager, job, calls = setup_archive(tmp_path)
    try:
        archive.tick()
        assert archive.status()["songs"]["1"]["state"] == "archived"
        row = manager.inventory()["entries"][0]
        assert row["nas_bytes"] > 0 and row["local_bytes"] > 0
        manager.delete(row["id"], row["revision"], local_only=True)
        assert not queue.jobs and archive._catalog("1").is_file()
        # A different request receives its own job, with NAS media restored; no model/source call.
        again = queue.submit(JobRequest(song_id="1", title="合成曲目", request_id=str(uuid.uuid4())))
        result = queue.futures[again["id"]].result(3)
        assert result["state"] == "ready" and result["cache_hit"]
        assert calls == ["acquire"]
        assert manager.inventory()["entries"][0]["nas_bytes"] > 0
    finally:
        queue.close()


def test_explicit_delete_removes_prepared_archive_but_preserves_raw_source(tmp_path):
    queue, archive, manager, job, calls = setup_archive(tmp_path)
    try:
        archive.tick()
        raw = archive.entries["1"]["files"]["source"]["file"]
        row = manager.inventory()["entries"][0]
        manager.delete(row["id"], row["revision"])
        assert not queue.jobs
        assert (archive.root / "blobs" / raw).is_file()
        assert set(archive.entries["1"]["files"]) == {"source"}
        assert manager.inventory()["entries"] == []
    finally:
        queue.close()


def test_mount_loss_does_not_claim_archival_or_remove_local(tmp_path):
    queue, archive, manager, job, calls = setup_archive(tmp_path)
    try:
        archive.mounted = lambda path: False
        archive.tick()
        assert archive.status()["songs"]["1"]["state"] == "waiting"
        assert not archive._catalog("1").exists()
        assert (queue.jobs_dir / job["id"] / "original.wav").is_file()
    finally:
        queue.close()


def test_archive_tamper_rejected_before_source_publish(tmp_path):
    queue, archive, manager, job, calls = setup_archive(tmp_path)
    try:
        archive.tick()
        item = archive.entries["1"]["files"]["source"]
        (archive.root / "blobs" / item["file"]).write_bytes(b"bad")
        destination = tmp_path / "restored"
        destination.mkdir()
        with pytest.raises(ValueError, match="archive_copy_invalid"):
            archive.restore_source("1", destination)
        assert not (destination / "1/source.audio").exists()
    finally:
        queue.close()


def test_hot_cache_trim_keeps_protected_and_nas_copy(tmp_path):
    queue, archive, manager, job, calls = setup_archive(tmp_path)
    try:
        archive.tick()
        archive.hot_limit = 0  # Tiny synthetic budget to exercise eviction, not production configuration.
        manager.protected = lambda: {job["id"]}
        archive.trim_local()
        assert job["id"] in queue.jobs
        manager.protected = lambda: set()
        archive.trim_local()
        assert job["id"] not in queue.jobs
        assert archive.summaries()["1"]["bytes"] > 0
    finally:
        queue.close()


def test_local_eviction_rejects_corrupted_nas_and_keeps_local_media(tmp_path):
    from fastapi import HTTPException
    queue, archive, manager, job, calls = setup_archive(tmp_path)
    try:
        archive.tick()
        item = archive.entries['1']['files']['instrumental']
        (archive.root / 'blobs' / item['file']).write_bytes(b'corrupted')
        row = manager.inventory()['entries'][0]
        with pytest.raises(HTTPException) as failure:
            manager.delete(row['id'], row['revision'], local_only=True)
        assert failure.value.detail == 'archive_not_ready'
        assert job['id'] in queue.jobs
        assert (queue.jobs_dir / job['id'] / 'instrumental.wav').is_file()
    finally:
        queue.close()


@pytest.mark.parametrize("preference", ["auto", "song"])
@pytest.mark.parametrize("origin", ["mv", "mv_direct", "cloud"])
def test_song_request_does_not_restore_other_audio_edition(tmp_path, preference, origin):
    queue, archive, manager, job, calls = setup_archive(tmp_path)
    try:
        queue.jobs[job["id"]]["audio_origin"] = origin
        archive.tick()
        before = set((archive.root / "blobs").iterdir())
        fresh = queue.submit(JobRequest(song_id="1", title="Song audio", request_id=str(uuid.uuid4()),
                                        source_preference=preference))
        result = queue.futures[fresh["id"]].result(3)
        assert result["state"] == "ready" and result["audio_origin"] == "song"
        assert not result["cache_hit"] and calls == ["acquire", "acquire"]
        assert before <= set((archive.root / "blobs").iterdir())
        assert (queue.jobs_dir / job["id"] / "original.wav").is_file()
    finally:
        queue.close()


def test_archive_missing_origin_cannot_relabel_same_mv_as_song(tmp_path):
    queue, archive, manager, job, calls = setup_archive(tmp_path)
    try:
        archive.tick()
        path = archive._catalog("1")
        entry = json.loads(path.read_text())
        entry.pop("audio_origin", None)
        entry["video"] = {"method": "same_mv_audio"}
        path.write_text(json.dumps(entry))
        folder = tmp_path / "restored"
        folder.mkdir()
        assert archive.restore_job("1", folder, queue.jobs[job["id"]]["binding"], audio_origin="song") is None
        assert list(folder.iterdir()) == []
    finally:
        queue.close()


def test_archive_mv_source_metadata_cannot_restore_as_raw_song(tmp_path):
    queue, archive, manager, job, calls = setup_archive(tmp_path)
    try:
        archive.tick()
        path = archive._catalog("1")
        entry = json.loads(path.read_text())
        entry.pop("audio_origin", None)
        entry["source_metadata"]["identity_basis"] = "exact_mv_id_full_decode"
        path.write_text(json.dumps(entry))
        folder = tmp_path / "restored"
        folder.mkdir()
        assert not archive.restore_source("1", folder)
        assert archive.restore_job("1", folder, queue.jobs[job["id"]]["binding"]) is None
        assert list(folder.iterdir()) == []
    finally:
        queue.close()
