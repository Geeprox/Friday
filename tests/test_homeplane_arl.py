"""Synthetic SDK fixtures. Actual ARL process integration is a separate checkpoint."""
import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.arl import ARLError, ARLRuntime, sdk_client
from homeplane.decision import DecisionRequest
from homeplane.decision_providers import ARLDecisionProvider, registry_descriptors

pytest.importorskip("arl", reason="Install the verified shared ARL SDK for upstream contract tests")

MODEL_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["provider", "model", "prompt", "max_tokens"], "properties": {
        "provider": {"type": "string", "minLength": 1, "maxLength": 64},
        "model": {"type": "string", "minLength": 1, "maxLength": 200},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 1024},
        "max_tokens": {"type": "integer", "minimum": 8, "maximum": 512}}}


class FixtureFault(Exception):
    def __init__(self, code):
        self.code = code


class SDK:
    def __init__(self):
        self.calls, self.factory_calls = [], []
        self.error = None
        self.delay = 0
        self.submit_error = False
        self.state = "succeeded"
        self.providers = [{"name": "mock", "availability": "ready", "models": ["deterministic-v1"], "paid": False},
            {"name": "codex_app_server", "availability": "disabled", "paid": True}]
        self.start = threading.Event()

    def settings_factory(self, home):
        self.factory_calls.append((home, False))
        return self

    def factory(self, home, start):
        self.factory_calls.append((home, start))
        return self

    def connect(self, timeout=10):
        self.calls.append(("connect", timeout))

    def rpc(self, method, params=None):
        self.calls.append((method, params))
        if self.error:
            raise FixtureFault(self.error)
        if method == "health":
            return {"ready": True, "version": "0.1.0"}
        if method == "overview":
            return {"schema_version": 1, "protocol": "arl/1", "version": "0.1.1",
                    "health": {"live": True, "ready": True},
                    "policy": {"local_inference_enabled": False, "identity_verifier_configured": True},
                    "providers": self.providers, "capabilities": [{"name": "model.complete",
                        "availability": "ready", "identity_gate": "none", "authorization_gate": "not_required"}]}
        if method == "capabilities":
            return {"version": 1, "protocol": "arl/1", "local_inference_enabled": False, "tools": [
                {"name": "runtime.ping", "version": 1, "permissions": [], "availability": "ready",
                 "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
                {"name": "model.complete", "version": 1, "permissions": [], "availability": "ready", "inputSchema": MODEL_SCHEMA}]}
        if method == "providers":
            return self.providers
        if method == "submit":
            self.start.set()
            time.sleep(self.delay)
            if self.submit_error:
                raise FixtureFault("disconnected")
            self.tool = params["tool"]
            self.args = params["args"]
            return self.job()
        if method == "get":
            return self.job()
        if method == "cancel":
            return self.job()
        raise AssertionError(method)

    def job(self):
        result = {"pong": True} if self.tool == "runtime.ping" else {
            "provider": self.args["provider"], "model": self.args["model"], "text": "simulated",
            "simulated": True, "usage": {"output_tokens": 8}}
        return {"id": "SIMULATED-job", "state": self.state, "tool": self.tool, "result": result, "error": None}


@pytest.fixture
def setup(tmp_path):
    sdk = SDK()
    runtime = ARLRuntime(lambda: tmp_path, client_factory=sdk.factory, settings_client_factory=sdk.settings_factory)
    return runtime, sdk


@pytest.mark.asyncio
async def test_passive_status_and_explicit_bootstrap(setup):
    runtime, sdk = setup
    report = await runtime.status()
    assert report["ready"] and not report["inference_attempted"] and not report["action_authority"]
    assert sdk.factory_calls[0][1] is False
    assert report["metadata_access"] == "settings_public_read_only"
    assert report["identity"] == "not_verified_by_friday" and report["identity_verifier_configured"]
    assert [m for m, _ in sdk.calls] == ["overview"]
    assert not any(method in {"submit", "connect"} for method, _ in sdk.calls)
    sdk.calls.clear()
    assert (await runtime.status(start=True))["ready"]
    assert all(start is False for _, start in sdk.factory_calls)
    assert [m for m, _ in sdk.calls] == ["overview"]


@pytest.mark.asyncio
async def test_explicit_bootstrap_only_when_public_socket_is_absent(setup):
    runtime, sdk = setup
    original = sdk.rpc
    def rpc(method, params=None):
        if method == "overview" and not any(m == "connect" for m, _ in sdk.calls):
            sdk.calls.append((method, params))
            raise FixtureFault("disconnected")
        return original(method, params)
    sdk.rpc = rpc
    assert (await runtime.status())["status"] == "disconnected"
    assert not any(m == "connect" for m, _ in sdk.calls)
    sdk.calls.clear()
    assert (await runtime.status(start=True))["ready"]
    assert [m for m, _ in sdk.calls] == ["overview", "connect", "overview"]
    assert sdk.factory_calls[-1][1] is True


@pytest.mark.asyncio
async def test_missing_runtime_is_passive_and_private_errors_sanitized(tmp_path):
    sdk = SDK()
    runtime = ARLRuntime(lambda: tmp_path / "missing", client_factory=sdk.factory,
                         settings_client_factory=sdk.settings_factory)
    assert (await runtime.status())["ready"] is False
    assert not sdk.calls and not (tmp_path / "missing").exists()
    runtime.home = lambda: tmp_path
    sdk.error = "private token / text"
    assert (await runtime.status())["status"] == "runtime_error"
    sdk.error = "user_disabled"
    assert (await runtime.status(start=True))["status"] == "user_disabled"


@pytest.mark.asyncio
async def test_real_schema_boundary_explicit_model_and_no_action_tools(setup):
    runtime, sdk = setup
    result = await runtime.complete("mock", "deterministic-v1", "simulated", deadline=time.monotonic()+2)
    assert result["simulated"]
    submit = next(p for m, p in sdk.calls if m == "submit")
    assert submit["tool"] == "model.complete" and set(submit["args"]) == {"provider", "model", "prompt", "max_tokens"}
    assert (await runtime.smoke())["hardware_verified"] is False
    with pytest.raises(ARLError, match="tool_not_authorized"):
        await runtime.run_job("apple.calendar.list", {}, deadline=time.monotonic()+1)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,prompt,tokens,reason", [
    ("mock", "deterministic-v1", "中"*500, 256, "model_budget_exceeded"),
    ("mock", "deterministic-v1", "simulated", 257, "model_budget_exceeded"),
    ("mock", "missing-v1", "simulated", 256, "model_not_installed"),
    ("codex_app_server", "simulated-model", "simulated", 256, "provider_unavailable"),
    ("mock", "deterministic-v1", "simulated", True, "invalid_arguments"),
])
async def test_preflight_never_submits_on_unsupported_budget_or_provider(setup, provider, model, prompt, tokens, reason):
    runtime, sdk = setup
    with pytest.raises(ARLError, match=reason):
        await runtime.complete(provider, model, prompt, deadline=time.monotonic()+1, max_tokens=tokens)
    assert not any(method == "submit" for method, _ in sdk.calls)


