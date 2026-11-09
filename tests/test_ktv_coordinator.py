"""Offline NAS/TV/worker contracts with isolated files and synthetic media only."""
import base64
import copy
import hashlib
import io
import struct
import threading
import time
import uuid
import wave
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from integrations.ktv_public.nas_server import Refusal, Server, Store, make_handler

AGENT = "a" * 43
ASSETS = Path(__file__).resolve().parents[1] / "integrations/ktv_public/web"


def uid():
    return str(uuid.uuid4())


def registration(instance=None, **changes):
    result = {"instance": instance or uid(), "origin": "http://10.20.30.40:18080",
              "capabilities": {name: True for name in ("source", "model", "download", "lyrics", "media")}}
    result.update(changes)
    return result


@pytest.fixture
def store(tmp_path, monkeypatch):
    value = Store(tmp_path / "nas", "https://example.test")
    value.coordinator.refresh_health()
    # These unit tests never issue a network request. Cache workers are invoked
    # explicitly below with a memory-only HTTP response replacement.
    monkeypatch.setattr(value.coordinator.cache_pool, "submit", lambda *args: None)
    yield value
    value.coordinator.close()


def started(store):
    instance = uid()
    party = store.coordinator.start({"instance": instance, "request_id": uid()})
    return instance, party


def admitted(store, party):
    key = parse_qs(urlsplit(party["join_url"]).fragment)["k"][0]
    room = store.room(party["room_id"], key)
    return room, key, store.join(room)


def wav_bytes(seconds=2, level=0):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(struct.pack("<hh", level, level) * (48000 * seconds))
    return output.getvalue()


def manifest(store, worker=None):
    worker = worker or registration()
    store.coordinator.register_worker(worker)
    instance, party = started(store)
    room, key, guest = admitted(store, party)
    requested = store.add(room, {"song_id": "123", "title": "合成测试歌曲", "request_id": uid()}, guest)
    song = requested.get("id", requested.get("song", ""))
    if not song:
        song = store.status(room)["songs"][0]["id"]
    local = uid()
    data = {"original": wav_bytes(level=100), "instrumental": wav_bytes(level=200)}
    media = {kind: {"url": "/v1/jobs/" + local + "/media/" + kind,
                    "sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value),
                    "duration_seconds": 2, "sample_rate": 48000, "channels": 2,
                    "encoding": "pcm_s16le_wav"} for kind, value in data.items()}
    value = {"id": song, "instance": worker["instance"], "version": hashlib.sha256(b"synthetic-v1").hexdigest(),
             "timeline": {"sample_rate": 48000, "frames": 96000, "duration_ms": 2000},
             "job": {"id": song, "song_id": "123", "title": "合成测试歌曲", "state": "ready", "code": "none", "simulated": True,
                     "media": media, "video": {"status": "unavailable"}},
             "lyrics": {"lines": [{"at_ms": 0, "text": "合成测试"}]},
             "media_ticket": str(int(time.time()) + 600) + "." + "x" * 43,
             "media_expires": int(time.time()) + 600}
    return value, data, instance, party, room


def png_bytes():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff" * 64) * 64)) + chunk(b"IEND", b"")


def publish_qr(store, instance, party):
    png = png_bytes()
    return store.coordinator.qr({"room_id": party["room_id"], "instance": instance, "join_url": party["join_url"],
                                 "sha256": hashlib.sha256(png).hexdigest(), "png": base64.b64encode(png).decode()})


def test_persistent_session_survives_pause_empty_room_and_old_expiry(store):
    party = store.create()
    key = parse_qs(urlsplit(party["join_url"]).fragment)["k"][0]
    room = store.room(party["room_id"], key)
    guest = store.join(room)
    store.poll()
    store.add(room, {"song_id": "123", "title": "合成测试", "request_id": uid()}, guest)
    with store.connection() as db:
        db.execute("UPDATE rooms SET expires=?,playback_state='paused' WHERE id=?", (int(time.time()) - 90000, room["id"]))
    store.poll()
    assert store.room(room["id"], key)["closed"] == 0
    assert len(store.status(room)["songs"]) == 1
    store.playback(room["id"], {"current": "", "played": [], "state": "preparing", "last_command": 0,
                               "safety_latched": False, "applied_revision": 0, "mic_muted": True})
    store.poll()
    assert len(store.status(room)["songs"]) == 1
    reopened = Store(store.root, "https://example.test")
    try:
        assert reopened.room(room["id"], key)["closed"] == 0
        assert reopened.guest(room, guest["key"])["id"] == guest["id"]
        assert len(reopened.status(room)["songs"]) == 1
    finally:
        reopened.coordinator.close()


def test_tv_adopts_room_without_evicting_joined_guests_and_reopen_is_idempotent(store):
    old = store.create()
    room, old_key, guest = admitted(store, old)
    instance, party = started(store)
    assert party["room_id"] == old["room_id"]
    assert store.room(room["id"], old_key, guest["key"])["id"] == room["id"]
    with pytest.raises(Refusal) as error:
        store.room(room["id"], old_key)
    assert error.value.code == "room_unavailable"
    store.coordinator.tv_readiness({"instance": instance, "protocol": 1, "playback": True, "audio": True, "media": True})
    store.controls(room, {"mic_muted": 0})
    repeat = store.coordinator.start({"instance": instance, "request_id": uid()})
    assert repeat["controls"]["mic_muted"] == 0
    with store.connection() as db:
        db.execute("INSERT INTO commands(room,request_id,action,song,created) VALUES(?,?,?,?,?)", (room["id"], uid(), "replay", "", int(time.time())))
    newer = store.coordinator.start({"instance": uid(), "request_id": uid()})
    assert newer["room_id"] == party["room_id"]
    assert newer["household_id"] == party["household_id"]
    assert newer["controls"]["mic_muted"] == 1
    assert newer["commands"] == []
    with pytest.raises(Refusal) as error:
        store.coordinator.close_party(instance, {"room_id": party["room_id"]})
    assert error.value.code == "tv_instance_changed"
    store.close(room["id"])
    next_party = store.coordinator.start({"instance": uid(), "request_id": uid()})
    assert next_party["household_id"] == party["household_id"]
    assert next_party["room_id"] != party["room_id"]


