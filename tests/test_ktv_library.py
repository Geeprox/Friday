"""Isolated synthetic files only; never the household cache, audio or a TV."""
import hashlib
import json
import threading
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from homeplane.ktv_library import SongLibrary
from homeplane.ktv_service import Queue, atomic_json, create_app


def uid():
    return str(uuid.uuid4())


def job(queue, sid, *, used=1, key=None):
    jid = uid()
    folder = queue.jobs_dir / jid
    folder.mkdir()
    media = {}
    for kind in ("original", "instrumental"):
        data = (sid + kind).encode() * 8
        (folder / (kind + ".wav")).write_bytes(data)
        media[kind] = {"filename": kind + ".wav", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "duration_seconds": 1}
    value = {"id": jid, "song_id": sid, "title": "合成歌曲 " + sid, "request_id": uid(), "state": "ready", "code": "none",
             "created": used, "last_used": used, "simulated": True, "media": media, "video": {"status": "unavailable"}}
    queue.jobs[jid], queue.events[jid] = value, threading.Event()
    atomic_json(folder / "state.json", value)
    if key:
        atomic_json(folder / "preparation.json", {"cache_key": key})
    return jid


@pytest.fixture
def library(tmp_path):
    queue = Queue(tmp_path / "runtime", acquire=lambda *a: None, separate=lambda *a: None, transcode=lambda *a: None, simulated=True)
    prepared = tmp_path / "separation"
    prepared.mkdir()
    value = SongLibrary(queue, prepared)
    yield value
    queue.close()


def test_full_song_purge_includes_all_editions_and_preserves_sessions_models_and_input(library, tmp_path):
    queue = library.queue
    key = "a" * 64
    first, second = job(queue, "1", key=key), job(queue, "1")
    job(queue, "2")
    stem = library.prepared / "prepared" / key
    stem.mkdir(parents=True)
    (stem / "vocals.wav").write_bytes(b"synthetic stem")
    for name in ("api-downloads", "cloud-downloads"):
        folder = queue.root / name / "1"
        folder.mkdir(parents=True)
        (folder / "source.audio").write_bytes(b"private managed copy")
        (folder / "manifest.json").write_text('{"song_id":"1"}')
    lyric = queue.root / "source-account/cloud-lyrics/1.json"
    lyric.parent.mkdir(parents=True)
    lyric.write_text('{}')
    session = queue.root / "source-account/session.json"
    session.write_text('{"cookie":"synthetic test credential"}')
    model = library.prepared / "models/test.weights"
    model.parent.mkdir()
    model.write_bytes(b"model must stay")
    personal = tmp_path / "personal-original.mp3"
    personal.write_bytes(b"outside the managed roots")
    plan = library.preview({"ids": ["song:1"], "scope": "mac"})
    result = library.delete({"plan": plan["plan"]})
    assert result["complete"] and result["results"]["mac"]["original_downloads_deleted"]
    assert library.delete({"plan": plan["plan"]}) == result  # lost reply retry
    assert first not in queue.jobs and second not in queue.jobs and len(queue.jobs) == 1
    assert not stem.exists() and not lyric.exists()
    assert not (queue.root / "api-downloads/1").exists() and not (queue.root / "cloud-downloads/1").exists()
    assert session.exists() and model.exists() and personal.exists()


def test_stale_preview_and_new_active_job_refuse_without_deleting(library):
    jid = job(library.queue, "1")
    plan = library.preview({"ids": ["song:1"], "scope": "mac"})
    (library.queue.jobs_dir / jid / "original.wav").write_bytes(b"changed")
    with pytest.raises(HTTPException) as error:
        library.delete({"plan": plan["plan"]})
    assert error.value.detail == "cache_changed" and jid in library.queue.jobs
    library.queue.jobs[jid]["state"] = "acquiring"
    assert not library.inventory()["entries"][0]["mac"]["can_delete"]


def test_symlinks_never_followed_and_external_file_kept(library, tmp_path):
    jid = job(library.queue, "1")
    outside = tmp_path / "keep"
    outside.write_bytes(b"untouched")
    (library.queue.jobs_dir / jid / "link").symlink_to(outside)
    row = library.inventory()["entries"][0]
    assert row["mac"]["reason"] == "unsafe"
    with pytest.raises(HTTPException):
        library.preview({"ids": ["song:1"], "scope": "mac"})
    assert outside.read_bytes() == b"untouched"


