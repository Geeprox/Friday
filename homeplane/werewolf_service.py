"""One in-memory party game, private phone credentials and public TV output.

The optional LAN listener is created only by an explicit lobby creation command.
It has no host controls, household APIs, device IDs, camera frames or raw audio.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from collections import OrderedDict, deque
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .werewolf import GameError, WerewolfGame
from .werewolf_camera import observation as camera_observation, hints as camera_advice, picture as camera_picture

STATIC = Path(__file__).parent / "static"


def local_party_addresses():
    """Passive interface inventory; no discovery packets, socket connects or DNS."""
    command = ["/sbin/ifconfig"] if sys.platform == "darwin" else ["ip", "-j", "address", "show"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=2, check=True)
        if len(result.stdout) > 128_000:
            return []
        if sys.platform == "darwin":
            addresses = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)\b", result.stdout)
        else:
            addresses = [a["local"] for i in json.loads(result.stdout) for a in i.get("addr_info", []) if a.get("family") == "inet"]
        return sorted({v for v in addresses if (a := ipaddress.ip_address(v)).is_private
                       and not (a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast)})[:8]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return []


class Join(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(pattern=r"^\d{6}$")
    seat: int = Field(ge=1, le=12)
    name: str = Field(min_length=1, max_length=20)
    session: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    attempt_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def recovery_context(self):
        if self.attempt_id is not None and self.session is None:
            raise ValueError('join_session_required')
        return self


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: str = Field(pattern=r"^(attack|check|guard|save|poison|vote|shoot|pass)$")
    target: int | None = Field(default=None, ge=1, le=12)
    revision: int = Field(ge=0)
    event_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{12,64}$")


class Ready(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ready: bool


class Lobby(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(pattern=r"^\d{6}$")


class PhoneGateway:
    def __init__(self):
        self.server = self.task = self.sock = None
        self.origin = None

    async def open(self, address, owner):
        import uvicorn

        class PartyServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                # The main application owns process signals, not a game listener.
                yield

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.bind((address, 0))
            self.sock.setblocking(False)
            self.origin = f"http://{address}:{self.sock.getsockname()[1]}"
            self.server = PartyServer(uvicorn.Config(phone_app(owner, self.origin), log_level="warning",
                                                     access_log=False, lifespan="off", limit_concurrency=64,
                                                     timeout_graceful_shutdown=1, timeout_keep_alive=5))
            self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]), name="friday-party-phones")
            for _ in range(100):
                if self.server.started:
                    return self.origin
                if self.task.done():
                    await self.task
                    raise ValueError("phone_gateway_unavailable")
                await asyncio.sleep(.02)
            raise ValueError("phone_gateway_unavailable")
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.server:
            self.server.should_exit = True
        try:
            if self.task:
                try:
                    async with asyncio.timeout(3):
                        await self.task
                except TimeoutError:
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
        finally:
            if self.sock:
                self.sock.close()
            self.server = self.task = self.sock = None
            self.origin = None


class WerewolfService:
    def __init__(self, effects, *, embodied=None, gateway_factory=PhoneGateway,
                 addresses=local_party_addresses, clock=time.monotonic, game_factory=WerewolfGame):
        self.effects = effects
        self.embodied = embodied
        self.gateway_factory = gateway_factory
        self.addresses = addresses
        self.clock = clock
        self.game_factory = game_factory
        self.lock = asyncio.Lock()
        self.game = None
        self.session = None
        self.join_code = self.public_token = None
        self.credentials = {}
        self.join_retries = {}
        self.join_key = None
        self.retries = OrderedDict()
        self.host_retries = OrderedDict()
        self.gateway = self.pump = self.effect_task = None
        self.effect_tasks = set()
        self.origin = None
        self.created = None
        self.camera_enabled = False
        self.bindings = {}
        self.camera_hints = []
        self.camera_candidates = []
        self.camera_generation = 0
        self.camera_process = None
        self.camera_until = None
        self.camera_view_busy = False
        self.camera_sequence = 0
        self.camera_hold = None
        self.last_effect_revision = None

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _player(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            raise GameError("player_access_required")
        seat = self.credentials.get(self._digest(token))
        if seat is None or self.game is None or seat not in self.game.players:
            raise GameError("player_access_required")
        return seat

    def public_access(self, token):
        return bool(self.public_token and isinstance(token, str) and secrets.compare_digest(self.public_token, token))

    def public(self):
        if self.game is None or self.session is None:
            return dict(active=False)
        effects = self.effects.status()
        lights = effects.get("lights", {})
        return dict(active=True, session=self.session, game=self.game.public(),
                    camera_hints=list(self.camera_hints) if self._camera_current() and not self.game.paused
                        and self.game.phase in {"discussion", "vote", "runoff", "runoff_discussion"} else [],
                    effects=dict(narration=effects.get("narration", False), voice=effects.get("voice", "off"),
                                 physical_audio_verified=False,
                                 lights=dict(selected=lights.get("selected", 0),
                                             recovery_pending=lights.get("recovery_pending", False),
                                             physical_effect_verified=False)))

    def host(self):
        report = self.public()
        if report["active"]:
            camera_available = self._camera_current() and self.game.phase == 'lobby'
            report.update(effects=self.effects.status(), join_code=self.join_code,
                          phone_url=self.origin + "/phone#code=" + self.join_code if self.origin else None,
                          display_url=self.origin + "/display#token=" + self.public_token if self.origin else None,
                          camera_enabled=self.camera_enabled, camera_available=camera_available,
                          camera_candidates=list(self.camera_candidates) if camera_available else [],
                          camera_bindings=[dict(seat=s, camera=v[0], session=v[1], track=v[2]) for s, v in self.bindings.items()])
        return report

    async def create(self, count, *, address=None, lights=(), narration=False, camera=False):
        async with self.lock:
            if self.game:
                raise GameError("game_already_exists")
            game = self.game_factory(count, clock=self.clock)
            addresses = await asyncio.to_thread(self.addresses) if address else []
            if address is not None and address not in addresses:
                raise GameError("phone_address_unavailable")
            session = secrets.token_hex(16)
            if lights:
                await self.effects.lights.select(session, list(lights))
            gateway = self.gateway_factory() if address else None
            self.game, self.session = game, session
            self.join_retries.clear()
            self.join_key = secrets.token_bytes(32)
            self.created = self.clock()
            self.join_code = str(secrets.randbelow(900_000) + 100_000)
            self.public_token = secrets.token_hex(32)
            self.effects.narration = narration
            self.effects.lights_active = bool(lights)
            self.effects.last_announcement = 0
            self.effects.last_scene = None
            self.effects.voice_state = 'off'
            self.camera_enabled = camera
            try:
                if gateway:
                    self.origin = await gateway.open(address, self)
                self.gateway = gateway
                self.pump = asyncio.create_task(self._run(), name="friday-werewolf")
            except BaseException:
                if gateway:
                    await gateway.close()
                if lights:
                    await self.effects.lights.restore()
                self.game = None
                self.session = None
                self.join_retries.clear()
                self.join_key = None
                raise
            return self.host()

    async def join(self, payload: Join):
        async with self.lock:
            if self.game is None or not secrets.compare_digest(self.join_code or "", payload.code):
                raise GameError("room_not_found")
            if payload.session is not None and payload.session != self.session:
                raise GameError('room_not_found')
            key = fingerprint = None
            if payload.attempt_id is not None:
                if self.join_key is None:
                    raise GameError('room_not_found')
                key = self._digest(payload.attempt_id)
                fingerprint = self._digest(json.dumps((payload.session, payload.code, payload.seat, payload.name)))
                token = hmac.new(self.join_key, (self.session+payload.attempt_id).encode(), hashlib.sha256).hexdigest()
                if key in self.join_retries:
                    if self.join_retries[key] != fingerprint:
                        raise GameError('event_conflict')
                    seat = self._player(token)  # Removed players stay revoked.
                    return dict(token=token, session=self.session, player=self.game.private(seat))
                # Never evict a revoked receipt and accidentally revive its old
                # intent. Only a new room retires this bounded in-memory table.
                if len(self.join_retries) >= 128:
                    raise GameError('join_recovery_full')
            else:
                token = secrets.token_hex(32)
            self.game.join(payload.seat, payload.name)
            self.credentials[self._digest(token)] = payload.seat
            if key is not None:
                self.join_retries[key] = fingerprint
            return dict(token=token, session=self.session, player=self.game.private(payload.seat))

    async def lobby(self, code):
        async with self.lock:
            if self.game is None or not secrets.compare_digest(self.join_code or "", code):
                raise GameError("room_not_found")
            if self.game.phase != "lobby":
                raise GameError("join_closed")
            return dict(self.game.public(), session=self.session)

    async def private(self, token):
        async with self.lock:
            seat = self._player(token)
            return self.game.private(seat)

    async def ready(self, token, value):
        async with self.lock:
            seat = self._player(token)
            self.game.ready(seat, value)
            return self.game.private(seat)

    async def choose(self, token, choice: Choice):
        async with self.lock:
            seat = self._player(token)
            self.game.tick()
            key = (seat, choice.event_id)
            digest = hashlib.sha256(choice.model_dump_json().encode()).hexdigest()
            if key in self.retries:
                if self.retries[key] != digest:
                    raise GameError("event_conflict")
                return self.game.private(seat)
            self.game.act(seat, choice.action, choice.target, choice.revision)
            self.retries[key] = digest
            if len(self.retries) > 2048:
                self.retries.popitem(last=False)
            return self.game.private(seat)

    def _host_admission(self, proof, action, seat=None):
        if proof is None:
            return None, False
        camera_command = action in {"bind", "unbind"}
        if (not camera_command and proof.action != action) or proof.seat != seat:
            raise GameError("host_action_invalid")
        session = proof.game_session if camera_command else proof.session
        key = (session, proof.event_id)
        digest = hashlib.sha256((action + ':' + proof.model_dump_json()).encode()).hexdigest()
        if key in self.host_retries:
            if self.host_retries[key] != digest:
                raise GameError("event_conflict")
            return None, True
        if self.game is None or self.session != session:
            raise GameError("room_changed")
        if self.game.revision != proof.revision:
            raise GameError("phase_changed")
        return (key, digest), False

    def _remember_host(self, admission):
        if admission is not None:
            key, digest = admission
            self.host_retries[key] = digest
            while len(self.host_retries) > 256:
                self.host_retries.popitem(last=False)

    async def control(self, action, *, seat=None, proof=None):
        async with self.lock:
            admission, replay = self._host_admission(proof, action, seat)
            if replay:
                return self.host()
            if self.game is None:
                raise GameError("game_not_found")
            if action == "start":
                self.game.start()
            elif action == "advance":
                self.game.advance()
            elif action in {"pause", "resume"}:
                self.game.pause(action == "pause")
            elif action == "end":
                self.game.end()
            elif action == "remove":
                self.game.remove(seat)
                self.credentials = {key: s for key, s in self.credentials.items() if s != seat}
                self.bindings.pop(seat, None)
            else:
                raise GameError("host_action_invalid")
            self._remember_host(admission)
            result = self.host()
        self._schedule_effects()
        if self.game is not None and self.game.phase=='finished':
            await self._release_camera()
        return result

    def _schedule_effects(self):
        if self.game is None or self.game.revision == self.last_effect_revision:
            return
        # 撤销旧阶段。播报与灯光各自等待同一路旧任务完成清理。
        for task in tuple(self.effect_tasks):
            if not task.done() and not task.cancelling():
                task.cancel()
        session, revision = self.session, self.game.revision
        self.last_effect_revision = revision
        public = self.game.public()
        current = lambda: self.session == session and self.game is not None and self.game.revision == revision
        async def apply():
            if current():
                try:
                    await self.effects.apply(public, current)
                except (ValueError, OSError, TimeoutError):
                    if current():
                        self.effects.voice_state = 'unavailable'
        self.effect_task = asyncio.create_task(apply(), name="friday-werewolf-effects")
        self.effect_tasks.add(self.effect_task)
        def finished(task):
            self.effect_tasks.discard(task)
            if not task.cancelled():
                task.exception()
        self.effect_task.add_done_callback(finished)

    async def step(self):
        async with self.lock:
            if self.game is None:
                return
            if self.clock() - self.created > 6 * 3600 and self.game.phase != "finished":
                self.game.end()
            else:
                self.game.tick()
        self._schedule_effects()
        if self.camera_enabled and self.game is not None and self.game.phase!='finished':
            await self._camera()
        else:
            self._clear_camera()
            await self._release_camera()

    async def _run(self):
        while self.game is not None:
            if self.clock() - self.created > 6 * 3600:
                asyncio.create_task(self.close_game(), name="friday-werewolf-expire")
                return
            await self.step()
            await asyncio.sleep(.5)

    def _clear_camera(self):
        self.camera_hints = []
        self.camera_candidates = []
        self.camera_process = None
        self.camera_until = None

    async def _hold_camera(self,report,game,session,process):
        capability=report.get('party_camera')if isinstance(report,dict)else None
        owner=self.embodied
        if (not isinstance(capability,dict)or type(capability.get('protocol'))is not int or capability['protocol']!=1
                or not isinstance(capability.get('scope'),str)
                or not re.fullmatch(r'[a-f0-9]{32}',capability['scope'])
                or type(capability.get('frame_mono'))not in{int,float}
                or self.game is not game or self.session!=session or not self.camera_enabled
                or game.phase=='finished'or getattr(owner,'process',None)is not process
                or not callable(getattr(owner,'party_camera',None))):
            return False
        self.camera_sequence+=1
        sequence=self.camera_sequence
        # Record before sending: a lost acknowledgement still owes this room an end.
        self.camera_hold=(owner,process,session,capability['scope'])
        result=await owner.party_camera(dict(action='renew',context=session,
            sequence=sequence,expected_scope=capability['scope'],not_after=self.clock()+1,
            camera=report['camera_id'],session=report['session_id'],frame_mono=capability['frame_mono']))
        return (isinstance(result,dict)and result.get('scope')==capability['scope']
                and result.get('context')==session and type(result.get('sequence'))is int and result['sequence']==sequence
                and result.get('active')is True)

    async def _release_camera(self):
        entry,self.camera_hold=self.camera_hold,None
        if entry is None:return
        owner,process,context,scope=entry
        self.camera_sequence+=1
        if getattr(owner,'process',None)is not process or process.returncode is not None:return
        try:
            async with asyncio.timeout(1):
                await owner.party_camera(dict(action='end',context=context,
                    sequence=self.camera_sequence,expected_scope=scope,not_after=self.clock()+1))
        except(ValueError,OSError,TimeoutError,httpx.HTTPError):
            # The worker's three-second lease bounds an unconfirmed release.
            pass

    def _camera_current(self):
        process = getattr(self.embodied, 'process', None)
        return (self.camera_enabled and self.game is not None and self.session is not None
                and process is not None and process is self.camera_process and process.returncode is None
                and self.camera_until is not None and self.clock() < self.camera_until)

    async def _camera(self):
        self.camera_generation += 1
        generation = self.camera_generation
        self._clear_camera()
        if self.embodied is None or not self.game or not self.camera_enabled or self.game.phase=='finished':
            return
        owner, game, session = self.embodied, self.game, self.session
        process = getattr(owner, "process", None)
        if process is None or process.returncode is not None:
            return
        began = self.clock()
        try:
            async with asyncio.timeout(1):
                report = await owner.request("/api/status", timeout=1)
        except (ValueError, OSError, TimeoutError, httpx.HTTPError):
            return
        if (generation != self.camera_generation or self.game is not game or self.session != session
                or not self.camera_enabled or self.embodied is not owner
                or getattr(owner, 'process', None) is not process or process.returncode is not None):
            return
        sample = camera_observation(report)
        if sample is None:
            return
        try:
            async with asyncio.timeout(1):
                held=await self._hold_camera(report,game,session,process)
        except(ValueError,OSError,TimeoutError,httpx.HTTPError):
            return
        if (not held or generation!=self.camera_generation or self.game is not game or self.session!=session
                or not self.camera_enabled or self.embodied is not owner
                or getattr(owner,'process',None)is not process or process.returncode is not None):
            return
        now = self.clock()
        elapsed = now - began
        remaining = 1 - sample['age_ms'] / 1000 - elapsed
        if not 0 <= elapsed < 1 or remaining <= 0:
            return
        self.camera_candidates = [dict(camera=sample['camera'], session=sample['session'], track=track)
                                  for track in sample['people']]
        self.camera_hints = camera_advice(sample, self.bindings, game.players)
        self.camera_until, self.camera_process = now + remaining, process

    async def camera_view(self, game_session):
        if game_session != self.session or self.game is None:
            raise GameError('room_changed')
        if not self.camera_enabled or self.game.phase != 'lobby':
            raise GameError('camera_binding_unavailable')
        owner, game, session = self.embodied, self.game, self.session
        process = getattr(owner, 'process', None)
        if self.camera_view_busy or process is None or process.returncode is not None:
            return dict(available=False)
        began = self.clock()
        self.camera_view_busy = True
        try:
            async with asyncio.timeout(1):
                report = await owner.party_frame()
            if (self.game is not game or self.session != session or not self.camera_enabled
                    or game.phase != 'lobby' or self.embodied is not owner
                    or getattr(owner, 'process', None) is not process or process.returncode is not None):
                return dict(available=False)
            sample = camera_picture(report)
            elapsed = self.clock() - began
            if sample is None or not 0 <= elapsed < 1:
                return dict(available=False)
            async with asyncio.timeout(1):
                held=await self._hold_camera(report,game,session,process)
            elapsed=self.clock()-began
            if (not held or self.game is not game or self.session!=session or not self.camera_enabled
                    or game.phase!='lobby'or self.embodied is not owner
                    or getattr(owner,'process',None)is not process or process.returncode is not None
                    or not 0<=elapsed<1):
                return dict(available=False)
            remaining = 1 - sample.pop('age_ms') / 1000 - elapsed
            if remaining <= 0:
                return dict(available=False)
            return dict(available=True, ttl_ms=int(remaining * 1000), **sample)
        except (ValueError, OSError, TimeoutError, httpx.HTTPError):
            return dict(available=False)
        finally:
            self.camera_view_busy = False

    async def bind(self, seat, camera, session, track, *, proof=None):
        async with self.lock:
            admission, replay = self._host_admission(proof, "bind", seat)
            if replay:
                return self.host()
            if self.game is None or self.game.phase != "lobby" or seat not in self.game.players or not self.camera_enabled:
                raise GameError("camera_binding_unavailable")
            await self._camera()
            value = dict(camera=camera, session=session, track=track)
            if value not in self.camera_candidates or any(v == (camera, session, track) for s, v in self.bindings.items() if s != seat):
                raise GameError("camera_track_unavailable")
            if self.bindings.get(seat) != (camera, session, track):
                self.bindings[seat] = (camera, session, track)
                self.game.revision += 1
            self._remember_host(admission)
            return self.host()

    async def unbind(self, seat, camera, session, track, *, proof=None):
        async with self.lock:
            admission, replay = self._host_admission(proof, 'unbind', seat)
            if replay:
                return self.host()
            if self.game is None or self.game.phase != 'lobby' or seat not in self.game.players:
                raise GameError('camera_binding_unavailable')
            if self.bindings.get(seat) != (camera, session, track):
                raise GameError('camera_binding_changed')
            del self.bindings[seat]
            self.game.revision += 1
            self._remember_host(admission)
            return self.host()

    async def close_game(self, *, proof=None):
        async with self.lock:
            admission, replay = self._host_admission(proof, "close")
            if replay:
                return self.host()
            # Keep a bounded tombstone across rooms, including an interrupted
            # close. Retrying this exact command must never close a new room.
            self._remember_host(admission)
            # Revoke both phone/public access before waiting for any output.
            self.session = self.join_code = self.public_token = None
            self.credentials.clear()
            self.join_retries.clear()
            self.join_key = None
            self.camera_generation += 1
            self._clear_camera()
            self.camera_enabled = False
            try:
                await self._release_camera()
                if self.pump:
                    self.pump.cancel()
                    await asyncio.gather(self.pump, return_exceptions=True)
                    self.pump = None
                for task in tuple(self.effect_tasks):
                    if not task.done() and not task.cancelling():
                        task.cancel()
                # 灯光恢复由 SceneLights 串行，扬声器清理在下方单独回收。
                if getattr(self.effects, "lights_active", False):
                    try:
                        await self.effects.lights.restore()
                    except (ValueError, OSError, TimeoutError):
                        self.effects.lights.recovery = True
            finally:
                try:
                    tasks=set(self.effect_tasks)|{task for task in (self.pump,self.effect_task) if task is not None}
                    for task in tasks:
                        if not task.done() and not task.cancelling():task.cancel()
                    await asyncio.shield(asyncio.gather(*tasks,return_exceptions=True))
                    if self.gateway:
                        await self.gateway.close()
                finally:
                    self.game = self.gateway = None
                    self.pump = self.effect_task = None
                    self.origin = None
                    self.effects.lights_active = False
                    self.effects.narration = False
                    self.retries.clear()
                    self.bindings.clear()
                    self.camera_hints = []
                    self.camera_candidates = []
                    self.last_effect_revision = None
            return self.host()

    async def close(self):
        await self.close_game()


def phone_app(owner, origin):
    """LAN surface. Deliberately separate from the host/household ASGI app."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    attempts = OrderedDict()

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.headers.get("host") != origin.split("://", 1)[1] or request.headers.get("origin", origin) != origin:
            return JSONResponse({"detail": "party_origin_required"}, status_code=403)
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    return JSONResponse({"detail": "request_too_large"}, status_code=413)
            request._body = bytes(body)
        if request.url.path.startswith("/api/"):
            identity = ('address', request.client.host if request.client else '')
            supplied = request.headers.get('authorization', '')
            supplied = supplied[7:] if supplied.startswith('Bearer ') else ''
            if request.url.path == '/api/join':
                try:
                    join = Join.model_validate_json(request._body)
                    if (owner.session is not None and join.session == owner.session
                            and secrets.compare_digest(join.code, owner.join_code or '')):
                        # The verified current lobby shares one bounded budget.
                        # Random attempt IDs cannot manufacture rate buckets.
                        identity = ('room', owner.session)
                except (ValueError, AttributeError):
                    pass
            # Authenticated players sharing a hotspot/proxy must not consume
            # one another's ordinary polling budget. Invalid/random tokens
            # retain the address budget; they cannot mint unlimited buckets.
            if request.url.path in {'/api/player', '/api/ready', '/api/choice'}:
                try:
                    owner._player(supplied)
                except GameError:
                    pass
                else:
                    identity = ('player', owner._digest(supplied))
            key = (identity, request.url.path)
            now = owner.clock()
            events = attempts.setdefault(key, deque())
            while events and now - events[0] > 60:
                events.popleft()
            limit = (30 if identity[0] == 'room' else 10) if request.url.path == "/api/join" else 150
            if len(events) >= limit:
                retry = min(60, max(1, int(61 - (now - events[0]))))
                return JSONResponse({"detail": "party_rate_limited"}, status_code=429,
                                    headers={'Retry-After':str(retry), 'Cache-Control':'no-store'})
            events.append(now)
            attempts.move_to_end(key)
            if len(attempts) > 512:
                attempts.popitem(last=False)
        try:
            response = await call_next(request)
        except GameError as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=403 if str(exc).endswith("access_required") else 409)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"})
        return response

    def token(request):
        value = request.headers.get("authorization", "")
        return value[7:] if value.startswith("Bearer ") else ""

    @app.get("/phone")
    @app.get("/display")
    async def page():
        return FileResponse(STATIC / "werewolf-party.html")

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name == 'friday-icon.png':
            return FileResponse(STATIC / 'brand/friday.png')
        if name == 'werewolf-scene.png':
            return FileResponse(STATIC / 'games-art/werewolf.png')
        if name not in {"werewolf-party.js", "werewolf-party.css"}:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/api/public")
    async def public(request: Request):
        if not owner.public_access(token(request)):
            raise HTTPException(403, detail="public_access_required")
        return owner.public()

    @app.post("/api/join")
    async def join(payload: Join):
        return await owner.join(payload)

    @app.post("/api/lobby")
    async def lobby(payload: Lobby):
        return await owner.lobby(payload.code)

    @app.get("/api/player")
    async def player(request: Request):
        return await owner.private(token(request))

    @app.post("/api/ready")
    async def ready(payload: Ready, request: Request):
        return await owner.ready(token(request), payload.ready)

    @app.post("/api/choice")
    async def choice(payload: Choice, request: Request):
        return await owner.choose(token(request), payload)

    return app
