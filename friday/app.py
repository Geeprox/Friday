import asyncio
import base64
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlparse
from uuid import UUID, uuid4

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adapters import DemoAdapter, HomeAssistantAdapter, MilocoAdapter
from .camera import MilocoCapture
from .config import CONFIG_PATH, Anchor, load_settings
from .core import Controller, Evidence, Observation
from .home import Credentials, HAHome
from .journal import Journal
from .models import Perception
from .orchestrator import Orchestrator
from .providers import ARLPlanner, ARLRouteDecision, runtime_for

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app):
    s = load_settings()
    app.state.settings = s
    app.state.journal = Journal(
        Path("data/diagnostics-live.sqlite" if s.mode == "live" else "data/diagnostics.sqlite")
    )
    app.state.token = secrets.token_urlsafe(32)
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        adapter = (
            DemoAdapter(s.devices)
            if s.mode == "demo"
            else (MilocoAdapter(s, client) if s.backend == "miloco" else HomeAssistantAdapter(s, client))
        )
        app.state.controller = Controller(s, adapter, app.state.journal, allow_simulated_admission=s.mode == 'demo')
        app.state.arl = runtime_for(s)
        app.state.perception = Perception(s, runtime=app.state.arl)
        app.state.inference_lock = asyncio.Lock()
        app.state.credentials = Credentials(s, client)
        app.state.home = HAHome(s, client, app.state.credentials)
        planner = ARLPlanner(s, app.state.arl)
        app.state.orchestrator = Orchestrator(
            s, app.state.home, planner, ARLRouteDecision(s, app.state.arl), app.state.controller, app.state.journal
        )
        app.state.oauth_states = {}
        app.state.client = client

        async def media(event_id, session, captured_at, wav, frames):
            return await process_media(app.state, event_id, session, captured_at, wav, frames)

        def failed():
            app.state.controller.stop()
            app.state.orchestrator.cancel()

        app.state.capture = MilocoCapture(s, media, failed, app.state.journal)
        app.state.journal.record("lifecycle", "boot")

        async def sync_initial():
            if s.mode == "live" and app.state.credentials.connected():
                try:
                    await app.state.home.discover()
                except Exception as exc:
                    app.state.home.error = type(exc).__name__

        sync_task = asyncio.create_task(sync_initial())
        yield
        sync_task.cancel()
        await asyncio.gather(sync_task, return_exceptions=True)
        app.state.controller.stop()
        await app.state.capture.stop()
        app.state.journal.close()


app = FastAPI(title="Friday", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]"])


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        return Response("Same-origin requests only", 403)
    if request.method not in ("GET", "HEAD"):
        if not secrets.compare_digest(request.headers.get("x-friday-token", ""), request.app.state.token):
            return Response("Missing session token", 403)
        try:
            if int(request.headers.get("content-length", "0")) > 3_000_000:
                return Response("Payload too large", 413)
        except ValueError:
            return Response("Invalid content length", 400)
        # Bound chunked bodies too; no middleware logging of body content.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 3_000_000:
                return Response("Payload too large", 413)
        request._body = bytes(body)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
    )
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status(request: Request):
    st = request.app.state
    s, c = st.settings, st.controller
    return {
        "mode": s.mode,
        "wake_mode": s.wake_mode,
        "running": c.running,
        "token": st.token,
        "camera_id": s.camera_id,
        "room": s.room,
        "camera_configured": bool(s.camera_did),
        "backend": s.backend,
        "capture": {"status": st.capture.status, "error": st.capture.error, "level": st.capture.level},
        "devices": [
            {"id": d.id, "name": d.name, "room": d.room, "anchors": [a.model_dump() for a in d.anchors]}
            for d in s.devices
        ],
        "events": st.journal.events(),
    }


@app.get("/api/devices")
async def devices(request: Request):
    st = request.app.state

    async def read(d):
        try:
            if st.settings.mode == "live" and st.settings.whole_home and d.entity_id:
                if not st.credentials.connected():
                    return {"id": d.id, "not_connected": True}
                value = await st.home.read(d.entity_id)
                return {
                    "id": d.id,
                    "on": value["state"] == "on",
                    "brightness": round(value["attributes"]["brightness"] / 255 * 100)
                    if value["attributes"].get("brightness") is not None
                    else None,
                    "unavailable": value["state"] in ("unknown", "unavailable"),
                }
            return {"id": d.id, **await st.controller.adapter.read(d)}
        except Exception:
            return {"id": d.id, "unavailable": True}

    return await asyncio.gather(*(read(d) for d in st.settings.devices))


