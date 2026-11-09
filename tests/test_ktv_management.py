"""NAS owner storage/control tests with synthetic files and isolated SQLite."""
import hashlib
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest

from integrations.ktv_public.nas_server import Refusal, Server, Store, make_handler


def uid():
    return str(uuid.uuid4())


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "nas", "https://example.test")
    yield value
    value.coordinator.close()


def media(store, sid, used, raw=None):
    raw = raw or (b"synthetic managed media " + sid.encode())
    sha = hashlib.sha256(raw).hexdigest()
    path = store.coordinator.media_root / sha
    path.write_bytes(raw)
    with store.connection() as db:
        db.execute("INSERT OR REPLACE INTO coordinator_cache(sha,bytes,state,updated) VALUES(?,?,'ready',?)", (sha, len(raw), used))
        db.execute("INSERT INTO managed_media VALUES(?,?,?,?,?)", (sid, sha, "合成歌曲 " + sid, len(raw), used))
    return path


def selection(store, ids=None):
    return [{"id": r["id"], "revision": r["revision"]} for r in store.management.inventory()["entries"] if ids is None or r["id"] in ids]


def test_nas_batch_deletion_idempotency_and_shared_content(store):
    shared = media(store, "1", 1)
    media(store, "2", 2, shared.read_bytes())
    own = media(store, "3", 3)
    payload = {"items": selection(store, {"song:1", "song:3"}), "request_id": uid()}
    result = store.management.delete(payload)
    assert result["removed"] == 2 and own.exists() is False and shared.exists()
    assert store.management.delete(payload) == result
    with pytest.raises(Refusal) as error:
        store.management.delete({"items": selection(store), "request_id": payload["request_id"]})
    assert error.value.code == "idempotency_conflict"


def test_nas_changed_revision_and_symlink_do_not_delete(store, tmp_path):
    first, second = media(store, "1", 1), media(store, "2", 2)
    payload = {"items": selection(store), "request_id": uid()}
    second.write_bytes(b"changed")
    with pytest.raises(Refusal) as error:
        store.management.delete(payload)
    assert error.value.code == "cache_changed" and first.exists()
    second.unlink()
    outside = tmp_path / "personal"
    outside.write_bytes(b"personal")
    second.symlink_to(outside)
    assert next(r for r in store.management.inventory()["entries"] if r["id"] == "song:2")["reason"] == "unsafe"
    assert outside.read_bytes() == b"personal"


def test_nas_lru_and_current_queue_protection(store, monkeypatch):
    old, recent, protected = media(store, "1", 1), media(store, "2", 10), media(store, "3", 0)
    room = store.create()["room_id"]
    with store.connection() as db:
        db.execute("INSERT INTO songs(id,room,song_id,title,request_id,state,created) VALUES(?,?,?,?,?,'ready',0)", (uid(), room, "3", "保护曲目", uid()))
    used = store.management.inventory()["bytes"]
    limit = used - old.stat().st_size
    monkeypatch.setattr(store.management, "policy", lambda *a: {"enabled": True, "limit_bytes": limit})
    assert store.management.enforce()["state"] == "within_limit"
    assert not old.exists() and recent.exists() and protected.exists()


def test_nas_reserve_accounts_for_parallel_downloads_and_legacy_quota(store, monkeypatch):
    monkeypatch.setattr(store.management, "policy", lambda *a: {"enabled": True, "limit_bytes": 1000})
    store.management.save("external_storage", {"bytes": 800, "delegated": True})
    assert store.management.reserve({"sha256": "a" * 64, "bytes": 120})
    assert not store.management.reserve({"sha256": "b" * 64, "bytes": 120})


def test_defaults_and_owner_room_queue_controls_without_tv(store):
    assert not store.management.room()["active"]
    store.management.preferences({"theme": "festival", "music_gain": 40})
    room_id = store.create()["room_id"]
    state = store.management.room()
    assert state["status"]["theme"] == "festival" and state["status"]["controls"]["music_gain"] == 40
    with store.connection() as db:
        db.execute("INSERT INTO heartbeat VALUES(1,?)", (int(time.time()),))
    def add(sid):
        return store.management.room_action("songs", {"room_id": room_id, "value": {"song_id": sid, "title": "测试", "request_id": uid(), "source": "song"}})["id"]
    first, second = add("1"), add("2")
    state = store.management.room()["status"]
    store.management.room_action("order", {"room_id": room_id, "value": {"ids": [second, first], "revision": state["queue_revision"]}})
    state = store.management.room()["status"]
    store.management.room_action("queue-remove", {"room_id": room_id, "value": {"ids": [first, second], "revision": state["queue_revision"]}})
    assert store.management.room()["status"]["songs"] == []
    assert set(store.poll()["cancelled"]) == {first, second}
    # No command can be left waiting to play when the TV comes back tomorrow.
    with pytest.raises(Refusal) as error:
        store.management.room_action("command", {"room_id": room_id, "value": {"action": "resume", "song": "", "request_id": uid()}})
    assert error.value.code == "tv_offline"
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0


