import sqlite3

from fastapi.testclient import TestClient

from homeplane.people.catalog import PeopleCatalog
from homeplane.people.demo import create_demo


def test_disabled_empty_store_has_zero_optional_import_or_producer_work(monkeypatch):
    import builtins

    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] in ("cv2", "sherpa_onnx", "sklearn", "numpy", "homeplane.native_media_worker"):
            raise AssertionError("unavailable optional runtime imported")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    c = PeopleCatalog(sqlite3.connect(":memory:"))
    page = c.page()
    assert not page.samples and page.recognition_state == "disabled"
    assert page.active_speaker_dependency == page.tracking_dependency == "unavailable"


def test_public_review_routes_no_inference_enrollment_or_upload():
    app = create_demo()
    client = TestClient(app)
    response = client.get("/api/people")
    assert response.headers["cache-control"] == "no-store"
    page = response.json()
    assert page["embeddings_persisted"] is False
    target = next(c for c in page["clusters"] if c["state"] == "candidate")
    named = client.post(
        "/api/people/name",
        json={
            "expected_revision": page["revision"],
            "cluster_id": target["cluster_id"],
            "name": "<script>fixture</script>",
        },
    )
    assert named.status_code == 200 and named.headers["cache-control"] == "no-store"
    assert any(p["name"] == "<script>fixture</script>" for p in named.json()["people"])
    stale = client.post(
        "/api/people/name",
        json={"expected_revision": page["revision"], "cluster_id": target["cluster_id"], "name": "stale"},
    )
    assert stale.status_code == 409 and stale.json() == {"detail": {"code": "people_revision_conflict"}}
    forbidden = client.post(
        "/api/people/name",
        json={
            "expected_revision": named.json()["revision"],
            "cluster_id": target["cluster_id"],
            "name": "fixture",
            "action_authorized": True,
        },
    )
    assert forbidden.status_code == 422
    paths = set(app.openapi()["paths"])
    assert paths == {
        "/api/people",
        "/api/people/name",
        "/api/people/merge",
        "/api/people/split",
        "/api/people/correct",
        "/api/people/undo",
        "/api/people/voice-review",
        "/",
    }
    for path in (
        "/api/people/reindex",
        "/api/people/enroll",
        "/api/people/activate",
        "/api/people/embeddings",
    ):
        assert client.post(path, json={}).status_code == 404


def test_demo_markup_and_static_module_use_text_nodes():
    client = TestClient(create_demo())
    assert "Friday · 人物" in client.get("/").text
    source = client.get("/people-static/people.js").text
    assert "textContent" in source and "innerHTML" not in source
    assert "expected_revision" in source
    assert "核对摄像头中的人物" in source
