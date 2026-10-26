"""Workspace-contained supervisor. No cloud images, credentials, or media in process arguments."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def environment():
    env = dict(os.environ)
    env["MILOCO_HOME"] = str(ROOT / "data/miloco")
    env["HF_HOME"] = str(ROOT / "data/huggingface")
    env["FRIDAY_CONFIG"] = env.get("FRIDAY_CONFIG", str(ROOT / "friday.home.json"))
    config = ROOT / "data/miloco/config.json"
    if config.exists():
        token = json.loads(config.read_text()).get("server", {}).get("token")
        if token:
            env["FRIDAY_MILOCO_TOKEN"] = token
    return env


async def main():
    import httpx

    env = environment()
    logs = ROOT / "data/logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Refuse to start a duplicate stack; never kill a process just because it owns a port.
    async with httpx.AsyncClient(trust_env=False) as client:
        for port in (8730, 1810):
            try:
                await client.get(f"http://127.0.0.1:{port}/", timeout=1)
            except httpx.ConnectError:
                continue
            except httpx.TimeoutException:
                pass
            raise SystemExit(f"Port {port} is occupied. Stop the existing service before starting Friday.")
    commands = {
        "miloco": [str(ROOT / "data/miloco-venv/bin/miloco-backend")],
        "friday": [sys.executable, "-m", "friday.launch"],
    }
    processes, tasks = {}, []
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)
    try:
        for name, args in commands.items():
            path = logs / (name + ".log")
            if path.exists() and path.stat().st_size > 2_000_000:
                path.replace(path.with_suffix(".previous.log"))
            with path.open("ab") as output:
                path.chmod(0o600)
                processes[name] = await asyncio.create_subprocess_exec(
                    *args, cwd=ROOT, env=env, stdout=output, stderr=output
                )
            print(f"{name}: started", flush=True)
            tasks.append(asyncio.create_task(processes[name].wait()))
        print("Friday: http://127.0.0.1:8730 · Miloco: http://127.0.0.1:1810", flush=True)
        tasks.append(asyncio.create_task(shutdown.wait()))
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for name, process in processes.items():
            if process.returncode is not None:
                print(f"{name}: exited ({process.returncode}); see data/logs/{name}.log", flush=True)
    finally:
        for process in processes.values():
            if process.returncode is None:
                process.terminate()
        for process in processes.values():
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in tasks:
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
