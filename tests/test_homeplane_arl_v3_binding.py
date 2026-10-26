"""Actual copied-runtime ARL transport with SIMULATED provider/rules only.

No production installation, account/model, household credentials or device calls.
The supplied factory owns the relay; the test runner injects only the provider.
"""

import asyncio
import importlib
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from homeplane.arl import ARLError
from homeplane.arl_actor_binding import ReviewedPlanningPackage
from homeplane.arl_actor_intake import inspect_candidate
from homeplane.contracts import ActionRequest
from tests.test_homeplane_arl_rule_planning import approval, request, setup
from tests.test_homeplane_rule_deployment import close

ROOT = Path(__file__).parents[1]
CANDIDATE = Path(
    "/path/to/ARL/integrations/friday/dist/planning-candidate-v3"
)
MANIFEST = "69b13bf58eaaa3e84f4bf4089e421045d0fce74f2ae749333cc9c18c5cefd987"
LOADER = "d76e39a4437ba3f12c0999edaec42462d58d4588daa35af9d20124227958be4d"
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not CANDIDATE.is_dir(), reason="Pinned macOS ARL v3 candidate required"
)


@pytest.fixture(scope="module")
def package():
    original_arl, original_path = importlib.import_module("arl"), list(sys.path)
    result = ReviewedPlanningPackage(CANDIDATE, MANIFEST, LOADER, product_root=ROOT)
    assert sys.modules["arl"] is original_arl and sys.path == original_path
    return result


@pytest.fixture
def candidate():
    with tempfile.TemporaryDirectory(prefix="friday-v3-", dir=ROOT / ".artifacts/tmp") as folder:
        target = Path(folder) / "actor"
        shutil.copytree(CANDIDATE, target)
        target.chmod(0o700)
        yield target


class DoubleSession:
    """Fixture only: actual upstream execute(), explicit simulated Provider."""

    def __init__(self, runner):
        self.events, self.cancelled = queue.Queue(), threading.Event()
        self.cancels, self.failure = 0, None
        execute = importlib.import_module(runner.package.sdk.__package__ + ".friday_actor_host").execute

        def run():
            try:
                self.events.put(
                    execute(
                        *runner.args, adapter=runner.adapter, on_event=self.events.put, cancel=self.cancelled
                    )
                )
            except Exception as exc:
                self.failure = getattr(exc, "code", type(exc).__name__)
                self.events.put({"status": "rejected", "code": self.failure})

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def next(self, timeout):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def cancel(self):
        self.cancels += 1
        self.cancelled.set()

    def finished(self):
        self.thread.join(5)
        assert not self.thread.is_alive() and not self.failure


class DoubleRunner:
    def __init__(self, package, candidate, review, peer, runtime, *, wait=False, lost=False):
        self.package = package
        binding = package.sdk.FridayReviewBinding.from_review(review)
        self.args = (candidate, MANIFEST, binding.sha256)
        self.started, self.calls, self.sessions = 0, 0, []
        self.entered, self.wait, self.lost = threading.Event(), wait, lost
        self.busy = []

        def provider(args, context):
            self.calls += 1
            self.busy.append(runtime._busy.locked())
            self.entered.set()
            while self.wait:
                context.check_cancelled()
                threading.Event().wait(0.01)
            return peer.provider(args, context)  # SIMULATED real-shaped parsing branch only.

        self.adapter = SimpleNamespace(run=provider)

    def start(self):
        self.started += 1
        session = DoubleSession(self)
        self.sessions.append(session)
        if self.lost:
            actual = session.next

            def lost(timeout):
                actual(timeout)
                return None

            session.next = lost
        return session

    def close(self):
        for session in self.sessions:
            if session.thread.is_alive():
                session.cancel()
            session.thread.join(8)
            assert not session.thread.is_alive()


def cleanup(core, manager, service, runner=None):
    service.close()
    if runner:
        runner.close()
    close(core, manager)


def test_v3_payload_and_runtime_are_verified_without_production_authority(package):
    report = package.seal.report()
    assert report["payload_sha256"] == "823540794be176fbf0ed45d9c169cc83b6058b819c7d4cb743342a9e4e2273ce"
    assert report["file_count"] == 1236 and report["runtime_file_count"] == 1192
    assert report["payload_bytes"] == 41315641 and report["site_import_disabled"]
    assert report["current_product_subset_matches"] and report["copied_runtime_verified"]
    assert not report["complete_executable_environment_verified"] and not report["actor_registered"]
    assert not report["execution_grants"] and not report["model_inference_attempted"]


def test_loader_hash_mismatch_refused_before_namespace_code_runs():
    before = set(sys.modules)
    with pytest.raises(ARLError, match="actor_loader_changed"):
        ReviewedPlanningPackage(CANDIDATE, MANIFEST, "0" * 64, product_root=ROOT)
    assert not (set(sys.modules) - before)


