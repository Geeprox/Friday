"""Home-domain adapter over the shared ARL SDK; no model or RPC transport here.

Only non-sensitive runtime/model tools are used. Home credentials and action authority
never enter ARL. Missing capability, identity, timeout and cancellation fail closed.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.metadata
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .runtime_log import observe, stage_span

DEFAULT_HOME = Path.home() / "Library/Application Support/ARL"
TERMINAL = {"succeeded", "failed", "cancelled", "needs_review", "needs_unlock"}


class ARLError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _LocalWorkerBusy(ARLError):
    """No SDK call was made; an owned worker still holds the gate."""


@dataclass(frozen=True)
class ModelTask:
    task_kind: str
    requested_class: str
    deadline: float
    deadline_at_ms: int
    current: Callable[[], bool] | None = None


_model_task = ContextVar("friday_model_task", default=None)


def current_model_task():
    return _model_task.get()


@contextmanager
def model_task(task_kind, requested_class, *, deadline, deadline_at_ms=None, current=None):
    """Local model intent/budget only. Host grants and action authority remain separate."""
    if (type(task_kind) is not str or not 1 <= len(task_kind) <= 100
            or type(requested_class) is not str or requested_class not in {"background", "interactive", "realtime"}
            or not math.isfinite(deadline) or (current is not None and not callable(current))):
        raise ARLError("invalid_model_task")
    if deadline_at_ms is None:
        deadline_at_ms = math.floor(time.time() * 1000 + (deadline - time.monotonic()) * 1000)
    if type(deadline_at_ms) is not int or not 1 <= deadline_at_ms <= 2**53 - 1:
        raise ARLError("invalid_model_task")
    token = _model_task.set(ModelTask(task_kind, requested_class, deadline, deadline_at_ms, current))
    try:
        yield _model_task.get()
    finally:
        _model_task.reset(token)


def model_deadline(timeout):
    deadline = time.monotonic() + timeout
    task = current_model_task()
    return min(deadline, task.deadline) if task else deadline


def sdk_client(home: Path, start=False, *, app="friday"):
    # Import the installed upstream SDK, never vendor it or implement a parallel socket protocol.
    try:
        from arl import PROTOCOL
        from arl.client import Client, RuntimeLauncher
    except ImportError:
        raise ARLError("sdk_missing") from None
    if PROTOCOL != "arl/1":
        raise ARLError("unsupported_protocol")
    # Current SDK launcher inherits its parent's environment. Refuse bootstrap when
    # known home/development credentials would cross that boundary; reconnect remains
    # allowed. Upstream owns any future sanitized-launch environment contract.
    if start and any(value for name, value in os.environ.items()
                     if name in {"HOME_HA_TOKEN", "OPENAI_API_KEY", "JEV_API_KEY"}
                     or (name.startswith(("FRIDAY_", "MILOCO_")) and name.endswith(("TOKEN", "KEY", "PASSCODE")))):
        raise ARLError("runtime_launch_environment_not_isolated")
    return Client(home, app, launcher=RuntimeLauncher(home) if start else None)


def sdk_version():
    try:
        return importlib.metadata.version("arl-runtime")
    except importlib.metadata.PackageNotFoundError:
        return None


def sdk_settings_client(home: Path):
    """Use upstream's public metadata identity, without a launcher or task access."""
    return sdk_client(home, app="settings")


def project_overview(report):
    # Store v2 is unrelated to arl/1 and settings overview schema_version=1.
    if (not isinstance(report, dict) or report.get("protocol") != "arl/1"
            or type(report.get("schema_version")) is not int or report["schema_version"] != 1):
        raise ARLError("unsupported_overview_contract")
    health, policy = report.get("health"), report.get("policy")
    if (not isinstance(health, dict) or any(type(health.get(k)) is not bool for k in ("live", "ready"))
            or not isinstance(policy, dict) or type(policy.get("local_inference_enabled")) is not bool
            or type(policy.get("identity_verifier_configured")) is not bool):
        raise ARLError("invalid_overview")
    # Expose only bounded public labels; never pass arbitrary upstream result fields.
    def rows(key, fields):
        values = report.get(key)
        if not isinstance(values, list) or len(values) > 100:
            raise ARLError("invalid_overview")
        output = []
        for row in values:
            if not isinstance(row, dict):
                raise ARLError("invalid_overview")
            item = {k: row[k] for k in fields if k in row}
            for key, value in item.items():
                if key == "paid":
                    valid = type(value) is bool
                elif key == "models":
                    valid = isinstance(value, list) and len(value) <= 100 and all(label(v) for v in value)
                else:
                    valid = label(value)
                if not valid:
                    raise ARLError("invalid_overview")
            if "name" not in item or "availability" not in item:
                raise ARLError("invalid_overview")
            output.append(item)
        return output
    def label(value):
        import re
        return (isinstance(value, str) and 0 < len(value) <= 200
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", value) is not None
                and ".." not in value and "://" not in value)
    if not label(report.get("version")):
        raise ARLError("invalid_overview")
    return {"status": "ready" if health["live"] and health["ready"] else "starting" if health["live"] else "disconnected",
            "ready": health["live"] and health["ready"], "runtime_version": report["version"],
            "overview_schema_version": 1, "metadata_access": "settings_public_read_only",
            "local_inference_enabled": policy["local_inference_enabled"],
            "identity_verifier_configured": policy["identity_verifier_configured"],
            "providers": rows("providers", ("name", "availability", "models", "paid", "inference_policy")),
            "tools": rows("capabilities", ("name", "availability", "identity_gate", "authorization_gate"))}


