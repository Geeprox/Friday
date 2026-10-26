"""Authorized real HA read-only stream probe; exports only aggregate evidence."""
import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from homeplane.ha_configured import build


async def check(config, seconds):
    adapter = await build(config)
    try:
        async with asyncio.timeout(45):
            started = await adapter.subscribe()
            if started["status"] != "connected":
                raise ValueError("ha_subscription_not_ready")
            first = await adapter.snapshot()
            await asyncio.sleep(seconds)
            last = await adapter.snapshot()
            health = adapter.health()
            if not health["continuous_subscription"]:
                raise ValueError("ha_subscription_lost_during_probe")
            temperatures = [o for o in last.observations if o.capability == "temperature"]
            report = {"observed_at": datetime.now(UTC).isoformat(), "status": "PASS_REAL_READ_ONLY_STREAM",
                      "ha_version": health["ha_version"], "observation_seconds": seconds,
                      "counts": health["subscription"]["counts"], "event_kinds": dict(Counter(e.kind for e in last.events)),
                      "function_count": len(last.entities), "device_count": sum(d.category == "device" for d in last.devices),
                      "service_count": sum(d.category == "service" for d in last.devices), "projection_count": len(last.devices),
                      "temperature_count": len(temperatures), "temperature_hardware_sample_times_known": sum(o.measured_at is not None for o in temperatures),
                      "snapshot_version_changed": first.version != last.version,
                      "global_event_order": "unavailable", "real_device_writes": 0,
                      "hardware_verified": False, "real_outage_test": "NOT_RUN",
                      "restart_replay": "NOT_RUN_REAL_SIMULATED_REGRESSION_ONLY",
                      "sensitivity": "household_aggregate_metadata", "retention_days": 7,
                      "contains_credentials_raw_frames_entity_ids_or_recognized_content": False}
    finally:
        await adapter.close()
    report["worker_closed"] = adapter._stream.task is None
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/homeplane/settings.json")
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--output", default="docs/evidence/ha-stream-live.json")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 20:
        parser.error("--seconds must be between 1 and 20")
    try:
        report = asyncio.run(check(Path(args.config), args.seconds))
    except Exception:
        # Upstream credential/network exceptions may contain sensitive text.
        raise SystemExit("HA_READ_ONLY_STREAM_PROBE_FAILED; no device command sent") from None
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("status", "ha_version", "function_count", "device_count", "worker_closed", "real_device_writes")}))


if __name__ == "__main__":
    main()
