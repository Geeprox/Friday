"""Preparation/transfer contracts; in-memory HTTP, synthetic bytes, no devices."""
import copy
import hashlib
import io
import json
import ssl
import threading
import time
import uuid
import wave

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from homeplane.ktv_service import create_app
from homeplane.ktv_worker import (
    MediaTickets,
    PreparationWorker,
    WorkerCapabilities,
    manifest_version,
    media_origin,
)

KEY = "s" * 43


class PreparedQueue:
    simulated = True

    def __init__(self):
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            writer.writeframes(b"\x01\0\x01\0" * 96000)
        self.bytes = output.getvalue()
        self.job_id = str(uuid.uuid4())
        self.job = {"id": self.job_id, "song_id": "123", "title": "Synthetic", "state": "ready", "code": "none",
                    "created": 1, "simulated": True, "audio_origin": "song", "video": {"status": "unavailable"},
                    "media": {kind: {"url": f"/v1/jobs/{self.job_id}/media/{kind}",
                        "sha256": hashlib.sha256(self.bytes).hexdigest(), "bytes": len(self.bytes),
                        "duration_seconds": 2, "sample_rate": 48000, "channels": 2, "encoding": "pcm_s16le_wav"}
                        for kind in ("original", "instrumental")}}

    def get(self, job_id):
        if job_id != self.job_id:
            raise ValueError("unknown_job")
        return copy.deepcopy(self.job)

    def media(self, job_id, kind):
        return io.BytesIO(self.bytes), self.get(job_id)["media"][kind]

    def submit(self, request):
        return self.get(self.job_id)

    def close(self):
        pass


def test_media_tickets_bind_current_job_version_expiry_and_no_management_privileges():
    queue = PreparedQueue()
    tickets = MediaTickets(KEY)
    token, expires = tickets.issue(queue.job)
    assert expires <= time.time() + 600
    assert tickets.permits(token, queue.job)
    changed = copy.deepcopy(queue.job)
    changed["media"]["instrumental"]["sha256"] = "a" * 64
    assert not tickets.permits(token, changed)
    changed = copy.deepcopy(queue.job)
    changed["video"] = {"status": "ready", "sha256": "b" * 64}
    assert tickets.permits(token, changed, kind="instrumental")
    assert not tickets.permits(token, changed, kind="video")
    changed = copy.deepcopy(queue.job)
    changed["id"] = str(uuid.uuid4())
    assert not tickets.permits(token, changed)
    assert not tickets.permits(token, queue.job, now=expires)
    assert not tickets.permits(token[:-1] + ("a" if token[-1] != "a" else "b"), queue.job)
    with TestClient(create_app(queue, KEY)) as client:
        headers = {"X-KTV-Media": token}
        url = queue.job["media"]["original"]["url"]
        response = client.get(url, headers=headers)
        assert response.status_code == 200 and response.content == queue.bytes
        assert client.head(url, headers=headers).status_code == 200
        assert client.get(url, headers={**headers, "Range": "bytes=44-47"}).content == queue.bytes[44:48]
        assert client.get("/v1/status", headers=headers).status_code == 401
        assert client.get(f"/v1/jobs/{queue.job_id}/lyrics", headers=headers).status_code == 401
        assert client.post(url, headers=headers).status_code == 401
        assert client.post("/v1/party/close", headers=headers, json={}).status_code == 401
        assert client.get(url, headers={**headers, "Origin": "https://example.test"}).status_code == 403
        assert client.get(url + "?token=" + token).status_code == 401


@pytest.mark.parametrize("origin", ["http://8.8.8.8:8769", "http://127.0.0.1:8769", "http://0.0.0.0:8769",
    "http://169.254.1.1:8769", "http://192.0.2.1:8769", "http://example.test:8769", "https://10.0.0.2:8769",
    "http://10.0.0.2:8769/v1", "http://user@10.0.0.2:8769", "http://10.0.0.2:8769?token=x"])
def test_worker_origin_is_fixed_private_lan_not_a_generic_proxy(origin):
    with pytest.raises(ValueError):
        media_origin(origin)


