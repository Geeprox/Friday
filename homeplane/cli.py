import argparse
import asyncio
import json
import os
import platform
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .contracts import ActionRequest, ActionTrace, Snapshot
from .simulator import Simulator


def readiness(adapter=None, actions=None):
    import importlib.metadata
    targets = actions.targets(adapter._last) if actions and getattr(adapter, "_last", None) else []
    return {
        "schema_version": 1, "mode": "simulator" if adapter is None or isinstance(adapter, Simulator) else "ha-read-only",
        "real_writes": "scoped_explicit_execution_only" if targets else "disabled",
        "authorized_target_count": len(targets), "python": platform.python_version(), "platform": platform.system(),
        "dependencies": {n: importlib.metadata.version(n) for n in ("fastapi", "pydantic", "httpx", "websockets")},
        "retention": {"traces_days": 7, "max_rows": 1000, "sensitivity": "household", "media_capture": "disabled"},
        "capabilities": {
            "simulator": "available", "ha": "settings_oauth_readonly_activation",
            "geek": "requires_local_pairing_passcode", "camera": "account_stream_and_license_probe_required",
            "nas": "bounded_readonly_index_interval_and_original_mp4_player_simulated_real_timing_and_codec_pending", "room_output": "audibility_and_offline_test_pending",
            "laya": "arl_task_contract_pending", "codex": "arl_provider_required", "arl": "shared_sdk",
            "rules": "owned_simulator_and_reviewed_local_core_backend_native_deployment_unqualified",
            "measurement_settings": "source_bound_temperature_profiles_and_temporary_exclusion",
            "measurement_calibration_enrollment": "reviewed_relative_only_qualified_paired_samples_no_physical_independence_claim",
        },
        "offline_hardware": "unknown", "physical_verification": "NOT_RUN",
        "limitations": ["No hardware test follows from readiness.", "External rules are not fully visible or intercepted."],
        "ha_ingress": adapter.health() if adapter is not None and not isinstance(adapter, Simulator) else None,
    }


def adapter_from_env(args):
    if args.mode == "simulator":
        return Simulator(Path(args.fixture)) if args.fixture else Simulator()
    from .ha import HAReadOnlyAdapter
    url, token, instance = os.getenv("HOME_HA_URL"), os.getenv("HOME_HA_TOKEN"), os.getenv("HOME_HA_INSTANCE")
    if not all((url, token, instance)):
        raise SystemExit("Set HOME_HA_URL, HOME_HA_TOKEN and HOME_HA_INSTANCE locally; never paste credentials in chat.")
    return HAReadOnlyAdapter(url, token, instance)


