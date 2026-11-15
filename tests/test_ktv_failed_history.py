"""使用合成队列验证失败历史写入边界与准确的 NAS 重试。"""
import json
import threading
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homeplane.ktv_accompaniment import MODEL_REVISION
from homeplane.ktv_game_api import create_router
from homeplane.ktv_service import Queue, atomic_json, create_app, nas_attempt_request_id

TOKEN = "test_token_" + "h" * 32
HEADERS = {"X-KTV-Key": TOKEN}
ORIGIN = "http://127.0.0.1:8769"


def uid():
    return str(uuid.uuid4())


def failed_job(root, *, song="123", source="cloud", code="download_timeout", request=None, cache_only=False):
    job_id = uid()
    folder = root / "jobs" / job_id
    folder.mkdir(parents=True)
    row = {"id": job_id, "song_id": song, "title": "原来选择的版本", "created": 1, "state": "failed",
           "simulated": True, "code": code, "request_id": request or uid(), "cache_only": cache_only}
    if source:
        row["source_preference"] = source
    atomic_json(folder / "state.json", row)
    return job_id


def queue_for(root, acquire=lambda *unused: None):
    return Queue(root, acquire=acquire, separate=lambda *unused: None, transcode=lambda *unused: None,
                 simulated=True)


def client_for(queue, *, bridge=None, local=True):
    library = SimpleNamespace(remote=lambda operation, value=None: bridge.manage(operation, value or {}),
                              start=lambda: None, close=lambda: None) if bridge else None
    app = create_app(queue, TOKEN, console_origin=ORIGIN, bridge=bridge, library=library)
    return TestClient(app, base_url=ORIGIN, client=("127.0.0.1" if local else "192.0.2.80", 9000))


def path(job_id, action):
    return f"/v1/manage/jobs/{job_id}/{action}"


def test_remove_hides_terminal_record_only_and_survives_restart(tmp_path):
    job_id = failed_job(tmp_path)
    folder = tmp_path / "jobs" / job_id
    retained = folder / "original.wav"
    retained.write_bytes(b"synthetic media stays")
    queue = queue_for(tmp_path)
    with client_for(queue) as client:
        assert client.post(path(job_id, "remove"), json={}).status_code == 401
        client.get("/console").raise_for_status()
        assert client.post(path(job_id, "remove"), json={}, headers={"Origin": "http://other.invalid"}).status_code == 403
        assert client.post(path(job_id, "remove"), json={"delete": True}, headers={"Origin": ORIGIN}).status_code == 400
        for _ in range(2):
            response = client.post(path(job_id, "remove"), json={}, headers={"Origin": ORIGIN})
            assert response.json() == {"removed": True, "scope": "history"}
        assert client.get("/v1/jobs", headers=HEADERS).json()["jobs"] == []
        assert client.get(f"/v1/jobs/{job_id}", headers=HEADERS).json()["state"] == "failed"
        assert retained.read_bytes() == b"synthetic media stays"
        assert client.post(path(job_id, "retry"), json={"request_id": uid()},
                           headers={"Origin": ORIGIN}).json() == {"detail": "failed_history_hidden"}
    persisted = json.loads((folder / "state.json").read_text())
    assert persisted["history_dismissed"] is True and persisted["state"] == "failed"
    restored = queue_for(tmp_path)
    with client_for(restored) as client:
        assert client.get("/v1/jobs", headers=HEADERS).json()["jobs"] == []
        assert client.post(path(job_id, "remove"), json={}, headers=HEADERS).json()["removed"] is True
    assert retained.read_bytes() == b"synthetic media stays"


def test_historical_retry_preserves_source_and_does_not_duplicate_running_work(tmp_path):
    original = failed_job(tmp_path, song="987", source="mv_direct")
    entered, release = threading.Event(), threading.Event()
    seen = []

    def acquire(song_id, folder, unused):
        seen.append((song_id, queue.jobs[folder.name]["source_preference"]))
        entered.set()
        assert release.wait(3)
        raise ValueError("download_timeout")

    queue = queue_for(tmp_path, acquire)
    with client_for(queue) as client:
        try:
            first = client.get("/v1/jobs", headers=HEADERS).json()["jobs"][0]
            assert first["source_preference"] == "mv_direct" and first["can_retry"] and first["can_remove"]
            body = {"request_id": uid()}
            receipt = client.post(path(original, "retry"), json=body, headers=HEADERS).json()
            assert receipt["scope"] == "history" and receipt["job_id"] != original
            assert entered.wait(2) and seen == [("987", "mv_direct")]
            assert client.post(path(original, "retry"), json=body, headers=HEADERS).json() == receipt
            attempt = client.post(path(original, "retry"), json={"request_id": uid()}, headers=HEADERS)
            assert (attempt.status_code, attempt.json()["detail"]) == (409, "retry_in_progress")
            assert client.post(path(receipt["job_id"], "remove"), json={}, headers=HEADERS).status_code == 409
            assert queue.get(original)["state"] == "failed"
            data = json.loads((tmp_path / "jobs" / receipt["job_id"] / "state.json").read_text())
            assert data["song_id"] == "987" and data["source_preference"] == "mv_direct"
            assert data["preparation_retry_of"] == original and data["cache_only"] is False
        finally:
            release.set()