def test_added_python_zip_refused_by_owner_intake_before_binding(package, candidate, tmp_path):
    core, manager, peer, runtime, service = setup(tmp_path, enabled=False)

    async def check():
        review = await service.prepare(request(service))
        (candidate / "runtime/lib/python312.zip").write_bytes(b"SIMULATED extra import location")
        with pytest.raises(ARLError, match="actor_source_set_changed"):
            package.bind(service, review, candidate=candidate)
        assert service.authorizer is None and service.runtime is runtime
        assert not (candidate / "state").exists() and not (candidate / "reviewed-request.json").exists()
        assert not peer.calls

    try:
        asyncio.run(check())
    finally:
        cleanup(core, manager, service)


@pytest.mark.parametrize("reason", ["cancelled", "deadline"])
def test_finished_or_expired_local_review_not_written_into_actor(package, candidate, tmp_path, reason):
    core, manager, peer, runtime, service = setup(tmp_path, enabled=False)

    async def check():
        review = await service.prepare(request(service))
        if reason == "cancelled":
            assert service.cancel(approval(review)).status == "rejected"
        else:
            service.deadlines[review.id] = 0
        with pytest.raises(ARLError, match="actor_owner_review_changed"):
            package.bind(service, review, candidate=candidate)
        assert not (candidate / "reviewed-request.json").exists() and not (candidate / "state").exists()
        assert service.authorizer is None and service.runtime is runtime and not peer.calls

    try:
        asyncio.run(check())
    finally:
        cleanup(core, manager, service)


def test_default_factory_wiring_stays_off_and_validate_only_starts_no_worker(package, candidate, tmp_path):
    core, manager, peer, runtime, service = setup(tmp_path, enabled=False)

    async def check():
        review = await service.prepare(request(service))
        factory = package.bind(service, review, candidate=candidate)
        assert not factory.authorizes(review) and service.runtime is runtime
        assert not service.context().analysis_enabled
        result = await service.analyze(approval(review))
        assert result.status == "unsupported" and not result.planner_call_attempted
        assert not (candidate / "state").exists() and not peer.calls
        assert (candidate / "reviewed-request.json").stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            package.bind(service, review, candidate=candidate)
        command = [
            str(candidate / "runtime/bin/python3.12"),
            "-I",
            "-S",
            "-B",
            str(candidate / "planning_host.py"),
            "--validate-only",
            "--manifest-sha256",
            MANIFEST,
            "--approved-review-sha256",
            factory.review_sha,
        ]
        output = subprocess.run(
            command, capture_output=True, timeout=8,
            env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                 'TMPDIR': str(ROOT / '.artifacts/tmp'), 'PYTHONDONTWRITEBYTECODE': '1'},
        )
        assert output.returncode == 0, output.stdout
        assert json.loads(output.stdout) == {
            "status": "source_and_review_verified",
            "calls_started": 0,
            "execution_grants": False,
        }
        assert not (candidate / "state").exists() and not list(candidate.rglob("*.pyc"))

    try:
        asyncio.run(check())
    finally:
        cleanup(core, manager, service)


def test_actual_v3_factory_existing_gate_copied_actor_receipt_to_local_diff(package, candidate, tmp_path):
    core, manager, peer, runtime, service = setup(tmp_path, enabled=False)
    runner = None

    async def check():
        nonlocal runner
        old = manager.backend.records()[0].rule.model_dump()
        review = await service.prepare(request(service))
        runner = DoubleRunner(package, candidate, review, peer, runtime)
        factory = package.bind(service, review, candidate=candidate, runner=runner)
        assert factory.authorizes(review) and service.runtime._bounded == runtime._bounded
        assert service.planning_executor is None and runtime.home() == tmp_path
        result = await service.analyze(approval(review))
        assert result.status == "planned", (result.status, result.code)
        assert result.baseline_comparison == "matched" and runner.started == runner.calls == 1
        assert runner.busy == [True] and runner.sessions[0].cancels == 0
        assert [a.type for a in result.plan.new_rule.actions] == ["light.set"]
        assert result.plan.new_rule.trigger.model_dump() == old["trigger"]
        assert result.plan.new_rule.conditions == manager.backend.records()[0].rule.conditions
        assert manager.backend.records()[0].rule.model_dump() == old
        assert not result.real_write_attempted and not result.physically_confirmed
        assert core.store.connection.execute("select count(*) from light_actions").fetchone()[0] == 0
        repeated = await service.analyze(approval(review))
        assert repeated.plan.id == result.plan.id and runner.started == runner.calls == 1
        assert (candidate / "state/relay_admission.json").is_file()
        assert not list(candidate.rglob("*.pyc"))
        print(
            json.dumps(
                {
                    "evidence": "friday_arl_v3_factory_to_diff",
                    "status": "PASS",
                    "manifest_sha256": MANIFEST,
                    "copied_runtime_files": 1192,
                    "shared_owner_gate": runner.busy == [True],
                    "parent_status_runtime_unchanged": True,
                    "actual_copied_actor_and_kernel_registration": True,
                    "factory_admissions": 1,
                    "provider": "SIMULATED test double",
                    "provider_calls": 1,
                    "retained_light_trigger_conditions": True,
                    "original_rule_unchanged": True,
                    "real_model_calls": 0,
                    "real_device_calls": 0,
                    "deployment_calls": 0,
                    "production_registration": False,
                    "production_grants": False,
                },
                sort_keys=True,
            )
        )

    try:
        asyncio.run(check())
    finally:
        cleanup(core, manager, service, runner)