class ControlRequest(BaseModel):
    running: bool
    source: Literal["server", "miloco", "browser", "text", "demo"] | None = None
    wake_mode: Literal["optional", "required"] | None = None


@app.post("/api/control")
async def control(body: ControlRequest, request: Request):
    st = request.app.state
    if body.wake_mode:
        st.settings.wake_mode = body.wake_mode
    if body.running:
        if st.controller.running:
            raise HTTPException(409, "请先暂停当前控制会话")
        st.controller.start()
        if body.source == "server":
            try:
                await st.capture.start()
            except Exception:
                st.controller.stop()
                raise HTTPException(503, "摄像头服务未就绪，请检查 Miloco 登录和运行依赖") from None
    else:
        st.controller.stop()
        st.orchestrator.cancel()
        await st.capture.stop()
    return {"running": st.controller.running, "generation": st.controller.generation}


class DemoRequest(BaseModel):
    scenario: Literal["named", "point", "dim", "tv", "ambiguous", "friday"]


@app.post("/api/demo")
async def demo(body: DemoRequest, request: Request):
    st = request.app.state
    if st.settings.mode != "demo":
        raise HTTPException(403, "Demo disabled in live mode")
    scenarios = {
        "named": (
            "打开客厅灯",
            Evidence(live_speaker=True, people_count=1, facing_camera=True, attention=0.98),
        ),
        "point": (
            "把这个打开",
            Evidence(
                live_speaker=True,
                people_count=1,
                pointing=True,
                attention=0.98,
                target_id="floor-lamp",
                target_confidence=0.98,
            ),
        ),
        "dim": (
            "这个暗一点",
            Evidence(live_speaker=True, people_count=1, facing_camera=True, attention=0.98),
        ),
        "tv": ("打开客厅灯", Evidence(live_speaker=False, people_count=0, attention=0.1)),
        "ambiguous": (
            "灯打开",
            Evidence(live_speaker=True, people_count=1, facing_camera=True, attention=0.98),
        ),
        "friday": ("Friday，打开客厅灯", Evidence(live_speaker=True, people_count=1, attention=0.98)),
    }
    text, ev = scenarios[body.scenario]
    from homeplane.interaction_contracts import AttentionAdmission, InteractionBinding

    now = time.time()
    binding = InteractionBinding(input_kind='ambient', session_id='demo', room_id=st.settings.room,
        camera_id=st.settings.camera_id, map_revision='simulated-demo', source_revision='simulated-demo',
        authority_revision='simulated-no-real-authority', boot_id='simulated-demo',
        generation=st.controller.generation, subject_ref='simulated-viewer', track_ref='simulated-track',
        track_epoch='simulated-epoch', associated=True, speaker_count=ev.people_count)
    admission = AttentionAdmission(producer_ref='simulated-demo', source_revision=binding.source_revision,
        speaker_ref=binding.subject_ref, track_ref=binding.track_ref, track_epoch=binding.track_epoch,
        room_id=binding.room_id, camera_id=binding.camera_id, captured_at=now, expires_at=now+30,
        source_kind='fixture', final_transcript=True, addressed_to_system=ev.live_speaker,
        quality_ok=ev.live_speaker, gaze='unavailable', pointing_admitted=ev.pointing)
    obs = Observation(str(uuid4()), text, ev, now, "demo", st.settings.camera_id,
                      interaction=binding, admission=admission)
    return await st.controller.process(obs, st.controller.generation)


class MediaRequest(BaseModel):
    event_id: UUID
    session: UUID
    captured_at: float
    audio: str = Field(max_length=1_100_000)
    frames: list[str] = Field(min_length=1, max_length=5)