def test_complete_readiness_requires_each_real_receipt_and_stales_fail_closed(store):
    coordinator = store.coordinator
    assert coordinator.readiness()["state"] == "closed"
    worker = registration()
    coordinator.register_worker(worker)
    assert not coordinator.recovery()["requested"], "phone/worker preview cannot open a session"
    instance, party = started(store)
    coordinator.tv_readiness({"instance": instance, "playback": True, "audio": True, "media": True, "protocol": 1})
    assert not coordinator.readiness()["ready"], "QR is not ready yet"
    publish_qr(store, instance, party)
    assert coordinator.readiness()["ready"]
    broken = copy.deepcopy(worker)
    broken["capabilities"]["model"] = False
    coordinator.register_worker(broken)
    assert not coordinator.readiness()["ready"]
    assert not coordinator.readiness()["gates"]["worker_model"]
    coordinator.register_worker(worker)
    coordinator.worker["at"] -= 21
    assert not coordinator.readiness()["gates"]["link_worker"]
    coordinator.register_worker(worker)
    with store.connection() as db:
        db.execute("UPDATE coordinator_session SET tv_at=tv_at-13")
    assert not coordinator.readiness()["gates"]["link_tv"]
    assert coordinator.active(), "stale online status cannot close the session"
    with pytest.raises(Refusal):
        coordinator.register_worker({**worker, "origin": "http://127.0.0.1:18080"})
    with pytest.raises(Refusal):
        coordinator.register_worker({**worker, "origin": "http://10.20.30.41:18080"})
    with store.connection() as db:
        db.execute("UPDATE coordinator_session SET opened=opened-31")
    assert not coordinator.recovery()["requested"]


class Response(io.BytesIO):
    status = 200
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def fake_cache(monkeypatch, bodies, observed):
    class Opener:
        def open(self, request, timeout):
            observed.append((request.full_url, dict(request.header_items())))
            kind = request.full_url.rsplit("/", 1)[-1]
            return Response(bodies[kind])
    monkeypatch.setattr("integrations.ktv_public.coordinator.urllib.request.build_opener", lambda *args: Opener())


def test_manifest_allows_first_play_before_archive_then_prefers_verified_nas_cache(store, monkeypatch):
    value, data, instance, party, room = manifest(store)
    result = store.coordinator.manifest(value)
    assert result["archive"] == "pending"
    job = store.coordinator.jobs()["jobs"][0]
    assert job["state"] == "ready" and job["version"] == value["version"]
    source = job["media"]["original"]["sources"][0]
    assert source["origin"] == "http://10.20.30.40:18080" and source["header"] == "X-KTV-Media"
    assert source["credential"] == value["media_ticket"]
    store.coordinator.worker["capabilities"]["model"] = False
    assert store.coordinator.jobs()["jobs"][0]["state"] == "ready", "a model fault must not withhold finished media"
    store.coordinator.worker["capabilities"]["model"] = True
    assert store.poll()["archives"][0]["state"] == "pending"
    observed = []
    fake_cache(monkeypatch, data, observed)
    row = store.coordinator._row(value["id"])
    for kind, spec in store.coordinator._specs(row).items():
        store.coordinator._cache_one(row, kind, spec)
    assert store.coordinator.archive_state(value["id"]) == "ready"
    assert store.poll()["archives"][0]["state"] == "ready"
    sources = store.coordinator.jobs()["jobs"][0]["media"]["original"]["sources"]
    assert sources[0] == {"url": "/v1/jobs/" + value["id"] + "/media/original"}
    assert len(sources) == 2
    assert all("X-ktv-media" in headers and "X-KTV-Key" not in headers for _, headers in observed)
    store.coordinator.worker["at"] -= 21
    job = store.coordinator.jobs()["jobs"][0]
    assert job["state"] == "ready" and len(job["media"]["original"]["sources"]) == 1
    assert not store.coordinator.readiness()["ready"], "cached playback is degraded, not complete worker readiness"


def test_corrupt_archive_keeps_direct_source_and_cannot_publish_wrong_bytes(store, monkeypatch):
    value, data, *_ = manifest(store)
    store.coordinator.manifest(value)
    fake_cache(monkeypatch, {kind: b"x" * len(body) for kind, body in data.items()}, [])
    row = store.coordinator._row(value["id"])
    for kind, spec in store.coordinator._specs(row).items():
        store.coordinator._cache_one(row, kind, spec)
    assert store.coordinator.archive_state(value["id"]) == "failed"
    assert store.coordinator.jobs()["jobs"][0]["state"] == "ready"
    assert store.coordinator.jobs()["jobs"][0]["media"]["original"]["sources"][0].get("origin")
    assert not list(store.coordinator.media_root.glob(".download-*"))
    with pytest.raises(Refusal) as error:
        store.coordinator.media_file(value["id"], "original")
    assert error.value.code == "media_cache_pending"