@pytest.mark.asyncio
async def test_no_paid_inference_or_resubmit_after_lost_receipt(setup):
    runtime, sdk = setup
    sdk.providers[0]["paid"] = True
    with pytest.raises(ARLError, match="cloud_not_enabled"):
        await runtime.complete("mock", "deterministic-v1", "simulated", deadline=time.monotonic()+1)
    assert not any(method == "submit" for method, _ in sdk.calls)
    sdk.submit_error = True
    with pytest.raises(ARLError, match="disconnected"):
        await runtime.smoke()
    assert sum(method == "submit" for method, _ in sdk.calls) == 1


@pytest.mark.asyncio
async def test_deadline_and_cancellation_cancel_late_receipt_without_backlog(setup):
    runtime, sdk = setup
    sdk.delay = .12
    with pytest.raises(ARLError, match="deadline_exceeded"):
        await runtime.run_job("runtime.ping", {}, deadline=time.monotonic()+.03)
    with pytest.raises(ARLError, match="runtime_busy"):
        await runtime.smoke()
    await asyncio.sleep(.14)
    assert any(method == "cancel" for method, _ in sdk.calls)
    assert not runtime._busy.locked()
    sdk.calls.clear()
    sdk.start.clear()
    task = asyncio.create_task(runtime.smoke())
    await asyncio.to_thread(sdk.start.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(.14)
    assert any(method == "cancel" for method, _ in sdk.calls) and not runtime._busy.locked()


@pytest.mark.asyncio
async def test_configuration_change_discards_inflight_and_cancels_old_job(setup, tmp_path):
    runtime, sdk = setup
    sdk.delay = .08
    task = asyncio.create_task(runtime.smoke())
    await asyncio.to_thread(sdk.start.wait, 1)
    runtime.home = lambda: tmp_path / "other"
    with pytest.raises(ARLError, match="discarded_configuration_changed"):
        await task
    assert any(method == "cancel" for method, _ in sdk.calls)


@pytest.mark.asyncio
async def test_formal_fast_engine_abstains_without_inventing_task_api(setup):
    runtime, sdk = setup
    engine = ARLDecisionProvider(runtime)
    result = await engine.decide(DecisionRequest("intent", {"text": "simulated"}, time.monotonic()+2))
    assert result.label == "unknown" and result.shadow and not result.actionable
    assert result.unknown_reason == "arl_task_decision_contract_unavailable"
    assert not any(m == "submit" for m, _ in sdk.calls)
    assert (await engine.decide(DecisionRequest("intent", {}, time.monotonic()-1))).unknown_reason == "expired"
    assert (await engine.decide(DecisionRequest("intent", {"text": "中"*500}, time.monotonic()+1))).unknown_reason == "context_overflow"
    assert all(p["runtime"] == "arl" for p in registry_descriptors())


def test_settings_sdk_roundtrip_never_writes_devices_and_requires_origin(setup, tmp_path):
    runtime, sdk = setup
    app = create_app(db_path=tmp_path / "trace.sqlite", settings_path=tmp_path / "settings.json", arl_runtime=runtime)
    with TestClient(app, base_url="http://127.0.0.1:8740") as client:
        assert client.post("/api/settings/arl/connect").status_code == 403
        assert client.post("/api/settings/arl/probe", headers={"origin": "https://untrusted.invalid"}).status_code == 403
        assert not sdk.calls
        assert client.get("/api/settings/arl/status").json()["ready"]
        result = client.post("/api/settings/arl/probe", headers={"origin": "http://127.0.0.1:8740"}).json()
        assert not result["device_write_attempted"]
        assert client.get("/api/snapshot").json()["mode"] == "simulator"
        assert "arl" in client.get("/api/decision/providers").json()["runtime"]["protocol"]


@pytest.mark.asyncio
async def test_legacy_wrappers_reuse_arl_and_never_spawn_or_shortcut():
    from friday.config import Settings
    from friday.providers import ARLPlanner, ARLRouteDecision

    class Runtime:
        calls = []
        error = None
        async def complete(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if self.error:raise ARLError(self.error)
            return {"simulated": False, "decision": {"label": "simple", "confidence": .99}}
    runtime = Runtime()
    result = await ARLRouteDecision(Settings(), runtime).classify("simulated")
    assert result["confidence"] == 0 and result["shadow"] and runtime.calls[0][0][0] == "laya_mlx"
    runtime.calls.clear()
    with pytest.raises(ARLError, match="explicit_model_required"):
        await ARLPlanner(Settings(), runtime).plan({}, {})
    assert not runtime.calls
    runtime.error = 'invalid_arguments'
    with pytest.raises(ARLError, match="arl_planner_context_contract_insufficient"):
        await ARLPlanner(Settings(codex_model="simulated-version"), runtime).plan({}, {})
    assert len(runtime.calls) == 1 and runtime.calls[0][0][0] == 'codex_app_server'


@pytest.mark.asyncio
async def test_media_never_bypasses_missing_arl_capabilities():
    import base64
    import io
    import wave

    from friday.config import Settings
    from friday.models import Perception
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 8000)
    perception = Perception(Settings())
    with pytest.raises(ARLError, match="arl_asr_contract_unavailable"):
        await perception.transcribe(buffer.getvalue())
    with pytest.raises(ARLError, match="arl_visual_observation_contract_unavailable"):
        await perception.observe("simulated", [base64.b64encode(b"\xff\xd8simulated").decode()])
    with pytest.raises(ValueError):
        await perception.observe("simulated", [base64.b64encode(b"invalid").decode()])


def test_sdk_bootstrap_never_inherits_known_household_or_development_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_HA_TOKEN", "SIMULATED-private-never-forward")
    with pytest.raises(ARLError, match="runtime_launch_environment_not_isolated"):
        sdk_client(tmp_path, start=True)


@pytest.mark.asyncio
async def test_public_overview_does_not_grant_task_identity(setup):
    runtime, sdk = setup
    original = sdk.rpc
    def rpc(method, params=None):
        if method != "overview":
            raise FixtureFault("app_identity_unverified")
        return original(method, params)
    sdk.rpc = rpc
    assert (await runtime.status())["ready"]
    assert (await runtime.status(start=True))["ready"]
    with pytest.raises(ARLError, match="app_identity_unverified"):
        await runtime.smoke()
    assert not any(method == "submit" for method, _ in sdk.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"schema_version": 2}, "unsupported_overview_contract"),
    ({"schema_version": True}, "unsupported_overview_contract"),
    ({"health": {"live": True, "ready": "yes"}}, "invalid_overview"),
    ({"providers": [{"name": "mock", "availability": "ready", "models": ["/private/key"]}]}, "invalid_overview"),
])
async def test_changed_or_malformed_overview_is_not_connected(setup, change, reason):
    runtime, sdk = setup
    original = sdk.rpc
    def rpc(method, params=None):
        result = original(method, params)
        if method == "overview":
            result.update(change)
        return result
    sdk.rpc = rpc
    result = await runtime.status()
    assert result["status"] == reason and not result["ready"]
    assert [m for m, _ in sdk.calls] == ["overview"]