def test_batch_validation_no_partial_local_deletion_and_shared_stem_kept(library):
    key = "b" * 64
    one, two = job(library.queue, "1", key=key), job(library.queue, "2", key=key)
    stem = library.prepared / "prepared" / key
    stem.mkdir(parents=True)
    (stem / "instrumental.wav").write_bytes(b"shared")
    with pytest.raises(HTTPException):
        library.preview({"ids": ["song:1", "song:3"], "scope": "mac"})
    assert len(library.queue.jobs) == 2
    plan = library.preview({"ids": ["song:1"], "scope": "mac"})
    assert library.delete({"plan": plan["plan"]})["complete"]
    assert one not in library.queue.jobs and two in library.queue.jobs and stem.exists()
    plan = library.preview({"ids": ["song:2"], "scope": "mac"})
    assert library.delete({"plan": plan["plan"]})["complete"] and not stem.exists()


def test_mac_lru_uses_last_usage_and_protects_queue(library, monkeypatch):
    oldest, recent, protected = job(library.queue, "1", used=1), job(library.queue, "2", used=50), job(library.queue, "3", used=2)
    library.queue.jobs[protected]["state"] = "acquiring"
    used = library.local_inventory()["bytes"]
    old_bytes = next(r["bytes"] for r in library.local_inventory()["entries"] if r["id"] == "song:1")
    monkeypatch.setattr(library, "policy", lambda *a: {"enabled": True, "limit_bytes": used - old_bytes + 1})
    result = library.enforce()
    assert result["state"] == "within_limit" and oldest not in library.queue.jobs
    assert recent in library.queue.jobs and protected in library.queue.jobs


def test_policy_validation_and_persistence(library):
    value = {"enabled": True, "limit_bytes": 8 * 1024**3}
    assert library.policy(value) == value
    assert SongLibrary(library.queue, library.prepared).policy() == value
    for bad in ({"enabled": 1, "limit_bytes": 8 * 1024**3}, {"enabled": True, "limit_bytes": -1}, {"enabled": True}):
        with pytest.raises(HTTPException):
            library.policy(bad)


def test_owner_routes_require_loopback_exact_host_session_and_same_origin(library):
    job(library.queue, "1")
    origin = "http://127.0.0.1:8769"
    app = create_app(library.queue, "k" * 43, lambda: [], library=library, console_origin=origin)
    with TestClient(app, base_url=origin, client=("127.0.0.1", 49152)) as client:
        assert client.get("/v1/manage/library").status_code == 401
        assert client.get("/console").status_code == 200
        assert client.get("/v1/manage/library").status_code == 200
        assert client.post("/v1/manage/preview", json={"ids": ["song:1"], "scope": "mac"}).status_code == 403
        reply = client.post("/v1/manage/preview", headers={"Origin": origin}, json={"ids": ["song:1"], "scope": "mac"})
        assert reply.status_code == 200
        assert client.get("/v1/manage/library", headers={"Host": "evil.test"}).status_code == 404
        assert client.post("/v1/manage/delete", headers={"Origin": "https://evil.test"}, json={"plan": reply.json()["plan"]}).status_code == 403
    with TestClient(app, base_url=origin, client=("10.20.30.40", 49153)) as client:
        assert client.get("/v1/manage/library", headers={"X-KTV-Key": "k" * 43}).status_code == 404


def test_legacy_nas_purge_includes_original_versions_and_prevents_rearchival(library, tmp_path):
    from homeplane.ktv_archive import MediaArchive
    jid = job(library.queue, "1")
    mount = tmp_path / "nas"
    root = mount / "media"
    root.mkdir(parents=True)
    config = tmp_path / "archive.json"
    config.write_text(json.dumps({"mount": str(mount), "root": str(root)}))
    config.chmod(0o600)
    archive = MediaArchive(library.queue, config, mounted=lambda p: True)
    library.archive = archive
    archive.archive(library.queue.jobs[jid])
    # A retained original edition is also part of the owner deletion.
    name = "e" * 64 + ".audio"
    (root / "blobs" / name).write_bytes(b"old source edition")
    archive.entries["1"]["originals"] = [{"file": name, "bytes": 18, "sha256": "e" * 64}]
    atomic_json(archive._catalog("1"), archive.entries["1"])
    plan = library.preview({"ids": ["song:1"], "scope": "nas"})
    result = library.delete({"plan": plan["plan"]})
    assert result["complete"] and jid in library.queue.jobs
    assert not list((root / "blobs").iterdir()) and not archive._catalog("1").exists()
    with pytest.raises(ValueError, match="archive_excluded"):
        archive.archive(library.queue.jobs[jid])


def test_invalid_batch_and_plan_types_are_client_errors(library):
    for payload in ({"ids": [], "scope": "mac"}, {"ids": ["../../"], "scope": []}, {"ids": [1], "scope": "mac"}):
        with pytest.raises(HTTPException) as error:
            library.preview(payload)
        assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        library.delete({"plan": "expired"})
    assert error.value.detail == "plan_expired"


