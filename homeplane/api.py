"""Local Console API with read-only ingress and a separately scoped Action Gateway."""

import asyncio
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.background import BackgroundTask

from .action_contracts import (
    DevicePlanRequest,
    ExecutePlan,
    LightExecutionTrace,
    LightPlanRequest,
    ResumeDeviceControls,
)
from .actions import ActionController
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
from .archive_index import ArchiveIndex
from .archive_playback import ArchivePlayer
from .arl import DEFAULT_HOME, ARLError, ARLRuntime
from .arl_rule_planning import (
    ARLRuleChanges,
    RuleAnalysisApproval,
    RuleAnalysisContext,
    RuleAnalysisRequest,
    RuleAnalysisResult,
    RuleAnalysisReview,
)
from .contracts import ActionRequest, ActionTrace, Event, Snapshot
from .core import HomeCore
from .core_rules import (
    CoreRuleApproval,
    CoreRulePlan,
    CoreRuleProposal,
    CoreRuleRecord,
    CoreRuleRun,
    CoreRuleRuntime,
    CoreRules,
)
from .diagnostics import Diagnostics, DiagnosticsExportRequest, DiagnosticsPolicy
from .discovery import HADiscovery
from .family_media import FamilyMediaCatalog
from .family_media.api import create_router as family_media_router
from .file_steward import FileSteward
from .file_steward.api import create_router as file_steward_router
from .geek_background import GeekBackground
from .geek_background import configured as geek_background_configured
from .geek_background import enabled as geek_background_enabled
from .geek_background import last_attempt as geek_background_last_attempt
from .geek_inventory import GeekInventoryView
from .geek_session import GeekSession, GeekSessionView
from .geek_setup import GeekSetup
from .ha_setup import install_oauth
from .home_flow import HomeFlow
from .home_flow import create_router as home_flow_router
from .measurement_settings import MeasurementSettings
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
from .media_probe import MediaProbe, RecognitionProbe
from .miloco_models import MilocoModels, ModelProbe, ModelSelection
from .nas_connection import (
    NASConnectionCommand,
    NASConnectionEdit,
    NASConnectionError,
    NASConnectionManager,
    NASConnectionSettings,
    NASConnectionStatus,
)
from .nas_connection.unavailable import UnavailableNASReader
from .people.api import create_router as people_router
from .people_feature_bridge import close_people_catalog, qualify_people_catalog
from .query import HomeQueries, HomeQueryAdoption, HomeQueryAdoptionConfig
from .query_contracts import HomeQuery, HomeQueryResult, ReadScope, arl_query_descriptor
from .recognition_contracts import RecognitionOffRequest, RecognitionStatus
from .rule_compiler import compile_rule
from .rule_contracts import RuleCompilation, RuleIR
from .rule_deployment import RemoveRuleAction, RuleApproval, RuleDeployment, RuleManager, RulePlanRef
from .rule_runtime import RuleRun, RuleRuntime
from .rule_text import RuleTextChanges, RuleTextContext, RuleTextRequest, RuleTextResult
from .settings import SettingsService
from .simulator import Simulator
from .store import TraceStore

SNAPSHOT_TIMEOUT_SECONDS = 5


class GeekCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passcode: SecretStr = Field(min_length=1, max_length=128)


