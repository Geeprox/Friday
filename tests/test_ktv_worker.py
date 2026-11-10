"""Preparation/transfer contracts; in-memory HTTP, synthetic bytes, no devices."""
import copy
import hashlib
import io
import json
import time
import uuid
import wave

from fastapi.testclient import TestClient
import pytest

from homeplane.ktv_service import create_app
from homeplane.ktv_worker import MediaTickets, PreparationWorker, WorkerCapabilities, manifest_version, media_origin

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


def make_worker(tmp_path):
    config = tmp_path / "nas.json"
    config.write_text(json.dumps({"endpoint": "https://example.test/ktv", "agent_key": "a" * 43,
                                  "coordination": "nas", "media_origin": "http://10.0.0.2:8769"}))
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


def test_optional_lyrics_failure_does_not_block_a_verified_playable_manifest(tmp_path):
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
    first.lyrics_provider = lambda _: {"status": "ready", "lines": [{"at_ms": 0, "text": "Synthetic"}]}
    second = None
    try:
        first.tick()
        original = next(body for path, body in calls if path == "/agent/manifests")
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