async def process_media(st, event_id, session, captured_at, wav, frames):
    if st.settings.mode != "live" or not st.controller.running:
        raise HTTPException(409, "Live listening is not enabled")
    if st.inference_lock.locked():
        raise HTTPException(429, "Still processing the previous utterance")
    generation = st.controller.generation
    if not -2 <= time.time() - captured_at <= 8:
        raise HTTPException(422, "Capture is stale")
    async with st.inference_lock:
        try:
            started = time.monotonic()
            text = await st.perception.transcribe(wav)
            if not text.strip():
                return {"status": "ignored", "reason": "没有识别到语音"}
            evidence = await st.perception.observe(text, frames)
            if st.settings.whole_home:
                result = await st.orchestrator.handle(
                    text,
                    event_id,
                    generation,
                    camera_id=st.settings.camera_id,
                    session=session,
                    evidence=evidence,
                    captured_at=captured_at,
                )
            else:
                obs = Observation(event_id, text, evidence, captured_at, session, st.settings.camera_id)
                result = await st.controller.process(obs, generation)
            st.journal.record(
                "pipeline", "completed", event_id, latency_ms=round((time.monotonic() - started) * 1000)
            )
            return result
        except asyncio.CancelledError:
            st.journal.record("perception", "cancelled", event_id)
            raise
        except Exception as exc:
            st.journal.record("perception", "failure", event_id, error_type=type(exc).__name__)
            raise HTTPException(502, "感知失败，请检查本地语音与视觉服务") from None


@app.post("/api/perceive")
async def perceive(body: MediaRequest, request: Request):
    try:
        wav = base64.b64decode(body.audio, validate=True)
    except ValueError:
        raise HTTPException(422, "Invalid audio encoding") from None
    return await process_media(
        request.app.state, str(body.event_id), str(body.session), body.captured_at, wav, body.frames
    )


@app.get("/api/camera/frame")
async def camera_frame(request: Request):
    capture = request.app.state.capture
    if capture.preview is None:
        raise HTTPException(404, "No live frame")
    return Response(capture.preview, media_type="image/jpeg")


class BindRequest(BaseModel):
    device_id: str
    box: tuple[float, float, float, float]


@app.post("/api/anchors")
async def bind(body: BindRequest, request: Request):
    st = request.app.state
    if st.controller.running or st.inference_lock.locked():
        raise HTTPException(409, "Pause listening before changing bindings")
    device = next((d for d in st.settings.devices if d.id == body.device_id), None)
    if not device:
        raise HTTPException(404, "Unknown device")
    try:
        anchor = Anchor(camera_id=st.settings.camera_id, box=body.box)
    except ValueError:
        raise HTTPException(422, "Draw a valid rectangle") from None
    # Persist atomically before exposing a new binding to inference.
    updated = st.settings.model_copy(deep=True)
    target = next(d for d in updated.devices if d.id == device.id)
    target.anchors = [a for a in target.anchors if a.camera_id != anchor.camera_id] + [anchor]
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(updated.model_dump_json(indent=2))
    temp.chmod(0o600)
    temp.replace(CONFIG_PATH)
    device.anchors = target.anchors
    st.controller.focus = None
    st.journal.record("binding", "saved", device_id=device.id)
    return {"saved": True}


@app.get("/api/diagnostics")
async def diagnostics(request: Request):
    rows = request.app.state.journal.events(1000)
    return Response(
        "\n".join(json.dumps(row) for row in reversed(rows)),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="friday-diagnostics.jsonl"'},
    )


