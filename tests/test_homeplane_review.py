"""Adversarial regressions; all data synthetic and no hardware access."""
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from homeplane.contracts import ActionRequest, CanonicalState, Observation
from homeplane.core import HomeCore
from homeplane.quality import greater_than, resolve
from homeplane.simulator import Simulator
from homeplane.store import TraceStore


def synthetic_observation(now, **updates):
    values = dict(entity_id="sensor.simulated", capability="temperature", value=24,
                  physical_source_id="simulated", ingest_source="simulated", received_at=now,
                  measured_at=now, freshness_basis="device_sample", cached=False,
                  transport_health="online", clock_uncertainty_seconds=1, simulated=True)
    values.update(updates)
    return Observation(**values)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_observation_cannot_become_healthy_or_boolean(value):
    now = datetime.now(UTC)
    try:
        observation = synthetic_observation(now, value=value)
    except ValidationError:
        return
    state = resolve(observation, now)
    assert state.quality != "healthy"
    assert greater_than(state, 25, now) == "unknown"


def test_negative_clock_uncertainty_cannot_revive_old_measurement():
    now = datetime.now(UTC)
    try:
        observation = synthetic_observation(now, measured_at=now - timedelta(seconds=3600),
                                            clock_uncertainty_seconds=-3500)
    except ValidationError:
        return
    assert resolve(observation, now).quality != "healthy"


@pytest.mark.asyncio
async def test_unrelated_healthy_device_metric_cannot_authorize_light_preview(tmp_path):
    simulator = Simulator()
    snapshot = await simulator.snapshot()
    now = datetime.now(UTC)
    states = [s for s in snapshot.states if not s.id.startswith("light.hall:")]
    states.append(CanonicalState(id="light.hall:firmware_version", value="simulated-v1", quality="healthy",
                                 reasons=["simulated_unrelated_metric"], selected_source="sim:hall-bulb",
                                 as_of=now, valid_until=now + timedelta(minutes=5), simulated=True))
    bad_snapshot = snapshot.model_copy(update={"states": states})

    class Adapter:
        async def snapshot(self):
            return bad_snapshot

    store = TraceStore(tmp_path / "review.sqlite")
    try:
        trace = await HomeCore(Adapter(), store).dry_run(ActionRequest(
            target="light.hall", desired={"power": "on"}, expected_version=snapshot.version,
            expires_at=now + timedelta(seconds=30),
        ))
        assert trace.status == "rejected" and trace.code == "stale_input"
    finally:
        store.close()
