"""Temporary procedural vectors exercise identity persistence, never real people."""
import asyncio
import importlib.util
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homeplane.people.catalog import PeopleCatalog, PeopleError
from homeplane.people.live import FaceIdentityPipeline, IdentityStore, analyze
from homeplane.people.live_api import create_router
from homeplane.recognition.artifacts import SFACE_SHA256
from homeplane.recognition.contracts import Embedding


def vector(first=1, second=0, *, simulated=False, model=SFACE_SHA256):
    return Embedding("face", model, (float(first), float(second)) + (0.0,) * 126, simulated)


@pytest.fixture
def local(tmp_path):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    clock = [100.0]
    store = IdentityStore(folder / "identities.sqlite", clock=lambda: clock[0])
    yield store, clock, folder
    store.close()


def test_persisted_identity_is_shared_across_cameras(local):
    store, clock, folder = local
    store.enroll("person_a", [vector()] * 3)
    other = IdentityStore(folder / "identities.sqlite", clock=lambda: clock[0])
    try:
        assert other.match(vector())[0] == "person_a"
        store.observe("camera_a", "session_a", "1", "person_a", "matched", True)
        other.observe("camera_b", "session_b", "19", "person_a", "matched", False)
        assert {r["camera"] for r in other.presence() if r["present"]} == {"camera_a", "camera_b"}
        assert len({r["person_id"] for r in other.presence()}) == 1
        assert "vectors" not in json.dumps(other.presence())
    finally:
        other.close()


def test_ambiguous_templates_do_not_choose_nearest_person(local):
    store, _, _ = local
    store.enroll("person_a", [vector()] * 3)
    store.enroll("person_b", [vector(1, 0.05)] * 3)
    assert store.match(vector()) == (None, "ambiguous")


def test_additional_room_enrollment_keeps_prior_room_evidence(local):
    store, _, _ = local
    store.enroll("person_a", [vector()] * 3)
    store.enroll("person_b", [vector(0.8, -0.6)] * 3)
    store.enroll("person_a", [vector(0.7, 0.714)] * 3)
    assert store.match(vector())[0] == "person_a"
    assert store.match(vector(0.7, 0.714))[0] == "person_a"


@pytest.mark.parametrize("bad", [vector(simulated=True), vector(model="a" * 64)])
def test_mixed_provenance_cannot_enroll_or_match(local, bad):
    store, _, _ = local
    with pytest.raises(ValueError, match="identity_model_mismatch"):
        store.enroll("person_a", [bad] * 3)
    assert store.match(bad) == (None, "unavailable")


def test_inconsistent_enrollment_and_unknown_face(local):
    store, _, _ = local
    with pytest.raises(ValueError, match="inconsistent"):
        store.enroll("person_a", [vector(), vector(), vector(0, 1)])
    store.enroll("person_a", [vector()] * 3)
    assert store.match(vector(0, 1)) == (None, "unknown")


def test_facing_and_presence_expire_independently(local):
    store, clock, _ = local
    store.observe("camera", "session", "1", "person_a", "matched", True)
    clock[0] += 1.1
    assert store.presence()[0]["present"] is True
    assert store.presence()[0]["facing"] is None
    clock[0] += 2
    assert store.presence()[0]["present"] is False
    assert store.presence()[0]["person_id"] == "person_a"


def test_end_session_and_forget_revoke_live_projection(local):
    store, _, _ = local
    store.enroll("person_a", [vector()] * 3)
    store.observe("camera", "session", "1", "person_a", "matched", True)
    store.clear_camera("camera", "session")
    assert store.presence()[0]["present"] is False
    store.forget("person_a")
    assert not store.registered() and not store.presence()
    assert store.match(vector()) == (None, "unknown")


def test_registration_reuses_catalog_ids_and_revision_fences():
    catalog = PeopleCatalog(sqlite3.connect(":memory:", check_same_thread=False))
    pid = catalog.register_profile("测试人物", 0)
    assert catalog.register_profile("测试人物", 1, pid) == pid
    with pytest.raises(PeopleError, match="revision_conflict"):
        catalog.register_profile("另一位", 1)
    with pytest.raises(PeopleError, match="name_conflict"):
        catalog.register_profile("测试人物", 2)
    assert catalog.page().people[0].action_authorized is False
    catalog.connection.close()


def test_live_api_rejects_stale_session_and_feature_upload(local, monkeypatch):
    store, _, folder = local
    monkeypatch.setattr("homeplane.embodied.paths.PRIVATE", folder)
    catalog = PeopleCatalog(sqlite3.connect(":memory:", check_same_thread=False))

    class Service:
        def cameras(self):
            return []

        async def identity_status(self):
            return dict(camera="cam_aaaaaaaaaa", session="new", tracks=[dict(track="1", enroll_ready=True)])

    app = FastAPI()
    app.include_router(create_router(catalog, Service()))
    with TestClient(app) as client:
        body = dict(expected_revision=0, camera="cam_aaaaaaaaaa", session="old", track="1", name="测试")
        assert client.post("/api/people/live/register", json=body).status_code == 409
        assert not catalog.page().people
        assert client.post("/api/people/live/register", json=dict(body, embedding=[1])).status_code == 422
        response = client.get("/api/people/live")
        assert response.headers["cache-control"] == "no-store"
        assert "vectors" not in response.text
    catalog.connection.close()