def test_manifest_identity_versions_paths_and_old_instance_are_closed(store):
    value, *_ = manifest(store)
    store.coordinator.manifest(value)
    replay = copy.deepcopy(value)
    replay["job"]["title"] = "changed"
    with pytest.raises(Refusal) as error:
        store.coordinator.manifest(replay)
    assert error.value.code == "manifest_version_conflict"
    replay["version"] = hashlib.sha256(b"version-2").hexdigest()
    store.coordinator.manifest(replay)
    with pytest.raises(Refusal) as error:
        store.coordinator.manifest(value)
    assert error.value.code == "manifest_superseded"
    unsafe = copy.deepcopy(replay)
    unsafe["version"] = "f" * 64
    unsafe["job"]["media"]["original"]["url"] = "http://example.test/private"
    with pytest.raises(Refusal) as error:
        store.coordinator.manifest(unsafe)
    assert error.value.code == "invalid_manifest_media"
    store.coordinator.register_worker(registration())
    with pytest.raises(Refusal) as error:
        store.coordinator.manifest(replay)
    assert error.value.code == "worker_instance_changed"


def test_ready_without_manifest_is_not_tv_ready_and_playback_epoch_roundtrips(store):
    value, _, instance, party, room = manifest(store)
    store.update({"updates": [{"id": value["id"], "state": "ready", "code": "none"}]})
    assert store.coordinator.jobs()["jobs"][0]["state"] == "transcoding"
    epoch = uid()
    report = {"room_id": party["room_id"], "current": value["id"], "played": [], "state": "playing", "last_command": 0,
              "safety_latched": False, "applied_revision": 0, "mic_muted": True, "playback_epoch": epoch}
    store.coordinator.playback(report, instance)
    assert store.status(room)["playback"]["playback_epoch"] == epoch
    store.coordinator.playback({**report, "state": "paused"}, instance)
    assert store.status(room)["playback"]["playback_epoch"] == epoch
    with pytest.raises(Refusal):
        store.coordinator.playback(report, uid())


def test_events_wake_on_real_control_change_and_keep_state_confirmation_separate(store):
    instance, party = started(store)
    room, _, _ = admitted(store, party)
    cursor = store.coordinator.cursor
    result = []
    thread = threading.Thread(target=lambda: result.append(store.coordinator.events(room, cursor, 1)))
    thread.start()
    receipt = store.controls(room, {"music_gain": 30})
    thread.join(2)
    assert result and result[0]["cursor"] != cursor
    assert result[0]["status"]["controls"]["control_revision"] == receipt["revision"]
    assert result[0]["status"]["playback"]["applied_revision"] < receipt["revision"]
    with pytest.raises(Refusal):
        store.coordinator.events(room, cursor, 6)


