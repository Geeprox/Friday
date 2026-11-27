"""Supervise one explicitly started, read-only Geek worker outside Home Core.

No process recovery replay, persistent session token or permanent-login claim.
The PIN stays in the existing private credential file, never in argv or reports.
"""
from __future__ import annotations

import asyncio
import os
import time
from uuid import uuid4

from .geek_contracts import GeekSessionView, WorkerStatus
from .geek_setup import ROOT

class GeekSession:
    def __init__(self, setup, *, launcher=None, clock=time.monotonic):
        self.setup, self.launcher, self.clock = setup, launcher or self._launch, clock
        self.gate = asyncio.Lock()
        self.process = self.task = None
        self.url = None
        self.generation = 0
        self.last_seen = self.last_inventory = None
        self.report = {"status": "stopped"}
        self.last_change = None
        self.inventory_root = None
        self.inventory_gate = asyncio.Lock()
        self.evidence_lease = None

    async def _launch(self, url, output):
        return await asyncio.create_subprocess_exec(
            "node", "--max-old-space-size=192", str(ROOT / "workers/geek/session.mjs"),
            "--url", url, "--passcode-file", str(self.setup.secret.absolute()),
            "--output", str(output.absolute()), cwd=ROOT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=8192,
            env={k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ},
        )

    def live(self):
        return bool(self.process and self.process.returncode is None and self.task and not self.task.done())

    def status(self):
        report = self.report.copy()
        live = self.live()
        recent = self.last_inventory is not None and 0 <= self.clock() - self.last_inventory <= 180
        if report["status"] == "online" and (not live or not recent):
            report["status"] = "stale" if live else "worker_stopped"
        return GeekSessionView.model_validate({
            **report, "last_change": self.last_change, "session_live": live,
            "credential_available": self.setup.available(), "inventory_stale": report["status"] != "online",
        }).model_dump(exclude_none=True)

    async def connect(self, url):
        async with self.gate:
            if self.live() and self.url == url:
                return self.status()
            await self._stop()
            if not url or not self.setup.available():
                self.report = {"status": "pairing_required"}
                return self.status()
            # Share the existing URL validation; no new discovery or credential destination.
            if not self.setup.valid_url(url):
                self.report = {"status": "unsupported_gateway_address"}
                return self.status()
            try:
                self.setup.prune()
                self.setup.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
                if any(p.is_symlink() for p in (self.setup.evidence, *self.setup.evidence.parents)):
                    raise ValueError("evidence_symlink")
                os.chmod(self.setup.evidence, 0o700)
                output = self.setup.evidence / ("session-" + str(uuid4()))
                self.evidence_lease = self.setup.lease(output)
                launch = asyncio.create_task(self.launcher(url, output))
                try:
                    process = await asyncio.wait_for(asyncio.shield(launch), timeout=10)
                except (asyncio.CancelledError, TimeoutError):
                    # A process appearing after its HTTP caller has gone away is
                    # never admitted; close only that owned late process.
                    def clean_late(task):
                        if not task.cancelled() and task.exception() is None:
                            asyncio.create_task(self._terminate(task.result()))
                    launch.add_done_callback(clean_late)
                    raise
                self.process, self.url = process, url
                self.inventory_root = output
                self.last_seen = self.clock()
                self.last_inventory = None
                self.report = {"status": "starting"}
                self.generation += 1
                self.task = asyncio.create_task(self._read(process, self.generation))
            except (OSError, ValueError, TimeoutError):
                self._release_evidence()
                self.report = {"status": "worker_unavailable"}
            except asyncio.CancelledError:
                self._release_evidence()
                raise
            return self.status()

    async def _read(self, process, generation):
        window, messages = self.clock(), 0
        try:
            while generation == self.generation:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=190)
                if not line:
                    break
                if len(line) > 4096 or not line.endswith(b"\n"):
                    raise ValueError("worker_output_budget")
                if self.clock() - window >= 10:
                    window, messages = self.clock(), 0
                messages += 1
                if messages > 12:
                    raise ValueError("worker_rate_budget")
                report = WorkerStatus.model_validate_json(line).model_dump(exclude_none=True)
                if report["status"] == "online" and any(report.get(k) is None for k in (
                    "device_count", "graph_count", "scope_count", "fingerprint", "changes", "session_seconds"
                )):
                    raise ValueError("incomplete_inventory")
                if generation != self.generation:
                    break
                self.report = report
                self.last_seen = self.clock()
                if report["status"] == "online":
                    self.last_inventory = self.clock()
                    changes = report["changes"]
                    if any(changes.values()):
                        self.last_change = {"checked_at": report["checked_at"], **changes}
            if generation == self.generation and self.report["status"] not in {"blocked", "stopped"}:
                self.report = {"status": "worker_stopped"}
        except (ValueError, OSError, TimeoutError):
            if generation == self.generation:
                self.report = {"status": "worker_fault", "failure_code": "check_failed"}
        finally:
            await self._terminate(process)
            if generation == self.generation:
                self._release_evidence()

    @staticmethod
    async def _terminate(process):
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.stdin:
            process.stdin.close()

    async def _stop(self):
        self.generation += 1
        task, process = self.task, self.process
        self.task = self.process = None
        self.url = None
        self.inventory_root = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # A just-created reader can be cancelled before its coroutine enters
        # try/finally. Always reap the owned process as well.
        if process:
            await self._terminate(process)
        self._release_evidence()
        self.report = {"status": "stopped"}
        self.last_seen = self.last_inventory = None
        self.last_change = None

    def _release_evidence(self):
        if self.evidence_lease is not None:
            os.close(self.evidence_lease)
            self.evidence_lease = None

    async def close(self):
        async with self.gate:
            await self._stop()

    async def release(self):
        await self.close()

    async def refresh(self):
        return self.status()

    async def inventory(self):
        from .geek_contracts import GeekInventoryView
        from .geek_inventory import read_inventory

        def unavailable(status):
            return GeekInventoryView(session=self.status(), status=status)

        before = self.status()
        if before["status"] != "online" or not self.inventory_root:
            return unavailable("stale" if self.last_inventory is not None else "unavailable")
        if self.inventory_gate.locked():
            return unavailable("unavailable")
        generation, root, fingerprint = self.generation, self.inventory_root, before["fingerprint"]
        async with self.inventory_gate:
            try:
                result = await asyncio.wait_for(asyncio.to_thread(
                    read_inventory, root, fingerprint, before["graph_count"]
                ), timeout=2)
            except (OSError, ValueError, TypeError, KeyError, RecursionError, TimeoutError):
                return unavailable("integrity_failed")
        after = self.status()
        # A result from an old session/endpoint or prior content is never shown
        # as current, even when local IO finishes after disconnect/reconnect.
        if generation != self.generation or root != self.inventory_root or after["status"] != "online" or fingerprint != after.get("fingerprint"):
            return unavailable("stale")
        return GeekInventoryView(session=after, status="ready", **result)