def connect_nas(library, tmp_path):
    from integrations.ktv_public.nas_server import Refusal, Store
    store = Store(tmp_path / "coordinator", "https://example.test")
    class Bridge:
        lock = threading.RLock()
        guide = None
        mapping = {}
        party = {}
        playback = {}
        archive_pending = set()
        terminal_acked = set()
        published_metadata = {}
        metadata_root = tmp_path / "publications"
        def manage(self, op, value):
            try:
                return store.management.dispatch({"operation": op, "value": value})
            except Refusal as error:
                raise HTTPException(error.status, error.code) from None
        def _save(self):
            pass
    Bridge.metadata_root.mkdir()
    library.bridge, library.direct = Bridge(), True
    return store


def test_both_hosts_lost_remote_reply_resumes_without_double_deletion(library, tmp_path):
    store = connect_nas(library, tmp_path)
    try:
        job(library.queue, "1")
        raw = b"synthetic NAS file"
        sha = hashlib.sha256(raw).hexdigest()
        path = store.coordinator.media_root / sha
        path.write_bytes(raw)
        with store.connection() as db:
            db.execute("INSERT INTO managed_media VALUES(?,?,?,?,1)", ("1", sha, "测试", len(raw)))
        plan = library.preview({"ids": ["song:1"], "scope": "both"})
        delegate = library.bridge.manage
        lost = [False]
        def unreliable(op, value):
            result = delegate(op, value)
            if op == "delete" and not lost[0]:
                lost[0] = True
                raise HTTPException(503, "nas_management_unavailable")
            return result
        library.bridge.manage = unreliable
        first = library.delete({"plan": plan["plan"]})
        assert not first["complete"] and first["failed_host"] == "nas"
        assert not path.exists() and library.queue.jobs  # no false cross-host success
        result = library.delete({"plan": plan["plan"]})
        assert result["complete"] and not library.queue.jobs
        assert result["results"]["nas"]["removed_bytes"] == len(raw)
    finally:
        store.coordinator.close()


def test_new_nas_queue_entry_protects_previously_previewed_mac_file(library, tmp_path):
    store = connect_nas(library, tmp_path)
    try:
        jid = job(library.queue, "1")
        plan = library.preview({"ids": ["song:1"], "scope": "mac"})
        room = store.create()["room_id"]
        with store.connection() as db:
            db.execute("INSERT INTO songs(id,room,song_id,title,request_id,state,created) VALUES(?,?,?,?,?,'queued',1)", (uid(), room, "1", "新增待唱", uid()))
        with pytest.raises(HTTPException):
            library.delete({"plan": plan["plan"]})
        assert jid in library.queue.jobs
    finally:
        store.coordinator.close()


def test_nas_lru_includes_legacy_sources_and_does_not_delete_mac(library, tmp_path, monkeypatch):
    from homeplane.ktv_archive import MediaArchive
    store = connect_nas(library, tmp_path)
    mount = tmp_path / "mounted"
    root = mount / "media"
    root.mkdir(parents=True)
    config = tmp_path / "archive.json"
    atomic_json(config, {"mount": str(mount), "root": str(root)})
    archive = MediaArchive(library.queue, config, mounted=lambda p: True)
    library.archive = archive
    try:
        for sid in ("1", "2"):
            jid = job(library.queue, sid, used=int(sid))
            archive.archive(library.queue.jobs[jid])
            raw = ("NAS synthetic " + sid).encode()
            sha = hashlib.sha256(raw).hexdigest()
            (store.coordinator.media_root / sha).write_bytes(raw)
            with store.connection() as db:
                db.execute("INSERT INTO managed_media VALUES(?,?,?,?,?)", (sid, sha, "测试", len(raw), int(sid)))
        before = library.inventory()
        old_bytes = next(r["nas"]["bytes"] for r in before["entries"] if r["id"] == "song:1")
        limit = before["nas"]["bytes"] - old_bytes + 1
        monkeypatch.setattr(store.management, "policy", lambda *a: {"enabled": True, "limit_bytes": limit})
        library.enforce_nas()
        after = library.inventory()
        assert after["nas"]["bytes"] <= limit
        assert next(r for r in after["entries"] if r["id"] == "song:1")["nas"] is None
        assert archive._catalog("2").is_file() and len(library.queue.jobs) == 2
        assert all((library.queue.jobs_dir / jid / "original.wav").exists() for jid in library.queue.jobs)
    finally:
        store.coordinator.close()


def test_owner_api_refuses_offline_unmute_before_forwarding_to_legacy_scene(library, tmp_path):
    store = connect_nas(library, tmp_path)
    origin = "http://127.0.0.1:8769"
    room = store.create()["room_id"]
    app = create_app(library.queue, "k" * 43, lambda: [], library=library, console_origin=origin)
    try:
        with TestClient(app, base_url=origin, client=("127.0.0.1", 49152)) as client:
            client.get("/console")
            result = client.post("/v1/manage/room/controls", headers={"Origin": origin},
                                 json={"room_id": room, "value": {"mic_muted": 0}})
            assert result.status_code == 409 and result.json()["detail"] == "tv_offline"
            assert store.management.room()["status"]["controls"]["mic_muted"] == 1
    finally:
        store.coordinator.close()