def test_worker_readiness_requires_source_identity_and_pinned_model_not_http_success(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from homeplane import ktv_accompaniment as audio
    from homeplane import ktv_worker as worker_module
    model = tmp_path / "models" / audio.MODEL
    model.mkdir(parents=True)
    for name in audio.PINNED_MODEL_ASSETS:
        (model / name).write_bytes(b"synthetic pinned fixture")
    responses = [{"body": {"code": 200}}, {"body": {"data": {"profile": {"userId": 7}}}}]
    source = SimpleNamespace(_session=lambda: "local fixture only", rpc=lambda *a, **kw: responses.pop(0),
                             node="node", bridge=tmp_path / "bridge.cjs")
    source.bridge.write_text("// synthetic")
    checked = []
    monkeypatch.setattr(audio, "_model_files", lambda p: checked.append(p) or {"verified": "fixture"})
    monkeypatch.setattr(worker_module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(worker_module.shutil, "which", lambda name: "/synthetic/ffmpeg")
    health = WorkerCapabilities(source, tmp_path, download_enabled=True, runtime=tmp_path / "python")
    first = health()
    assert first["source"] is False and first["download"] is False
    second = health()
    assert all(second.values())
    assert len(checked) == 1, "unchanged dependency bytes do not block the control lane with rehashes"
    responses.append({"body": {"data": {"profile": {"userId": 7}}}})
    monkeypatch.setattr(worker_module.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("native runtime unavailable")))
    assert health()["model"] is False, "a version label cannot substitute for usable native dependencies"
    (model / "config.json").write_bytes(b"changed")
    def reject(_):
        raise ValueError("model_identity_mismatch")
    monkeypatch.setattr(audio, "_model_files", reject)
    responses.append({"body": {"data": {"profile": {"userId": 7}}}})
    assert health()["model"] is False


def make_worker(tmp_path, **worker_config):
    config = tmp_path / "nas.json"
    config.write_text(json.dumps({"endpoint": "https://example.test/ktv", "agent_key": "a" * 43,
                                  "coordination": "nas", "media_origin": "http://10.0.0.2:8769", **worker_config}))
    config.chmod(0o600)
    queue = PreparedQueue()
    worker = PreparationWorker(queue, config, signing_key=KEY, health=lambda: dict.fromkeys(("source", "model", "download", "lyrics", "media"), True))
    worker.capabilities = worker.health_check()
    worker.capabilities_at = time.monotonic()
    remote, room = str(uuid.uuid4()), str(uuid.uuid4())
    worker.mapping = {remote: queue.job_id}
    response = {"tasks": [], "cancelled": [], "rooms": [{"room_id": room, "direct": True, "tv_instance": str(uuid.uuid4()), "order": [remote],
        "join_url": "https://example.test/ktv/#r=" + room + "&k=" + "b" * 43}]}
    calls = []

    def request(path, body=None):
        calls.append((path, copy.deepcopy(body)))
        return copy.deepcopy(response) if path == "/agent/tasks" else {"accepted": True, "archive": "pending"}
    worker.request = request
    return worker, queue, remote, response, calls


@pytest.fixture
def lan_config(tmp_path):
    config = tmp_path / "nas.json"

    def save(**updates):
        value = {"endpoint": "https://nas.example.test:8443/ktv", "agent_key": KEY, "coordination": "nas",
                 "media_origin": "http://10.0.0.2:8769", "lan_ipv4": "10.4.0.2"}
        config.write_text(json.dumps({**value, **updates}))
        config.chmod(0o600)
        return config

    return save


def test_lan_pin_keeps_verified_original_nas_identity(lan_config, monkeypatch):
    connections, handshakes, writes = [], [], []
    ok = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}'
    redirect = b'HTTP/1.1 307 Temporary Redirect\r\nLocation: https://elsewhere.invalid/\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}'
    responses = iter((ok, ok, redirect))

    class MemoryStream:
        def __init__(self, response):
            self.response = response
            self.written = bytearray()
            writes.append(self.written)

        def start_tls(self, ssl_context, server_hostname, timeout=None):
            handshakes.append((server_hostname, ssl_context.check_hostname, ssl_context.verify_mode))
            return self

        def read(self, max_bytes, timeout=None):
            chunk, self.response = self.response[:max_bytes], self.response[max_bytes:]
            return chunk

        def write(self, buffer, timeout=None):
            self.written.extend(buffer)

        def close(self):
            pass

        def get_extra_info(self, name):
            return None

    def connect(self, host, port, **_):
        connections.append((host, port))
        return MemoryStream(next(responses))

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8000")
    worker = PreparationWorker(PreparedQueue(), lan_config(), signing_key=KEY, health=lambda: {})
    try:
        assert worker.endpoint == "https://nas.example.test:8443/ktv"
        assert worker.manage("room", {}) == {}
        assert worker.request("/agent/worker", {}) == {}
        assert worker.http.get(worker.endpoint + "/health").status_code == 307
        for url in ("https://elsewhere.invalid:8443/ktv/health", "https://nas.example.test:8443/other/health",
                    "http://nas.example.test:8443/ktv/health"):
            with pytest.raises(httpx.UnsupportedProtocol, match="configured_nas_endpoint_required"):
                worker.http.get(url)
        assert connections == [("10.4.0.2", 8443)] * 3
        assert handshakes == [("nas.example.test", True, ssl.CERT_REQUIRED)] * 3
        assert all(b"Host: nas.example.test:8443\r\n" in request for request in writes)
        assert [request.partition(b"\r\n")[0] for request in writes] == [
            b"POST /ktv/agent/manage HTTP/1.1", b"POST /ktv/agent/worker HTTP/1.1", b"GET /ktv/health HTTP/1.1",
        ]
    finally:
        worker.close()


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.1.2", "100.64.0.2", "8.8.8.8", "fd00::1",
                                     "nas.example.test", "10.01.0.2", None, 0x0A040002])
