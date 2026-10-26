import json
from pathlib import Path

from fastapi.testclient import TestClient

from friday.app import app


def test_api_control_and_boundaries(tmp_path, monkeypatch):
    example = Path("friday.example.json").read_text()
    monkeypatch.chdir(tmp_path)
    Path("friday.example.json").write_text(example)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        s = client.get("/api/status").json()
        headers = {"X-Friday-Token": s["token"]}
        assert not s["running"]
        assert client.post("/api/control", json={"running": True}).status_code == 403
        assert (
            client.post(
                "/api/control", headers={**headers, "Origin": "http://evil.example"}, json={"running": True}
            ).status_code
            == 403
        )
        assert client.post("/api/control", headers=headers, json={"running": True}).status_code == 200
        r = client.post("/api/demo", headers=headers, json={"scenario": "named"}).json()
        assert r["status"] == "simulated", r
        assert (
            client.post(
                "/api/anchors", headers=headers, json={"device_id": "floor-lamp", "box": [0.1, 0.1, 0.3, 0.9]}
            ).status_code
            == 409
        )
        client.post("/api/control", headers=headers, json={"running": False})
        assert (
            client.post(
                "/api/anchors", headers=headers, json={"device_id": "floor-lamp", "box": [0.1, 0.1, 0.3, 0.9]}
            ).status_code
            == 200
        )
        assert Path("friday.local.json").exists()
        data = client.get("/api/diagnostics").text
        assert "打开客厅灯" not in data and "token" not in data
        assert client.get("/api/devices").json()[1]["on"]
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_live_disables_demo(tmp_path, monkeypatch):
    example = json.loads(Path("friday.example.json").read_text())
    example["mode"] = "live"
    monkeypatch.chdir(tmp_path)
    Path("friday.example.json").write_text(json.dumps(example))
    with TestClient(app) as client:
        headers = {"X-Friday-Token": client.get("/api/status").json()["token"]}
        assert client.post("/api/demo", headers=headers, json={"scenario": "point"}).status_code == 403
