"""Recent private history needs current source evidence before showing presence."""

import sqlite3
import time

import httpx
import pytest
from fastapi import FastAPI

from homeplane.people.catalog import PeopleCatalog
from homeplane.people.live import IdentityStore
from homeplane.people.live_api import create_router, qualify_presence
from homeplane.recognition.artifacts import SFACE_SHA256
from homeplane.recognition.contracts import Embedding


@pytest.mark.parametrize("change", [None, "ended", "session", "expired", "duplicate", "person"])
def test_presence_requires_one_fresh_matching_camera_session_track_and_person(change):
    stored = dict(camera="camera", session="source", track="1", person_id="person", present=True, facing=True)
    track = dict(track="1", person_id="person", state="matched", observed_monotonic=100)
    source = dict(camera="camera", session="source", state="live", tracks=[track])
    if change == "ended":
        source["state"] = "unavailable"
    if change == "session":
        source["session"] = "replacement"
    if change == "expired":
        track["observed_monotonic"] = 98
    if change == "duplicate":
        source["tracks"].append(dict(track))
    if change == "person":
        track["person_id"] = "another"
    result = qualify_presence([stored], {"sessions": [source]}, now=100)[0]
    assert result["present"] is (change is None)
    if change:
        assert result["facing"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
async def test_actual_metadata_route_does_not_treat_saved_presence_as_an_active_camera(
    tmp_path, monkeypatch, live
):
    from homeplane.embodied import paths

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(paths, "PRIVATE", private)
    store = IdentityStore(private / "identities.sqlite")
    catalog = PeopleCatalog(sqlite3.connect(":memory:"))
    person = catalog.register_profile("Synthetic", catalog.page().revision)
    embedding = Embedding("face", SFACE_SHA256, (1.0,) + (0.0,) * 127, False)
    store.enroll(person, [embedding] * 3)
    store.observe("camera", "source", "1", person, "matched")
    assert store.presence()[0]["present"] is True

    class Source:
        def cameras(self):
            return []

        async def identity_status(self):
            return {
                "sessions": [
                    dict(
                        camera="camera",
                        session="source",
                        state="live" if live else "stopped",
                        tracks=[
                            dict(
                                track="1",
                                person_id=person,
                                state="matched",
                                observed_monotonic=time.monotonic(),
                            )
                        ],
                    )
                ]
            }

    app = FastAPI()
    app.include_router(create_router(catalog, Source()))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://synthetic"
        ) as client:
            response = await client.get("/api/people/live")
        assert response.status_code == 200
        value = response.json()["observations"][0]
        assert value["person_id"] == person and value["name"] == "Synthetic"
        assert value["present"] is live
        assert value["last_seen"] == pytest.approx(store.presence()[0]["last_seen"])
        assert store.presence()[0]["present"] is True, "The read route must not rewrite last-seen history"
    finally:
        store.close()
        catalog.connection.close()