def test_legacy_nas_guide_is_bound_to_song_by_exact_local_content_hash(library, tmp_path):
    from homeplane.ktv_archive import MediaArchive
    from homeplane.ktv_guide import GuidePublisher
    jid = job(library.queue, "1")
    mount, root, guides = tmp_path / "nas", tmp_path / "nas/media", tmp_path / "nas/guides"
    root.mkdir(parents=True)
    guides.mkdir()
    config = tmp_path / "archive.json"
    atomic_json(config, {"mount": str(mount), "root": str(root)})
    atomic_json(tmp_path / "guide.json", {"mount": str(mount), "directory": str(guides)})
    archive = MediaArchive(library.queue, config, mounted=lambda p: True)
    library.archive = archive
    archive.archive(library.queue.jobs[jid])
    raw = b"synthetic-encoded-guide" * 100
    checksum = hashlib.sha256(raw).hexdigest()
    original_sha = library.queue.jobs[jid]["media"]["original"]["sha256"]
    local = library.queue.jobs_dir / jid / (".guide-original-" + original_sha + ".mp3")
    local.write_bytes(raw)
    remote = guides / (checksum + ".mp3")
    remote.write_bytes(raw)
    publisher = GuidePublisher(library.queue, local=True)
    archive.on_exclude = publisher.exclude
    try:
        inventory = library.inventory()
        assert inventory["nas"]["legacy_guide_files"] == inventory["nas"]["legacy_guide_linked_files"] == 1
        assert len(inventory["entries"]) == 1  # no hidden guide orphan or double count
        plan = library.preview({"ids": ["song:1"], "scope": "nas"})
        assert library.delete({"plan": plan["plan"]})["complete"]
        assert not remote.exists() and local.exists()  # selected host only
        assert publisher.get(jid) is None and not publisher.futures  # never regenerates deleted old cache
        assert library.inventory()["entries"][0]["nas"] is None
    finally:
        publisher.close()


def test_unassociated_legacy_guides_visible_for_batch_delete_personal_file_preserved(library, tmp_path):
    from homeplane.ktv_archive import MediaArchive
    mount, root, guides = tmp_path / "nas", tmp_path / "nas/media", tmp_path / "nas/guides"
    root.mkdir(parents=True)
    guides.mkdir()
    config = tmp_path / "archive.json"
    atomic_json(config, {"mount": str(mount), "root": str(root)})
    atomic_json(tmp_path / "guide.json", {"mount": str(mount), "directory": str(guides)})
    library.archive = MediaArchive(library.queue, config, mounted=lambda p: True)
    for sha in ("a" * 64, "b" * 64):
        (guides / (sha + ".mp3")).write_bytes(b"synthetic cached guide")
    personal = guides / "personal-input.mp3"
    personal.write_bytes(b"never a managed cache entry")
    data = library.inventory()
    assert len(data["entries"]) == 2 and all(r["kind"] == "orphan" for r in data["entries"])
    plan = library.preview({"ids": [r["id"] for r in data["entries"]], "scope": "nas"})
    assert library.delete({"plan": plan["plan"]})["complete"]
    assert list(guides.iterdir()) == [personal]


def test_purge_reconciles_completed_nas_queue_so_no_unplayable_ready_item_remains(library, tmp_path):
    store = connect_nas(library, tmp_path)
    try:
        job(library.queue, "1")
        scene = store.create()["room_id"]
        old = uid()
        raw = b"synthetic archive"
        checksum = hashlib.sha256(raw).hexdigest()
        (store.coordinator.media_root / checksum).write_bytes(raw)
        with store.connection() as db:
            db.execute("INSERT INTO managed_media VALUES(?,?,?,?,1)", ("1", checksum, "测试", len(raw)))
            db.execute("INSERT INTO songs(id,room,song_id,title,request_id,state,created) VALUES(?,?,?,?,?,'ready',1)", (old, scene, "1", "已唱歌曲", uid()))
            db.execute("UPDATE rooms SET played=? WHERE id=?", (json.dumps([old]), scene))
        plan = library.preview({"ids": ["song:1"], "scope": "both"})
        assert library.delete({"plan": plan["plan"]})["complete"]
        assert store.management.room()["status"]["songs"] == []
        with store.connection() as db:
            assert db.execute("SELECT removed FROM songs WHERE id=?", (old,)).fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0
    finally:
        store.coordinator.close()