def main():
    parser = argparse.ArgumentParser(prog="home", description="Home control plane; default simulated, live actions require a scoped local authority and explicit execution")
    parser.add_argument("--mode", choices=["simulator", "ha-read-only"], default="simulator")
    parser.add_argument("--fixture")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("simulate")
    sub.add_parser("snapshot")
    sub.add_parser("schema")
    diagnostics = sub.add_parser("diagnostics", help="Download a redacted support bundle from the running local Console")
    diagnostics_sub = diagnostics.add_subparsers(dest="operation", required=True)
    export = diagnostics_sub.add_parser("export")
    export.add_argument("--redacted", action="store_true", required=True)
    export.add_argument("--console-url", default="http://127.0.0.1:8740")
    export.add_argument("--output", type=Path)
    service = sub.add_parser("geek-service", help="Manage the user-owned read-only connection service")
    service.add_argument("operation", choices=["install", "enable", "status", "stop", "remove"])
    service.add_argument("--directory", type=Path, default=Path("data/homeplane"))
    actor = sub.add_parser("inspect-arl-actor", help="Read-only source subset inspection; never registers or launches an actor")
    actor.add_argument("--candidate", type=Path, required=True)
    actor.add_argument("--manifest-sha256", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8740)
    args = parser.parse_args()
    if args.command == "diagnostics":
        import httpx

        from .diagnostic_export import download, save

        output = args.output or Path("data/homeplane/diagnostic-exports") / (
            f"diagnostics-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}.zip")
        try:
            result = save(download(args.console_url), output)
        except (OSError, ValueError, httpx.HTTPError):
            print(json.dumps({"status": "not_saved", "code": "diagnostics_export_unavailable"}))
            raise SystemExit(1) from None
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "geek-service":
        from .geek_background import GeekBackground
        from .geek_service import enable, install, remove
        async def manage():
            client = GeekBackground(args.directory)
            if args.operation == "install":
                return await install(args.directory)
            if args.operation == "enable":
                return await enable(args.directory)
            if args.operation == "remove":
                return await remove(args.directory)
            if args.operation == "stop":
                await client.close()
            return await client.refresh()
        try:
            print(json.dumps(asyncio.run(manage()), ensure_ascii=False, indent=2))
        except (OSError, ValueError, TimeoutError):
            print(json.dumps({"status": "service_operation_unverified", "writes_attempted": False}))
            raise SystemExit(1) from None
        return
    if args.command == "inspect-arl-actor":
        from .arl import ARLError, safe_code
        from .arl_actor_intake import inspect_candidate

        try:
            report = inspect_candidate(args.candidate, args.manifest_sha256, product_root=Path(__file__).resolve().parents[1]).report()
        except (ARLError, OSError, ValueError, TypeError) as exc:
            print(json.dumps({"status": "refused", "code": safe_code(exc), "actor_registered": False, "execution_grants": False}))
            raise SystemExit(1) from None
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    adapter = adapter_from_env(args)
    if args.command == "serve":
        import uvicorn

        from .api import create_app
        uvicorn.run(create_app(adapter), host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
        return
    if args.command == "doctor":
        from .arl import DEFAULT_HOME, ARLRuntime
        from .settings import SettingsService
        settings = SettingsService()
        runtime = ARLRuntime(lambda: Path(settings.config.arl_home) if settings.config.arl_home else DEFAULT_HOME)
        print(json.dumps({**readiness(adapter), "arl": asyncio.run(runtime.status())}, ensure_ascii=False, indent=2))
    elif args.command == "schema":
        from .action_contracts import (
            DevicePlanRequest,
            HomeDeviceAuthority,
            LightAuthority,
            LightExecutionTrace,
            LightPlanRequest,
        )
        from .archive_contracts import (
            ArchiveCameraEdit,
            ArchivePlaybackCancel,
            ArchivePlaybackPlan,
            ArchivePlaybackRequest,
            ArchiveResolution,
            ArchiveResolveRequest,
            ArchiveSegmentRef,
            ArchiveTimeRange,
        )
        from .arl_rule_planning import (
            RuleAnalysisApproval,
            RuleAnalysisContext,
            RuleAnalysisRequest,
            RuleAnalysisResult,
            RuleAnalysisReview,
        )
        from .contracts import PhysicalDevice
        from .core_rules import CoreRuleApproval, CoreRulePlan, CoreRuleProposal, CoreRuleRecord, CoreRuleRun
        from .diagnostics import DiagnosticsExportRequest, DiagnosticsPolicy, DiagnosticsReport
        from .geek_background_contracts import BackgroundRequest, ConnectionIntent
        from .geek_inventory import GeekInventoryView
        from .geek_session import GeekSessionView
        from .measurement_settings_contracts import (
            ApproveRelativeCalibration,
            CalibrationPreview,
            EditMeasurement,
            EditMeasurementGroup,
            ExcludeMeasurement,
            MeasurementChange,
            MeasurementSettingsView,
            PreviewRelativeCalibration,
        )
        from .media_contracts import MediaProbeRequest, MediaProbeResult
        from .query_contracts import HomeQuery, HomeQueryResult
        from .rule_contracts import ModeLease, RuleCompilation, RuleEvaluation, RuleIR, RuleTrial
        from .rule_deployment import (
            ManagedRuleRecord,
            RemoveRuleAction,
            RuleApproval,
            RuleDeployment,
            RulePlanRef,
        )
        from .rule_runtime import RuleRun
        from .rule_text import RuleTextContext, RuleTextRequest, RuleTextResult
        out = Path("schemas")
        out.mkdir(exist_ok=True)
        for name, model in [("GeekBackgroundRequest", BackgroundRequest), ("GeekConnectionIntent", ConnectionIntent),
                            ("GeekSessionStatus", GeekSessionView), ("GeekInventory", GeekInventoryView)]:
            (out / f"{name}.json").write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
        for model in (MediaProbeRequest, MediaProbeResult, DiagnosticsExportRequest, DiagnosticsPolicy, DiagnosticsReport,
                      Snapshot, ActionRequest, ActionTrace, PhysicalDevice, LightPlanRequest, LightExecutionTrace, LightAuthority,
                      ArchiveCameraEdit, ArchiveResolution, ArchiveResolveRequest, ArchiveSegmentRef, ArchiveTimeRange,
                      ArchivePlaybackCancel, ArchivePlaybackPlan, ArchivePlaybackRequest,
                      DevicePlanRequest, HomeDeviceAuthority,
                      RuleIR, RuleCompilation, RuleEvaluation, ModeLease, RuleTrial, ManagedRuleRecord,
                      RemoveRuleAction, RuleApproval, RuleDeployment, RulePlanRef, RuleRun, HomeQuery, HomeQueryResult,
                      EditMeasurement, ExcludeMeasurement, MeasurementChange, MeasurementSettingsView,
                      EditMeasurementGroup, PreviewRelativeCalibration, CalibrationPreview, ApproveRelativeCalibration,
                      RuleTextContext, RuleTextRequest, RuleTextResult,
                      RuleAnalysisApproval, RuleAnalysisContext, RuleAnalysisRequest, RuleAnalysisResult, RuleAnalysisReview,
                      CoreRuleApproval, CoreRulePlan, CoreRuleProposal, CoreRuleRecord, CoreRuleRun):
            (out / f"{model.__name__}.json").write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
    else:
        async def run():
            try:
                print((await adapter.snapshot()).model_dump_json(indent=2))
            finally:
                await adapter.close()
        asyncio.run(run())


if __name__ == "__main__":
    main()
