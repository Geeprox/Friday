"""Actual shared SDK/process roundtrip, with synthetic model data and no device access."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from homeplane.arl import ARLRuntime, sdk_client, sdk_settings_client


async def check_strict_public_metadata():
    """Real socket with a test verifier denying all task identities; no production trust."""
    from homeplane.arl import ARLError
    with tempfile.TemporaryDirectory(prefix="friday-arl-strict-", dir="/private/tmp") as directory:
        home = Path(directory)
        command = "from arl.service import Runtime; import sys; Runtime(sys.argv[1], identity_verifier=lambda connection, app: None).run()"
        environment = {k: v for k, v in os.environ.items() if k in {"PATH", "TMPDIR", "LANG", "LC_ALL"}}
        child = subprocess.Popen([sys.executable, "-c", command, str(home)], env=environment,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await asyncio.to_thread(sdk_settings_client(home).connect, 3)
            runtime = ARLRuntime(lambda: home)
            report = await runtime.status()
            assert report["ready"] and report["identity_verifier_configured"]
            assert report["identity"] == "not_verified_by_friday"
            assert (await runtime.status(start=True))["ready"]
            try:
                await runtime.smoke()
            except ARLError as exc:
                assert exc.code == "app_identity_unverified", exc.code
            else:
                raise AssertionError("Public status granted task identity")
            return {"real_socket": True, "public_overview": "PASS", "task_identity": "REFUSED",
                    "verifier": "synthetic_reject_all", "production_identity_verified": False}
        finally:
            if child.poll() is None:
                child.terminate()
            await asyncio.to_thread(child.wait, 5)


async def check():
    # Short owner-only Unix socket path. Never reuse a production ARL home or queue.
    with tempfile.TemporaryDirectory(prefix="friday-arl-", dir="/private/tmp") as directory:
        home = Path(directory)
        clients = []
        def factory(path, start):
            client = sdk_client(path, start)
            clients.append(client)
            return client
        runtime = ARLRuntime(lambda: home, client_factory=factory)
        evidence = {"observed_at": datetime.now(UTC).isoformat(), "synthetic_input": True,
                    "real_sdk_process": True, "real_model_inference": False,
                    "home_credentials_sent": False, "device_writes": False,
                    "hardware_verified": False, "production_identity_verified": False}
        try:
            report = await runtime.status(start=True, timeout=10)
            assert report["ready"], report["status"]
            evidence["runtime_version"] = report["runtime_version"]
            evidence["sdk_version"] = report["sdk_version"]
            evidence["protocol"] = report["protocol"]
            evidence["providers"] = report["providers"]
            evidence["tools"] = report["tools"]
            evidence["metadata_access"] = report["metadata_access"]
            evidence["overview_schema_version"] = report["overview_schema_version"]
            settings_client = sdk_settings_client(home)
            assert settings_client.app == "settings" and settings_client.launcher is None
            from arl.contracts import Fault
            for method in ("capabilities", "providers", "submit", "get", "stop"):
                try:
                    settings_client.rpc(method)
                except Fault as exc:
                    assert exc.code == "method_not_authorized", (method, exc.code)
                else:
                    raise AssertionError("Public settings client accepted " + method)
            evidence["settings_task_methods"] = "REFUSED_BY_REAL_SERVICE"
            evidence["ping"] = await runtime.smoke()
            args = {"provider": "mock", "model": "deterministic-v1", "prompt": "simulated home fixture", "max_tokens": 8}
            first = await runtime.run_job("model.complete", args, deadline=time.monotonic()+3, key="SIMULATED-dedupe")
            second = await runtime.run_job("model.complete", args, deadline=time.monotonic()+3, key="SIMULATED-dedupe")
            assert first["id"] == second["id"] and second["attempts"] == 1 and second["result"]["simulated"]
            evidence["model"] = {"provider": "mock", "simulated": True, "state": second["state"], "attempts": second["attempts"], "deduplicated": True}
            stopped = sdk_client(home).rpc("stop")
            assert stopped["disabled"]
            after = await runtime.status(start=True)
            assert after["status"] == "user_disabled"
            evidence["disabled_bootstrap"] = after["status"]
            evidence["strict_public_metadata"] = await check_strict_public_metadata()
            evidence["status"] = "PASS_SHARED_SDK_ONLY"
            return evidence
        finally:
            for client in clients:
                for child in getattr(client.launcher, "children", []):
                    if child.poll() is None:
                        child.terminate()
                    await asyncio.to_thread(child.wait, 5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/evidence/arl-sdk-roundtrip.json")
    args = parser.parse_args()
    report = asyncio.run(check())
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("status", "runtime_version", "protocol", "real_model_inference", "hardware_verified")}))


if __name__ == "__main__":
    main()