@app.websocket("/api/camera/{kind}")
async def camera_socket(socket: WebSocket, kind: str):
    st = socket.app.state
    origin = socket.headers.get("origin", "")
    if (
        urlparse(origin).netloc != socket.headers.get("host")
        or kind not in ("video", "audio")
        or st.settings.mode != "live"
        or not st.settings.camera_did
        or not st.controller.running
    ):
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        # Token in initial message, never query strings/access logs.
        auth = await asyncio.wait_for(socket.receive_text(), 3)
        if not secrets.compare_digest(auth, st.token):
            await socket.close(code=1008)
            return
        base = st.settings.miloco_url.rstrip("/")
        base = ("wss" if base.startswith("https:") else "ws") + base[base.index(":") :]
        query = urlencode({"camera_id": st.settings.camera_did, "channel": 0})
        token = os.environ.get(st.settings.miloco_token_env)
        if not token:
            raise ValueError("Miloco token missing")
        url = f"{base}/api/miot/ws/{kind}_stream?{query}"
        generation = st.controller.generation
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=4_000_000,
            open_timeout=8,
            proxy=None,
        ) as upstream:
            st.journal.record("camera", f"{kind}_connected")

            async def relay():
                async for message in upstream:
                    if not st.controller.running or st.controller.generation != generation:
                        break
                    if isinstance(message, bytes):
                        await socket.send_bytes(message)
                    else:
                        # Only forward codec metadata and generic errors, never upstream free text.
                        data = json.loads(message)
                        if data.get("type") == "init":
                            await socket.send_json(
                                {
                                    k: v
                                    for k, v in data.items()
                                    if k in ("type", "codec", "container", "sampleRate", "numberOfChannels")
                                }
                            )
                        elif data.get("type") == "error":
                            raise ValueError("Camera stream error")

            async def disconnect():
                while st.controller.running and st.controller.generation == generation:
                    try:
                        await asyncio.wait_for(socket.receive_text(), 0.5)
                    except asyncio.TimeoutError:
                        pass

            tasks = [asyncio.create_task(relay()), asyncio.create_task(disconnect())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as exc:
        st.journal.record("camera", "failure", error_type=type(exc).__name__)
        try:
            await socket.send_json(
                {
                    "type": "error",
                    "message": "Camera connection failed. Check Miloco token, camera and network.",
                }
            )
        except Exception:
            pass
    finally:
        st.journal.record("camera", f"{kind}_disconnected")
        try:
            await socket.close()
        except Exception:
            pass


class TextCommand(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    event_id: UUID


@app.post("/api/commands")
async def command(body: TextCommand, request: Request):
    st = request.app.state
    if st.settings.mode != "live" or not st.controller.running:
        raise HTTPException(409, "请先开启真实控制")
    return await st.orchestrator.handle(body.text, str(body.event_id), st.controller.generation)


@app.post("/api/plans/{plan_id}/confirm")
async def confirm(plan_id: str, request: Request):
    try:
        return await request.app.state.orchestrator.confirm(plan_id)
    except ValueError:
        raise HTTPException(409, "计划已失效，请重新发出指令") from None


@app.post("/api/home/sync")
async def sync_home(request: Request):
    st = request.app.state
    try:
        entities = await st.home.discover()
        st.journal.record("home", "synced")
        return {"entities": [e.model_dump() for e in entities]}
    except Exception as exc:
        st.home.error = type(exc).__name__
        raise HTTPException(502, "HA 同步失败，请先连接账号并检查网络") from None


@app.get("/api/home")
async def home(request: Request):
    st = request.app.state
    return {
        "connected": st.credentials.connected(),
        "last_sync": st.home.last_sync,
        "error": st.home.error,
        "entities": [e.model_dump() for e in st.home.entities.values()],
        "latest": st.orchestrator.latest,
        "planner_provider": "arl:" + st.settings.arl_planner_provider,
        "planner_model": st.settings.codex_model if st.settings.arl_planner_provider == "codex_app_server" else st.settings.planner_model,
    }


@app.post("/api/auth/ha/start")
async def auth_start(request: Request):
    st = request.app.state
    base = str(request.base_url).rstrip("/")
    nonce = secrets.token_urlsafe(32)
    st.oauth_states = {nonce: (time.time() + 1800, base)}
    query = urlencode({"client_id": base, "redirect_uri": base + "/auth/ha/callback", "state": nonce})
    return {"url": st.settings.ha_url.rstrip("/") + "/auth/authorize?" + query}


@app.get("/auth/ha/callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    st = request.app.state
    pending = st.oauth_states.pop(state, None)
    if not pending or pending[0] < time.time() or not code:
        raise HTTPException(400, "登录会话已过期，请从 Friday 重新连接")
    try:
        await st.credentials.exchange(code, pending[1])
        await st.home.discover()
        st.journal.record("home", "connected")
        return RedirectResponse("/?connected=ha", status_code=303)
    except Exception as exc:
        st.journal.record("home", "connection_failed", error_type=type(exc).__name__)
        return RedirectResponse("/?connection=failed", status_code=303)


@app.get("/api/system")
async def system_status(request: Request):
    st = request.app.state
    runtime = await st.arl.status()
    return {
        "runtime": runtime,
        "ha_connected": st.credentials.connected(),
        "miloco_url": st.settings.miloco_url,
        "camera_id": st.settings.camera_id,
        "camera_configured": bool(st.settings.camera_did),
        "fixed_view": st.settings.camera_fixed_view,
        "planner": "arl:" + st.settings.arl_planner_provider,
        "mode": st.settings.mode,
    }


app.mount("/static", StaticFiles(directory=STATIC), name="static")
