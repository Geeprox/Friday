"""Console App library and narrow lifecycle/device bridge."""

import secrets
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from homeplane.runtime_log import observe

from .catalog import RECOGNIZERS, TennisVariant, catalog, get_app
from .camera_follow_picker import FollowCommand
from .game_difficulty import DifficultyId, difficulty_options
from .household import DeviceCommands, basic_command
from .paths import PRIVATE
from .ski_route import SkiTrailId, ski_trail_options


class Start(BaseModel):
    open_privacy: bool = False
    model_config = ConfigDict(extra="forbid")
    camera: str | None = Field(default=None, pattern=r"^cam_[a-f0-9]{10}$")
    app: str | None = Field(default=None, max_length=32)
    difficulty: DifficultyId | None = None
    ski_trail: SkiTrailId | None = None
    tennis_variant: TennisVariant | None = None
    expected_intent: str = Field(pattern=r'^[a-f0-9]{64}$', strict=True)


class GameControl(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_intent: str = Field(pattern=r'^[a-f0-9]{64}$')


class MiHomeConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(status|begin|finish)$")
    return_url: str | None = Field(default=None,max_length=4096)


class CameraControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: str | None = Field(default=None,pattern="^(left|right|up|down)$")
    mode: str | None = Field(default=None,pattern="^(hold|auto|pets|survey)$")


class CameraPower(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    enabled:bool
    revision:str=Field(pattern=r'^[a-f0-9]{64}$')
    event_id:str=Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


class StopPreview(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    token:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')


class SceneRun(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    scene:str=Field(pattern=r'^[a-f0-9]{64}$')
    revision:str=Field(pattern=r'^[a-f0-9]{64}$')
    event_id:str=Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


class Quiet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quiet: bool


class VoiceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


class PracticeStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    gesture: str = Field(min_length=1, max_length=40)
    request_id: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    issued_at_ms: int | None = Field(default=None,ge=0,strict=True)

    @model_validator(mode='after')
    def paired_request(self):
        if (self.request_id is None) != (self.issued_at_ms is None):
            raise ValueError('practice_request_pair_required')
        return self


class PracticeToken(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str | None = Field(default=None,pattern=r'^[a-f0-9]{32}$')
    request_id: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def current_request(self):
        if self.token is None and self.request_id is None:
            raise ValueError('practice_request_required')
        return self


class GestureContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    binding: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: int = Field(ge=0)
    observed_mono: float = Field(ge=0)


class Command(BaseModel):
    gesture: GestureContext | None = None
    target: str | None = Field(default=None, min_length=1, max_length=256)
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    device: str | None = Field(default=None, min_length=1, max_length=80)
    action: str | None = Field(default=None, max_length=48)
    room: str | None = Field(default=None, max_length=64)
    event_id: str = Field(min_length=1, max_length=160)
    preview: bool = False


class Resume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ModelIntent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(pattern=r'^[a-z0-9-]{1,128}$')
    input: str = Field(min_length=1, max_length=12000)
    voice_revision:int|None=Field(default=None,ge=0,strict=True)


def create_router(service, core, lock, infrastructure=None, *, arl_runtime=None, archive=None, home_flow=None):
    router = APIRouter()
    from .voice_preferences import install as install_voice_preferences
    voice_preferences=install_voice_preferences(router,private=PRIVATE)
    from .audio_output import install as install_speech_output
    install_speech_output(router)
    from .safety_api import install as install_safety
    service.safety_output = install_safety(router, service, archive=archive,infrastructure=infrastructure)
    devices = DeviceCommands(core, lock, service)
    if home_flow is not None:
        from .voice_questions import install as install_voice_questions
        voice_source=install_voice_questions(router,service,home_flow,infrastructure,voice_preferences,PRIVATE)
        from .voice_tasks import install as install_voice_tasks
        install_voice_tasks(router,voice_source)

    @router.post('/api/embodied/model')
    async def model(request: ModelIntent, incoming: Request):
        try:
            expected = (PRIVATE / 'control-key').read_text().strip()
        except OSError:
            raise HTTPException(403) from None
        supplied = incoming.headers.get('x-friday-worker', '')
        if not 32 <= len(expected) <= 512 or not secrets.compare_digest(expected, supplied):
            raise HTTPException(403)
        if arl_runtime is None:
            raise HTTPException(503, detail='application_model_unavailable')
        try:voice_revision=voice_preferences.read().revision
        except (ValueError,OSError):raise HTTPException(409,detail='voice_preferences_unavailable')from None
        if request.voice_revision is not None and request.voice_revision!=voice_revision:
            raise HTTPException(409,detail='voice_preferences_changed')
        def current():
            try:current_revision=voice_preferences.read().revision
            except (ValueError,OSError):return False
            return bool(current_revision==voice_revision and service.capture_enabled and not (PRIVATE / 'voice-input-paused').exists()
                        and (infrastructure is None or infrastructure.policy.enabled
                             and infrastructure.policy.recognition and infrastructure.policy.interaction))
        if not current():
            raise HTTPException(409, detail='model_input_paused')
        from homeplane.arl import ARLError, model_task
        from homeplane.preview_request import prepare_for_viewer
        deadline = time.monotonic() + 30
        try:
            with model_task('household.voice.intent', 'interactive', deadline=deadline, current=current):
                result=await prepare_for_viewer(incoming, lambda: arl_runtime.complete_scenario(
                    'voice_intent', request.input, request_id=request.request_id, deadline=deadline), timeout=32)
                if not current():raise HTTPException(409,detail='model_input_changed')
                return result
        except (ARLError, ValueError, TimeoutError):
            raise HTTPException(503, detail='application_model_unavailable') from None

    @router.get("/api/games")
    async def games():
        return dict(apps=catalog(), recognition=RECOGNIZERS, difficulties=difficulty_options(),
                    ski_trails=ski_trail_options(preview=True), service=await service.status())

    @router.get('/api/embodied/scenes')
    async def scenes():
        try:return await service.scenes()
        except (ValueError,OSError,TimeoutError):raise HTTPException(409,detail='mijia_scenes_unavailable')from None

    @router.post('/api/embodied/scenes/run')
    async def run_scene(request:SceneRun):
        try:return await service.scenes(scene=request.scene,revision=request.revision,event_id=request.event_id)
        except (ValueError,OSError,TimeoutError):raise HTTPException(409,detail='mijia_scene_not_confirmed')from None

    @router.get("/api/embodied/status")
    async def status():
        return await service.status()

    @router.post('/api/embodied/practice/start')
    async def practice_start(request: PracticeStart):
        try:
            return await service.practice('start', request.model_dump(exclude_none=True))
        except (ValueError, OSError) as error:
            code = str(error)
            raise HTTPException(409, detail=code if code in {
                'practice_already_active', 'practice_recognition_not_running',
            } else 'practice_not_ready') from None

    @router.post('/api/embodied/practice/status')
    async def practice_status(request: PracticeToken):
        try:
            return await service.practice('status', request.model_dump(exclude_none=True))
        except (ValueError, OSError):
            raise HTTPException(409, detail='practice_unavailable') from None

    @router.post('/api/embodied/practice/stop')
    async def practice_stop(request: PracticeToken):
        try:
            return await service.practice('stop', request.model_dump(exclude_none=True))
        except (ValueError, OSError):
            raise HTTPException(409, detail='practice_unavailable') from None

    @router.post('/api/embodied/voice-input')
    async def voice_input(request: VoiceInput):
        return await service.voice_input(request.enabled)

    @router.post("/api/embodied/cameras/{camera_id}/control")
    async def camera_control(camera_id: str, request: CameraControl):
        if camera_id not in {c['id'] for c in service.cameras()}:
            raise HTTPException(409,detail="camera_not_running")
        if bool(request.direction)==bool(request.mode):raise HTTPException(422)
        try:
            return await service.camera_control(camera_id,direction=request.direction,mode=request.mode)
        except (ValueError,OSError):raise HTTPException(409,detail="camera_control_unavailable")from None

    @router.get('/api/embodied/connection')
    async def mijia_connection_status():
        return await service.connection()

    @router.post('/api/embodied/connection')
    async def mijia_connection(request:MiHomeConnection):
        return await service.connection(request.action,request.return_url)

    @router.get("/api/embodied/cameras")
    async def cameras():
        return dict(cameras=service.cameras())

    @router.get('/api/embodied/cameras/{camera_id}/power')
    async def camera_power_status(camera_id:str):
        try:return await service.camera_power(camera_id)
        except (ValueError,OSError,TimeoutError):raise HTTPException(409,detail='camera_power_unavailable')from None

    @router.post('/api/embodied/cameras/{camera_id}/power')
    async def camera_power(camera_id:str,request:CameraPower):
        try:return await service.camera_power(camera_id,enabled=request.enabled,revision=request.revision,event_id=request.event_id)
        except (ValueError,OSError,TimeoutError):raise HTTPException(409,detail='camera_power_unavailable')from None

    @router.post('/api/embodied/cameras/{camera_id}/preview/stop')
    async def stop_preview(camera_id:str,request:StopPreview):
        try:return await service.stop_preview(camera_id,request.token)
        except (ValueError,OSError):raise HTTPException(409,detail='camera_preview_unavailable')from None

    @router.get("/api/embodied/devices")
    async def inventory():
        return dict(devices=await devices.inventory(), authority=core.actions.policy_report())

    @router.post("/api/embodied/control/pause")
    async def pause_controls():
        return core.actions.pause()

    @router.post("/api/embodied/control/resume")
    async def resume_controls(request: Resume):
        core.actions.resume(request.expected_revision)
        return core.actions.policy_report()

    @router.post("/api/embodied/cameras/{camera_id}/preview")
    async def start_preview(camera_id: str):
        try:
            return await service.preview(camera_id, start=True,explicit=True)
        except ValueError:
            raise HTTPException(409, detail="camera_preview_unavailable") from None

    @router.get("/api/embodied/cameras/{camera_id}/preview")
    async def preview_status(camera_id: str):
        try:
            return await service.preview(camera_id)
        except ValueError:
            raise HTTPException(409, detail="camera_preview_unavailable") from None

    @router.get("/api/embodied/cameras/{camera_id}/frame")
    async def preview_frame(camera_id: str):
        try:
            return Response(await service.preview(camera_id, frame=True), media_type="image/jpeg")
        except ValueError:
            raise HTTPException(409, detail="camera_frame_unavailable") from None

    @router.get('/api/embodied/cameras/{camera_id}/follow-target')
    async def follow_target_view(camera_id: str):
        try:
            report = await service.camera_follow_target(camera_id)
        except (ValueError, OSError, httpx.HTTPError):
            raise HTTPException(409, detail='follow_target_unavailable') from None
        return JSONResponse(report, headers={'Cache-Control': 'no-store'})

    @router.post('/api/embodied/cameras/{camera_id}/follow-target')
    async def follow_target_command(camera_id: str, command: FollowCommand):
        try:
            return await service.camera_follow_target(camera_id, command.model_dump())
        except (ValueError, OSError, httpx.HTTPError):
            raise HTTPException(409, detail='follow_target_unavailable') from None

    @router.post("/api/embodied/start")
    async def start(request: Start):
        if request.app:
            if get_app(request.app).kind == 'party':
                raise HTTPException(409, detail='party_host_required')
        try:
            options = request.model_dump(include={'difficulty', 'ski_trail', 'tennis_variant'}, exclude_none=True)
            return await service.start(request.camera, request.app, open_privacy=request.open_privacy,
                                       expected_intent=request.expected_intent, **options)
        except (OSError, ValueError, httpx.HTTPError) as error:
            detail = 'game_state_changed' if str(error) == 'game_state_changed' else 'embodied_start_unavailable'
            raise HTTPException(409, detail=detail) from None

    @router.post("/api/embodied/stop")
    async def stop():
        if infrastructure is not None:
            return await infrastructure.configure(infrastructure.policy.model_copy(update={"enabled": False}))
        return await service.stop()

    @router.post("/api/embodied/game/stop")
    async def stop_game(request: GameControl):
        try:
            return await service.game_command({"mode": "off"}, expected_intent=request.expected_intent)
        except (OSError, ValueError, httpx.HTTPError) as error:
            detail = 'game_state_changed' if str(error) == 'game_state_changed' else 'game_stop_unavailable'
            raise HTTPException(409, detail=detail) from None

    @router.post("/api/embodied/quiet")
    async def quiet(request: Quiet):
        return await service.quiet(request.quiet)

    @router.get("/api/games/{app_id}/open")
    async def open_app(app_id: str, request: Request):
        app = get_app(app_id)
        if app.kind == 'party':
            raise HTTPException(409, detail='party_host_required')
        report = await service.status()
        if not report["owned"]:
            raise HTTPException(409, detail="embodied_not_running")
        intent = request.headers.get('x-friday-game-intent')
        if intent is not None:
            game = report.get('game') or {}
            mode = game.get('mode')
            if mode == 'off' and game.get('game_input_enabled') is True:
                mode = game.get('selected_mode')
            if intent != report.get('game_intent') or mode != app.mode:
                raise HTTPException(409, detail='game_state_changed')
        return dict(url=service.viewer(app_id), tv_url=service.viewer(app_id, tv=True))

    @router.get('/api/embodied/recognition/open')
    async def open_recognition():
        if not (await service.status()).get('owned'):
            raise HTTPException(409, detail='embodied_not_running')
        return dict(url=service.recognition_viewer())

    @router.post("/api/embodied/command")
    @observe("embodied.command")
    async def command(request: Command, incoming: Request):
        # Both Console same-origin and process-scoped capability calls pass the
        # main API origin gate. A capability does not create device authority.
        if incoming.headers.get("x-friday-worker"):
            try:
                expected = (PRIVATE / "control-key").read_text().strip()
            except OSError:
                raise HTTPException(403) from None
            if not secrets.compare_digest(expected, incoming.headers["x-friday-worker"]):
                raise HTTPException(403)
        plan = basic_command(request.text, attending=True) if request.text else None
        if plan and plan["route"] == "no_op":
            return dict(status="no_op", write_attempted=False, reply=plan["reply"])
        query = plan.get("device") if plan else request.device
        action = plan.get("action") if plan else request.action
        if (not query and not request.target) or not action:
            return dict(status="clarify", write_attempted=False, reply="请说明房间、设备和动作。")
        def current_gesture():
            if request.gesture is None:
                return True
            import time
            if time.monotonic() < getattr(service, 'practice_deadline', 0):
                return False
            from .interaction import load
            try:
                config = load()
                binding = next((b for b in config.bindings if b.id == request.gesture.binding), None)
                return bool(config.enabled and config.revision == request.gesture.revision and binding
                    and binding.enabled and binding.target == request.target and binding.action == action
                    and 0 <= time.monotonic() - request.gesture.observed_mono <= 1.5
                    and (infrastructure is None or infrastructure.policy.enabled
                         and infrastructure.policy.recognition and infrastructure.policy.interaction))
            except (OSError, ValueError):
                return False
        return await devices.command(
            query, action, request.event_id, room=request.room, preview=request.preview, target=request.target,
            dispatch_guard=current_gesture if request.gesture is not None else None,
        )

    return router
