"""SIMULATED source seals plus a separately labelled native ARL actor test."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from homeplane.arl import ARLError
from homeplane.arl_actor_intake import inspect_candidate

ROOT = Path(__file__).parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def synthetic_candidate(tmp_path):
    candidate = tmp_path.resolve() / "SIMULATED-actor"
    candidate.mkdir(mode=0o700)
    sdk_names = (
        "__init__",
        "client",
        "friday_planning",
        "friday_planning_client",
        "friday_profile",
        "supervisor",
        "identity",
    )
    files = {}
    for name in sdk_names:
        relative = "sdk/arl/" + name + ".py"
        p = candidate / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "# SIMULATED source contract, never executed\n"
            + (
                "def profile():\n return LaunchSpec('friday', None, ('-I', 'SIMULATED.py'), None, ())\n"
                if name == "friday_profile"
                else ""
            )
        )
        p.chmod(0o600)
        files[relative] = sha(p)
    for name in ("__init__.py", "arl.py"):
        p = candidate / "product/homeplane" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "homeplane" / name, p)
        p.chmod(0o600)
        files[str(p.relative_to(candidate))] = sha(p)
    for name in ("planning_host.py", "planning_consumer.py"):
        p = candidate / name
        p.write_text("# SIMULATED actor, never launched\n")
        p.chmod(0o600)
    # This source-only fixture never launches an interpreter. Do not depend on
    # CI's shared framework executable permissions or modify a host interpreter.
    exe = tmp_path.resolve() / 'SIMULATED-interpreter'
    exe.write_bytes(b'SIMULATED interpreter bytes; never executed')
    exe.chmod(0o700)
    m = {
        "schema_version": 1,
        "files": files,
        "source_set_sha256": hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "product": "Friday Planning",
        "owner": "friday",
        "product_version": "SIMULATED",
        "identity": "host_owned_pid_uid_birth_plus_exact_source_set",
        "python_executable": str(exe),
        "python_sha256": sha(exe),
        "host_sha256": sha(candidate / "planning_host.py"),
        "entry_sha256": sha(candidate / "planning_consumer.py"),
        "production_enabled": False,
        "trusted_product_subset": {
            k.removeprefix("product/"): v for k, v in files.items() if k.startswith("product/")
        },
    }
    manifest = candidate / "source-manifest.json"
    manifest.write_text(json.dumps(m))
    manifest.chmod(0o600)
    return candidate, sha(manifest)


def inspect(value):
    return inspect_candidate(*value, product_root=ROOT)


def test_source_subset_does_not_claim_environment_actor_or_grant(synthetic_candidate, monkeypatch):
    from homeplane import arl_actor_intake

    candidate, _ = synthetic_candidate
    (candidate / "state").mkdir(mode=0o700)
    (candidate / "state/SIMULATED-private-journal").write_text("SIMULATED not read")
    (candidate / "reviewed-request.json").write_text("SIMULATED not read")
    original, reads = arl_actor_intake._read, []

    def record(path, *args):
        reads.append(str(path))
        return original(path, *args)

    monkeypatch.setattr(arl_actor_intake, "_read", record)
    report = inspect(synthetic_candidate).report()
    assert report["current_product_subset_matches"]
    assert not report["site_import_disabled"] and not report["complete_executable_environment_verified"]
    assert not any(
        report[k]
        for k in (
            "console_identity_verified",
            "actor_registered",
            "execution_grants",
            "model_inference_attempted",
            "device_write_attempted",
        )
    )
    assert not any("reviewed-request" in p or "/state/" in p for p in reads)


@pytest.mark.parametrize(
    "kind,code",
    [
        ("manifest", "actor_manifest_changed"),
        ("entry", "actor_source_changed"),
        ("extra", "actor_source_set_changed"),
        ("symlink", "actor_unsafe_path"),
        ("permissions", "actor_unsafe_path"),
    ],
)
def test_changed_extra_unsafe_source_refused(synthetic_candidate, kind, code):
    candidate, _ = synthetic_candidate
    if kind == "manifest":
        with (candidate / "source-manifest.json").open("ab") as f:
            f.write(b"\n")
    elif kind == "entry":
        (candidate / "planning_host.py").write_text("# SIMULATED drift\n")
    elif kind == "extra":
        (candidate / "extra.py").write_text("# SIMULATED unexpected module\n")
    elif kind == "symlink":
        (candidate / "state").symlink_to(candidate.parent, target_is_directory=True)
    else:
        (candidate / "sdk/arl/client.py").chmod(0o622)
    with pytest.raises(ARLError, match=code):
        inspect(synthetic_candidate)


def test_missing_import_subset_refused(synthetic_candidate):
    candidate, _ = synthetic_candidate
    manifest = candidate / "source-manifest.json"
    m = json.loads(manifest.read_text())
    del m["files"]["sdk/arl/client.py"]
    manifest.write_text(json.dumps(m))
    with pytest.raises(ARLError, match="incomplete_source_subset"):
        inspect((candidate, sha(manifest)))


def test_interpreter_drift_refused(synthetic_candidate):
    candidate, _ = synthetic_candidate
    manifest = candidate / "source-manifest.json"
    m = json.loads(manifest.read_text())
    m["python_sha256"] = "0" * 64
    manifest.write_text(json.dumps(m))
    with pytest.raises(ARLError, match="actor_interpreter_changed"):
        inspect((candidate, sha(manifest)))


def test_duplicate_manifest_keys_are_not_silent_overrides(synthetic_candidate):
    candidate, _ = synthetic_candidate
    manifest = candidate / "source-manifest.json"
    manifest.write_text(manifest.read_text()[:-1] + ',"owner":"friday"}')
    with pytest.raises(ARLError, match="invalid_manifest"):
        inspect((candidate, sha(manifest)))


@pytest.mark.parametrize("pin_changed", [False, True])
def test_cli_only_inspects_the_explicit_source_subset(synthetic_candidate, pin_changed):
    candidate, manifest_sha = synthetic_candidate
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "homeplane.cli",
            "inspect-arl-actor",
            "--candidate",
            str(candidate),
            "--manifest-sha256",
            "0" * 64 if pin_changed else manifest_sha,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
    )
    report = json.loads(result.stdout)
    assert result.returncode == int(pin_changed)
    assert report["status"] == ("refused" if pin_changed else "source_subset_verified")
    assert not report["actor_registered"] and not report["execution_grants"]
    assert not (candidate / "state").exists() and not (candidate / "reviewed-request.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "other"),
        ("execution_id", "other"),
        ("request_sha256", "0" * 64),
        ("context_fingerprint", "0" * 64),
        ("action_authority", True),
        ("simulated", True),
        ("provider", "other"),
        ("expires_at_ms", 0),
        ("job_id", ""),
        ("schema_version", True),
    ],
)
def test_private_executor_cannot_bypass_local_receipt_correlation(tmp_path, field, value):
    pytest.importorskip("arl.friday_planning", reason="Optional ARL typed planning SDK")
    import asyncio

    from homeplane.arl_rule_planning import planning_sdk
    from tests.test_homeplane_arl_rule_planning import approval, request, setup
    from tests.test_homeplane_rule_deployment import close

    core, manager, peer, runtime, service = setup(tmp_path)

    async def corrupt(*args, **kwargs):
        result = await planning_sdk()[1](*args, **kwargs)
        result[field] = value
        return result

    service.planning_executor = corrupt

    async def check():
        before = manager.backend.records()
        review = await service.prepare(request(service))
        result = await service.analyze(approval(review))
        assert result.status == "needs_review" and result.code == "analysis_receipt_mismatch"
        assert result.plan is None and manager.backend.records() == before
        assert not result.real_write_attempted and not result.physically_confirmed

    try:
        asyncio.run(check())
    finally:
        service.close()
        close(core, manager)


def test_private_executor_alone_never_enables_a_model(tmp_path):
    pytest.importorskip("arl.friday_planning", reason="Optional ARL typed planning SDK")
    import asyncio

    from tests.test_homeplane_arl_rule_planning import approval, request, setup
    from tests.test_homeplane_rule_deployment import close

    core, manager, peer, runtime, service = setup(tmp_path, enabled=False)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No host authorization")

    service.planning_executor = forbidden

    async def check():
        review = await service.prepare(request(service))
        result = await service.analyze(approval(review))
        assert result.status == "unsupported" and not result.planner_call_attempted
        assert not service.context().analysis_enabled and not peer.calls

    try:
        asyncio.run(check())
    finally:
        service.close()
        close(core, manager)


@pytest.mark.skipif(sys.platform != "darwin", reason="Actual macOS kernel peer identity required")
def test_native_sealed_actor_receipt_enters_existing_local_diff_without_effects(tmp_path, short_actor_root):
    """Actual private actor/IPC, SIMULATED provider, no Codex/account/device calls."""
    import asyncio
    import threading
    from types import SimpleNamespace

    arl = pytest.importorskip('arl', reason='Optional ARL typed planning SDK')
    from arl.client import Client
    from arl.contracts import Fault
    from arl.friday_profile import compose_isolated

    from tests.test_homeplane_arl_rule_planning import approval, request, setup
    from tests.test_homeplane_rule_deployment import close

    upstream = Path(arl.__file__).parents[1] / "integrations/friday/dist/planning-candidate-v2"
    if not upstream.is_dir():
        pytest.skip("Reviewed upstream source candidate is not installed")
    candidate = short_actor_root / "SIMULATED"
    shutil.copytree(upstream, candidate)
    seal = inspect((candidate, sha(candidate / "source-manifest.json")))
    assert seal.product_matches and not seal.report()["execution_grants"]
    core, manager, peer, runtime, service = setup(tmp_path)
    observations = []
    actor_failures = []

    async def execute(_runtime, reviewed, *, deadline, current_context):
        assert current_context() == reviewed.request["context_fingerprint"]
        payload = candidate / "reviewed-request.json"
        payload.write_text(json.dumps({"request": reviewed.request, "model": reviewed.model}))
        payload.chmod(0o600)
        # Explicit test-only provider double; production-shaped false is used only
        # to exercise the real typed receipt branch, not to prove model inference.
        calls = []

        def provider(args, context):
            calls.append("SIMULATED provider")
            return peer.provider(args, context)

        host, supervisor = compose_isolated(
            candidate, seal.manifest_sha256, sha(payload), adapter=SimpleNamespace(run=provider)
        )
        errors = []

        def serve():
            try:
                host.run()
            except Exception as e:
                errors.append(repr(e))  # The isolated fixture contains no credentials or household input.

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            assert host.ready.wait(3)
            child = supervisor.launch("friday")
            output, _ = await asyncio.to_thread(child.communicate, timeout=8)
            assert child.returncode == 0
            receipt = json.loads(output)
            assert receipt["status"] == "advisory_only" and calls == ["SIMULATED provider"]
            assert host.store.db.execute("select calls from budget where owner='friday'").fetchone()[0] == 1
            with pytest.raises(Fault) as refusal:
                Client(candidate / "state", "friday").rpc("capabilities")  # Exited/unknown peer is refused.
            assert refusal.value.code == "app_identity_unverified"
            assert current_context() == receipt["result"]["context_fingerprint"]
            observations.append(
                {
                    "actor_pid": child.pid,
                    "simulated_provider": True,
                    "unregistered_peer_refusal_code": refusal.value.code,
                    "model_inference_attempted": False,
                    "device_write_attempted": False,
                }
            )
            return receipt["result"]
        except Exception as exc:
            actor_failures.append(repr(exc))
            raise
        finally:
            supervisor.close()
            host.shutdown()
            thread.join(8)
            if errors:
                actor_failures.extend(errors)
            assert not errors and not thread.is_alive(), errors

    service.planning_executor = execute

    async def check():
        original = manager.backend.records()[0].rule.model_dump()
        review = await service.prepare(request(service))
        result = await service.analyze(approval(review))
        assert result.status == "planned" and result.baseline_comparison == "matched", (
            result.status,
            result.code,
            actor_failures,
        )
        assert [a.type for a in result.plan.new_rule.actions] == ["light.set"]
        assert result.plan.new_rule.trigger.model_dump() == original["trigger"]
        assert result.plan.new_rule.conditions == manager.backend.records()[0].rule.conditions
        assert not result.real_write_attempted and not result.physically_confirmed
        assert manager.backend.records()[0].rule.model_dump() == original  # No install or old-rule deletion.
        assert core.store.connection.execute("select count(*) from light_actions").fetchone()[0] == 0
        assert len(observations) == 1 and not observations[0]["model_inference_attempted"]
        # Reproducible protocol evidence with pytest --capture=tee-sys. It contains
        # fixture metadata only, never a model input, account or household values.
        print(
            json.dumps(
                {
                    "evidence": "friday_arl_native_simulated_provider",
                    "status": "PASS_NATIVE_ACTOR_TO_LOCAL_DIFF_SIMULATED_PROVIDER",
                    "input": "SIMULATED owned-rule fixture",
                    "actor_pid": observations[0]["actor_pid"],
                    "candidate_manifest_sha256": seal.manifest_sha256,
                    "candidate_fullset_sha256": seal.fullset_sha256,
                    "actual_kernel_peer_registration": True,
                    "unregistered_peer_refused_after_exit": True,
                    "unregistered_peer_refusal_code": observations[0]["unregistered_peer_refusal_code"],
                    "provider": "SIMULATED test double",
                    "provider_calls": 1,
                    "model_inference_attempted": False,
                    "device_write_attempted": False,
                    "deployment_attempted": False,
                    "original_rule_unchanged": True,
                    "retained_action_types": [a.type for a in result.plan.new_rule.actions],
                    "trigger_conditions_unchanged": True,
                    "deterministic_baseline": result.baseline_comparison,
                    "complete_executable_environment_verified": False,
                    "production_actor_registered": False,
                    "production_execution_grants": False,
                },
                sort_keys=True,
            )
        )

    try:
        asyncio.run(check())
    finally:
        service.close()
        close(core, manager)


@pytest.fixture
def short_actor_root():
    # Darwin sockaddr_un limit also applies to this real local IPC fixture.
    with tempfile.TemporaryDirectory(prefix="friday-arl-", dir=str(Path("/tmp").resolve())) as folder:
        yield Path(folder)