def safe_code(exc):
    # Exception bodies may contain prompts or paths. Only bounded machine codes escape.
    code = getattr(exc, "code", "runtime_error")
    return code if isinstance(code, str) and code.isascii() and code.replace("_", "").isalnum() and len(code) < 80 else "runtime_error"


class ARLRuntime:
    def __init__(self, home: Callable[[], Path] | None = None, *, client_factory=sdk_client,
                 settings_client_factory=sdk_settings_client):
        self.home = home or (lambda: DEFAULT_HOME)
        self.client_factory = client_factory
        self.settings_client_factory = settings_client_factory
        # SDK calls can outlive asyncio cancellation until their bounded socket timeout.
        # Keep the gate held in the worker until it finishes: no accumulating backlog.
        self._busy = threading.Lock()
        # Public metadata only, never identity, grants or model results.
        self._status_cache = None
        self._status_cache_home = None
        self._status_generation = 0


    @observe('arl.worker', upstream='arl')
    async def _bounded(self, operation, timeout):
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ARLError("invalid_timeout")
        if not self._busy.acquire(blocking=False):
            raise _LocalWorkerBusy("runtime_busy")
        stop = threading.Event()
        def run():
            try:
                with stage_span('arl.thread', upstream='arl'):
                    return operation(stop)
            except ARLError:
                raise
            except Exception as exc:
                raise ARLError(safe_code(exc)) from None
            finally:
                self._busy.release()
        task = asyncio.create_task(asyncio.to_thread(run))
        # Drain late exceptions; late results never reach a caller or trigger an action.
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            stop.set()
            raise ARLError("deadline_exceeded") from None
        except asyncio.CancelledError:
            stop.set()
            raise

    @observe('arl.status', upstream='arl')
    async def status(self, *, start=False, timeout=3):
        home = self.home()
        if home != self._status_cache_home:
            self._status_cache_home = home
            self._status_cache = None
            self._status_generation += 1
        generation = self._status_generation
        base = {"home": str(home), "sdk_version": sdk_version(), "protocol": "arl/1",
                "ready": False, "action_authority": False, "automatic_cloud_fallback": False,
                "providers": [], "tools": [], "task_decisions": "unsupported",
                "identity": "not_verified_by_friday", "inference_attempted": False}
        def remembered(report):
            if home == self.home() and generation == self._status_generation:
                self._status_cache = copy.deepcopy(report)
            return report

        if not start and not home.exists():
            return remembered({**base, "status": "not_running" if base["sdk_version"] else "sdk_missing",
                               "metadata_freshness": "unavailable", "metadata_refresh_pending": False,
                               "metadata_refresh_code": "not_running", "metadata_refresh_source": "local_configuration"})
        def probe(stop):
            client = self.settings_client_factory(home)
            try:
                report = client.rpc("overview")
            except Exception as exc:
                if not start or safe_code(exc) != "disconnected":
                    raise
                if stop.is_set() or home != self.home():
                    raise ARLError("discarded_configuration_changed")
                # Explicit bootstrap only when absent; an existing strict runtime's
                # public connection must not require Friday's task registration.
                self.client_factory(home, True).connect(timeout=timeout)
                report = client.rpc("overview")
            if stop.is_set() or home != self.home():
                raise ARLError("discarded_configuration_changed")
            return {**base, **project_overview(report)}
        try:
            report = await self._bounded(probe, timeout)
            if home != self.home() or generation != self._status_generation:
                raise ARLError("discarded_configuration_changed")
            return remembered({**report, "metadata_freshness": "current", "metadata_refresh_pending": False,
                               "metadata_refresh_code": "none", "metadata_refresh_source": "sdk"})
        except ARLError as exc:
            if home != self.home() or generation != self._status_generation:
                active_home = self.home()
                current = self._status_cache if self._status_cache_home == active_home else None
                return {**copy.deepcopy(current or {**base, "home": str(active_home), "status": "discarded_configuration_changed"}),
                        "metadata_freshness": "last_known" if current else "unknown",
                        "metadata_refresh_pending": True,
                        "metadata_refresh_code": "discarded_configuration_changed",
                        "metadata_refresh_source": "local_configuration"}
            source = "local_worker" if isinstance(exc, _LocalWorkerBusy) else "sdk"
            # Busy/timeouts/read errors do not prove that ARL disconnected.
            unavailable = exc.code in {"disconnected", "not_running", "sdk_missing", "user_disabled", "unsafe_home"}
            if unavailable:
                return remembered({**base, "status": exc.code, "metadata_freshness": "unavailable",
                                   "metadata_refresh_pending": False, "metadata_refresh_code": exc.code,
                                   "metadata_refresh_source": source})
            previous = self._status_cache if generation == self._status_generation and home == self.home() else None
            return {**copy.deepcopy(previous or {**base, "status": exc.code}),
                    "metadata_freshness": "last_known" if previous else "unknown",
                    "metadata_refresh_pending": True, "metadata_refresh_code": exc.code,
                    "metadata_refresh_source": source}

    @observe('arl.job', upstream='arl')
    async def run_job(self, tool: str, args: dict, *, deadline: float, key: str | None = None,
                      task_kind="model.inference", requested_class="interactive"):
        if tool not in {"runtime.ping", "model.complete"}:
            raise ARLError("tool_not_authorized")
        intent = current_model_task() if tool == "model.complete" else None
        if intent:
            deadline = min(deadline, intent.deadline)
            task_kind, requested_class = intent.task_kind, intent.requested_class
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ARLError("expired")
        # Freeze once before factory/connect/transfer, bounded by the original business deadline.
        deadline_at_ms = math.floor(time.time() * 1000 + remaining * 1000)
        if intent:
            deadline_at_ms = min(deadline_at_ms, intent.deadline_at_ms)
        home = self.home()
        if not home.exists():
            raise ARLError("not_running")
        key = key or "friday-" + str(uuid.uuid4())
        def work(stop):
            client = self.client_factory(home, False)
            job_id = None
            complete = False
            scheduling = accepted = profile = binding = None
            def check():
                if stop.is_set() or time.monotonic() >= deadline:
                    raise ARLError("expired")
                if home != self.home():
                    raise ARLError("discarded_configuration_changed")
                if intent and intent.current and intent.current() is not True:
                    raise ARLError("discarded_model_task_changed")
                if tool == "model.complete" and time.time() * 1000 >= deadline_at_ms:
                    raise ARLError("expired")

            def check_scheduling(job):
                if type(job) is not dict or job.get("id") != job_id:
                    raise ARLError("priority_receipt_mismatch")
                validate_acceptance(scheduling, job.get("scheduling"))
                facts = job["scheduling"]
                if (facts != accepted or facts["effective_class"] not in profile["allowed_classes"]
                        or facts["effective_class"] not in binding["allowed_classes"]
                        or facts["estimate_ms"] < binding["estimate_floor_ms"]
                        or facts["execution_budget_ms"] > binding["execution_budget_ms"]):
                    raise ARLError("priority_receipt_mismatch")
            try:
                check()
                client.connect(timeout=min(2, max(.001, deadline-time.monotonic())))
                check()
                caps = client.rpc("capabilities")
                if caps.get("protocol") != "arl/1" or caps.get("version") != 1:
                    raise ARLError("unsupported_capability_version")
                descriptor = next((t for t in caps["tools"] if t["name"] == tool), None)
                if descriptor is None or descriptor.get("version") != 1 or descriptor.get("permissions"):
                    raise ARLError("unsupported_tool_contract")
                if descriptor.get("availability") != "ready":
                    raise ARLError("tool_unavailable")
                # Reuse upstream schema validation, including actual deployment bounds.
                from arl.contracts import validate
                validate(args, descriptor["inputSchema"])
                if tool == "model.complete":
                    if len(args["prompt"].encode()) > 1024 or args["max_tokens"] > 256:
                        raise ARLError("model_budget_exceeded")
                    check()
                    providers = client.rpc("providers")
                    provider = next((p for p in providers if p["name"] == args["provider"]), None)
                    if not provider or provider.get("availability") != "ready":
                        raise ARLError("provider_unavailable")
                    if provider.get("paid") is not False:
                        raise ARLError("cloud_not_enabled")
                    if args["model"] not in provider.get("models", []):
                        raise ARLError("model_not_installed")
                check()
                # One SDK RPC, no automatic reconnect/resubmit after a lost submission.
                # ARL owns durable dedupe. A lost receipt is unknown, never a fresh job.
                priority_names = ("negotiate_priority_v2", "priority_request_v2", "submit_priority")
                if tool == "model.complete" and all(callable(getattr(client, name, None)) for name in priority_names):
                    from arl.priority import validate_profile_v2, select_binding_v2, validate_acceptance
                    # Convenience methods reconnect with a new timeout. Use the existing
                    # bounded connection and upstream v2 schema/route helpers without replay.
                    profile = copy.deepcopy(client.rpc("priority.negotiate.v2", {"requested_profile": {
                        "classes": ["background", "interactive", "realtime"]}}))
                    check()
                    validate_profile_v2(profile)
                    # Only an exact host-produced binding for the validated generic model
                    # route can select a resource. Null fingerprint is exact, never a wildcard.
                    binding = select_binding_v2(profile, submit_method="priority.submit.v1", tool=tool,
                        task_kind=task_kind, provider=args["provider"], model=args["model"], model_fingerprint=None)
                    scheduling = client.priority_request_v2(profile, submit_method="priority.submit.v1", tool=tool,
                        task_kind=task_kind, provider=args["provider"], model=args["model"], model_fingerprint=None,
                        requested_class=requested_class, deadline_at_ms=deadline_at_ms,
                        estimate_ms=binding["estimate_floor_ms"], allow_interrupt=False,
                        allow_fallback=True, minimum_class="background")
                    check()
                    submitted = client.rpc("priority.submit.v1", {"tool": tool, "args": args,
                        "key": key, "scheduling": scheduling})
                else:
                    if tool == "model.complete" and any(callable(getattr(client, name, None)) for name in
                            (*priority_names, "negotiate_priority", "priority_request")):
                        raise ARLError("priority_v2_required")
                    # Old SDK wire stays unchanged. An enabled host treats it conservatively;
                    # no priority/effective class is claimed and no negotiation error retries here.
                    submitted = client.rpc("submit", {"tool": tool, "args": args, "key": key})
                job_id = submitted["id"]
                if scheduling:
                    if type(job_id) is not str or not 1 <= len(job_id) <= 128:
                        job_id = None
                        raise ARLError("priority_receipt_mismatch")
                    accepted = copy.deepcopy(submitted.get("scheduling"))
                    check_scheduling(submitted)
                check()
                job = submitted
                while job["state"] not in TERMINAL:
                    check()
                    job = client.rpc("get", {"id": job_id})
                    if scheduling:
                        check_scheduling(job)
                    stop.wait(.02)
                check()
                if job["state"] != "succeeded":
                    raise ARLError("job_" + job["state"])
                if scheduling is None and "scheduling" in job:
                    raise ARLError("priority_receipt_mismatch")
                complete = True
                return job
            finally:
                if job_id and not complete:
                    try:
                        client.rpc("cancel", {"id": job_id})
                    except Exception:
                        pass  # Cancellation is best effort, never a claim of undoing inference.
        return await self._bounded(work, min(30, remaining))

    @observe('arl.complete', upstream='arl')
    async def complete(self, provider: str, model: str, prompt: str, *, deadline: float, max_tokens=256,
                       task_kind="model.inference", requested_class="interactive"):
        job = await self.run_job("model.complete", {"provider": provider, "model": model,
            "prompt": prompt, "max_tokens": max_tokens}, deadline=deadline,
            task_kind=task_kind, requested_class=requested_class)
        result = job["result"]
        if (not isinstance(result, dict) or result.get("provider") != provider or result.get("model") != model
                or type(result.get("simulated")) is not bool):
            raise ARLError("invalid_model_result")
        result = dict(result)
        result.pop("scheduling", None)  # A provider cannot mint host scheduling facts.
        if "scheduling" in job:
            result["scheduling"] = copy.deepcopy(job["scheduling"])
        return result

    async def smoke(self):
        job = await self.run_job("runtime.ping", {}, deadline=time.monotonic() + 3)
        if job["result"] != {"pong": True}:
            raise ARLError("invalid_ping_result")
        return {"status": "sdk_roundtrip_pass", "job_id": job["id"], "inference_attempted": False,
                "device_write_attempted": False, "hardware_verified": False}