@pytest.mark.parametrize("stale_nas_health", [False, True])
def test_http_credential_scopes_direct_ownership_qr_and_protocol_routes(store, stale_nas_health):
    server = Server(("127.0.0.1", 0), make_handler(store, AGENT, ASSETS))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url="http://127.0.0.1:" + str(server.server_port), timeout=3, trust_env=False) as client:
            tv = {"X-KTV-TV-Key": store.coordinator.tv_key}
            agent = {"Authorization": "Bearer " + AGENT}
            assert client.get("/v1/status").status_code == 404
            assert client.get("/v1/status", headers={"X-KTV-TV-Key": AGENT}).status_code == 401
            assert client.get("/agent/tasks", headers=tv).status_code == 401
            instance = uid()
            party = client.post("/v1/party", headers=tv, json={"instance": instance, "request_id": uid()}).json()
            tv["X-KTV-TV-Instance"] = instance
            assert party["active"]
            assert client.get("/v1/party/qr", headers=tv).status_code == 503
            publish_qr(store, instance, party)
            assert client.get("/v1/party/qr", headers=tv).content == png_bytes()
            assert client.post("/agent/rooms/" + party["room_id"] + "/playback", headers=agent, json={}).status_code == 409
            assert client.post("/agent/rooms/" + party["room_id"] + "/close", headers=agent, json={}).status_code == 409
            assert client.post("/agent/worker", headers=agent, json=registration()).status_code == 200
            if stale_nas_health:
                store.coordinator.health_at = time.monotonic() - 16
                stale = client.get("/v1/status", headers=tv).json()["readiness"]
                assert not stale["gates"]["nas_data"] and not stale["gates"]["nas_media"]
            # 此协议夹具关闭后台刷新；在要求完整就绪前采集真实的新文件/数据库证据。
            store.coordinator.refresh_health()
            response = client.post("/v1/tv/readiness", headers=tv, json={"instance": instance, "protocol": 1, "playback": True, "audio": True, "media": True})
            assert response.status_code == 200 and response.json()["ready"], response.json()
            assert client.get("/agent/tasks", headers=agent).json()["rooms"][0]["direct"] is True
            assert client.post("/agent/manifests", headers=agent, content='{"x":NaN}').status_code == 400
            assert client.post("/v1/party/close", headers=tv, json={"room_id": party["room_id"]}).json()["closed"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_late_start_and_close_cannot_reopen_or_end_a_replacement_session(store):
    instance, request = uid(), uid()
    party = store.coordinator.start({"instance": instance, "request_id": request})
    same = store.coordinator.start({"instance": instance, "request_id": request})
    assert same["room_id"] == party["room_id"]
    store.coordinator.close_party(instance, {"room_id": party["room_id"]})
    with pytest.raises(Refusal) as error:
        store.coordinator.start({"instance": instance, "request_id": request})
    assert error.value.code == "room_closed"
    replacement = store.coordinator.start({"instance": instance, "request_id": uid()})
    with pytest.raises(Refusal) as error:
        store.coordinator.close_party(instance, {"room_id": party["room_id"]})
    assert error.value.code == "room_changed"
    assert store.coordinator.party()["room_id"] == replacement["room_id"]
    old_request = uid()
    store.coordinator.start({"instance": instance, "request_id": old_request})
    store.coordinator.start({"instance": uid(), "request_id": uid()})
    with pytest.raises(Refusal) as error:
        store.coordinator.start({"instance": instance, "request_id": old_request})
    assert error.value.code == "tv_instance_changed"


def test_nas_restart_keeps_instance_queue_and_allows_readiness_renewal(store):
    value, _, instance, party, room = manifest(store)
    store.coordinator.manifest(value)
    store.coordinator.tv_readiness({"instance": instance, "protocol": 1, "playback": True, "audio": True, "media": True})
    reopened = Store(store.root, store.origin)
    try:
        assert reopened.coordinator.party()["room_id"] == party["room_id"]
        assert reopened.coordinator.active()["instance"] == instance
        assert not reopened.coordinator.readiness()["gates"]["link_tv"]
        reopened.coordinator.tv_readiness({"instance": instance, "protocol": 1, "playback": True, "audio": True, "media": True})
        assert reopened.coordinator.readiness()["gates"]["link_tv"]
        assert reopened.status(room)["songs"][0]["id"] == value["id"]
    finally:
        reopened.coordinator.close()


def test_recovery_is_tv_requested_bounded_pinned_and_listener_ack_is_not_ready(store, monkeypatch):
    import urllib.error
    worker = registration()
    worker["capabilities"]["model"] = False
    store.coordinator.register_worker(worker)
    submitted = []
    monkeypatch.setattr(store.coordinator.recovery_pool, "submit", lambda *args: submitted.append(args))
    store.coordinator.recovery_enabled = True
    store.coordinator.status()
    assert submitted == [], "worker/phone status cannot trigger recovery"
    instance, request = uid(), uid()
    party = store.coordinator.start({"instance": instance, "request_id": request})
    assert len(submitted) == 1
    store.coordinator.start({"instance": instance, "request_id": request})
    assert len(submitted) == 1, "same-request retries cannot multiply recovery jobs"
    times, attempts = [0.0], []
    class Cancel:
        def wait(self, duration):
            times[0] += duration
            return False
    class Opener:
        def open(self, request, timeout):
            attempts.append((times[0], request.full_url, request.header_items(), timeout))
            raise urllib.error.HTTPError(request.full_url, 401, "synthetic", {}, None)
    monkeypatch.setattr("integrations.ktv_public.coordinator.time.monotonic", lambda: times[0])
    monkeypatch.setattr("integrations.ktv_public.coordinator.urllib.request.build_opener", lambda *args: Opener())
    store.coordinator._recover_worker((instance, request), Cancel())
    assert [row[0] for row in attempts] == [0, 2, 6, 14]
    assert all(url == worker["origin"] + "/v1/status" and headers == [] and timeout == 1 for _, url, headers, timeout in attempts)
    assert store.coordinator.recovery()["attempt"] == 4
    assert store.coordinator.recovery()["last_result"] == "listener_response"
    assert not store.coordinator.readiness()["ready"], "401 is only listener activation evidence"
    assert store.coordinator.recovery()["cannot_wake"] is True
    store.coordinator.close_party(instance, {"room_id": party["room_id"]})
    assert not store.coordinator.recovery()["requested"]


def test_movie_alignment_evidence_is_bound_to_audio_and_preserved(store):
    value, *_ = manifest(store)
    original = value["job"]["media"]["original"]
    local = original["url"].split("/")[3]
    video = {"status": "ready", "url": "/v1/jobs/" + local + "/media/video", "sha256": "f" * 64,
             "bytes": 100, "duration_seconds": 2, "encoding": "h264_mp4_video_only",
             "source_audio_sha256": original["sha256"], "video_offset_ms": 30,
             "score_min": .97, "window_count": 6, "offset_drift_ms": 10,
             "method": "multi_window_waveform_correlation"}
    value["job"]["video"] = video
    store.coordinator.manifest(value)
    received = store.coordinator.jobs()["jobs"][0]["video"]
    for field in ("source_audio_sha256", "video_offset_ms", "score_min", "window_count", "offset_drift_ms", "method"):
        assert received[field] == video[field]
    invalid = copy.deepcopy(value)
    invalid["version"] = "e" * 64
    invalid["job"]["video"]["source_audio_sha256"] = "0" * 64
    with pytest.raises(Refusal) as error:
        store.coordinator.manifest(invalid)
    assert error.value.code == "invalid_manifest_video"
    alternate = copy.deepcopy(value)
    alternate["version"] = "d" * 64
    alternate["job"]["video"].update(method="same_mv_audio", video_offset_ms=0, derived_from_video_sha256="c" * 64)
    store.coordinator.manifest(alternate)
    assert store.coordinator.jobs()["jobs"][0]["video"]["derived_from_video_sha256"] == "c" * 64


def test_phone_guide_is_published_only_after_both_checked_small_caches_are_ready(store, monkeypatch):
    value, data, instance, party, room = manifest(store)
    local = value["job"]["media"]["original"]["url"].split("/")[3]
    # This tests transport/hash/publication, not MP3 decoding or acoustics.
    compressed = {"guide_original": b"ID3" + b"o" * 2048, "guide_reference": b"ID3" + b"i" * 2048}
    specs = {kind: {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "duration_ms": 2000,
                    "url": "/v1/jobs/" + local + "/media/" + kind} for kind, body in compressed.items()}
    value["guide"] = dict(specs["guide_original"], reference=dict(specs["guide_reference"], kind="accompaniment"))
    store.coordinator.manifest(value)
    store.coordinator.playback({"room_id": party["room_id"], "current": value["id"], "played": [], "state": "playing", "last_command": 0,
                               "safety_latched": False, "applied_revision": 0, "mic_muted": True}, instance)
    assert store.status(room)["playback"]["guide"] is None
    fake_cache(monkeypatch, {**data, **compressed}, [])
    row = store.coordinator._row(value["id"])
    store.coordinator._cache_one(row, "guide_original", store.coordinator._specs(row)["guide_original"])
    assert store.status(room)["playback"]["guide"] is None
    store.coordinator._cache_one(row, "guide_reference", store.coordinator._specs(row)["guide_reference"])
    published = store.status(room)["playback"]["guide"]
    assert published["sha256"] == specs["guide_original"]["sha256"]
    assert published["reference"]["kind"] == "accompaniment"
    file, size = store.guide_file(room, value["id"], "reference")
    assert size == len(compressed["guide_reference"]) and file.read_bytes() == compressed["guide_reference"]


def test_changed_cache_identity_is_not_advertised_or_acknowledged_and_hashes_off_request_path(store, monkeypatch):
    value, data, *_ = manifest(store)
    store.coordinator.manifest(value)
    fake_cache(monkeypatch, data, [])
    row = store.coordinator._row(value["id"])
    for kind, spec in store.coordinator._specs(row).items():
        store.coordinator._cache_one(row, kind, spec)
    original = value["job"]["media"]["original"]
    path = store.coordinator.media_root / original["sha256"]
    path.chmod(0o600)
    path.write_bytes(b"x" * original["bytes"])
    path.chmod(0o400)
    with monkeypatch.context() as patched:
        def forbidden(*args):
            raise AssertionError("request path cannot hash large media")
        patched.setattr("integrations.ktv_public.coordinator.hashlib.sha256", forbidden)
        assert store.coordinator.archive_state(value["id"]) == "pending"
        sources = store.coordinator.jobs()["jobs"][0]["media"]["original"]["sources"]
        assert len(sources) == 1 and sources[0].get("origin")
        assert store.controls({"id": store.coordinator.active()["room"]}, {"music_gain": 12})["accepted"]
    assert original["sha256"] in store.coordinator.verify_pending
    store.coordinator._verify_cached(original)
    assert store.coordinator.archive_state(value["id"]) != "ready"
    store.coordinator._cache_one(row, "original", original)
    assert store.coordinator.archive_state(value["id"]) == "ready"


def test_cached_media_range_and_head_keep_exact_lengths_and_scopes(store, monkeypatch):
    value, data, *_ = manifest(store)
    store.coordinator.manifest(value)
    fake_cache(monkeypatch, data, [])
    row = store.coordinator._row(value["id"])
    for kind, spec in store.coordinator._specs(row).items():
        store.coordinator._cache_one(row, kind, spec)
    server = Server(("127.0.0.1", 0), make_handler(store, AGENT, ASSETS))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url="http://127.0.0.1:" + str(server.server_port), timeout=3, trust_env=False) as client:
            path = "/v1/jobs/" + value["id"] + "/media/original"
            tv = {"X-KTV-TV-Key": store.coordinator.tv_key}
            assert client.get(path).status_code == 404
            full = client.get(path, headers=tv)
            assert full.status_code == 200 and full.content == data["original"]
            assert int(full.headers["Content-Length"]) == len(data["original"])
            part = client.get(path, headers={**tv, "Range": "bytes=44-51"})
            assert part.status_code == 206 and part.content == data["original"][44:52]
            assert part.headers["Content-Range"] == "bytes 44-51/" + str(len(data["original"]))
            head = client.head(path, headers={**tv, "Range": "bytes=44-51"})
            assert head.status_code == 206 and int(head.headers["Content-Length"]) == 8 and head.content == b""
            assert client.get(path, headers={**tv, "Range": "bytes=-10"}).content == data["original"][-10:]
            for supplied in ("bytes=99999999-", "bytes=10-2", "bytes=0-1,4-5", "bytes=-0"):
                refused = client.get(path, headers={**tv, "Range": supplied})
                assert refused.status_code == 416 and refused.headers["Content-Range"].startswith("bytes */")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_explicit_lan_listener_is_pinned_and_partial_bind_failure_closes_loopback(store):
    from integrations.ktv_public.nas_server import create_listeners, listener_addresses
    assert listener_addresses({}) == [("127.0.0.1", 19089)]
    assert listener_addresses({"lan_host": "10.20.30.50", "port": 19090}) == [("127.0.0.1", 19090), ("10.20.30.50", 19090)]
    for address in ("0.0.0.0", "127.0.0.1", "169.254.1.2", "8.8.8.8", "example.test", "10.20.30.50/path", "[::1]"):
        with pytest.raises(ValueError):
            listener_addresses({"lan_host": address})
    opened = []
    class FakeServer:
        def __init__(self, address, handler):
            if address[0] != "127.0.0.1":
                raise OSError("synthetic bind failure")
            self.closed = False
            opened.append(self)
        def server_close(self):
            self.closed = True
    with pytest.raises(OSError):
        create_listeners({"lan_host": "10.20.30.50"}, store, AGENT, ASSETS, FakeServer)
    assert len(opened) == 1 and opened[0].closed


