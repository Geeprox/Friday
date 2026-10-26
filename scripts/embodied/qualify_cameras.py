"""Explicit bounded registered-stream acceptance; restore original privacy shutters.

Opening shutters requires the operator to explicitly supply --open-privacy.
The durable receipt is written before each property change. --restore recovers
an interrupted run without opening a camera or starting capture.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def main():
    import httpx
    from miot.cloud import MIoTHttpClient
    from miot.types import MIoTGetPropertyParam, MIoTSetPropertyParam

    from homeplane.embodied.paths import PRIVATE, PROJECT, configuration
    from homeplane.embodied.ptz_control import alive, atomic
    from homeplane.embodied.miot_spec import properties, property_ref

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--open-privacy", action="store_true")
    args = parser.parse_args()
    if not args.restore and not args.open_privacy:
        parser.error("explicit --open-privacy is required for this hardware acceptance run")
    receipt = PRIVATE / "camera-probe-lease.json"
    cameras = configuration()["cameras"]
    login = json.loads((PRIVATE / "miot-session.json").read_text())
    api = MIoTHttpClient(cloud_server="cn", access_token=login["access_token"])
    results = dict(cameras={}, restored=False, audio_enabled=False, recorded=False)

    async def read(camera):
        rows = await asyncio.wait_for(api.get_props_async([
            MIoTGetPropertyParam(did=camera["device"]["did"], siid=property_ref(properties(camera.get("spec", {})), "on", "read")[0], piid=property_ref(properties(camera.get("spec", {})), "on", "read")[1])]), 15)
        if len(rows) != 1 or rows[0].get("code") != 0 or type(rows[0].get("value")) is not bool:
            raise ValueError("camera_power_read_unavailable")
        return rows[0]["value"]

    async def write(camera, value):
        result = await asyncio.wait_for(api.set_prop_async(
            MIoTSetPropertyParam(did=camera["device"]["did"], siid=property_ref(properties(camera.get("spec", {})), "on", "write")[0], piid=property_ref(properties(camera.get("spec", {})), "on", "write")[1], value=value)), 15)
        if result.get("code") != 0:
            raise ValueError("camera_power_write_rejected")
        if await read(camera) is not value:
            raise ValueError("camera_power_readback_unconfirmed")

    lease = None
    try:
        if receipt.exists():
            old = json.loads(receipt.read_text())
            if old.get("state") != "restored":
                if alive(old["pid"]):
                    raise ValueError("camera_probe_owned")
                lease = old
                args.restore = True
        if not args.restore:
            before = {c["id"]: await read(c) for c in cameras}
            lease = dict(state="owned", pid=os.getpid(), before=before, changed=[])
            atomic(receipt, lease)
            for camera in cameras:
                if not before[camera["id"]]:
                    lease["changed"].append(camera["id"])
                    atomic(receipt, lease)
                    await write(camera, True)
            await asyncio.sleep(6)
            origin = configuration()["main_origin"]
            async with httpx.AsyncClient(base_url=origin, headers={"Origin": origin}, trust_env=False, timeout=10) as client:
                for camera in cameras:
                    r = await client.post(f"/api/embodied/cameras/{camera['id']}/preview", json={})
                    r.raise_for_status()
                deadline = time.monotonic() + 110
                while time.monotonic() < deadline:
                    for camera in cameras:
                        r = await client.get(f"/api/embodied/cameras/{camera['id']}/preview")
                        r.raise_for_status()
                        v = r.json()
                        results["cameras"][camera["id"]] = dict(state=v.get("state"), frames=v.get("frames"))
                    print(json.dumps(dict(phase="streams", cameras=list(results["cameras"].values()))), flush=True)
                    if all(v["state"] == "live" and (v["frames"] or 0) >= 10 for v in results["cameras"].values()):
                        results["registered_streams_readback_verified"] = True
                        break
                    await asyncio.sleep(5)
    finally:
        if lease:
            errors = []
            for camera in cameras:
                if camera["id"] in lease["changed"]:
                    try:
                        await write(camera, lease["before"][camera["id"]])
                    except Exception as error:
                        errors.append(type(error).__name__)
            lease["state"] = "restore_pending" if errors else "restored"
            atomic(receipt, lease)
            results.update(restored=not errors, restore_errors=errors)
        await api.deinit_async()
        output = PROJECT / ".artifacts/embodied-migration/camera-acceptance.json"
        output.write_text(json.dumps(results, indent=2))
        print(json.dumps({k: v for k, v in results.items() if k != "cameras"}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