@pytest.mark.parametrize("field", ["boot", "text", "digest", "context"])
def test_changed_owner_review_refused_before_private_file_or_process(package, candidate, tmp_path, field):
    core, manager, peer, runtime, service = setup(tmp_path, enabled=False)

    async def check():
        review = await service.prepare(request(service))
        changed = review.model_copy(deep=True)
        if field == "boot":
            changed.boot_id = "SIMULATED-other-boot"
        elif field == "text":
            changed.request["text"] = "SIMULATED different request"
        elif field == "digest":
            changed.approval_digest = "0" * 64
        else:
            changed.context.fingerprint = "0" * 64
        with pytest.raises(ARLError, match="actor_owner_review_changed"):
            package.bind(service, changed, candidate=candidate)
        assert not (candidate / "reviewed-request.json").exists()
        assert service.runtime is runtime and service.authorizer is None and not peer.calls

    try:
        asyncio.run(check())
    finally:
        cleanup(core, manager, service)


def test_v3_stdlib_drift_refuses_analysis_and_never_uses_runner(package, candidate, tmp_path):
    core, manager, peer, runtime, service = setup(tmp_path, enabled=False)
    runner = None

    async def check():
        nonlocal runner
        review = await service.prepare(request(service))
        runner = DoubleRunner(package, candidate, review, peer, runtime)
        factory = package.bind(service, review, candidate=candidate, runner=runner)
        p = candidate / "runtime/lib/python3.12/abc.py"
        p.write_bytes(p.read_bytes() + b"\n# SIMULATED drift\n")
        assert not factory.authorizes(review)
        with pytest.raises(ARLError, match="actor_source_changed"):
            inspect_candidate(candidate, MANIFEST, product_root=ROOT)
        result = await service.analyze(approval(review))
        assert result.status == "unsupported" and not result.planner_call_attempted
        assert runner.started == runner.calls == 0 and not (candidate / "state").exists()

    try:
        asyncio.run(check())
    finally:
        cleanup(core, manager, service, runner)


@pytest.mark.parametrize("failure", ["lost_ack", "cancel"])
def test_v3_uncertain_or_cancelled_attempt_is_never_replayed(package, candidate, tmp_path, failure):
    core, manager, peer, runtime, service = setup(tmp_path, enabled=False)
    runner = None

    async def check():
        nonlocal runner
        review = await service.prepare(request(service))
        runner = DoubleRunner(
            package, candidate, review, peer, runtime, lost=failure == "lost_ack", wait=failure == "cancel"
        )
        package.bind(service, review, candidate=candidate, runner=runner)
        task = asyncio.create_task(service.analyze(approval(review)))
        if failure == "cancel":
            for _ in range(300):
                if runner.entered.is_set():
                    break
                await asyncio.sleep(0.02)
            assert runner.entered.is_set() and runtime._busy.locked()
            # A blocked real ARL worker must not hold basic deterministic Core
            # state/preview paths. This remains a non-actuating SIMULATED check.
            snapshot = await asyncio.wait_for(core.snapshot(), 1)
            light = next(e for e in snapshot.entities if any(c.name == "light.set" for c in e.capabilities))
            preview = await asyncio.wait_for(
                core.dry_run(
                    ActionRequest(
                        target=light.id,
                        desired={"power": "on"},
                        actor="test",
                        expected_version=snapshot.version,
                        expires_at=datetime.now(UTC) + timedelta(seconds=20),
                        idempotency_key="SIMULATED-core-while-ARL-blocked",
                    )
                ),
                1,
            )
            assert preview.status == "planned" and not preview.real_write_attempted
            cancelled = service.cancel(approval(review))
            assert cancelled.status == "needs_review"
        result = await asyncio.wait_for(task, 12)
        assert result.status == "needs_review" and result.plan is None
        repeated = await service.analyze(approval(review))
        assert repeated.status == "needs_review" and repeated.plan is None
        assert runner.started == 1 and runner.calls <= 1
        assert (candidate / "state/relay_admission.json").is_file()
        assert runner.sessions[0].cancels >= 1
        assert core.store.connection.execute("select count(*) from light_actions").fetchone()[0] == 0

    try:
        asyncio.run(check())
    finally:
        cleanup(core, manager, service, runner)
