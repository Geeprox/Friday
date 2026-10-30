"""Console App library and narrow lifecycle/device bridge."""

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from homeplane.runtime_log import observe

from .catalog import RECOGNIZERS, catalog, get_app
from .household import DeviceCommands, basic_command
from .paths import PRIVATE


class Start(BaseModel):
    open_privacy: bool = False
    model_config = ConfigDict(extra="forbid")
    camera: str | None = Field(default=None, pattern=r"^cam_[a-f0-9]{10}$")
    app: str | None = Field(default=None, max_length=32)


class MiHomeConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(status|begin|finish)$")
    return_url: str | None = Field(default=None,max_length=4096)


class CameraControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: str | None = Field(default=None,pattern="^(left|right|up|down)$")
    mode: str | None = Field(default=None,pattern="^(hold|auto|survey)$")


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


def create_router(service, core, lock, infrastructure=None):
    router = APIRouter()
    from .audio_output import install as install_speech_output
    install_speech_output(router)
    from .safety_api import install as install_safety
    service.safety_output = install_safety(router, service)
    devices = DeviceCommands(core, lock, service)

    @router.get("/api/games")
    async def games():
        return dict(apps=catalog(), recognition=RECOGNIZERS, service=await service.status())

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

    @router.post("/api/embodied/start")
    async def start(request: Start):
        if request.app:
            get_app(request.app)
        try:
            return await service.start(request.camera, request.app, open_privacy=request.open_privacy)
        except (OSError, ValueError):
            raise HTTPException(409, detail="embodied_start_unavailable") from None

    @router.post("/api/embodied/stop")
    async def stop():
        if infrastructure is not None:
            return await infrastructure.configure(infrastructure.policy.model_copy(update={"enabled": False}))
        return await service.stop()

    @router.post("/api/embodied/game/stop")
    async def stop_game():
        return await service.request("/api/game", {"action": "select", "mode": "off"})

    @router.post("/api/embodied/quiet")
    async def quiet(request: Quiet):
        return await service.quiet(request.quiet)

    @router.get("/api/games/{app_id}/open")
    async def open_app(app_id: str):
        get_app(app_id)
        report = await service.status()
        if not report["owned"]:
            raise HTTPException(409, detail="embodied_not_running")
        return dict(url=service.viewer(app_id), tv_url=service.viewer(app_id, tv=True))

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
