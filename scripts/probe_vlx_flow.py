#!/usr/bin/env python3
"""Inspect readiness by default; --execute-public-demo calls one public preset.

Read the endpoint and key from the user's local environment. Never takes a
household video path, camera URL, or key on the command line.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeplane.embodied.vlx_flow import FlowConfig, probe_public_preset


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-public-demo", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=15)
    args = parser.parse_args()
    endpoint = os.environ.get("VLX_API_ENDPOINT", "")
    key = os.environ.get("VLX_API_KEY", "") or os.environ.get("OM_API_KEY", "")
    output = Path(__file__).resolve().parents[1] / ".artifacts/vlx-flow"
    readiness = dict(
        endpoint_configured=bool(endpoint),
        key_configured=bool(key),
        public_preset_only=True,
        automatic_monitoring=False,
        network_attempted=False,
    )
    if not args.execute_public_demo:
        print(json.dumps(readiness, ensure_ascii=False))
        return
    try:
        config = FlowConfig(endpoint, key)
    except ValueError:
        print(json.dumps(dict(readiness, reason="configure_endpoint_and_key_locally")))
        return
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = output / "pending-task.json"
    if pending.exists():
        print(json.dumps(dict(readiness, reason="review_previous_pending_task_before_retry")))
        return

    state = dict(
        task_id=None, stop="unconfirmed", endpoint_sha256=hashlib.sha256(endpoint.encode()).hexdigest()
    )
    # Persist the submission intent even if the network fails before taskCreated.
    # Unknown remote state must not permit automatic repeated submissions.
    with pending.open("x") as stream:
        pending.chmod(0o600)
        json.dump(state, stream)

    def save_task(task_id):
        state["task_id"] = task_id
        pending.write_text(json.dumps(state))

    receipt = await probe_public_preset(config, execute=True, max_seconds=args.max_seconds, on_task=save_task)
    target = output / "latest-receipt.json"
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    target.chmod(0o600)
    if receipt["stop"] == "accepted":
        pending.unlink(missing_ok=True)
    # Captions stay in the local receipt, never general stdout/diagnostics.
    print(json.dumps({k: v for k, v in receipt.items() if k != "segments"}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