def test_registration_requires_independent_fresh_frames(local):
    store, clock, _ = local
    pipeline = FaceIdentityPipeline(store, "camera", "session", "/tmp", clock=lambda: clock[0])
    pipeline.samples["1"] = [(99.9, vector())] * 3
    with pytest.raises(ValueError, match="samples_required"):
        pipeline.enroll("1", "person_a")
    pipeline.samples["1"] = [(98.9, vector()), (99.4, vector()), (99.9, vector())]
    pipeline.portraits["1"] = (99.9, b"\xff\xd8synthetic-face-crop\xff\xd9")
    assert pipeline.enroll("1", "person_a")["status"] == "enrolled"
    clock[0] += 2
    with pytest.raises(ValueError, match="samples_required"):
        pipeline.enroll("1", "person_a")


@pytest.mark.asyncio
async def test_cancel_waits_for_native_owner_before_cleanup():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Native:
        def process(self):
            entered.set()
            release.wait(2)
            finished.set()

    task = asyncio.create_task(analyze(Native()))
    await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_formal_desktop_forwards_people_writes_without_device_authority(local, monkeypatch):
    import httpx
    _, _, folder = local
    monkeypatch.setattr("homeplane.embodied.paths.PRIVATE", folder)
    source = Path(__file__).parents[1] / "native/FridayDesktop/backend/desktop_backend.py"
    spec = importlib.util.spec_from_file_location("people_desktop_consumer", source)
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    catalog = PeopleCatalog(sqlite3.connect(":memory:", check_same_thread=False))

    class Service:
        def cameras(self):
            return []

        async def identity_status(self):
            raise ValueError("identity_not_running")

    inner = FastAPI()
    inner.include_router(create_router(catalog, Service()))
    inner.state.core = SimpleNamespace(actions=SimpleNamespace(policy_report=lambda: dict(enabled=False)))
    wrapper = backend.DesktopService()
    wrapper.inner = inner
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=backend.ORIGIN) as client:
        assert (await client.post("/api/people/live/register", json={})).status_code == 403
        headers = {"Origin": backend.ORIGIN}
        rejected = await client.post("/api/people/live/register", headers=headers, json={"embedding": [1]})
        assert rejected.status_code == 422  # Actual typed route, not the wrapper's read-only rejection.
        assert (await client.post('/api/people/live/name-candidate',json={})).status_code==403
        assert (await client.post('/api/people/live/name-candidate',headers=headers,json={})).status_code==422
        response = await client.post("/api/people/live/forget", headers=headers,
                                     json={"person_id": "person_" + "a" * 24})
        assert response.status_code == 200 and response.json()["action_authorized"] is False
        assert (await client.post("/api/people/unreviewed", headers=headers, json={})).status_code == 409
    catalog.connection.close()


def test_discard_stale_evidence_keeps_resident_model(local):
    store, clock, _ = local
    pipeline = FaceIdentityPipeline(store, "camera", "session", "/unused", clock=lambda: clock[0])
    from unittest.mock import Mock
    provider = Mock()
    pipeline.provider = provider
    pipeline.samples["1"] = [(99.0, vector()), (99.5, vector()), (99.9, vector())]
    pipeline.results["1"] = dict(observed_monotonic=99.9)
    pipeline.votes["1"] = ("person_a", 3, 99.9)
    store.observe("camera", "session", "1", "person_a", "matched")
    pipeline.discard()
    assert pipeline.provider is provider
    provider.close.assert_not_called()
    assert not pipeline.public() and not pipeline.samples and not pipeline.votes
    assert not any(row["present"] for row in store.presence())
    with pytest.raises(ValueError, match="samples_required"):
        pipeline.enroll("1", "person_a")
    pipeline.close()
    provider.close.assert_called_once()


def test_pipeline_diagnostics_distinguish_skipped_observation_stages(tmp_path):
    private=tmp_path/'diagnostics';private.mkdir(mode=0o700)
    store=IdentityStore(private/'identities.sqlite')
    try:
        pipeline=FaceIdentityPipeline(store,'camera','session','/unused')
        pipeline.process(b'',[],moving=True)
        pipeline.process(b'',[])
        pipeline.process(b'',[dict(id=1)],sample=False)
        counts=pipeline.diagnostics()
        assert counts['frames']==3 and counts['moving']==1 and counts['no_body']==1 and counts['waiting_highres']==1
        assert counts['embeddings']==0 and pipeline.provider is None
        counts['moving']=999
        assert pipeline.diagnostics()['moving']==1
    finally:store.close()
