"""Synthetic probe tests; never access configured household endpoints."""
import json
import stat

import pytest

from scripts import probe_home_ha as probe


@pytest.mark.asyncio
async def test_probe_reports_missing_existing_auth_without_network(tmp_path, monkeypatch):
    config = tmp_path / "friday.home.json"
    config.write_text(json.dumps({"ha_url": "http://simulated.invalid", "ha_token_env": "SYNTHETIC_PROBE_TOKEN"}))
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "REPORT", report_path)
    monkeypatch.delenv("SYNTHETIC_PROBE_TOKEN", raising=False)
    assert await probe.probe(config) == 2
    report = json.loads(report_path.read_text())
    assert report["reason"] == "ha_token_environment_and_existing_oauth_file_missing"
    assert "simulated.invalid" not in report_path.read_text()
    assert not report["real_write_attempted"]


def test_private_evidence_mode_and_atomic_replace(tmp_path):
    target = tmp_path / "private.json"
    probe.write_json(target, {"simulated": True}, private=True)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text()) == {"simulated": True}
    probe.write_json(target, {"simulated": True, "new": True}, private=True)
    assert json.loads(target.read_text())["new"] is True


@pytest.mark.asyncio
async def test_office_candidate_is_light_only_and_keeps_capability_attrs():
    from homeplane.contracts import Binding
    from homeplane.simulator import Simulator

    snapshot = await Simulator().snapshot()
    lamp = snapshot.entities[0].model_copy(update={"name": "Simulated study lamp", "room": "书房", "kind": "light",
                                                       "bindings": [Binding(source="ha", external_id="light.hall")]})
    candidate_snapshot = snapshot.model_copy(update={"entities": [lamp]})
    rows = [{"entity_id": "light.hall", "state": "off", "attributes": {
        "brightness": 2, "supported_color_modes": ["brightness"], "private_unrelated_attribute": "omit",
    }}]
    result = probe.office_candidates(candidate_snapshot, rows)
    assert len(result) == 1
    assert result[0]["attributes"] == {"brightness": 2, "supported_color_modes": ["brightness"]}