def test_lan_pin_rejects_noncanonical_or_nonprivate_ipv4(lan_config, address):
    with pytest.raises(ValueError, match="private_nas_lan_ipv4_required"):
        PreparationWorker(PreparedQueue(), lan_config(lan_ipv4=address), signing_key=KEY, health=lambda: {})


@pytest.mark.parametrize("changes", [{"coordination": None}, {"endpoint": "http://127.0.0.1:18769/ktv"}])
def test_lan_pin_requires_nas_coordination_and_https(lan_config, changes):
    with pytest.raises(ValueError, match="nas_lan_https_required"):
        PreparationWorker(PreparedQueue(), lan_config(**changes), signing_key=KEY, health=lambda: {}, allow_loopback=True)


def test_worker_publishes_before_nas_cache_and_never_relays_room_or_controls(tmp_path):
    worker, queue, remote, response, calls = make_worker(tmp_path)
    try:
        worker.tick()
        manifest = next(body for path, body in calls if path == "/agent/manifests")
        assert manifest["id"] == manifest["job"]["id"] == remote
        assert manifest["job"]["media"]["original"]["url"].startswith(f"/v1/jobs/{queue.job_id}/")
        assert manifest["timeline"] == {"sample_rate": 48000, "frames": 96000, "duration_ms": 2000}
        assert worker.tickets.permits(manifest["media_ticket"], queue.job)
        assert KEY not in json.dumps(manifest)
        assert queue.job_id in worker.protected_jobs(), "manifest ACK is not archive completion"
        assert any(path == "/agent/qr" for path, _ in calls)
        assert not any("/agent/rooms" in path for path, _ in calls)
        assert worker.current()["active"] is False
        for method, args in ((worker.create_party, ()), (worker.close_party, ()),
                             (worker.report_playback, ({},)), (worker.control_from_tv, ({},))):
            with pytest.raises(ValueError):
                method(*args)
        response["rooms"][0]["playback"] = {"current": "", "played": [remote]}
        response["archives"] = [{"id": remote, "version": manifest["version"], "state": "failed"}]
        worker.tick()
        assert queue.job_id in worker.protected_jobs()
        response["archives"][0]["state"] = "ready"
        worker.tick()
        assert queue.job_id not in worker.protected_jobs()
        assert len([p for p, _ in calls if p == "/agent/manifests"]) == 1
        response["archives"][0]["state"] = "failed"
        worker.tick()
        assert queue.job_id in worker.protected_jobs(), "a damaged NAS cache must re-protect the repair source"
    finally:
        worker.close()


def test_new_guide_is_published_immediately_with_new_version_not_after_ticket_refresh(tmp_path):
    worker, queue, remote, response, calls = make_worker(tmp_path)
    class Guide:
        protected = set()
        value = None
        def protect(self, jobs):
            self.protected = set(jobs)
        def get(self, job):
            return self.value
        def close(self):
            pass
    guide = Guide()
    worker.guide = guide
    try:
        worker.tick()
        guide.value = {"sha256": "c" * 64, "bytes": 2048, "duration_ms": 2000,
                       "url": f"/v1/jobs/{queue.job_id}/media/guide_original", "reference": {
                           "kind": "accompaniment", "sha256": "d" * 64, "bytes": 2048, "duration_ms": 2000,
                           "url": f"/v1/jobs/{queue.job_id}/media/guide_reference"}}
        worker.tick()
        manifests = [body for path, body in calls if path == "/agent/manifests"]
        assert len(manifests) == 2 and manifests[0]["version"] != manifests[1]["version"]
        assert "guide" not in manifests[0] and manifests[1]["guide"] == guide.value
        assert worker.tickets.permits(manifests[1]["media_ticket"], queue.job)
        assert manifest_version(queue.job) != manifests[1]["version"]
        response["rooms"][0]["playback"] = {"current": "", "played": [remote]}
        worker.manifest_acked[remote] = (manifests[1]["version"], time.monotonic() - 241)
        worker.tick()
        latest = [body for path, body in calls if path == "/agent/manifests"][-1]
        assert not guide.protected and latest["guide"] == guide.value
        assert latest["version"] == manifests[1]["version"], "leaving prewarm scope cannot roll back a published edition"
        assert not any(path == "/agent/guides" for path, _ in calls)
    finally:
        worker.close()