def test_qr_loss_or_same_size_damage_requests_worker_republication(store):
    instance, party = started(store)
    publish_qr(store, instance, party)
    assert store.coordinator.qr_ready()
    assert store.poll()["rooms"][0]["qr_ready"] is True
    path = store.coordinator.qr_file()
    path.write_bytes(b"x" * path.stat().st_size)
    assert not store.coordinator.qr_ready()
    assert store.poll()["rooms"][0]["qr_ready"] is False
    publish_qr(store, instance, party)
    assert store.coordinator.qr_ready()
    path.unlink()
    assert not store.coordinator.qr_ready()


def test_status_queues_one_health_refresh_without_waiting_for_disk_and_stale_proof_is_not_ready(store, monkeypatch):
    started(store)
    submitted = []
    store.coordinator.background_health = True
    store.coordinator.health_at = 0
    monkeypatch.setattr(store.coordinator.health_pool, "submit", lambda *args: submitted.append(args))
    def forbidden(*args):
        raise AssertionError("status cannot synchronously probe/fsync the media volume")
    monkeypatch.setattr(store.coordinator, "refresh_health", forbidden)
    monkeypatch.setattr(store.coordinator, "_nas_media_probe", forbidden)
    for _ in range(5):
        snapshot = store.coordinator.status()
        assert not snapshot["readiness"]["gates"]["nas_data"]
        assert not snapshot["readiness"]["gates"]["nas_media"]
    assert len(submitted) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_health_refresh_publishes_only_completed_snapshot_and_retains_expiry(store, monkeypatch, failed):
    coordinator = store.coordinator
    coordinator.background_health = False
    previous_at = coordinator.health_at
    assert coordinator.readiness()["gates"]["nas_media"]
    entered, release = threading.Event(), threading.Event()
    from integrations.ktv_public import coordinator as module
    original_fsync = module.os.fsync

    def paused_fsync(fd):
        entered.set()
        assert release.wait(3), "synthetic fsync must be released"
        if failed:
            raise OSError("synthetic storage failure")
        return original_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", paused_fsync)
    thread = threading.Thread(target=coordinator.refresh_health, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        during = coordinator.readiness()["gates"]
        assert during["nas_data"] and during["nas_media"]
        with coordinator.lock:
            coordinator.health_at = time.monotonic() - 16
        expired = coordinator.readiness()["gates"]
        assert not expired["nas_data"] and not expired["nas_media"]
        with coordinator.lock:
            coordinator.health_at = previous_at
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    after = coordinator.readiness()["gates"]
    assert after["nas_data"]
    assert after["nas_media"] is (not failed)
    assert coordinator.health_at > previous_at


def test_nas_supervisor_restart_budget_is_bounded_and_resets_only_after_established_run():
    from integrations.ktv_public.supervisor import RestartBudget
    budget = RestartBudget()
    assert [budget.exited(1) for _ in range(5)] == [2, 4, 8, 16, None]
    assert budget.exited(1) is None
    assert budget.exited(300) == 2


def test_unmute_requires_live_tv_and_unacknowledged_gesture_cannot_arrive_after_reconnect(store):
    instance, party = started(store)
    room, _, _ = admitted(store, party)
    with pytest.raises(Refusal) as error:
        store.controls(room, {"mic_muted": 0})
    assert error.value.code == "tv_offline"
    store.coordinator.tv_readiness({"instance": instance, "protocol": 1, "playback": True, "audio": True, "media": True})
    request = store.controls(room, {"mic_muted": 0})
    with store.connection() as db:
        db.execute("UPDATE rooms SET mic_request_at=mic_request_at-6 WHERE id=?", (room["id"],))
    resumed = store.coordinator.party()
    assert resumed["controls"]["mic_muted"] == 1
    assert resumed["controls"]["control_revision"] > request["revision"]


def test_only_fresh_actual_unmute_confirmation_satisfies_pending_enable_deadline(store):
    instance, party = started(store)
    room, _, _ = admitted(store, party)
    store.coordinator.tv_readiness({"instance": instance, "protocol": 1, "playback": True, "audio": True, "media": True})
    def report(revision, confirmed):
        return {"room_id": party["room_id"], "current": "", "played": [], "state": "idle", "last_command": 0,
                "safety_latched": False, "applied_revision": revision, "mic_muted": False,
                "audio_capabilities": {"vocal_presets": ["dry"], "sfx": False,
                                       "voice_protection": {"state": "monitoring", "effective_gain": 20, "mute_confirmed": confirmed}}}
    request = store.controls(room, {"mic_muted": 0})
    store.coordinator.playback(report(request["revision"], False), instance)
    with store.connection() as db:
        assert db.execute("SELECT mic_request_at FROM rooms WHERE id=?", (room["id"],)).fetchone()[0] > 0
        db.execute("UPDATE rooms SET mic_request_at=mic_request_at-6 WHERE id=?", (room["id"],))
    assert store.coordinator.party()["controls"]["mic_muted"] == 1
    request = store.controls(room, {"mic_muted": 0})
    store.coordinator.playback(report(request["revision"] - 1, True), instance)
    with store.connection() as db:
        assert db.execute("SELECT mic_request_at FROM rooms WHERE id=?", (room["id"],)).fetchone()[0] > 0
    store.coordinator.playback(report(request["revision"], True), instance)
    with store.connection() as db:
        assert db.execute("SELECT mic_request_at FROM rooms WHERE id=?", (room["id"],)).fetchone()[0] == 0
    store.poll()
    assert store.coordinator.party()["controls"]["mic_muted"] == 0


def test_worker_leases_only_the_direct_room_after_adopting_latest_legacy_session(store):
    store.poll()
    older = store.create()
    old_room, _, old_guest = admitted(store, older)
    first = store.add(old_room, {"song_id": "101", "title": "旧场合成歌", "request_id": uid()}, old_guest)
    newer = store.create()
    new_room, _, new_guest = admitted(store, newer)
    second = store.add(new_room, {"song_id": "102", "title": "当前合成歌", "request_id": uid()}, new_guest)
    store.coordinator.register_worker(registration())
    _, adopted = started(store)
    assert adopted["room_id"] == newer["room_id"]
    polled = store.poll()
    assert [item["id"] for item in polled["tasks"]] == [second["id"]]
    assert [item["room_id"] for item in polled["rooms"] if item.get("direct")] == [newer["room_id"]]
    with store.connection() as db:
        old_song = db.execute("SELECT state,lease FROM songs WHERE id=?", (first["id"],)).fetchone()
        assert old_song["state"] == "queued" and old_song["lease"] == 0
        assert db.execute("SELECT closed FROM rooms WHERE id=?", (older["room_id"],)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM guests WHERE room=?", (older["room_id"],)).fetchone()[0] == 1
    assert store.poll()["tasks"] == [], "current leased tasks are not duplicated while old queues stay untouched"


def test_explicitly_closed_retention_purges_room_metadata_and_keeps_active_scene_and_content_cache(store, monkeypatch):
    old, data, old_instance, old_party, _ = manifest(store)
    store.coordinator.manifest(old)
    fake_cache(monkeypatch, data, [])
    row = store.coordinator._row(old["id"])
    for kind, spec in store.coordinator._specs(row).items():
        store.coordinator._cache_one(row, kind, spec)
    with store.connection() as db:
        db.execute("INSERT INTO room_tokens VALUES(?,?,?)", (old_party["room_id"], "old-token-hash", 2147483647))
        db.execute("INSERT INTO retries VALUES(?,?,?)", (old_party["room_id"], uid(), old["id"]))
        db.execute("INSERT INTO commands(room,request_id,action,song,created) VALUES(?,?,?,?,?)", (old_party["room_id"], uid(), "pause", "", 0))
    store.coordinator.close_party(old_instance, {"room_id": old_party["room_id"]})
    current, _, _, current_party, _ = manifest(store)
    store.coordinator.manifest(current)
    independent_sha = "a" * 64
    independent_content = b"independent synthetic archive"
    (store.coordinator.media_root / independent_sha).write_bytes(independent_content)
    with store.connection() as db:
        db.execute("INSERT INTO room_tokens VALUES(?,?,?)", (current_party["room_id"], "current-token-hash", 2147483647))
        db.execute("INSERT INTO retries VALUES(?,?,?)", (current_party["room_id"], uid(), current["id"]))
        db.execute("INSERT INTO commands(room,request_id,action,song,created) VALUES(?,?,?,?,?)", (current_party["room_id"], uid(), "pause", "", 0))
        db.execute("INSERT INTO coordinator_cache(sha,bytes,state,updated,attempts) VALUES(?,?,'pending',0,0)", (independent_sha, len(independent_content)))
        # Even an old compatibility timestamp cannot expire an active scene.
        db.execute("UPDATE rooms SET expires=0 WHERE id=?", (current_party["room_id"],))
        assert db.execute("SELECT count(*) FROM coordinator_manifests WHERE song=?", (old["id"],)).fetchone()[0] == 1
        db.execute("UPDATE rooms SET expires=? WHERE id=?", (int(time.time()) - 86401, old_party["room_id"]))
    store.poll()
    with store.connection() as db:
        for table in ("coordinator_manifests", "coordinator_manifest_versions"):
            assert db.execute("SELECT count(*) FROM " + table + " WHERE song=?", (old["id"],)).fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM " + table + " WHERE song=?", (current["id"],)).fetchone()[0] == 1
        for table in ("room_tokens", "coordinator_start_requests", "retries", "commands", "songs", "song_requests", "guests"):
            assert db.execute("SELECT count(*) FROM " + table + " WHERE room=?", (old_party["room_id"],)).fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM " + table + " WHERE room=?", (current_party["room_id"],)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM rooms WHERE id=?", (old_party["room_id"],)).fetchone()[0] == 0
        assert db.execute("SELECT room FROM coordinator_session WHERE id=1").fetchone()[0] == current_party["room_id"]
        assert db.execute("SELECT bytes FROM coordinator_cache WHERE sha=?", (independent_sha,)).fetchone()[0] == len(independent_content)
    assert (store.coordinator.media_root / independent_sha).read_bytes() == independent_content


def test_archive_check_survives_scene_end_pending_completion_and_metadata_retention(store, monkeypatch):
    value, data, instance, party, _ = manifest(store)
    store.coordinator.manifest(value)
    row = store.coordinator._row(value["id"])
    files = [{k: spec[k] for k in ("sha256", "bytes")} for spec in value["job"]["media"].values()]
    local_id = value["job"]["media"]["original"]["url"].split("/")[3]
    check = {"items": [{"id": local_id, "files": files}]}
    assert store.coordinator.archive_check(check)["items"] == [{"id": local_id, "state": "pending"}]
    updated = copy.deepcopy(value)
    updated["version"] = "c" * 64
    updated["lyrics"] = {"lines": [{"at_ms": 0, "text": "补全的合成歌词"}]}
    store.coordinator.manifest(updated)
    store.coordinator.close_party(instance, {"room_id": party["room_id"]})
    fake_cache(monkeypatch, data, [])
    for kind, spec in store.coordinator._specs(row).items():
        store.coordinator._cache_one(row, kind, spec)
    assert store.coordinator.archive_check(check)["items"] == [{"id": local_id, "state": "ready"}]
    with store.connection() as db:
        db.execute("UPDATE rooms SET expires=? WHERE id=?", (int(time.time()) - 86401, party["room_id"]))
    store.poll()
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM coordinator_session").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM coordinator_manifests").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM coordinator_start_requests").fetchone()[0] == 0
    assert store.coordinator.archive_check(check)["items"] == [{"id": local_id, "state": "ready"}]


