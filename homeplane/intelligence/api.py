"""本机同源设置与主动语音；不新增任何设备动作入口。"""

from __future__ import annotations

import asyncio
import ipaddress
import sqlite3
import time
from dataclasses import asdict
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.websockets import WebSocketState

from homeplane.decision import DecisionRequest

from .backgrounds import SongBackgrounds
from .budget import EnhancementUnavailable
from .decisions import OpenAIDecisionProvider
from .voice import RealtimeVoice


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: Literal["addressed_to_home", "intent", "needs_grounding", "response_policy"]
    text: str = Field(min_length=1, max_length=1024)


def create_router(runtime, *, origin="http://127.0.0.1:8740", backgrounds=None, voice=None):
    router = APIRouter(prefix="/api/settings/intelligence")
    router.backgrounds = backgrounds or SongBackgrounds(runtime)
    voice = voice or RealtimeVoice(runtime)

    @router.get("")
    async def status():
        try:
            return await asyncio.to_thread(runtime.status)
        except (OSError, ValueError, EnhancementUnavailable, sqlite3.Error):
            raise HTTPException(503, detail="intelligence_settings_unavailable") from None

    @router.put("")
    async def configure(patch: dict, request: Request):
        try:
            await asyncio.to_thread(runtime.settings.update, patch)
        except (OSError, ValueError):
            raise HTTPException(422, detail="invalid_intelligence_configuration") from None
        return await status()

    @router.post("/credential")
    async def save_credential(value: Credential):
        try:
            await asyncio.to_thread(runtime.settings.save_key, value.api_key.get_secret_value())
            await asyncio.to_thread(runtime.ledger.clear_blocks)
        except (OSError, ValueError):
            raise HTTPException(503, detail="intelligence_credential_unavailable") from None
        return await status()

    @router.delete("/credential")
    async def remove_credential():
        await asyncio.to_thread(runtime.settings.remove_key)
        return await status()

    @router.post("/retry")
    async def retry():
        await asyncio.to_thread(runtime.ledger.clear_blocks)
        return await status()

    @router.post("/decide")
    async def decide(value: DecisionInput):
        result = await OpenAIDecisionProvider(runtime).decide(
            DecisionRequest(value.task, {"text": value.text}, time.monotonic() + 5)
        )
        return asdict(result)

    @router.get("/backgrounds/{identifier}")
    async def background_image(identifier: str):
        if not router.backgrounds.cached(identifier):
            raise HTTPException(404, detail="background_unavailable")
        return FileResponse(router.backgrounds.directory / (identifier + ".png"), media_type="image/png")

    @router.websocket("/voice")
    async def voice_input(socket: WebSocket):
        try:
            local = ipaddress.ip_address(socket.client.host).is_loopback
        except (ValueError, AttributeError):
            local = False
        if (
            not local
            or socket.headers.get("origin") != origin
            or socket.headers.get("host") != urlsplit(origin).netloc
        ):
            await socket.close(code=1008)
            return
        await socket.accept()

        async def source():
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect()
                if message.get("bytes") is not None:
                    if len(message["bytes"]) > 65536:
                        raise EnhancementUnavailable("voice_input_invalid")
                    yield message["bytes"]
                elif message.get("text") == '{"event":"finish"}':
                    return
                else:
                    raise EnhancementUnavailable("voice_input_invalid")

        try:
            await voice.transcribe(source(), socket.send_json)
        except WebSocketDisconnect:
            pass
        except (EnhancementUnavailable, OSError, ValueError, TimeoutError, sqlite3.Error) as exc:
            if socket.client_state == WebSocketState.CONNECTED:
                await socket.send_json(
                    {
                        "event": "unavailable",
                        "code": exc.code if isinstance(exc, EnhancementUnavailable) else "voice_unavailable",
                    }
                )
        finally:
            if socket.client_state == WebSocketState.CONNECTED:
                await socket.close()

    return router
