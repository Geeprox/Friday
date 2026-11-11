"""Loopback host controls. Private phone APIs live on the party-only gateway."""

import asyncio
import importlib.util
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .werewolf import GameError, RULES, SPEAKER_SECONDS
from .werewolf_effects import HostEffects, SceneLights
from .werewolf_service import WerewolfService, join_qr_svg


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    players: int = Field(ge=6, le=12)
    address: str | None = Field(default=None, max_length=45)
    lights: list[str] = Field(default_factory=list, max_length=12)
    narration: bool = False
    camera: bool = False
    discussion_mode: Literal["free", "turns"] = "free"


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["start", "advance", "pause", "resume", "end", "remove", "reseat", "close", "speaker", "rematch"]
    seat: int | None = Field(default=None, ge=1, le=12)
    session: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=0)
    event_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{12,64}$")


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seat: int = Field(ge=1, le=12)
    camera: str = Field(pattern=r"^cam_[a-f0-9]{10}$")
    session: str = Field(min_length=1, max_length=128)
    track: int = Field(ge=0)
    game_session: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=0)
    binding_revision: int | None = Field(default=None, ge=0)
    event_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{12,64}$")


class CameraView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session: str = Field(min_length=1, max_length=128)
    phase: Literal['lobby', 'discussion', 'runoff_discussion'] | None = None
    round: int | None = Field(default=None, ge=0)


def create_router(core, lock, embodied, directory, *, owner=None):
    if owner is None:
        from .embodied.audio_output import report
        from .embodied.speech import Speaker
        effects = HostEffects(SceneLights(core, lock, Path(directory) / "werewolf-light-recovery.json"),
                              speak=Speaker().say, speech_status=report)
        owner = WerewolfService(effects, embodied=embodied)

    async def local_host(request: Request):
        if request.method == "POST" and request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, detail="host_origin_required")

    router = APIRouter(prefix="/api/games/werewolf", dependencies=[Depends(local_host)])
    router.game_owner = owner

    async def guarded(operation):
        try:
            return await operation
        except (GameError, ValueError, OSError, TimeoutError) as exc:
            code = str(exc) if isinstance(exc, ValueError) and len(str(exc)) < 80 else "game_unavailable"
            raise HTTPException(409, detail=code) from None

    @router.get("/context")
    async def context():
        from .embodied.audio_output import report
        addresses, lights = await asyncio.gather(asyncio.to_thread(owner.addresses), owner.effects.lights.choices())
        return dict(addresses=addresses, lights=lights, speech=report(),
                    recovery=owner.effects.lights.status(), current=owner.host(),
                    rules=list(RULES), speaker_seconds=SPEAKER_SECONDS,
                    qr_available=importlib.util.find_spec("qrcode") is not None)

    @router.get("/join-qr")
    async def join_qr(session: str):
        phone = owner.phone_url()
        if session != owner.session or phone is None:
            raise HTTPException(404)
        try:
            image = join_qr_svg(phone)
        except ImportError:
            raise HTTPException(409, detail="qr_unavailable") from None
        return Response(image, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @router.get("/host")
    async def host():
        return owner.host()

    @router.post("/create")
    async def create(payload: Create):
        return await guarded(owner.create(payload.players, address=payload.address,
                                           lights=payload.lights, narration=payload.narration, camera=payload.camera,
                                           discussion_mode=payload.discussion_mode))

    @router.post("/control")
    async def control(payload: Control):
        if payload.action == "close":
            return await guarded(owner.close_game(proof=payload))
        return await guarded(owner.control(payload.action, seat=payload.seat, proof=payload))

    @router.post("/camera-bind")
    async def bind(payload: Binding):
        return await guarded(owner.bind(payload.seat, payload.camera, payload.session, payload.track, proof=payload))

    @router.post("/camera-unbind")
    async def unbind(payload: Binding):
        return await guarded(owner.unbind(payload.seat, payload.camera, payload.session, payload.track, proof=payload))

    @router.post("/camera-view")
    async def camera_view(payload: CameraView):
        return JSONResponse(await guarded(owner.camera_view(payload.session, payload.phase, payload.round)),
                            headers={"Cache-Control": "no-store"})

    @router.post("/restore-lights")
    async def restore_lights():
        if owner.game is not None and owner.game.phase != "finished":
            raise HTTPException(409, detail="game_in_progress")
        lights = owner.effects.lights
        room, scene = owner.session, lights.session
        def current():
            # Another restoration may finish this scene while we are queued.
            # An empty, recovered scene is success only in the same room.
            return owner.session == room and (
                lights.session == scene or lights.session is None and not lights.records and not lights.recovery
            ) and (owner.game is None or owner.game.phase == 'finished')
        return await guarded(lights.restore(current=current))

    return router