@pytest.mark.asyncio
async def test_overview_projection_drops_unadvertised_private_fields(setup):
    runtime, sdk = setup
    sdk.providers[0]["endpoint"] = "SIMULATED-private-endpoint"
    original = sdk.rpc
    def rpc(method, params=None):
        result = original(method, params)
        if method == "overview":
            result["private_result"] = "SIMULATED-private-prompt"
            result["health"]["ready"] = False
        return result
    sdk.rpc = rpc
    result = await runtime.status()
    assert result["status"] == "starting" and not result["ready"]
    assert "SIMULATED-private" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"version": 2}, "unsupported_tool_contract"),
    ({"permissions": ["SIMULATED-private-scope"]}, "unsupported_tool_contract"),
    ({"availability": "disabled"}, "tool_unavailable"),
])
async def test_changed_capability_contract_never_submits(setup, change, reason):
    runtime, sdk = setup
    original = sdk.rpc
    def rpc(method, params=None):
        result = original(method, params)
        if method == "capabilities":
            result["tools"][0].update(change)
        return result
    sdk.rpc = rpc
    with pytest.raises(ARLError, match=reason):
        await runtime.smoke()
    assert not any(method == "submit" for method, _ in sdk.calls)


@pytest.mark.parametrize("module", ["scripts.check_runtime", "scripts.benchmark_home_decision"])
def test_retired_public_commands_do_not_load_direct_providers(module):
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-m", module], capture_output=True, text=True, timeout=2)
    assert result.returncode == 2 and "scripts.check_arl" in result.stderr
    assert not result.stdout and "Traceback" not in result.stderr