def test_only_exact_prepared_edition_suppresses_retry(tmp_path):
    blocked = failed_job(tmp_path, song="401", source="cloud", code="source_rights_denied")
    prepared = failed_job(tmp_path, song="402", source="cloud", code="source_rights_denied")
    ambiguous = failed_job(tmp_path, song="403", source="")
    queue = queue_for(tmp_path)
    for number, origin in [("401", "song"), ("402", "cloud")]:
        ready_id = uid()
        queue.jobs[ready_id] = {"id": ready_id, "song_id": number, "title": "原来选择的版本", "created": 1,
                                "simulated": True, "code": "none", "state": "ready", "audio_origin": origin,
                                "binding": MODEL_REVISION, "media": {
                                    kind: {"sha256": "a" * 64, "bytes": 48, "duration_seconds": 1}
                                    for kind in ("original", "instrumental")}}
    with client_for(queue) as client:
        values = {row["id"]: row for row in client.get("/v1/jobs", headers=HEADERS).json()["jobs"] if row["state"] == "failed"}
        expected = {blocked: "source_rights_denied", prepared: "failed_history_already_prepared",
                    ambiguous: "failed_history_source_unknown"}
        for job_id, reason in expected.items():
            assert values[job_id]["retry_reason"] == reason and not values[job_id]["can_retry"] and values[job_id]["can_remove"]
            response = client.post(path(job_id, "retry"), json={"request_id": uid()}, headers=HEADERS)
            assert (response.status_code, response.json()["detail"]) == (409, reason)


@pytest.mark.parametrize("mapped,wrong_source", [(True, False), (False, False), (True, True)])
def test_nas_retry_targets_exact_active_request_and_dismissal_never_relays(tmp_path, mapped, wrong_source):
    remote, room_id = uid(), uid()
    job_id = failed_job(tmp_path, code="storage_full", request=nas_attempt_request_id(remote, 2))
    calls = []
    payload = {"active": True, "room_id": room_id, "status": {"songs": [
        {"id": uid(), "song_id": "123", "title": "原来选择的版本", "source": "cloud", "state": "failed"},
        {"id": remote, "song_id": "123", "source": "song" if wrong_source else "cloud", "state": "failed"},
    ]}}

    def manage(operation, body):
        calls.append((operation, body))
        return payload if operation == "room" else {"accepted": True}

    bridge = SimpleNamespace(lock=threading.RLock(), mapping={remote: job_id} if mapped else {},
                             protected_jobs=lambda: {job_id}, manage=manage, start=lambda: None, close=lambda: None)
    queue = queue_for(tmp_path)
    with client_for(queue, bridge=bridge) as client:
        req = uid()
        response = client.post(path(job_id, "retry"), json={"request_id": req}, headers=HEADERS)
        if wrong_source:
            assert (response.status_code, response.json()["detail"]) == (409, "failed_history_source_changed")
            assert calls == [("room", {})]
        else:
            assert response.json() == {"accepted": True, "scope": "room"}
            assert calls == [("room", {}), ("retry", {"room_id": room_id, "value": {"id": remote, "request_id": req}})]
        before = list(calls)
        assert client.post(path(job_id, "remove"), json={}, headers=HEADERS).json()["removed"] is True
        assert calls == before and set(queue.jobs) == {job_id}


def test_failed_history_operations_reject_lan_even_with_worker_key(tmp_path):
    job_id = failed_job(tmp_path)
    with client_for(queue_for(tmp_path), local=False) as client:
        assert client.post(path(job_id, "retry"), json={"request_id": uid()}, headers=HEADERS).status_code == 404
        assert client.post(path(job_id, "remove"), json={}, headers=HEADERS).status_code == 404


def test_desktop_owner_proxy_keeps_failed_history_routes_exact(tmp_path):
    config = tmp_path / "config.json"
    atomic_json(config, {"token": TOKEN})
    seen = []

    def upstream(request):
        seen.append((request.url.path, json.loads(request.content)))
        assert request.headers.get("x-ktv-key") == TOKEN
        return httpx.Response(409, json={"detail": "retry_in_progress"})

    app = FastAPI()
    app.include_router(create_router(config, transport=httpx.MockTransport(upstream)))
    origin = "http://127.0.0.1:8740"
    job_id = uid()
    route = f"/api/games/ktv/manage/jobs/{job_id}"
    payload = {"request_id": uid()}
    with TestClient(app, base_url=origin, client=("127.0.0.1", 9000)) as client:
        assert client.post(route + "/retry", json=payload).status_code == 403
        assert client.post(route + "/delete-media", json={}, headers={"Origin": origin}).status_code == 404
        assert client.post(route + "/remove?extra=1", json={}, headers={"Origin": origin}).status_code == 400
        assert seen == []
        result = client.post(route + "/retry", json=payload, headers={"Origin": origin})
    assert result.status_code == 409 and result.json() == {"detail": "retry_in_progress"}
    assert seen == [(path(job_id, "retry"), payload)]