def create_app(
    adapter=None,
    db_path=None,
    *,
    settings_path=None,
    settings_service=None,
    oauth_transport=None,
    authority_path=None,
    arl_runtime=None,
    arl_planning_authorizer=None,
    arl_planning_executor=None,
    archive_runner=None,
    geek_launcher=None,
    geek_readonly_directory=None,
    media_probe=None,
    nas_manager=None,
    nas_credential_resolver=None,
    family_media_simulated=False,
    file_steward_enabled=False,
    family_media_indexed_kinds=("image", "video"),
    query_adoption_config=None,
    people_catalog=None,
    flow_household_timezone=None,
    console_read_only=False,
    runtime_log=None,
    embodied_service=None,
    background_enabled=False,
):
    adapter = adapter or Simulator()
    store = TraceStore(Path(db_path or "data/homeplane/traces.sqlite"))
    core = HomeCore(adapter, store)
    diagnostics = Diagnostics(store.path)
    from .runtime_log import RuntimeLog, RuntimeLogMiddleware
    runtime_log = runtime_log or RuntimeLog(store.path.parent / 'logs')
    core.measurements = MeasurementSettings(store)
    lock = asyncio.Lock()
    settings = settings_service or SettingsService(settings_path or "data/homeplane/settings.json")
    nas_failure = None
    try:
        nas = nas_manager or NASConnectionManager(
            settings.nas_connection_state_path,
            credential_resolver=nas_credential_resolver or settings.resolve_nas_credential,
        )
    except NASConnectionError as exc:
        nas, nas_failure = None, exc.code

    def nas_status():
        if nas is None:
            return NASConnectionStatus(config_generation=0, state="not_configured", failure=nas_failure)
        report = nas.status()
        return report.model_copy(update={"failure": nas_failure}) if nas_failure else report

    def nas_owner():
        if nas is None:
            raise NASConnectionError(nas_failure or "nas_unavailable")
        return nas

    camera_media = media_probe or MediaProbe(settings)
    if people_catalog is not None:
        qualify_people_catalog(people_catalog)
    camera_recognition = RecognitionProbe(settings, people_catalog=people_catalog)

    def record_media_quality(report):
        # Input evidence only: never enqueue camera diagnostics as rule triggers.
        store.append_events([Event(source="camera-media", kind="quality-change",
            correlation_id=report.probe_id, wall_time=datetime.now(UTC),
            monotonic_time=time.monotonic(), boot_id=core.boot_id, time_basis="local_event",
            simulated=report.simulated, payload={"reason": "camera_media_probe", "status": report.status,
                "probe_id": report.probe_id, "provider": report.provider, "trigger_eligible": False,
                "video_decoded_frames": report.video.decoded_frames,
                "audio_decoded_frames": report.audio.decoded_frames,
                "source_av_alignment": report.source_av_alignment,
                "video_source_timestamps_seen": report.video.source_timestamps_seen,
                "audio_source_timestamps_seen": report.audio.source_timestamps_seen,
                "failure": report.failure or report.video.failure or report.audio.failure,
                "failure_phase": report.failure_phase,
                "media_persisted": False, "model_invoked": False, "device_write": False})])

    camera_media.on_result = record_media_quality
    miloco_models = MilocoModels(settings)
    core.actions = ActionController(core, authority_path or settings.path.parent / "action-authority.json")
    rules = RuleManager(core)
    rule_runtime = RuleRuntime(rules)
    core_rules = CoreRules(core, rules)
    core_rule_runtime = CoreRuleRuntime(core_rules)
    family_media = FamilyMediaCatalog(store.connection, nas or UnavailableNASReader(nas_status),
                                     simulated=family_media_simulated,
                                     indexed_kinds=family_media_indexed_kinds)
    file_steward = FileSteward(store.connection, family_media, session_status=nas_status) if file_steward_enabled else None
    queries = HomeQueries(core, family_media=family_media)
    if query_adoption_config is not None and not isinstance(query_adoption_config, HomeQueryAdoptionConfig):
        raise ValueError("invalid_query_adoption_config")
    query_adoption = query_adoption_config.bind(queries) if query_adoption_config is not None else None
    geek_directory = Path(geek_readonly_directory) if geek_readonly_directory is not None else settings.path.parent
    geek = GeekSetup(geek_directory)
    geek_session = GeekBackground(geek_directory) if geek_readonly_directory is not None or (geek_launcher is None and geek_background_enabled(geek_directory)) else GeekSession(geek, launcher=geek_launcher)
    geek_gate = asyncio.Lock()

    def writable_geek():
        if geek_readonly_directory is not None:
            raise HTTPException(403,detail='geek_owner_managed_separately')

    @asynccontextmanager
    async def geek_operation():
        # Do not queue a second PIN attempt or a late connect behind an edit/stop.
        if geek_gate.locked():
            raise HTTPException(status_code=409, detail="geek_operation_in_progress")
        async with geek_gate:
            yield

    async def geek_public_status():
        report = await geek_session.refresh()
        return {**report, "background_last_attempt": geek_background_last_attempt(geek_directory)}

    discovery = HADiscovery()
    runtime = arl_runtime or ARLRuntime(
        lambda: Path(settings.config.arl_home) if settings.config.arl_home else DEFAULT_HOME
    )
    rule_text = RuleTextChanges(rules, runtime)
    rule_analysis = ARLRuleChanges(
        rule_text,
        core_rules,
        runtime,
        authorizer=arl_planning_authorizer,
        planning_executor=arl_planning_executor,
    )
    archive = ArchiveIndex(
        store, settings, lambda: isinstance(core.adapter, Simulator), runner=archive_runner
    )
    archive_player = ArchivePlayer(archive)
    home_flow = HomeFlow(core, settings, rules, rule_text, queries, archive, archive_player,
                         rule_runtime, core_rule_runtime, nas_status=nas_status,
                         household_timezone=flow_household_timezone)
    geek.prune()
    from .embodied.service import EmbodiedService
    from .embodied.api import create_router as embodied_router
    embodied = embodied_service or EmbodiedService()
    from .infrastructure import HouseholdInfrastructure, ServicePolicy
    from .ha import HAReadOnlyAdapter
    async def prepare_household_interactions():
        from .embodied.paths import PRIVATE
        if (PRIVATE / 'interactions.json').exists():
            return
        from .embodied.household import DeviceCommands
        from .embodied.interaction import ensure_defaults
        inventory = {d['id']:d for d in await DeviceCommands(core, lock, embodied).inventory()}
        ensure_defaults(inventory, {c['id'] for c in embodied.cameras()})

    infrastructure = HouseholdInfrastructure(
        embodied, settings.path.with_name("infrastructure.json"),
        admitted=lambda: background_enabled and isinstance(core.adapter, HAReadOnlyAdapter), people=people_catalog, prepare=prepare_household_interactions,
    )

    def ingress(active, events):
        if core.adapter is active:
            store.append_events(events)
            core_rule_runtime.enqueue(active, events)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal nas_failure
        from .ha import HAReadOnlyAdapter

        try:
            if nas is not None:
                try:
                    await nas.start()
                except NASConnectionError as exc:
                    nas_failure = exc.code
            if isinstance(core.adapter, HAReadOnlyAdapter):
                active = core.adapter
                await active.subscribe(event_sink=lambda events: ingress(active, events))
            core_rule_runtime.start()
            await app.state.home_experience.start()
            await infrastructure.start()
            if query_adoption:
                query_adoption.bind_loop()
            yield
        finally:
            await infrastructure.close()
            await app.state.home_experience.close()
            await embodied.close()
            await home_flow.close()
            if query_adoption:
                query_adoption.close()
            queries.close()
            # Cancel catalog operations before dropping shared reader refs/store.
            await family_media.close()
            if nas is not None:
                try:
                    await nas.close()
                except NASConnectionError:
                    pass
            rule_analysis.close()
            await camera_media.stop()
            await camera_recognition.stop()
            if people_catalog is not None:
                await asyncio.to_thread(close_people_catalog, people_catalog)
            await geek_session.release()
            await archive_player.close()
            await archive.close()
            core.measurements.enrollment.close()
            await rule_runtime.close()
            await core_rule_runtime.close()
            await core.adapter.close()
            core.actions.close()
            rules.close()
            store.close()

    app = FastAPI(title="Home Control Plane", version="1", lifespan=lifespan)
    app.state.embodied = embodied
    app.state.infrastructure = infrastructure

    @app.get("/api/embodied/infrastructure")
    async def infrastructure_status():
        return infrastructure.report()

    @app.post("/api/embodied/infrastructure")
    async def infrastructure_configure(request: ServicePolicy):
        try:
            return await infrastructure.configure(request)
        except ValueError:
            raise HTTPException(409, detail="source_conflict") from None
    app.include_router(embodied_router(embodied, core, lock, infrastructure))
    from .home_experience import create_router as home_experience_router
    home_product_router=home_experience_router(core, lock, settings, embodied, archive, core_rule_runtime)
    app.include_router(home_product_router)
    app.state.home_experience=home_product_router.home_owner
    from .automation_catalog import create_router as automation_catalog_router
    app.include_router(automation_catalog_router(settings.path.with_name('automation-catalog.json'), core_rules, lambda: geek_session.inventory()))
    app.state.core = core
    app.state.settings = settings
    app.state.nas_connection = nas
    app.state.family_media = family_media
    app.state.file_steward = file_steward
    app.state.discovery = discovery
    app.state.rules = rules
    app.state.rule_runtime = rule_runtime
    app.state.core_rules = core_rules
    app.state.core_rule_runtime = core_rule_runtime
    app.state.queries = queries
    app.state.query_adoption = query_adoption
    app.state.rule_text = rule_text
    app.state.arl = runtime
    app.state.rule_analysis = rule_analysis
    app.state.archive = archive
    app.state.archive_player = archive_player
    app.state.geek_session = geek_session
    app.state.camera_media = camera_media
    app.state.camera_recognition = camera_recognition
    app.state.people = people_catalog
    app.state.home_flow = home_flow
    app.state.runtime_log = runtime_log
    app.include_router(home_flow_router(home_flow, lock))
    if people_catalog is not None:
        app.include_router(people_router(people_catalog))
        from .people.live_api import create_router as live_people_router
        app.include_router(live_people_router(people_catalog, embodied))
    app.include_router(family_media_router(family_media))
    if file_steward is not None:
        app.include_router(file_steward_router(file_steward))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.get('/api/diagnostics/runtime')
    async def runtime_diagnostics():
        return runtime_log.export()

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # OAuth cookies and client_id use a single fixed loopback origin. Canonicalize
        # the UI alias before login instead of letting localhost start a broken flow.
        if (
            request.method == "GET"
            and request.url.path == "/"
            and request.headers.get("host") == "localhost:8740"
        ):
            return RedirectResponse(
                "http://127.0.0.1:8740/",
                status_code=303,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        # Loopback-only service plus same-origin writes; never enable CORS wildcard.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            if origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"code": "unauthorized"}, status_code=403)
        callback = request.method == "GET" and request.url.path == "/auth/ha/callback"
        if request.headers.get("sec-fetch-site") == "cross-site" and not callback:
            return JSONResponse({"code": "unauthorized"}, status_code=403)
        body_limit=2*1024*1024 if request.url.path in {"/api/home/layout/preview","/api/home/layout/import"} else 16384
        if request.url.path == "/api/home/automations/catalog":
            body_limit = 256 * 1024 + 1024
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > body_limit:
            return JSONResponse({"code": "request_budget_exceeded"}, status_code=413)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > body_limit:
                    return JSONResponse({"code": "request_budget_exceeded"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        if (
            request.url.path.startswith(("/api/settings", "/api/rules", "/api/query", "/api/archive", "/api/diagnostics", "/api/family-media", "/api/people", "/api/home-flow", "/api/embodied", "/api/home/"))
            or request.url.path == "/api/source/simulator"
            or request.url.path in {"/api/actions/plan", "/api/actions/execute", "/api/actions/reconcile"}
        ):
            if request.method not in {"GET", "HEAD", "OPTIONS"} and origin != str(request.base_url).rstrip(
                "/"
            ):
                return JSONResponse({"code": "unauthorized"}, status_code=403)
        response = await call_next(request)
        frame_source="'none'"
        try:
            from .embodied.paths import configuration as embodied_configuration
            frame_source=f"http://127.0.0.1:{int(embodied_configuration()['viewer_port'])}"
        except (OSError,ValueError,KeyError):pass
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; frame-src "+frame_source
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_failed(request, exc):
        # FastAPI's default includes rejected input, including passwords. Never echo it.
        return JSONResponse({"code": "invalid_request"}, status_code=422)

    @app.exception_handler(NASConnectionError)
    async def nas_failed(request, exc):
        status = 409 if exc.code in {"nas_busy", "nas_source_changed"} else 503
        return JSONResponse({"code": exc.code}, status_code=status)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        # Don't serialize arbitrary upstream exception strings or URLs/tokens.
        code = (
            str(exc)
            if str(exc)
            in {
                "source_conflict",
                "unsupported_scenario",
                "not_found",
                "action_capacity_exceeded",
                "simulation_only_or_owner_unavailable",
                "expired_preview_requires_new_plan",
                "independent_source_identity_required",
                "measurement_group_room_mismatch",
                "measurement_position_review_required",
                "room_reference_position_required",
                "aliased_measurement_sources",
                "managed_measurement_group_required",
                "measurement_group_binding_changed",
                "distinct_group_source_required",
                "calibration_requires_healthy_comparable_sources",
                "insufficient_distinct_calibration_pairs",
                "calibration_temperature_coverage_insufficient",
                "calibration_candidate_expired",
                "calibration_evidence_changed",
                "calibration_candidate_budget",
            }
            else "invalid_request"
        )
        return JSONResponse(
            {"code": code},
            status_code=409
            if code == "source_conflict"
            else 404
            if code == "not_found"
            else 403
            if code == "simulation_only_or_owner_unavailable"
            else 400,
        )

    @app.get("/api/snapshot", response_model=Snapshot)
    async def snapshot():
        try:
            # A timed-out browser read must not leave growing HTTP/lock waiters
            # while the event worker performs its independent bounded recovery.
            async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                async with lock:
                    return await core.snapshot()
        except Exception:
            return JSONResponse(
                {"code": "offline_dependency", "detail": "Read-only source unavailable"}, status_code=503
            )

    @app.get("/api/doctor")
    async def doctor():
        from .cli import readiness

        return {
            **readiness(core.adapter, core.actions),
            "core_rules": core_rule_runtime.status(),
            "geek_session": await geek_session.refresh(),
            "arl": await runtime.status(),
        }

    @app.get("/api/diagnostics", response_model=DiagnosticsPolicy)
    async def diagnostic_policy():
        return DiagnosticsPolicy()

    @app.post("/api/diagnostics/export")
    async def diagnostic_export(request: DiagnosticsExportRequest):
        from .ha import HAReadOnlyAdapter

        active = core.adapter
        cached = core._diagnostic_snapshot
        source = cached[1] if cached is not None and cached[0] is active else None
        mode = "simulator" if isinstance(active, Simulator) else "ha-read-only" if isinstance(active, HAReadOnlyAdapter) else "unavailable"
        ha = active.health() if isinstance(active, HAReadOnlyAdapter) else None
        try:
            data = await diagnostics.export(source, mode, ha, geek_session.status())
        except ValueError as error:
            busy = str(error) == "diagnostics_busy"
            return JSONResponse({"code": "diagnostics_busy" if busy else "diagnostics_unavailable"}, status_code=429 if busy else 503)
        except (TimeoutError, OSError, sqlite3.Error):
            return JSONResponse({"code": "diagnostics_unavailable"}, status_code=503)
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="friday-diagnostics.zip"',
            "Content-Length": str(len(data)),
        })

    @app.get("/api/settings/measurements", response_model=MeasurementSettingsView)
    async def measurement_settings():
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                async with lock:
                    return core.measurements.view(core.adapter, await core.snapshot())
        except (TimeoutError, ValueError):
            return JSONResponse({"code": "measurement_configuration_unavailable"}, status_code=503)

    async def measurement_change(request, operation):
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                async with lock:
                    current = await core.snapshot()
                    core.measurements.change(core.adapter, current, request, operation)
                    return core.measurements.view(core.adapter, await core.snapshot())
        except TimeoutError:
            return JSONResponse({"code": "measurement_deadline_exceeded"}, status_code=503)

    @app.post("/api/settings/measurements/profile", response_model=MeasurementSettingsView)
    async def measurement_edit(request: EditMeasurement):
        return await measurement_change(request, "edit")

    @app.post("/api/settings/measurements/exclude", response_model=MeasurementSettingsView)
    async def measurement_exclude(request: ExcludeMeasurement):
        return await measurement_change(request, "exclude")

    @app.post("/api/settings/measurements/resume", response_model=MeasurementSettingsView)
    async def measurement_resume(request: MeasurementChange):
        return await measurement_change(request, "resume")

    async def measurement_enrollment(request, operation):
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                async with lock:
                    current = await core.snapshot()
                    service = core.measurements.enrollment
                    result = getattr(service, operation)(core.adapter, current, request)
                    if operation == "preview":
                        return result
                    return core.measurements.view(core.adapter, await core.snapshot())
        except TimeoutError:
            return JSONResponse({"code": "measurement_deadline_exceeded"}, status_code=503)

    @app.post("/api/settings/measurements/group", response_model=MeasurementSettingsView)
    async def measurement_group(request: EditMeasurementGroup):
        return await measurement_enrollment(request, "edit_group")

    @app.post("/api/settings/measurements/calibration/preview", response_model=CalibrationPreview)
    async def calibration_preview(request: PreviewRelativeCalibration):
        return await measurement_enrollment(request, "preview")

    @app.post("/api/settings/measurements/calibration/approve", response_model=MeasurementSettingsView)
    async def calibration_approve(request: ApproveRelativeCalibration):
        return await measurement_enrollment(request, "approve")

    @app.get("/api/query/contracts")
    async def query_contracts():
        return {
            "request": HomeQuery.model_json_schema(),
            "result": HomeQueryResult.model_json_schema(),
            "arl_proposal": arl_query_descriptor(),
            "registered_in_arl": False,
            "consumer_adoption": query_adoption.status() if query_adoption else HomeQueryAdoption.unconfigured_status(),
        }

    @app.post("/api/query", response_model=HomeQueryResult)
    async def household_query(request: HomeQuery):
        # The local Console already owns household reads. No client-supplied ARL
        # identity/grant is accepted; a production bridge needs verified host scope.
        scope = ReadScope(
            subject="console",
            revision="local-console-read-v1",
            expires_at=datetime.now(UTC) + timedelta(seconds=5),
            boundary="loopback_console",
            all_home=True,
            history=True,
            permitted=True,
            # Separate local owner capability, never inferred from room access.
            family_media_sources=frozenset({nas_status().source_ref}) if nas_status().source_ref else frozenset(),
        )
        try:
            async with asyncio.timeout(5):
                async with lock:
                    return await queries.read(request, scope)
        except TimeoutError:
            return JSONResponse({"code": "query_deadline_exceeded"}, status_code=503)

    @app.get("/api/contracts")
    async def contracts():
        return {
            "schema_version": 1,
            "snapshot": Snapshot.model_json_schema(),
            "action_request": ActionRequest.model_json_schema(),
            "action_trace": ActionTrace.model_json_schema(),
            "light_plan_request": LightPlanRequest.model_json_schema(),
            "light_execution_trace": LightExecutionTrace.model_json_schema(),
            "device_plan_request": DevicePlanRequest.model_json_schema(),
            "geek_session_status": GeekSessionView.model_json_schema(),
            "geek_inventory": GeekInventoryView.model_json_schema(),
            "media_probe_request": MediaProbeRequest.model_json_schema(),
            "media_probe_result": MediaProbeResult.model_json_schema(),
            "measurement_edit": EditMeasurement.model_json_schema(),
            "measurement_exclude": ExcludeMeasurement.model_json_schema(),
            "measurement_change": MeasurementChange.model_json_schema(),
            "measurement_settings": MeasurementSettingsView.model_json_schema(),
            "measurement_group": EditMeasurementGroup.model_json_schema(),
            "calibration_preview_request": PreviewRelativeCalibration.model_json_schema(),
            "calibration_preview": CalibrationPreview.model_json_schema(),
            "calibration_approval": ApproveRelativeCalibration.model_json_schema(),
            "rule_ir": RuleIR.model_json_schema(),
            "rule_compilation": RuleCompilation.model_json_schema(),
            "rule_deployment": RuleDeployment.model_json_schema(),
            "rule_run": RuleRun.model_json_schema(),
            "rule_text_request": RuleTextRequest.model_json_schema(),
            "rule_text_context": RuleTextContext.model_json_schema(),
            "rule_text_result": RuleTextResult.model_json_schema(),
            "core_rule_proposal": CoreRuleProposal.model_json_schema(),
            "core_rule_plan": CoreRulePlan.model_json_schema(),
            "core_rule_record": CoreRuleRecord.model_json_schema(),
            "core_rule_approval": CoreRuleApproval.model_json_schema(),
            "core_rule_run": CoreRuleRun.model_json_schema(),
            "rule_analysis_request": RuleAnalysisRequest.model_json_schema(),
            "rule_analysis_context": RuleAnalysisContext.model_json_schema(),
            "rule_analysis_review": RuleAnalysisReview.model_json_schema(),
            "rule_analysis_approval": RuleAnalysisApproval.model_json_schema(),
            "rule_analysis_result": RuleAnalysisResult.model_json_schema(),
            "archive_time_range": ArchiveTimeRange.model_json_schema(),
            "archive_camera_edit": ArchiveCameraEdit.model_json_schema(),
            "archive_resolve_request": ArchiveResolveRequest.model_json_schema(),
            "archive_segment_ref": ArchiveSegmentRef.model_json_schema(),
            "archive_resolution": ArchiveResolution.model_json_schema(),
            "archive_playback_request": ArchivePlaybackRequest.model_json_schema(),
            "archive_playback_plan": ArchivePlaybackPlan.model_json_schema(),
            "archive_playback_cancel": ArchivePlaybackCancel.model_json_schema(),
        }

    @app.get("/api/rules/core/context")
    async def core_rule_context():
        return core_rules.context()

    @app.get("/api/rules/core/records")
    async def core_rule_records():
        return core_rules.listing()

    @app.get("/api/rules/core/runtime")
    async def core_rule_runtime_status():
        return core_rule_runtime.status()

    class CoreMode(BaseModel):
        model_config = ConfigDict(extra="forbid")
        mode: str = Field(pattern="^(awake|sleep|manual|unknown)$")
        ttl_seconds: int = Field(default=60, ge=1, le=1800, strict=True)

    @app.post("/api/rules/core/runtime/mode")
    async def core_mode(request: CoreMode):
        return await core_rule_runtime.set_mode(request.mode, request.ttl_seconds)

    @app.get("/api/rules/core/runs/{run_id}", response_model=CoreRuleRun)
    async def core_rule_run(run_id: str):
        return core_rule_runtime.get(run_id)

    @app.post("/api/rules/core/plans", response_model=CoreRulePlan)
    async def core_rule_plan(request: CoreRuleProposal):
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                async with lock:
                    return await core_rules.plan(request)
        except TimeoutError:
            return JSONResponse({"code": "core_rule_source_timeout"}, status_code=503)

    @app.get("/api/rules/core/plans/{plan_id}", response_model=CoreRulePlan)
    async def core_rule_plan_get(plan_id: str):
        return core_rules.plan_get(plan_id)

    @app.post("/api/rules/core/plans/trial", response_model=CoreRulePlan)
    async def core_rule_trial(request: RulePlanRef):
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                async with lock:
                    return await core_rules.trial(request.plan_id)
        except TimeoutError:
            return JSONResponse({"code": "core_rule_source_timeout"}, status_code=503)

    @app.post("/api/rules/core/plans/apply", response_model=CoreRulePlan)
    async def core_rule_apply(request: CoreRuleApproval):
        async with lock:
            await core_rule_runtime.invalidate("configuration_change_requires_new_window")
            return await core_rules.apply(request)

    @app.post("/api/rules/core/plans/rollback", response_model=CoreRulePlan)
    async def core_rule_rollback(request: RulePlanRef):
        async with lock:
            await core_rule_runtime.invalidate("configuration_rollback_requires_new_window")
            return await core_rules.rollback(request.plan_id)

    @app.get("/api/rules/runtime")
    async def runtime_status():
        return rule_runtime.status()

    @app.get("/api/rules/runs/{run_id}", response_model=RuleRun)
    async def runtime_run(run_id: str):
        return rule_runtime.get(run_id)

    class SimulatedMode(BaseModel):
        model_config = ConfigDict(extra="forbid")
        mode: str = Field(pattern="^(awake|sleep|manual|unknown)$")

    class SimulatedContact(BaseModel):
        model_config = ConfigDict(extra="forbid")
        contact: str = Field(pattern="^(open|closed)$")

    @app.post("/api/rules/runtime/mode")
    async def runtime_mode(request: SimulatedMode):
        async with lock:
            return await rule_runtime.set_mode(request.mode)

    @app.post("/api/rules/runtime/contact")
    async def runtime_contact(request: SimulatedContact):
        async with lock:
            rules._allowed()
            event = core.adapter.contact_event(request.contact)
            if event:
                await rule_runtime.ingest(event)
            return {
                "event_created": event is not None,
                "runtime": rule_runtime.status(),
                "snapshot": jsonable_encoder(await core.snapshot()),
            }

    @app.get("/api/rules/managed")
    async def managed_rules():
        return rules.listing()

    @app.post("/api/rules/compile", response_model=RuleCompilation)
    async def rule_compile(rule: RuleIR):
        async with lock:
            return compile_rule(rule, await core.snapshot())

    @app.post("/api/rules/plans/remove-action", response_model=RuleDeployment)
    async def rule_remove(request: RemoveRuleAction):
        async with lock:
            return await rules.plan_remove(request)

    @app.get("/api/rules/plans/{plan_id}", response_model=RuleDeployment)
    async def rule_plan_get(plan_id: str):
        return rules.get(plan_id)

    @app.get("/api/rules/text/context", response_model=RuleTextContext)
    async def text_context():
        async with lock:
            return rule_text.context()

    @app.post("/api/rules/text/plan", response_model=RuleTextResult)
    async def text_plan(request: RuleTextRequest):
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                async with lock:
                    return await rule_text.plan(request)
        except TimeoutError:
            return JSONResponse({"code": "rule_text_deadline_exceeded"}, status_code=503)

    @app.get("/api/rules/analysis/context", response_model=RuleAnalysisContext)
    async def analysis_context():
        return rule_analysis.context()

    @app.post("/api/rules/analysis/reviews", response_model=RuleAnalysisReview)
    async def analysis_review(request: RuleAnalysisRequest):
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                async with lock:
                    return await rule_analysis.prepare(request)
        except (ARLError, ValueError) as exc:
            code = exc.code if isinstance(exc, ARLError) else str(exc)
            return JSONResponse(
                {
                    "code": code
                    if code.startswith(("analysis_", "planning_"))
                    else "analysis_review_unavailable"
                },
                status_code=409,
            )
        except (TimeoutError, OSError):
            return JSONResponse({"code": "analysis_source_unavailable"}, status_code=503)

    @app.post("/api/rules/analysis/analyze", response_model=RuleAnalysisResult)
    async def analysis_analyze(request: RuleAnalysisApproval):
        # The potentially slow SDK call never owns the general Core/API lock.
        # Default-off composition rejects before any SDK/provider operation.
        return await rule_analysis.analyze(request)

    @app.post("/api/rules/analysis/cancel", response_model=RuleAnalysisResult)
    async def analysis_cancel(request: RuleAnalysisApproval):
        return rule_analysis.cancel(request)

    @app.post("/api/rules/plans/simulate", response_model=RuleDeployment)
    async def rule_simulate(request: RulePlanRef):
        async with lock:
            return await rules.simulate(request.plan_id)

    @app.post("/api/rules/plans/apply", response_model=RuleDeployment)
    async def rule_apply(request: RuleApproval):
        async with lock:
            return await rules.apply(request)

    @app.post("/api/rules/plans/reconcile", response_model=RuleDeployment)
    async def rule_reconcile(request: RulePlanRef):
        async with lock:
            return await rules.reconcile(request.plan_id)

    @app.post("/api/rules/plans/rollback", response_model=RuleDeployment)
    async def rule_rollback(request: RulePlanRef):
        async with lock:
            return await rules.rollback(request.plan_id)

    @app.get("/api/decision/providers")
    async def decision_providers():
        from .decision_providers import registry_descriptors

        return {
            "providers": registry_descriptors(),
            "default": "arl:laya_mlx",
            "runtime": await runtime.status(),
            "mode": "shadow",
            "automatic_cloud_fallback": False,
        }

    @app.get("/api/settings/arl/status")
    async def arl_status():
        return await runtime.status()

    @app.post("/api/settings/arl/connect")
    async def arl_connect():
        # Upstream SDK discovers an existing shared instance or boots one without inference.
        return await runtime.status(start=True, timeout=10)

    @app.post("/api/settings/arl/probe")
    async def arl_probe():
        try:
            return await runtime.smoke()
        except ARLError as exc:
            return {"status": exc.code, "inference_attempted": False, "device_write_attempted": False}

    @app.post("/api/actions/dry-run", response_model=ActionTrace)
    async def dry_run(request: ActionRequest):
        async with lock:
            return await core.dry_run(request)

    @app.post("/api/actions/plan", response_model=LightExecutionTrace)
    async def action_plan(request: LightPlanRequest | DevicePlanRequest):
        async with lock:
            return await core.actions.plan(request)

    @app.post("/api/actions/execute", response_model=LightExecutionTrace)
    async def action_execute(request: ExecutePlan):
        await core_rule_runtime.invalidate("explicit_console_action_takes_over")
        async with lock:
            return await core.actions.execute(request.plan_id)

    @app.post("/api/actions/reconcile", response_model=LightExecutionTrace)
    async def action_reconcile(request: ExecutePlan):
        async with lock:
            return await core.actions.reconcile(request.plan_id)

    @app.post("/api/settings/actions/pause")
    async def actions_pause():
        home_flow.invalidate()
        result = core.actions.pause()
        await core_rule_runtime.invalidate("operator_revoked_actions")
        return result

    @app.get("/api/settings/actions/status")
    async def actions_status():
        return core.actions.policy_report()

    @app.post("/api/settings/actions/resume", response_model=Snapshot)
    async def actions_resume(request: ResumeDeviceControls):
        home_flow.invalidate()
        async with lock:
            core.actions.resume(request.expected_revision)
            return await core.snapshot()

    @app.get("/api/traces/{action_id}", response_model=ActionTrace | LightExecutionTrace)
    async def trace(action_id: str):
        result = store.get(action_id) or store.light_action(action_id)
        return result if result else JSONResponse({"code": "not_found"}, status_code=404)

    @app.post("/api/simulator/scenario/{name}", response_model=Snapshot)
    async def scenario(name: str):
        home_flow.invalidate()
        if not isinstance(core.adapter, Simulator):
            return JSONResponse({"code": "unauthorized"}, status_code=403)
        async with lock:
            await rule_runtime.invalidate("simulator_scenario_changed")
            core.adapter.set_scenario(name)
            return jsonable_encoder(await core.snapshot())

    @app.get("/api/settings")
    async def settings_get():
        public = settings.get_public()
        public["credentials"]["geek"] = {"credential_available": geek.available()}
        public["geek_session"] = await geek_public_status()
        public["geek_background_configured"] = geek_background_configured(geek_directory)
        public["geek_background_enabled"] = geek_background_enabled(geek_directory)
        public["geek_managed_separately"] = geek_readonly_directory is not None
        return public

    class ArchiveContextRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        expected_context: str = Field(pattern=r"^[a-f0-9]{64}$")

    class ArchiveSimulation(ArchiveContextRequest):
        scenario: str = Field(pattern=r"^(late-arrives|offline|reset)$")

    async def archive_call(operation):
        try:
            async with asyncio.timeout(8):
                return await operation()
        except ValueError as exc:
            code = str(exc)
            return JSONResponse(
                {"code": code if code.startswith("archive_") else "archive_unavailable"}, status_code=409
            )
        except (TimeoutError, OSError, TypeError, KeyError, sqlite3.Error):
            return JSONResponse({"code": "archive_unavailable"}, status_code=503)

    @app.get("/api/archive/context")
    async def archive_context():
        try:
            return archive.context()
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            return JSONResponse({"code": "archive_unavailable"}, status_code=503)

    @app.post("/api/archive/scan")
    async def archive_scan(request: ArchiveContextRequest):
        return await archive_call(lambda: archive.scan(request.expected_context))

    @app.post("/api/archive/cameras")
    async def archive_camera(request: ArchiveCameraEdit):
        home_flow.invalidate()
        try:
            return archive.save_camera(request)
        except ValueError as exc:
            code = str(exc)
            return JSONResponse(
                {"code": code if code.startswith("archive_") else "archive_unavailable"}, status_code=409
            )
        except (TypeError, KeyError, sqlite3.Error):
            return JSONResponse({"code": "archive_unavailable"}, status_code=503)

    @app.post("/api/archive/resolve", response_model=ArchiveResolution)
    async def archive_resolve(request: ArchiveResolveRequest):
        return await archive_call(lambda: archive.resolve(request))

    @app.post("/api/archive/playback", response_model=ArchivePlaybackPlan)
    async def archive_playback(request: ArchivePlaybackRequest):
        return await archive_call(lambda: archive_player.prepare(request))

    @app.post("/api/archive/playback/cancel")
    async def archive_playback_cancel(request: ArchivePlaybackCancel):
        return archive_player.revoke(request.plan_id)

    @app.get("/api/archive/media/{plan_id}")
    async def archive_media(plan_id: str, request: Request):
        origin, site = request.headers.get("origin"), request.headers.get("sec-fetch-site")
        expected = str(request.base_url).rstrip("/")
        if origin != expected and (origin is not None or site != "same-origin"):
            return JSONResponse({"code": "unauthorized"}, status_code=403)
        if not re.fullmatch(r"[a-f0-9]{64}", plan_id):
            return JSONResponse({"code": "archive_playback_not_found"}, status_code=404)
        try:
            reader = await archive_player.read(plan_id, request.headers.get("range"))
        except ValueError as exc:
            code = str(exc)
            status = 409
            if code in {"archive_invalid_media_range", "archive_media_range_required"}:
                status = 416
            elif code == "archive_playback_not_found":
                status = 404
            elif code in {
                "archive_context_changed",
                "archive_playback_expired",
                "archive_source_changed_or_unavailable",
            }:
                status = 410
            return JSONResponse(
                {"code": code if code.startswith("archive_") else "archive_unavailable"}, status_code=status
            )
        except (TimeoutError, OSError, TypeError, KeyError, sqlite3.Error):
            return JSONResponse({"code": "archive_media_unavailable"}, status_code=503)
        headers = {
            "Content-Length": str(reader.length),
            "Accept-Ranges": "bytes",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "Content-Disposition": 'inline; filename="archive.mp4"',
        }
        if reader.partial:
            headers["Content-Range"] = (
                f"bytes {reader.start}-{reader.start + reader.length - 1}/{reader.lease.binding['size_bytes']}"
            )
        return StreamingResponse(
            reader.chunks(),
            status_code=206 if reader.partial else 200,
            media_type="video/mp4",
            headers=headers,
            background=BackgroundTask(reader.close),
        )

    @app.post("/api/archive/simulator")
    async def archive_simulator(request: ArchiveSimulation):
        home_flow.invalidate()
        try:
            return archive.simulate(request.expected_context, request.scenario)
        except ValueError as exc:
            return JSONResponse(
                {"code": str(exc) if str(exc).startswith("archive_") else "archive_unavailable"},
                status_code=409,
            )
        except (TypeError, KeyError, sqlite3.Error):
            return JSONResponse({"code": "archive_unavailable"}, status_code=503)

    @app.get("/api/settings/ha/discover")
    async def ha_discover():
        return await discovery.discover()

    @app.get("/api/settings/miloco/models")
    async def miloco_models_status():
        return await miloco_models.status()

    @app.post("/api/settings/miloco/models/save")
    async def miloco_models_save(selection: ModelSelection):
        try:
            return await miloco_models.save(selection)
        except (OSError, TimeoutError, httpx.HTTPError):
            return JSONResponse(
                {"status": "save_unverified", "verification": "unknown_outcome"}, status_code=503
            )

    @app.post("/api/settings/miloco/models/probe")
    async def miloco_models_probe(selection: ModelProbe):
        try:
            return await miloco_models.probe(selection.preset)
        except (OSError, TimeoutError, httpx.HTTPError):
            return JSONResponse({"status": "probe_unavailable", "verification": "NOT_RUN"}, status_code=503)

    @app.put("/api/settings")
    async def settings_update(patch: dict):
        home_flow.invalidate()
        before_camera = (settings.config.camera_service_url, settings.config.camera_id, settings.config.camera_media_source)
        if "geek_url" in patch:
            writable_geek()
            async with geek_operation():
                before = settings.config.geek_url
                result = settings.update(patch)
                if before != settings.config.geek_url:
                    await geek_session.close()
        else:
            result = settings.update(patch)
        # This private recognition scope binds the settings revision, including edits.
        await camera_recognition.stop()
        if before_camera != (settings.config.camera_service_url, settings.config.camera_id, settings.config.camera_media_source):
            await camera_media.stop(changed=True)
        result["geek_session"] = await geek_public_status()
        return result

    @app.post("/api/settings/nas/probe")
    async def nas_probe():
        return await settings.probe_nas()

    @app.get("/api/settings/nas/connection", response_model=NASConnectionStatus)
    async def nas_connection_status():
        return nas_status()

    @app.get("/api/settings/nas/connection/config", response_model=NASConnectionSettings)
    async def nas_connection_saved_settings():
        # Owner-local setup only. Cached fields are not authentication evidence;
        # no private resolver, transport, state migration or credential read.
        config = nas.config if nas is not None else None
        return NASConnectionSettings(config=config, status=nas_status(),
                                     credential_reference_saved=bool(config and config.credential_ref))

    @app.put("/api/settings/nas/connection", response_model=NASConnectionStatus)
    async def nas_connection_configure(edit: NASConnectionEdit):
        nonlocal nas_failure
        home_flow.invalidate()
        result = await nas_owner().configure(edit.config, expected_generation=edit.expected_generation)
        nas_failure = None
        return result

    @app.post("/api/settings/nas/connection/resume", response_model=NASConnectionStatus)
    async def nas_connection_resume(command: NASConnectionCommand):
        nonlocal nas_failure
        home_flow.invalidate()
        result = await nas_owner().resume(expected_generation=command.expected_generation)
        nas_failure = None
        return result

    @app.post("/api/settings/nas/connection/disable", response_model=NASConnectionStatus)
    async def nas_connection_disable(command: NASConnectionCommand):
        nonlocal nas_failure
        home_flow.invalidate()
        result = await nas_owner().disable(expected_generation=command.expected_generation)
        nas_failure = None
        return result

    @app.post("/api/settings/camera/probe")
    async def camera_probe():
        return await settings.probe_camera()

    @app.get("/api/settings/camera/media", response_model=MediaProbeResult)
    async def camera_media_status():
        return camera_media.status()

    @app.post("/api/settings/camera/media", response_model=MediaProbeResult)
    async def camera_media_start(request: MediaProbeRequest):
        try:
            return await camera_media.start(request)
        except ValueError:
            return JSONResponse({"code": "media_probe_busy"}, status_code=409)

    @app.get("/api/settings/camera/recognition", response_model=RecognitionStatus)
    async def camera_recognition_status():
        return camera_recognition.status()

    @app.post("/api/settings/camera/recognition/off", response_model=RecognitionStatus)
    async def camera_recognition_off(request: RecognitionOffRequest):
        settings.update({"camera_recognition_enabled": request.enabled})
        await camera_recognition.stop()
        return camera_recognition.status()

    @app.post("/api/settings/camera/media/stop", response_model=MediaProbeResult)
    async def camera_media_stop():
        await camera_media.stop()
        return camera_media.status()

    @app.post("/api/settings/geek/credential")
    async def geek_credential(credential: GeekCredential):
        writable_geek()
        async with geek_operation():
            await geek_session.close()
            return geek.save(credential.passcode.get_secret_value())

    @app.post("/api/settings/geek/probe")
    async def geek_probe():
        await geek_session.refresh()
        if isinstance(geek_session, GeekBackground) or geek_background_configured(settings.path.parent):
            # Never launch a competing one-shot handshake when the owner is
            # configured, even if its socket is unavailable or stopped.
            return await geek_public_status()
        if geek_session.live():
            return geek_session.status()
        revision = settings.revision
        report = await geek.probe(settings.config.geek_url)
        return settings._record("geek", report, revision)

    @app.get("/api/settings/geek/session", response_model=GeekSessionView)
    async def geek_session_status():
        return await geek_public_status()

    @app.get("/api/settings/geek/inventory", response_model=GeekInventoryView)
    async def geek_inventory():
        return await geek_session.inventory()

    @app.post("/api/settings/geek/connect", response_model=GeekSessionView)
    async def geek_connect():
        nonlocal geek_session
        writable_geek()
        async with geek_operation():
            if not settings.config.geek_url or not geek.available():
                await geek_session.close()
                return {**(await geek_public_status()), "status": "pairing_required"}
            if geek_launcher is None and not geek_background_enabled(settings.path.parent):
                await prepare_geek_background()
                return await geek_public_status()
            if geek_launcher is None and not geek_session.live() and geek_background_enabled(settings.path.parent):
                await geek_session.release()
                geek_session = GeekBackground(settings.path.parent)
                app.state.geek_session = geek_session
            await geek_session.connect(settings.config.geek_url)
            return await geek_public_status()

    @app.post("/api/settings/geek/background/install")
    async def geek_background_install():
        async with geek_operation():
            return await prepare_geek_background()

    async def prepare_geek_background():
        nonlocal geek_session
        writable_geek()
        from .geek_service import enable, install
        await install(settings.path.parent)
        result = await enable(settings.path.parent)
        preserved = geek_session.live()
        if result["enabled"]:
            await geek_session.release()  # Only after the new owner has a complete inventory.
            geek_session = GeekBackground(settings.path.parent)
            await geek_session.refresh()
            app.state.geek_session = geek_session
        return {"installed": True, "enabled": result["enabled"], "current_connection_preserved": preserved and not result["enabled"], "background": result["background"]}

    @app.post("/api/settings/geek/disconnect", response_model=GeekSessionView)
    async def geek_disconnect():
        writable_geek()
        async with geek_operation():
            await geek_session.close()
            return await geek_public_status()

    @app.post("/api/settings/ha/activate", response_model=Snapshot)
    async def ha_activate():
        home_flow.invalidate()
        from .ha_configured import build

        revision = settings.revision
        candidate = None
        try:
            candidate = await build(settings.path)
            from .ha import HAReadOnlyAdapter

            if isinstance(candidate, HAReadOnlyAdapter):
                selected = candidate
                await candidate.subscribe(event_sink=lambda events: ingress(selected, events))
            initial = await candidate.snapshot()
            if candidate.health().get("transport") != "online":
                raise ValueError("ha_unavailable")
            async with lock:
                if revision != settings.revision:
                    raise ValueError("source_conflict")
                store.append_events(initial.events)
                old = core.adapter
                await core_rule_runtime.invalidate("source_changed")
                await rule_runtime.invalidate("source_changed", reset_modes=True)
                core.adapter = candidate
                candidate = None
        except Exception:
            if candidate:
                await candidate.close()
            return JSONResponse(
                {"code": "ha_readonly_activation_failed", "source_unchanged": True}, status_code=503
            )
        try:
            await old.close()
        except Exception:
            pass
        return core.actions.decorate(initial)

    @app.post("/api/source/simulator", response_model=Snapshot)
    @app.post("/api/settings/source/simulator", response_model=Snapshot)
    async def simulator_activate():
        home_flow.invalidate()
        async with lock:
            old = core.adapter
            await core_rule_runtime.invalidate("source_changed")
            await rule_runtime.invalidate("source_changed", reset_modes=True)
            core.adapter = Simulator()
            await old.close()
            return await core.snapshot()

    install_oauth(app, settings.path, transport=oauth_transport)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/index.html", methods=["GET", "HEAD"], include_in_schema=False)
    def console_index():
        path = Path(__file__).parent / "static/index.html"
        if people_catalog is None and file_steward is None and not console_read_only:
            return FileResponse(path)
        attributes = (' data-people-enabled="true"' if people_catalog is not None else '')
        attributes += (' data-file-steward-enabled="true"' if file_steward is not None else '')
        attributes += (' data-console-read-only="true"' if console_read_only else '')
        return HTMLResponse(path.read_text().replace('<body>', '<body' + attributes + '>', 1))

    if people_catalog is not None:
        app.mount("/people-static", StaticFiles(directory=Path(__file__).parent / "people/static"), name="people-assets")
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="console")
    app.add_middleware(RuntimeLogMiddleware, journal=runtime_log)
    return app
