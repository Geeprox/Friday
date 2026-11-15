"""Synthetic scheduling/cache checks. No official client, model, microphone or speaker."""

import shutil
import threading
import time
import uuid
import wave

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from homeplane.ktv_cache import CacheManager
from homeplane.ktv_scheduler import PriorityPool
from homeplane.ktv_service import JobRequest, Queue, create_app


def wav(path):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(b"\0" * 1920)


def request(song="123"):
    return JobRequest(song_id=song, title="合成测试", request_id=str(uuid.uuid4()))


def wait(queue, job):
    return queue.futures[job["id"]].result(timeout=3)


def test_pending_stage_priorities_change_without_restarting_active_work():
    pool = PriorityPool(1, "test")
    started = threading.Event()
    release = threading.Event()
    calls = []

    def active():
        started.set()
        release.wait(2)
        calls.append("active")

    try:
        first = pool.submit("active", active)
        assert started.wait(1)
        b = pool.submit("b", lambda: calls.append("b"))
        c = pool.submit("c", lambda: calls.append("c"))
        pool.prioritize(["c", "b"])
        release.set()
        first.result(2)
        b.result(2)
        c.result(2)
        assert calls == ["active", "c", "b"]
    finally:
        release.set()
        pool.shutdown()


def test_download_of_next_song_overlaps_current_separation_and_ready_cache_skips_providers(tmp_path):
    source = tmp_path / "source.wav"
    wav(source)
    separating = threading.Event()
    release = threading.Event()
    next_downloaded = threading.Event()
    calls = []

    def acquire(song, folder, event):
        calls.append(("acquire", song))
        if song == "124":
            next_downloaded.set()
        return source

    def separate(source, folder, event):
        separating.set()
        release.wait(2)
        return source

    queue = Queue(
        tmp_path / "runtime",
        acquire=acquire,
        separate=separate,
        transcode=lambda s, o, c: shutil.copyfile(s, o),
        simulated=True,
    )
    try:
        a = queue.submit(request())
        assert separating.wait(1)
        b = queue.submit(request("124"))
        assert next_downloaded.wait(1)
        assert queue.get(b["id"])["state"] != "ready"
        queue.prioritize([b["id"], a["id"]])
        release.set()
        wait(queue, a)
        wait(queue, b)
        before = list(calls)
        cached = queue.submit(request())
        assert cached["state"] == "ready" and cached["cache_hit"] and calls == before
        assert queue.journal(a["id"])["events"][-1]["stage"] == "ready"
        assert not queue.journal(a["id"])["raw_audio_saved"]
    finally:
        release.set()
        queue.close()


def prepared(tmp_path):
    source = tmp_path / "original.wav"
    wav(source)
    queue = Queue(
        tmp_path / "runtime",
        acquire=lambda *a: source,
        separate=lambda s, *a: s,
        transcode=lambda s, o, c: shutil.copyfile(s, o),
        simulated=True,
    )
    item = queue.submit(request())
    wait(queue, item)
    root = tmp_path / "separation"
    root.mkdir(mode=0o700)
    models = root / "models"
    models.mkdir()
    (models / "keep").write_text("pinned")
    return queue, item, source, root


def test_cache_removal_is_revision_bound_and_preserves_model_and_original(tmp_path):
    queue, item, source, root = prepared(tmp_path)
    protected = {item["id"]}
    manager = CacheManager(queue, root, lambda: protected)
    try:
        first = manager.inventory()["entries"][0]
        assert not first["can_delete"]
        with pytest.raises(HTTPException) as failure:
            manager.delete(first["id"], first["revision"])
        assert failure.value.status_code == 409
        protected.clear()
        path = queue.jobs_dir / item["id"] / "instrumental.wav"
        path.touch()
        with pytest.raises(HTTPException):
            manager.delete(first["id"], first["revision"])
        current = manager.inventory()["entries"][0]
        assert manager.delete(current["id"], current["revision"])["removed"]
        assert source.exists() and (root / "models/keep").read_text() == "pinned"
        assert manager.inventory()["entries"] == []
    finally:
        queue.close()


