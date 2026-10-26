"""Task-owned ARL Codex scope. No production grants/configuration are changed."""

import atexit
import hashlib
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from arl.client import Client
from arl.codex_server import NAME, CodexService, CodexTextAdapter
from arl.compat_host import compose_codex_service
from arl.contracts import Fault
from arl.identity import RegisteredPeerVerifier, process_identity


class IntentRuntime:
    def __init__(self, model):
        self.active = True
        self.model = model
        self.pid = os.getpid()
        self.caller = "friday-embodied"
        self.runtime = None
        self.thread = None
        self.state = tempfile.TemporaryDirectory(
            prefix="friday-arl-",
            dir=str(__import__("homeplane.embodied.paths", fromlist=["PRIVATE"]).PRIVATE),
        )
        Path(self.state.name).chmod(0o700)
        self.peer = RegisteredPeerVerifier({self.caller})
        self.peer.register(self.caller, self.pid, process_identity(self.pid)["executable"])
        self.pins = {
            p: hashlib.sha256(p.read_bytes()).digest()
            for p in [
                Path(__file__),
                Path(__file__).with_name("intent.py"),
                Path(__file__).with_name("voice_worker.py"),
            ]
        }
        self.service = CodexService(
            CodexTextAdapter(enabled=True),
            caller=self.caller,
            model=model,
            max_calls=32,
            max_output_bytes=8192,
            reasoning_effort="low",
            service_tier="default",
            allow_model_selection=True,
            authorization_current=self.authorized,
            peer_current=lambda: self.peer.is_registered(self.caller, self.pid),
        )
        self.runtime = compose_codex_service(
            Path(self.state.name), service=self.service, peer_verifier=self.peer, isolated=True
        )
        self.runtime.shutdown_grace = 3
        self.thread = threading.Thread(target=self.runtime.run, daemon=True)
        self.thread.start()
        self.client = Client(Path(self.state.name), self.caller)
        self.client.connect(timeout=5)
        atexit.register(self.close)

    def authorized(self):
        return (
            self.active
            and all(hashlib.sha256(p.read_bytes()).digest() == h for p, h in self.pins.items())
        )

    def report(self):
        status = self.service.status()
        return dict(route="arl_codex_text", state=status.get("state", "unavailable"),
                    remaining_calls=status.get("remaining_calls", 0), automatic_renewal=False,
                    tools=False, device_authority=False)

    def complete(self, prompt):
        if (
            self.service.status()["state"] != "ready"
            or time.monotonic() - (self.service.checked_mono or 0) > 45
        ):
            self.service.preflight()
        model = "gpt-6-luna" if any(x["model"] == "gpt-6-luna" for x in self.service.catalog) else self.model
        job = self.client.submit(
            NAME,
            {
                "model": model,
                "input": prompt,
                "max_output_bytes": 8192,
                "reasoning_effort": "low",
                "service_tier": "default",
            },
            "voice-" + uuid.uuid4().hex,
        )
        deadline = time.monotonic() + 33
        try:
            while job["state"] not in {"succeeded", "failed", "cancelled", "needs_review"}:
                if time.monotonic() > deadline:
                    raise Fault("intent_deadline")
                time.sleep(0.05)
                job = self.client.rpc("get", {"id": job["id"]})
            if job["state"] != "succeeded":
                raise Fault(
                    job.get("error") if isinstance(job.get("error"), str) else "intent_" + job["state"]
                )
            return job["result"]
        except BaseException:
            try:
                self.client.rpc("cancel", {"id": job["id"]})
            except Exception:
                pass
            raise

    def close(self):
        if not self.active:
            return
        self.active = False
        self.service.close()
        if self.runtime:
            self.runtime.shutdown()
        if self.thread:
            self.thread.join(4)
        self.peer.revoke(self.pid)
        self.state.cleanup()