def test_archive_check_is_bounded_agent_only_and_changed_cache_checks_cannot_start_downloads(store, monkeypatch):
    value, data, *_ = manifest(store)
    store.coordinator.manifest(value)
    row = store.coordinator._row(value["id"])
    fake_cache(monkeypatch, data, [])
    for kind, spec in store.coordinator._specs(row).items():
        store.coordinator._cache_one(row, kind, spec)
    spec = value["job"]["media"]["original"]
    item = {"id": uid(), "files": [{k: spec[k] for k in ("sha256", "bytes")}]}
    assert store.coordinator.archive_check({"items": [item]})["items"][0]["state"] == "ready"
    wrong_size = copy.deepcopy(item)
    wrong_size["files"][0]["bytes"] += 1
    assert store.coordinator.archive_check({"items": [wrong_size]})["items"][0]["state"] == "failed"
    assert store.coordinator.archive_check({"items": [item]})["items"][0]["state"] == "ready", "wrong claimed length cannot invalidate good cache proof"
    for payload in ({"items": [dict(item, id=uid()) for _ in range(17)]},
                    {"items": [dict(item, files=item["files"] * 6)]},
                    {"items": [dict(item, files=[dict(item["files"][0], url="http://example.test")])]}):
        with pytest.raises(Refusal) as error:
            store.coordinator.archive_check(payload)
        assert error.value.code == "invalid_archive_check"
    path = store.coordinator.media_root / spec["sha256"]
    path.chmod(0o600)
    path.write_bytes(b"x" * spec["bytes"])
    queued = []
    monkeypatch.setattr(store.coordinator.cache_pool, "submit", lambda *args: queued.append(args))
    def no_download(*args):
        raise AssertionError("archive lookup must not start or retry downloads")
    monkeypatch.setattr(store.coordinator, "_queue_cache", no_download)
    assert store.coordinator.archive_check({"items": [item]})["items"][0]["state"] == "pending"
    assert len(queued) == 1 and queued[0][2] is False
    queued[0][0](*queued[0][1:])
    assert store.coordinator.archive_check({"items": [item]})["items"][0]["state"] == "failed"
    missing = {"id": uid(), "files": [{"sha256": "b" * 64, "bytes": 100}]}
    assert store.coordinator.archive_check({"items": [missing]})["items"][0]["state"] == "pending"
    server = Server(("127.0.0.1", 0), make_handler(store, AGENT, ASSETS))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url="http://127.0.0.1:" + str(server.server_port), timeout=3, trust_env=False) as client:
            endpoint = "/agent/archive-check"
            assert client.post(endpoint, headers={"X-KTV-TV-Key": store.coordinator.tv_key}, json={"items": []}).status_code == 401
            assert client.post(endpoint, json={"items": []}).status_code == 401
            response = client.post(endpoint, headers={"Authorization": "Bearer " + AGENT}, json={"items": [missing]})
            assert response.status_code == 200 and response.json()["items"] == [{"id": missing["id"], "state": "pending"}]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_legacy_adoption_runs_once_then_explicit_end_always_starts_empty_across_cleanup_and_restart(store):
    store.poll()
    older = store.create()
    old_room, _, old_guest = admitted(store, older)
    old_song = store.add(old_room, {"song_id": "201", "title": "保留旧场歌曲", "request_id": uid()}, old_guest)
    latest = store.create()
    latest_room, _, latest_guest = admitted(store, latest)
    latest_song = store.add(latest_room, {"song_id": "202", "title": "首次采用歌曲", "request_id": uid()}, latest_guest)
    instance = uid()
    adopted = store.coordinator.start({"instance": instance, "request_id": uid()})
    assert adopted["room_id"] == latest["room_id"]
    household = adopted["household_id"]
    store.close(adopted["room_id"])
    next_scene = store.coordinator.start({"instance": instance, "request_id": uid()})
    assert next_scene["room_id"] not in {older["room_id"], latest["room_id"]}
    assert next_scene["household_id"] == household and store.coordinator.jobs()["jobs"] == []
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM guests WHERE room=?", (next_scene["room_id"],)).fetchone()[0] == 0
        assert db.execute("SELECT id FROM songs WHERE room=?", (older["room_id"],)).fetchone()[0] == old_song["id"]
        assert db.execute("SELECT id FROM songs WHERE room=?", (latest["room_id"],)).fetchone()[0] == latest_song["id"]
        assert db.execute("SELECT closed FROM rooms WHERE id=?", (older["room_id"],)).fetchone()[0] == 0
    store.close(next_scene["room_id"])
    with store.connection() as db:
        db.execute("UPDATE rooms SET expires=? WHERE closed=1", (int(time.time()) - 86401,))
    store.poll()
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM coordinator_session").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM coordinator_start_requests").fetchone()[0] == 0
        assert db.execute("SELECT initialized FROM coordinator_household WHERE id=1").fetchone()[0] == 1
    store.coordinator.close()
    reopened = Store(store.root, store.origin)
    try:
        after_restart = reopened.coordinator.start({"instance": instance, "request_id": uid()})
        assert after_restart["room_id"] != older["room_id"]
        assert after_restart["household_id"] == household
        assert reopened.coordinator.jobs()["jobs"] == []
        with reopened.connection() as db:
            assert db.execute("SELECT count(*) FROM guests WHERE room=?", (after_restart["room_id"],)).fetchone()[0] == 0
            assert db.execute("SELECT id FROM songs WHERE room=?", (older["room_id"],)).fetchone()[0] == old_song["id"]
            assert db.execute("SELECT count(*) FROM guests WHERE room=?", (older["room_id"],)).fetchone()[0] == 1
            assert db.execute("SELECT initialized FROM coordinator_household WHERE id=1").fetchone()[0] == 1
    finally:
        reopened.coordinator.close()


def test_restart_before_first_tv_open_keeps_the_one_time_legacy_adoption_available(store):
    older, latest = store.create(), store.create()
    with store.connection() as db:
        assert db.execute("SELECT initialized FROM coordinator_household WHERE id=1").fetchone()[0] == 0
    store.coordinator.close()
    reopened = Store(store.root, store.origin)
    try:
        adopted = reopened.coordinator.start({"instance": uid(), "request_id": uid()})
        assert adopted["room_id"] == latest["room_id"]
        with reopened.connection() as db:
            assert db.execute("SELECT closed FROM rooms WHERE id=?", (older["room_id"],)).fetchone()[0] == 0
            assert db.execute("SELECT initialized FROM coordinator_household WHERE id=1").fetchone()[0] == 1
    finally:
        reopened.coordinator.close()