def test_preparation_worker_and_nas_exchange_real_contracts_without_mac_control_relay(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from integrations.ktv_public.nas_server import Store
    store = Store(tmp_path / "nas", "https://example.test")
    coordinator = store.coordinator
    monkeypatch.setattr(coordinator.cache_pool, "submit", lambda *args: None)
    worker, queue, _, _, _ = make_worker(tmp_path)
    worker.mapping.clear()
    worker.capabilities = dict.fromkeys(("source", "model", "download", "lyrics", "media"), True)
    worker.capabilities_at = time.monotonic()
    endpoints = {"/agent/worker": coordinator.register_worker, "/agent/qr": coordinator.qr,
                 "/agent/updates": store.update, "/agent/manifests": coordinator.manifest}
    def exchange(path, body=None):
        return store.poll() if path == "/agent/tasks" else endpoints[path](body)
    worker.request = exchange
    instance = str(uuid.uuid4())
    try:
        store.create()
        latest = store.create()
        party = coordinator.start({"instance": instance, "request_id": str(uuid.uuid4())})
        assert party["room_id"] == latest["room_id"]
        worker.tick()
        key = parse_qs(urlsplit(party["join_url"]).fragment)["k"][0]
        room = store.room(party["room_id"], key)
        guest = store.join(room)
        remote = store.add(room, {"song_id": "123", "title": "Synthetic", "request_id": str(uuid.uuid4())}, guest)["id"]
        worker.tick()
        job = coordinator.jobs()["jobs"][0]
        assert job["id"] == remote and job["state"] == "ready" and job["archive"] == "pending"
        direct = job["media"]["original"]["sources"][0]
        assert direct["header"] == "X-KTV-Media" and worker.tickets.permits(direct["credential"], queue.job)
        assert direct["url"] == queue.job["media"]["original"]["url"]
        assert KEY not in json.dumps(job)
        assert coordinator.qr_ready()
        assert queue.job_id in worker.protected_jobs()
        epoch = str(uuid.uuid4())
        coordinator.playback({"room_id": room["id"], "current": remote, "played": [], "state": "paused",
            "last_command": 0, "safety_latched": False, "applied_revision": 0, "mic_muted": True,
            "position_ms": 300, "playback_epoch": epoch}, instance)
        assert store.status(room)["playback"]["playback_epoch"] == epoch
        worker.tick()
        assert worker.playback["current"] == queue.job_id
        assert store.status(room)["playback"]["state"] == "paused"
    finally:
        worker.close()
        coordinator.close()


def test_pending_lyrics_do_not_block_archiving_stems(tmp_path):
    worker, _, _, _, calls = make_worker(tmp_path)
    def unavailable(_):
        raise ValueError("lyrics temporarily unavailable")
    worker.lyrics_provider = unavailable
    try:
        worker.tick()
        manifest = next(body for path, body in calls if path == "/agent/manifests")
        assert manifest["job"]["state"] == "ready" and manifest["lyrics"] == {"status": "unavailable", "lines": []}
    finally:
        worker.close()


def test_hung_lyrics_leave_audio_ready_for_archiving_and_update_the_same_media(tmp_path, monkeypatch):
    worker, queue, _, _, calls = make_worker(tmp_path)
    entered, release = threading.Event(), threading.Event()
    timed = {"status": "ready", "lines": [{"time_ms": at, "text": "合成歌词"} for at in (300, 600, 1000)]}
    editions = [{"status": "ready", "lines": []}, timed]

    def lookup(_):
        entered.set()
        assert release.wait(timeout=3)
        return editions.pop(0)

    monkeypatch.setattr("homeplane.ktv_worker.LYRIC_RETRY_SECONDS", (0,))
    worker.lyrics_provider = lookup
    edition = manifest_version(queue.job)
    try:
        worker.tick()
        assert entered.wait(timeout=1)
        initial = next(body for path, body in calls if path == "/agent/manifests")
        assert initial["lyrics"]["lines"] == []
        assert initial["job"]["state"] == "ready"
        release.set()
        worker.lyric_work[edition].future.result(timeout=1)
        worker.tick()
        worker.lyric_work[edition].future.result(timeout=1)
        worker.tick()
        versions = [body for path, body in calls if path == "/agent/manifests"]
        assert len(versions) == 2
        assert versions[-1]["lyrics"] == timed
        assert versions[-1]["job"]["media"] == initial["job"]["media"]
        assert versions[-1]["timeline"] == initial["timeline"]
        assert worker.tickets.permits(versions[-1]["media_ticket"], queue.job)
    finally:
        release.set()
        worker.close()


def test_optional_video_keeps_an_inflight_lyric_lookup_on_the_same_audio(tmp_path):
    worker, queue, _, _, calls = make_worker(tmp_path)
    entered, release = threading.Event(), threading.Event()
    invocations = []
    timed = {"status": "ready", "lines": [{"time_ms": at, "text": "合成歌词"} for at in (300, 600, 1000)]}

    def lookup(job_id):
        invocations.append(job_id)
        entered.set()
        assert release.wait(timeout=3)
        return timed

    worker.lyrics_provider = lookup
    edition = manifest_version(queue.job, include_video=False)
    try:
        worker.tick()
        assert entered.wait(timeout=1)
        lookup_future = worker.lyric_work[edition].future
        queue.job["video"] = {"status": "ready", "sha256": "b" * 64, "bytes": 4096}
        worker.tick()
        assert len(worker.lyric_work) == 1 and worker.lyric_work[edition].future is lookup_future
        release.set()
        assert lookup_future.result(timeout=1) == timed
        worker.tick()
        manifests = [body for path, body in calls if path == "/agent/manifests"]
        assert manifests[0]["job"]["video"]["status"] == "unavailable"
        assert manifests[-1]["lyrics"] == timed and manifests[-1]["job"]["video"] == queue.job["video"]
        assert manifests[-1]["timeline"] == manifests[0]["timeline"]
        assert invocations == [queue.job_id]
    finally:
        release.set()
        worker.close()


def test_mutually_independent_lyrics_lanes_repair_nas_only_tracks_without_repreparing(tmp_path):
    worker, queue, _, response, calls = make_worker(tmp_path)
    tasks = [{"id": str(uuid.uuid4()), "song_id": str(song), "version": "a" * 64, "title": "合成歌曲",
              "source": "song", "duration_ms": 2000} for song in (123, 124, 125)]
    first, second, verified = [(task["id"], task["version"]) for task in tasks]
    entered = {key[0]: threading.Event() for key in (first, second)}
    release = threading.Event()
    timed = {"status": "ready", "lines": [{"time_ms": at, "text": "合成歌词"} for at in (300, 600, 1000)]}

    class Provider:
        def primary_for(self, task):
            return timed if task["id"] == verified[0] else {"status": "unavailable", "lines": []}

        def recover(self, task):
            entered[task["id"]].set()
            assert release.wait(timeout=4)
            return {"status": "unavailable", "lines": []}

    worker.lyrics_provider = Provider()
    worker.mapping.clear()
    queue.submit = lambda _: pytest.fail("已归档歌曲补歌词时不能重新制备")
    queue.get = lambda _: pytest.fail("补歌词无需读取 Mac 音轨")
    response["rooms"][0]["order"] = [task["id"] for task in tasks]
    response["lyric_tasks"] = tasks[:2]
    try:
        worker.tick()
        for edition in (first, second):
            worker.lyric_work[edition].future.result(timeout=1)
        worker.tick()
        assert all(event.wait(timeout=1) for event in entered.values())
        response["lyric_tasks"] = tasks
        worker.tick()
        worker.lyric_work[verified].future.result(timeout=1)
        for _ in range(2):
            worker.tick()
        metadata = [body for path, body in calls if path == "/agent/lyrics"]
        assert metadata == [{"id": verified[0], "version": verified[1], "instance": worker.instance, "lyrics": timed}]
        assert not any(path == "/agent/manifests" for path, _ in calls)
    finally:
        release.set()
        worker.close()


def test_nas_only_lyrics_stop_when_retries_exhausted(tmp_path, monkeypatch):
    worker, _, remote, response, calls = make_worker(tmp_path)
    worker.mapping.clear()
    calls_to_provider = []
    worker.lyrics_provider = lambda task: calls_to_provider.append(task["id"]) or {"status": "unavailable", "lines": []}
    monkeypatch.setattr("homeplane.ktv_worker.LYRIC_RETRY_SECONDS", (0,))
    version = "a" * 64
    response["lyric_tasks"] = [{"id": remote, "song_id": "123", "version": version, "source": "song",
                                "duration_ms": 2000, "title": "合成歌曲"}]
    try:
        for _ in range(2):
            worker.tick()
            worker.lyric_work[(remote, version)].future.result(timeout=1)
        for _ in range(2):
            worker.tick()
        assert calls_to_provider == [remote, remote]
        assert [body["lyrics"] for path, body in calls if path == "/agent/lyrics"] == [
            {"status": "unavailable", "lines": [], "retry_exhausted": True, "lookup_revision": 3},
        ]
    finally:
        worker.close()


def test_worker_repair_opens_playable_nas_archive_without_changing_audio(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from homeplane.intelligence.ktv_lyrics import KtvLyricsRecovery
    from integrations.ktv_public.nas_server import Store
    from tests.test_ktv_coordinator import admitted, fake_cache, manifest, registration, started, uid

    store = Store(tmp_path / "nas", "https://example.test")
    coordinator = store.coordinator
    monkeypatch.setattr(coordinator.cache_pool, "submit", lambda *args: None)
    worker, queue, _, _, _ = make_worker(tmp_path)
    worker.mapping.clear()
    queue.get = queue.submit = lambda _: pytest.fail("补歌词不访问 Mac 音轨")
    endpoints = {"/agent/worker": coordinator.register_worker, "/agent/qr": coordinator.qr,
                 "/agent/lyrics": coordinator.repair_lyrics}
    worker.request = lambda path, body=None: store.poll() if path == "/agent/tasks" else endpoints[path](body)
    timed = {"status": "ready", "lines": [{"time_ms": at, "text": "合成歌词"} for at in (300, 600, 1000)]}
    source = SimpleNamespace(public_timed_lyrics=lambda song, duration: timed if (song, duration) == ("123", 2000) else None)
    worker.lyrics_provider = KtvLyricsRecovery(None, source, queue, lambda _: pytest.fail("读取了 Mac 歌单"))
    try:
        original, audio, _, _, old_room = manifest(store, registration(worker.instance, origin=worker.media_origin))
        original["lyrics"] = {"status": "unavailable", "lines": []}
        coordinator.manifest(original)
        fake_cache(monkeypatch, audio, [])
        row = coordinator._row(original["id"])
        for kind, spec in coordinator._specs(row).items():
            coordinator._cache_one(row, kind, spec)
        with store.connection() as db:
            db.execute("UPDATE rooms SET closed=1 WHERE id=?", (old_room["id"],))
        _, party = started(store)
        room, _, guest = admitted(store, party)
        song = store.add(room, {"song_id": "123", "title": "合成归档曲", "request_id": uid()}, guest)["id"]
        [before] = coordinator.jobs()["jobs"]
        assert store.status(room)["playable"] == [] and before["archive"] == "ready"
        worker.tick()
        worker.lyric_work[(song, before["version"])].future.result(timeout=1)
        worker.tick()
        [after] = coordinator.jobs()["jobs"]
        assert after["media"] == before["media"] and after["archive"] == "ready"
        assert coordinator.lyrics(song) == timed and store.poll()["lyric_tasks"] == []
        assert store.status(room)["playable"] == coordinator.party()["playable"] == [song]
    finally:
        worker.close()
        coordinator.close()


def test_public_lyric_priming_starts_before_audio_preparation_completes(tmp_path):
    worker, queue, remote, response, calls = make_worker(tmp_path)
    queue.job["state"] = "acquiring"
    response["tasks"] = [{"id": remote, "song_id": "123", "title": "合成歌曲", "source": "song"}]
    started = threading.Event()

    class Provider:
        def prefetch_public(self, song_id):
            assert song_id == "123"
            started.set()

        def __call__(self, _):
            raise AssertionError("音轨就绪前不校验时间轴")

    worker.lyrics_provider = Provider()
    try:
        worker.tick()
        assert started.wait(timeout=1)
        assert not any(path == "/agent/manifests" for path, _ in calls)
    finally:
        worker.close()


def test_lost_qr_is_republished_and_stalled_health_is_not_renewed_as_ready(tmp_path):
    worker, _, _, response, calls = make_worker(tmp_path)
    try:
        worker.tick()
        response["rooms"][0]["qr_ready"] = False
        worker.tick()
        assert len([path for path, _ in calls if path == "/agent/qr"]) == 2
        response["rooms"][0]["qr_ready"] = True
        worker.capabilities_at = time.monotonic() - 16
        worker.worker_registered = 0
        worker.tick()
        registration = [body for path, body in calls if path == "/agent/worker"][-1]
        assert not any(registration["capabilities"].values()), "a blocked health check cannot renew old true capabilities"
        assert len([path for path, _ in calls if path == "/agent/qr"]) == 2
    finally:
        worker.close()


def test_worker_publishes_configured_guest_url_over_existing_worker_endpoint(tmp_path):
    guest = "https://guests.example.test:9443/ktv"
    worker, _, _, response, calls = make_worker(tmp_path, public_join_endpoint=guest)
    try:
        old_url = response["rooms"][0]["join_url"]
        worker.tick()
        assert not any(path == "/agent/qr" for path, _ in calls)
        response["rooms"][0]["join_url"] = guest + "/#" + old_url.split("#", 1)[1]
        worker.tick()
        qr = next(body for path, body in calls if path == "/agent/qr")
        assert qr["join_url"] == response["rooms"][0]["join_url"]
        assert worker.endpoint == "https://example.test/ktv"
        assert worker.public_join_endpoint == guest
    finally:
        worker.close()


@pytest.mark.parametrize("value", ["http://guests.example.test/ktv", "https://guests.example.test/other",
    "https://user@example.invalid/ktv", "https://guests.example.test:65536/ktv",
    "https://guests.example.test/ktv#fragment", "https://guests.example.test/ktv?foo=1"])
def test_worker_guest_url_requires_explicit_https_base_without_credentials(value):
    from homeplane.ktv_worker import _public_join_endpoint
    with pytest.raises(ValueError, match="https_public_join_endpoint_required"):
        _public_join_endpoint(value, "https://example.test/ktv")


def test_room_qr_does_not_wait_for_preparation_lane(tmp_path):
    worker, _, _, _, calls = make_worker(tmp_path)
    worker.background = True
    worker._work_once = lambda: pytest.fail("room polling must not run the preparation lane")
    try:
        worker.tick()
        assert any(path == "/agent/qr" for path, _ in calls)
        assert worker.work_wake.is_set()
    finally:
        worker.close()


def test_direct_worker_does_not_select_or_prepare_a_legacy_room(tmp_path):
    worker, queue, _, response, _ = make_worker(tmp_path)
    try:
        response["rooms"][0].pop("direct")
        response["tasks"] = [{"id": str(uuid.uuid4()), "song_id": "123", "title": "Synthetic"}]
        queue.submit = lambda _: (_ for _ in ()).throw(AssertionError("legacy room must not auto-start preparation"))
        worker.tick()
        assert worker.party is None and not worker.pending_tasks
        response["rooms"] = [{"room_id": str(uuid.uuid4()), "direct": True} for _ in range(2)]
        with pytest.raises(ValueError, match="ambiguous_direct_room"):
            worker.tick()
    finally:
        worker.close()


def test_closed_scene_can_release_verified_archive_without_retaining_room_metadata(tmp_path):
    worker, queue, _, response, calls = make_worker(tmp_path)
    original = worker.request
    proof = []
    def exchange(path, body=None):
        if path == "/agent/archive-check":
            proof.extend(copy.deepcopy(body["items"]))
            return {"items": [{"id": item["id"], "state": "ready"} for item in body["items"]]}
        return original(path, body)
    worker.request = exchange
    try:
        worker.tick()
        assert queue.job_id in worker.protected_jobs()
        generation = worker.room_generation
        response["rooms"] = []
        worker.archive_checked = 0
        worker.tick()
        assert worker.party is None and worker.playback is None and worker.room_generation > generation
        assert queue.job_id not in worker.protected_jobs()
        assert proof[0]["id"] == queue.job_id and proof[0]["files"] == [
            {"sha256": item["sha256"], "bytes": item["bytes"]} for item in queue.job["media"].values()]
        assert not any("/agent/rooms" in path for path, _ in calls)
    finally:
        worker.close()


@pytest.mark.parametrize("close_scene", [False, True])
def test_late_video_is_archived_after_playback_and_reused_on_the_next_request(tmp_path, monkeypatch, close_scene):
    from integrations.ktv_public.nas_server import Store
    from tests.test_ktv_coordinator import admitted, fake_cache, started, uid

    worker, queue, _, _, _ = make_worker(tmp_path)
    worker.mapping.clear()
    queue.video_pending = {queue.job_id: True}
    store = Store(tmp_path / "nas", "https://example.test")
    coordinator = store.coordinator
    audio_calls, video_tasks, full_manifests = [], [], []
    video_bytes = b"synthetic video archive payload" * 5
    fake_cache(monkeypatch, {"original": queue.bytes, "instrumental": queue.bytes, "video": video_bytes}, audio_calls)
    monkeypatch.setattr(coordinator.cache_pool, "submit", lambda fn, *args: fn(*args))
    monkeypatch.setattr(coordinator.video_cache_pool, "submit", lambda *args: video_tasks.append(args))
    timed = {"status": "ready", "lines": [{"time_ms": at, "text": "合成歌词"} for at in (300, 600, 1000)]}
    worker.lyrics_provider = lambda _: timed

    def publish(value):
        full_manifests.append(value)
        return coordinator.manifest(value)

    endpoints = {"/agent/worker": coordinator.register_worker, "/agent/qr": coordinator.qr,
                 "/agent/updates": store.update, "/agent/manifests": publish,
                 "/agent/archive-check": coordinator.archive_check, "/agent/archive-videos": coordinator.archive_videos}
    worker.request = lambda path, body=None: store.poll() if path == "/agent/tasks" else endpoints[path](body)
    try:
        instance, party = started(store)
        room, _, guest = admitted(store, party)
        worker.tick()
        remote = store.add(room, {"song_id": "123", "title": "合成歌曲", "request_id": uid()}, guest)["id"]
        worker.tick()
        worker.lyric_work[manifest_version(queue.job)].future.result(timeout=1)
        worker.tick()
        assert store.status(room)["playable"] == [remote]
        assert [row["state"] for row in store.poll()["archives"]] == ["ready"]

        coordinator.playback({"room_id": room["id"], "current": "", "played": [remote], "state": "idle",
                              "last_command": 0, "safety_latched": False, "applied_revision": 0, "mic_muted": True}, instance)
        if close_scene:
            coordinator.close_party(instance, {"room_id": room["id"]})
        worker.archive_checked = 0
        worker.tick()
        assert queue.job_id in worker.protected_jobs()
        published_count = len(full_manifests)

        queue.job["video"] = {"status": "ready", "url": f"/v1/jobs/{queue.job_id}/media/video", "bytes": len(video_bytes),
                              "sha256": hashlib.sha256(video_bytes).hexdigest(), "duration_seconds": 2,
                              "encoding": "h264_mp4_video_only", "video_offset_ms": 0, "method": "same_mv_audio",
                              "source_audio_sha256": queue.job["media"]["original"]["sha256"], "derived_from_video_sha256": "e" * 64}
        del queue.video_pending[queue.job_id]
        worker.archive_checked = 0
        worker.tick()
        assert queue.job_id in worker.protected_jobs() and len(video_tasks) == 1
        assert len(full_manifests) == published_count

        if not close_scene:
            timed = {**timed, "lines": [{**line, "text": "更新的合成歌词"} for line in timed["lines"]]}
            coordinator.manifest({**full_manifests[-1], "lyrics": timed, "version": "c" * 64})
        run, *args = video_tasks.pop()
        run(*args)
        worker.archive_checked = 0
        worker.tick()
        assert queue.job_id not in worker.protected_jobs()
        assert not video_tasks

        if close_scene:
            _, next_party = started(store)
            room, _, guest = admitted(store, next_party)
            assert store.status(room)["songs"] == []
        coordinator.worker["at"] = time.time() - 60
        again = store.add(room, {"song_id": "123", "title": "再次点播", "request_id": uid()}, guest)
        assert again["id"] != remote and again["state"] == "ready"
        assert coordinator.lyrics(again["id"]) == timed
        video_path, _ = coordinator.media_file(again["id"], "video")
        assert video_path.read_bytes() == video_bytes
    finally:
        worker.close()
        coordinator.close()


def test_archive_check_cannot_release_a_changed_local_edition_with_an_old_proof(tmp_path):
    worker, queue, _, _, _ = make_worker(tmp_path)
    try:
        worker.tick()
        def old_proof(path, body=None):
            queue.job["media"]["original"]["sha256"] = "e" * 64
            return {"items": [{"id": queue.job_id, "state": "ready"}]}
        worker.request = old_proof
        worker.archive_checked = 0
        worker._check_archives()
        assert queue.job_id in worker.protected_jobs()
    finally:
        worker.close()


@pytest.mark.parametrize("after_commit", [False, True])
def test_worker_crash_around_manifest_commit_recovers_mapping_and_only_manifest_commits_ready(tmp_path, monkeypatch, after_commit):
    from urllib.parse import parse_qs, urlsplit

    from integrations.ktv_public.nas_server import Store
    class Crash(BaseException):
        pass
    store = Store(tmp_path / "nas", "https://example.test")
    coordinator = store.coordinator
    monkeypatch.setattr(coordinator.cache_pool, "submit", lambda *args: None)
    first, queue, _, _, _ = make_worker(tmp_path)
    first.mapping.clear()
    calls = []
    endpoints = {"/agent/worker": coordinator.register_worker, "/agent/qr": coordinator.qr,
                 "/agent/updates": store.update, "/agent/manifests": coordinator.manifest}
    def exchange(path, body=None):
        calls.append((path, copy.deepcopy(body)))
        return store.poll() if path == "/agent/tasks" else endpoints[path](body)
    def crash_at_commit(path, body=None):
        if path == "/agent/manifests":
            if after_commit:
                exchange(path, body)
            raise Crash()
        return exchange(path, body)
    first.request = crash_at_commit
    second = None
    try:
        party = coordinator.start({"instance": str(uuid.uuid4()), "request_id": str(uuid.uuid4())})
        first.tick()
        key = parse_qs(urlsplit(party["join_url"]).fragment)["k"][0]
        room = store.room(party["room_id"], key)
        remote = store.add(room, {"song_id": "123", "title": "Synthetic", "request_id": str(uuid.uuid4())}, store.join(room))["id"]
        with pytest.raises(Crash):
            first.tick()
        for path, body in calls:
            if path == "/agent/updates":
                assert all(row["state"] != "ready" for row in body["updates"])
        persisted = json.loads(first.path.read_text())
        assert persisted["mapping"][remote] == queue.job_id
        assert store.status(room)["songs"][0]["state"] == ("ready" if after_commit else "queued")
        first.close()
        second = PreparationWorker(queue, tmp_path / "nas.json", signing_key=KEY, health=lambda: {})
        second.request = exchange
        second.tick()
        assert coordinator._row(remote) is not None and store.status(room)["songs"][0]["state"] == "ready"
        assert second.mapping[remote] == queue.job_id
    finally:
        if second:
            second.close()
        first.close()
        coordinator.close()


def test_restart_keeps_published_good_lyrics_when_optional_provider_is_offline(tmp_path):
    first, queue, remote, response, calls = make_worker(tmp_path)
    first.lyrics_provider = lambda _: {"status": "ready", "lines": [{"time_ms": at, "text": "Synthetic"} for at in (0, 300, 600)]}
    second = None
    try:
        first.tick()
        first.lyric_work[manifest_version(queue.job)].future.result(timeout=1)
        first.tick()
        original = [body for path, body in calls if path == "/agent/manifests"][-1]
        assert original["lyrics"]["status"] == "ready"
        first.close()
        second = PreparationWorker(queue, tmp_path / "nas.json", signing_key=KEY, health=lambda: {})
        second.lyrics_provider = lambda _: (_ for _ in ()).throw(ValueError("offline"))
        second.request = lambda path, body=None: copy.deepcopy(response) if path == "/agent/tasks" else calls.append((path, copy.deepcopy(body))) or {"accepted": True}
        second.tick()
        latest = [body for path, body in calls if path == "/agent/manifests"][-1]
        assert latest["lyrics"] == original["lyrics"] and latest["version"] == original["version"]
    finally:
        if second:
            second.close()
        first.close()