def test_owner_console_cookie_is_local_and_cache_scoped(tmp_path):
    queue, item, source, root = prepared(tmp_path)
    manager = CacheManager(queue, root)
    app = create_app(queue, "a" * 43, cache_manager=manager, console_origin="http://127.0.0.1:8769")
    try:
        with TestClient(app, base_url="http://127.0.0.1:8769", client=("127.0.0.1", 50001)) as client:
            assert client.get("/v1/cache").status_code == 401
            response = client.get("/console")
            assert response.status_code == 200
            assert "HttpOnly" in response.headers["set-cookie"]
            assert client.get("/v1/cache").status_code == 200
            assert client.get("/v1/party").status_code == 401
            assert client.post("/v1/jobs", json={}, headers={"Origin": "http://127.0.0.1:8769"}).status_code == 401
            assert client.post("/v1/cache/delete", json={}).status_code == 403
            assert (
                client.post("/v1/cache/delete", headers={"Origin": "https://evil.test"}, json={}).status_code
                == 403
            )
            assert (
                client.post(
                    "/v1/cache/delete", headers={"Origin": "http://127.0.0.1:8769"}, json={}
                ).status_code
                == 400
            )
    finally:
        queue.close()
    with TestClient(app, base_url="http://127.0.0.1:8769", client=("192.168.1.42", 50002)) as remote:
        assert remote.get("/console").status_code == 404


def test_source_authorization_is_loopback_owner_only(tmp_path):
    queue, item, source, root = prepared(tmp_path)
    class Source:
        calls = 0
        def status(self):
            return {"logged_in": False, "client_window_required": False}
        def official_login(self):
            self.calls += 1
            return {"state": "waiting_official"}
    source_api = Source()
    app = create_app(queue, "a" * 43, console_origin="http://127.0.0.1:8769", source_api=source_api)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8769", client=("127.0.0.1", 50001)) as client:
            assert client.get("/v1/source/status").status_code == 401
            client.get("/console")
            assert client.get("/v1/source/status").json()["client_window_required"] is False
            assert client.post("/v1/source/login", json={}).status_code == 403
            assert client.post("/v1/source/login", json={}, headers={"Origin":"https://evil.test"}).status_code == 403
            assert source_api.calls == 0
            assert client.post("/v1/source/login", json={}, headers={"Origin":"http://127.0.0.1:8769"}).status_code == 200
            assert source_api.calls == 1
        with TestClient(app, base_url="http://127.0.0.1:8769", client=("192.168.1.42", 50002)) as remote:
            assert remote.get("/v1/source/status", headers={"X-KTV-Key":"a"*43}).status_code == 404
    finally:
        queue.close()


def test_pool_resize_and_scheduler_settings_persist_without_interrupting_active_job(tmp_path):
    source = tmp_path / "source.wav"
    wav(source)
    queue = Queue(
        tmp_path / "runtime",
        acquire=lambda *a: source,
        separate=lambda s, *a: s,
        transcode=lambda s, o, c: shutil.copyfile(s, o),
        simulated=True,
    )
    try:
        assert queue.scheduler_status()["settings"]["model"] == 3
        assert queue.scheduler_status()["limits"]["model"] == 10
        assert (
            queue.configure({"mode": "manual", "download": 3, "model": 2, "encode": 1})["settings"]["model"]
            == 2
        )
        assert queue.pools["acquire"].desired == 3 and queue.pools["separate"].desired == 2
        assert queue.configure({"mode": "manual", "download": 2, "model": 10, "encode": 2})["settings"]["model"] == 10
        with pytest.raises(HTTPException):
            queue.configure({"mode": "manual", "download": 2, "model": 11, "encode": 2})
        assert not queue.scheduler_status()["optimal_measured"]
        with pytest.raises(HTTPException):
            queue.configure({"mode": "manual", "download": 4, "model": 1, "encode": 1})
        with pytest.raises(HTTPException):
            queue.configure({"mode": "manual", "download": True, "model": 1, "encode": 1})
        assert queue.configure({"mode": "auto", "download": 3, "model": 2, "encode": 4})["settings"] == {
            "mode": "auto",
            "download": 2,
            "model": 3,
            "encode": 2,
        }
    finally:
        queue.close()
    restarted = Queue(
        tmp_path / "runtime",
        acquire=lambda *a: source,
        separate=lambda s, *a: s,
        transcode=lambda s, o, c: shutil.copyfile(s, o),
        simulated=True,
    )
    try:
        assert restarted.scheduler_status()["settings"]["mode"] == "auto"
    finally:
        restarted.close()