def test_queue_revision_and_playing_song_refusal(store):
    room_id = store.create()["room_id"]
    jid = uid()
    with store.connection() as db:
        db.execute("INSERT INTO songs(id,room,song_id,title,request_id,state,created) VALUES(?,?,?,?,?,'ready',0)", (jid, room_id, "1", "正在播放", uid()))
        db.execute("UPDATE rooms SET current_song=? WHERE id=?", (jid, room_id))
    with pytest.raises(Refusal) as error:
        store.management.room_action("queue-remove", {"room_id": room_id, "value": {"ids": [jid], "revision": 0}})
    assert error.value.code == "song_playing"
    with pytest.raises(Refusal) as error:
        store.management.room_action("controls", {"room_id": uid(), "value": {"mic_muted": 0}})
    assert error.value.code == "room_changed"


def test_agent_management_never_authorized_by_guest_or_tv_credentials(store):
    key = "k" * 43
    assets = Path(__file__).resolve().parents[1] / "integrations/ktv_public/web"
    server = Server(("127.0.0.1", 0), make_handler(store, key, assets))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url="http://127.0.0.1:" + str(server.server_port), trust_env=False) as client:
            payload = {"operation": "library", "value": {}}
            assert client.post("/agent/manage", json=payload).status_code == 401
            assert client.post("/agent/manage", json=payload, headers={"X-KTV-TV-Key": store.coordinator.tv_key}).status_code == 401
            assert client.post("/agent/manage", json=payload, headers={"Authorization": "Bearer " + key}).status_code == 200
            assert client.post("/agent/manage", json=payload, headers={"Authorization": "Bearer " + key, "Origin": "https://example.test"}).status_code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_bad_policy_and_preferences_are_rejected(store):
    for value in ({"enabled": 1, "limit_bytes": 100}, {"enabled": True, "limit_bytes": -1}):
        with pytest.raises(Refusal):
            store.management.policy(value)
    for value in ({"theme": []}, {"vocal_preset": {}}, {"mic_muted": 0}):
        with pytest.raises(Refusal):
            store.management.preferences(value)


def test_slow_file_deletion_does_not_hold_room_control_or_sqlite_writer(store, monkeypatch):
    path = media(store, "1", 1)
    room_id = store.create()["room_id"]
    payload = {"items": selection(store), "request_id": uid()}
    entered, release = threading.Event(), threading.Event()
    original = Path.unlink
    failures = []
    def slow_unlink(target, *args, **kwargs):
        if target == path:
            entered.set()
            assert release.wait(3)
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", slow_unlink)
    def remove():
        try:
            store.management.delete(payload)
        except Exception as error:
            failures.append(error)
    worker = threading.Thread(target=remove)
    worker.start()
    try:
        assert entered.wait(2)
        # This completes while the owner is blocked in filesystem I/O.
        before = time.monotonic()
        store.controls({"id": room_id}, {"mic_muted": 1})
        assert time.monotonic() - before < .5
        assert not release.is_set()
        with pytest.raises(Refusal) as error:
            store.add({"id": room_id}, {"song_id": "1", "title": "相同歌曲", "request_id": uid()})
        assert error.value.code == "cache_in_use"
    finally:
        release.set()
        worker.join(3)
    assert not failures and not worker.is_alive()


def test_quota_block_does_not_exhaust_media_retry_budget(store, monkeypatch):
    coordinator = store.coordinator
    sha = "a" * 64
    coordinator.worker_origin = "http://10.20.30.40:8769"
    spec = {"sha256": sha, "bytes": 100}
    row = {"origin": coordinator.worker_origin, "expires": int(time.time()) + 60, "song": uid(), "job": '{"media":{}}'}
    with store.connection() as db:
        db.execute("INSERT INTO coordinator_cache(sha,bytes,state,updated,attempts) VALUES(?,100,'pending',0,4)", (sha,))
    monkeypatch.setattr(store.management, "reserve", lambda spec: False)
    coordinator._cache_one(row, "original", spec)
    with store.connection() as db:
        record = db.execute("SELECT state,attempts FROM coordinator_cache WHERE sha=?", (sha,)).fetchone()
    assert record["state"] == "blocked" and record["attempts"] == 0