def test_default_pool_runs_three_separations_with_headroom(tmp_path):
    source = tmp_path / "source.wav"
    wav(source)
    release, started = threading.Event(), threading.Event()
    running = set()
    lock = threading.Lock()

    def separate(source, folder, event):
        with lock:
            running.add(folder.name)
            if len(running) == 3:
                started.set()
        assert release.wait(2)
        return source

    queue = Queue(tmp_path / "runtime", acquire=lambda *a: source, separate=separate,
                  transcode=lambda s, o, c: shutil.copyfile(s, o), simulated=True,
                  memory_available=lambda: 16 * 1024**3)
    try:
        jobs = [queue.submit(request(str(i))) for i in range(100, 103)]
        assert started.wait(1)
        assert queue.scheduler_status()["active_models"] == 3
        release.set()
        assert all(wait(queue, item)["state"] == "ready" for item in jobs)
    finally:
        release.set()
        queue.close()


def test_storage_wait_leaves_download_slot_free_and_resumes(tmp_path, monkeypatch):
    from homeplane import ktv_service

    source = tmp_path / "source.wav"
    wav(source)
    real = shutil.disk_usage(tmp_path)
    available = [100]
    monkeypatch.setattr(ktv_service.shutil, "disk_usage", lambda path: real._replace(free=available[0]))
    monkeypatch.setattr(ktv_service, "STORAGE_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(ktv_service, "STORAGE_WAIT_SECONDS", 1)
    monkeypatch.setattr(ktv_service, "PREPARATION_RESERVE_BYTES", 1024)
    monkeypatch.setattr(ktv_service, "MAX_STORAGE", 1)
    called = []
    trim_requests = []

    def acquire(song_id, *_):
        assert trim_requests[-1] == 0
        called.append(song_id)
        return source

    class Manager:
        def request_trim(self, amount=0):
            trim_requests.append(amount)

    class Archive:
        def restore_job(self, song_id, folder, binding, *, audio_origin, reserve):
            if song_id != "101":
                return None
            reserve(50)
            value = {"media": {}, "video": {"status": "unavailable"}, "cache_hit": True}
            for kind in ("instrumental", "original"):
                path = folder / (kind + ".wav")
                shutil.copyfile(source, path)
                value["media"][kind] = {"sha256": "a" * 64, "duration_seconds": 0.01,
                                         "filename": path.name, "bytes": path.stat().st_size}
            return value

    queue = Queue(tmp_path / "runtime", acquire=acquire, separate=lambda s, *a: s,
                  transcode=lambda s, o, c: shutil.copyfile(s, o), simulated=True, download_workers=1)
    queue.storage_manager, queue.archive = Manager(), Archive()
    try:
        pending = queue.submit(request("100"))
        deadline = time.monotonic() + 0.8
        while queue.get(pending["id"])["progress"]["stage"] != "waiting_storage" and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert queue.get(pending["id"])["progress"]["stage"] == "waiting_storage"
        assert trim_requests[0] > 0
        assert wait(queue, queue.submit(request("101")))["cache_hit"]
        assert not called
        available[0] = 4096
        assert wait(queue, pending)["state"] == "ready"
        assert called == ["100"] and queue.storage_reservations == {}
    finally:
        queue.close()

def test_ready_queue_publishes_final_stems_and_discards_owned_intermediates(tmp_path):
    original = tmp_path / "original.wav"
    wav(original)

    def acquire(song, folder, event):
        staged = folder / "downloaded-input.wav"
        shutil.copyfile(original, staged)
        return staged

    def separate(source, folder, event):
        temporary = folder / ".separation"
        temporary.mkdir()
        staged = temporary / "instrumental.wav"
        shutil.copyfile(source, staged)
        return staged

    queue = Queue(tmp_path / "runtime", acquire=acquire, separate=separate,
                  transcode=lambda s, o, c: shutil.copyfile(s, o), simulated=True)
    try:
        item = queue.submit(request())
        assert wait(queue, item)["state"] == "ready"
        folder = queue.jobs_dir / item["id"]
        assert sorted(p.name for p in folder.iterdir()) == ["instrumental.wav", "original.wav", "state.json"]
        assert original.is_file()
    finally:
        queue.close()


def test_explicit_mv_cache_stays_separate_and_auto_reuses_song_audio(tmp_path):
    source=tmp_path/'source.wav';wav(source);calls=[]
    def acquire(song,folder,event):
        calls.append(song)
        if queue.jobs[folder.name].get('source_preference')=='mv':
            queue.jobs[folder.name]['audio_origin']='mv'
        return source
    queue=Queue(tmp_path/'runtime',acquire=acquire,separate=lambda *a:source,transcode=lambda s,o,e:shutil.copyfile(s,o),simulated=True)
    try:
        album=queue.submit(request());wait(queue,album)
        mv=queue.submit(JobRequest(song_id='123',title='MV',request_id=str(uuid.uuid4()),source_preference='mv'))
        assert wait(queue,mv)['state']=='ready' and calls==['123','123']
        cached=queue.submit(JobRequest(song_id='123',title='MV',request_id=str(uuid.uuid4()),source_preference='mv'))
        assert cached['cache_hit'] and len(calls)==2
        automatic=queue.submit(request())
        assert automatic['cache_hit'] and automatic['audio_origin']=='song' and len(calls)==2
    finally:queue.close()


@pytest.mark.parametrize("preference", ["auto", "song"])
@pytest.mark.parametrize("old_origin", ["mv", "mv_direct", "cloud"])
def test_song_requests_never_reuse_other_audio_editions(tmp_path, preference, old_origin):
    source = tmp_path / "source.wav"
    wav(source)
    calls = []

    def acquire(song, folder, event):
        calls.append(song)
        return source

    queue = Queue(tmp_path / "runtime", acquire=acquire, separate=lambda *a: source,
                  transcode=lambda s, o, e: shutil.copyfile(s, o), simulated=True)
    try:
        old = queue.submit(request())
        wait(queue, old)
        # Represents an already prepared job from the old auto-MV policy.
        queue.jobs[old["id"]]["audio_origin"] = old_origin
        fresh = queue.submit(JobRequest(song_id="123", title="Song audio", request_id=str(uuid.uuid4()),
                                        source_preference=preference))
        result = wait(queue, fresh)
        assert result["state"] == "ready" and result["audio_origin"] == "song"
        assert not result["cache_hit"] and calls == ["123", "123"]
        assert (queue.jobs_dir / old["id"] / "original.wav").is_file()
    finally:
        queue.close()


def test_untagged_same_mv_cache_is_not_a_song_source(tmp_path):
    source = tmp_path / "source.wav"
    wav(source)
    queue = Queue(tmp_path / "runtime", acquire=lambda *a: source, separate=lambda *a: source,
                  transcode=lambda s, o, e: shutil.copyfile(s, o), simulated=True)
    try:
        old = queue.submit(request())
        wait(queue, old)
        queue.jobs[old["id"]]["video"] = {"method": "same_mv_audio"}
        fresh = queue.submit(request())
        assert not wait(queue, fresh)["cache_hit"]
    finally:
        queue.close()
