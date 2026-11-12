"""One in-memory party game, private phone credentials and public TV output.

The optional LAN listener is created only by an explicit lobby creation command.
It has no host controls, household APIs, device IDs, camera frames or raw audio.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import json
import math
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
from .werewolf_effects import lighting_scene

STATIC = Path(__file__).parent / "static"
CHOICE_EVENT_ID = r"^[a-zA-Z0-9_-]{12,64}$"
NARRATION_RECHECK_SECONDS = 2
SEAT_RECOVERY_SECONDS = 120


def join_qr_svg(url):
    """主持端与电视使用同一手机入口生成二维码。"""
    import qrcode
    import qrcode.image.svg

    return qrcode.make(url, image_factory=qrcode.image.svg.SvgPathFillImage).to_string()


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
    event_id: str = Field(pattern=CHOICE_EVENT_ID)
    expected_choice_version: int | None = Field(default=None, ge=0)


class Ready(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ready: bool
    lobby_revision: int | None = Field(default=None, ge=0)


class ConfirmIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)


class FinishSpeaking(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)


class Lobby(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(pattern=r"^\d{6}$")


class SeatClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seat: int = Field(ge=1, le=12)
    ticket: str = Field(pattern=r"^[a-zA-Z0-9_-]{43}$", repr=False)
    attempt_id: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)


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


@dataclass
class _EffectRun:
    scope: tuple[str, WerewolfGame, str | int]
    task: asyncio.Task | None = None
    retry_at: float | None = None

    def ready_to_retry(self, now):
        return self.retry_at is not None and now >= self.retry_at and self.task is not None and self.task.done()


@dataclass
class _SeatRecovery:
    event_id: str
    ticket: str = field(repr=False)
    expires_at: float
    attempt_digest: str | None = None


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
        self.seat_recoveries: dict[int, _SeatRecovery] = {}
        self.retries = OrderedDict()
        self.choice_versions = {}
        self.light_bindings = {}
        self.host_retries = OrderedDict()
        self.gateway = self.pump = self.effect_task = None
        self.effect_tasks = set()
        self.effect_runs: dict[str, _EffectRun] = {}
        self.origin = None
        self.created = None
        self.camera_enabled = False
        self.camera_context = None
        self.bindings = {}
        self.camera_binding_revision = 0
        self.camera_read_lock = asyncio.Lock()
        self.camera_task = None
        self.camera_hints = []
        self.camera_candidates = []
        self.camera_generation = 0
        self.camera_process = None
        self.camera_source = None
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

    def _private(self, seat, receipt_id=None):
        result = dict(self.game.private(seat), session=self.session,
                      choice_version=self.choice_versions.get(seat, 0))
        if receipt_id and re.fullmatch(CHOICE_EVENT_ID, receipt_id):
            result['choice_receipt'] = dict(event_id=receipt_id, recorded=(seat, receipt_id) in self.retries)
        return result

    def public_access(self, token):
        return bool(self.public_token and isinstance(token, str) and re.fullmatch(r'[a-f0-9]{64}', token)
                    and secrets.compare_digest(self.public_token, token))

    def phone_url(self):
        return self.origin + '/phone#code=' + self.join_code if self.origin and self.join_code else None

    def invitation_url(self, session):
        game = self.game
        if (not session or session != self.session or game is None
                or game.phase != 'lobby' or len(game.players) >= game.count):
            return None
        return self.phone_url()

    def display_invitation(self):
        url = self.invitation_url(self.session)
        if url is None:
            return None
        try:
            svg = base64.b64encode(join_qr_svg(url)).decode('ascii')
        except ImportError:
            svg = None
        return dict(session=self.session, lobby_revision=self.game.lobby_revision, room_code=self.join_code, qr_svg=svg)

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
            binding_open = self._camera_binding_window() is not None
            camera_available = binding_open and self._camera_current()
            report.update(effects=self.effects.status(), join_code=self.join_code,
                          phone_url=self.phone_url(),
                          display_url=self.origin + "/display#token=" + self.public_token if self.origin else None,
                          speaker_seats=[] if self.game.speaker_order else self.game.eligible_speakers(),
                          camera_enabled=self.camera_enabled, camera_available=camera_available,
                          camera_binding_open=binding_open, camera_binding_revision=self.camera_binding_revision,
                          camera_binding_seats=self._camera_binding_seats() if binding_open else [],
                          camera_source=dict(camera=self.camera_source[0], session=self.camera_source[1])
                              if camera_available else None,
                          camera_candidates=list(self.camera_candidates) if camera_available else [],
                          camera_bindings=[dict(seat=s, camera=v[0], session=v[1], track=v[2]) for s, v in self.bindings.items()])
        return report

    async def create(self, count, *, address=None, lights=(), narration=False, camera=False, discussion_mode="free"):
        async with self.lock:
            if self.game:
                raise GameError("game_already_exists")
            game = self.game_factory(count, clock=self.clock, discussion_mode=discussion_mode)
            addresses = await asyncio.to_thread(self.addresses) if address else []
            if address is not None and address not in addresses:
                raise GameError("phone_address_unavailable")
            session = secrets.token_hex(16)
            self.light_bindings = await self.effects.lights.select(session, list(lights)) if lights else {}
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
            self.camera_context = session
            self.camera_binding_revision = 0
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
                self.light_bindings = {}
                self.camera_context = None
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
                    return dict(token=token, session=self.session, player=self._private(seat))
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
            return dict(token=token, session=self.session, player=self._private(payload.seat))

    async def lobby(self, code):
        async with self.lock:
            if self.game is None or not secrets.compare_digest(self.join_code or "", code):
                raise GameError("room_not_found")
            if self.game.phase != "lobby":
                raise GameError("join_closed")
            return dict(self.game.public(), session=self.session)

    async def issue_seat_recovery(self, proof):
        async with self.lock:
            seat = proof.seat
            admission, replay = self._host_admission(proof, 'recover-seat', seat)
            if (self.session != proof.session or self.game is None or not self.origin
                    or seat not in self.game.players):
                raise GameError('seat_recovery_unavailable')
            now = self.clock()
            if replay:
                recovery = self.seat_recoveries.get(seat)
                if (recovery is None or recovery.event_id != proof.event_id
                        or recovery.expires_at <= now or recovery.attempt_digest is not None):
                    raise GameError('seat_recovery_unavailable')
            else:
                recovery = _SeatRecovery(proof.event_id, secrets.token_urlsafe(32), now + SEAT_RECOVERY_SECONDS)
                self.seat_recoveries[seat] = recovery
                self._remember_host(admission)
            url = f'{self.origin}/phone/recover#ticket={recovery.ticket}&seat={seat}'
            return dict(seat=seat, url=url, seconds_left=math.ceil(recovery.expires_at - now))

    async def claim_seat_recovery(self, payload: SeatClaim, origin: str):
        async with self.lock:
            recovery = self.seat_recoveries.get(payload.seat)
            if (self.origin != origin or self.game is None or self.join_key is None
                    or payload.seat not in self.game.players or recovery is None
                    or recovery.expires_at <= self.clock()
                    or not secrets.compare_digest(recovery.ticket, payload.ticket)):
                raise GameError('seat_recovery_unavailable')
            attempt = self._digest(payload.attempt_id)
            token = hmac.new(
                self.join_key, f'recover-seat:{self.session}:{payload.ticket}:{payload.attempt_id}'.encode(),
                hashlib.sha256,
            ).hexdigest()
            if recovery.attempt_digest is None:
                # 原手机可继续使用至新手机确认。切换在房间锁内完成，不改对局状态。
                self.credentials = {key: seat for key, seat in self.credentials.items() if seat != payload.seat}
                self.credentials[self._digest(token)] = payload.seat
                recovery.attempt_digest = attempt
            elif not secrets.compare_digest(recovery.attempt_digest, attempt):
                raise GameError('seat_recovery_unavailable')
            seat = self._player(token)
            return dict(token=token, session=self.session, player=self._private(seat))

    async def private(self, token, receipt_id=None):
        async with self.lock:
            seat = self._player(token)
            return self._private(seat, receipt_id)

    async def ready(self, token, value, lobby_revision=None):
        async with self.lock:
            seat = self._player(token)
            expected = 0 if lobby_revision is None else lobby_revision
            if self.game.phase == 'lobby' and expected != self.game.lobby_revision:
                raise GameError('phase_changed')
            self.game.ready(seat, value)
            return self._private(seat)

    async def confirm_identity(self, token, revision):
        async with self.lock:
            seat = self._player(token)
            self.game.confirm_identity(seat, revision)
            return self._private(seat)

    async def finish_speaking(self, token, revision):
        async with self.lock:
            seat = self._player(token)
            self.game.tick()
            self.game.finish_speaking(seat, revision)
            result = self._private(seat)
        self._schedule_effects()
        return result

    async def choose(self, token, choice: Choice):
        async with self.lock:
            seat = self._player(token)
            self.game.tick()
            key = (seat, choice.event_id)
            digest = hashlib.sha256(choice.model_dump_json().encode()).hexdigest()
            if key in self.retries:
                if self.retries[key] != digest:
                    raise GameError("event_conflict")
                return self._private(seat, choice.event_id)
            if choice.revision != self.game.revision:
                raise GameError("phase_changed")
            version = self.choice_versions.get(seat, 0)
            if choice.expected_choice_version is not None and choice.expected_choice_version != version:
                raise GameError("choice_changed")
            self.game.act(seat, choice.action, choice.target, choice.revision)
            self.choice_versions[seat] = version + 1
            self.retries[key] = digest
            if len(self.retries) > 2048:
                self.retries.popitem(last=False)
            return self._private(seat, choice.event_id)

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
        if camera_command and (self.game.phase != 'lobby' or proof.binding_revision is not None):
            if proof.binding_revision != self.camera_binding_revision:
                raise GameError('camera_binding_changed')
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
            elif action == "reseat":
                self.game.reseat()
                self.seat_recoveries.clear()
            elif action == "rematch":
                await self._rematch()
            elif action == "advance":
                self.game.advance()
            elif action == "speaker":
                self.game.set_speaker(seat)
            elif action in {"pause", "resume"}:
                self.game.pause(action == "pause")
            elif action == "end":
                self.game.end()
            elif action == "remove":
                self.game.remove(seat)
                self.credentials = {key: s for key, s in self.credentials.items() if s != seat}
                self.seat_recoveries.pop(seat, None)
                if self.bindings.pop(seat, None) is not None:
                    self.camera_binding_revision += 1
            else:
                raise GameError("host_action_invalid")
            self._remember_host(admission)
            result = self.host()
        self._schedule_effects()
        if self.game is not None and self.game.phase=='finished':
            await self._release_camera()
        return result

    async def _rematch(self):
        previous = self.game
        if previous.phase != 'finished':
            raise GameError('rematch_unavailable')
        game = self.game_factory(previous.count, clock=self.clock, discussion_mode=previous.discussion_mode)
        game.carry_seats(previous)
        await self._release_camera()
        if self.light_bindings:
            # 恢复原灯具后重新读取基线，重绑过的设备须由主持人重新选择。
            try:
                await self.effects.lights.restore()
                await self.effects.lights.select(secrets.token_hex(16), list(self.light_bindings), bindings=self.light_bindings)
            except httpx.HTTPError as exc:
                raise GameError('scene_light_unavailable') from exc
        self.game = game
        self.seat_recoveries.clear()
        self.retries.clear()
        self.choice_versions.clear()
        self.camera_generation += 1
        self.camera_context = secrets.token_hex(16)
        self._clear_camera()
        self.effects.last_scene = None

    def _schedule_effects(self):
        if self.game is None or self.session is None:
            return
        changed = self.game.revision != self.last_effect_revision
        narration = self.effect_runs.get('narration')
        if not changed and (narration is None or not narration.ready_to_retry(self.clock())):
            return
        public = self.game.public()
        if changed:
            self.last_effect_revision = self.game.revision
            self._schedule_effect('lighting', public, self.effects.lighting,
                                  lambda game: lighting_scene(game.phase, game.paused) if self.effects.lights_active else None)
        self._schedule_effect('narration', public, self.effects.narrate, self._narration_cue)
        self.effect_task = asyncio.gather(*tuple(self.effect_tasks), return_exceptions=True)

    def _narration_cue(self, game):
        if (self.effects.narration and not game.paused
                and (game.deadline is None or self.clock() < game.deadline)):
            return game.host_script['id']
        return None

    def _schedule_effect(self, channel, public, output, cue_of):
        session, game = self.session, self.game
        cue = cue_of(game)
        previous = self.effect_runs.get(channel)
        scope = (session, game, cue)
        if previous is not None and previous.scope == scope and not previous.ready_to_retry(self.clock()):
            return
        if previous is not None and not previous.task.done() and not previous.task.cancelling():
            previous.task.cancel()
        if cue is None:
            self.effect_runs.pop(channel, None)
            return
        run = self.effect_runs[channel] = _EffectRun(scope)

        def current():
            # 暂停后恢复到相同场景时，已撤销的旧输出仍不能取得发送资格。
            return (self.session == session and self.game is game
                    and self.effect_runs.get(channel) is run and cue_of(game) == cue)

        async def apply():
            if current():
                retry = await output(public, current)
                if channel == 'narration' and retry is True and current():
                    run.retry_at = self.clock() + NARRATION_RECHECK_SECONDS

        run.task = asyncio.create_task(apply(), name='friday-werewolf-' + channel)
        self.effect_tasks.add(run.task)

        def finished(task):
            self.effect_tasks.discard(task)
            if not task.cancelled():
                task.exception()
        run.task.add_done_callback(finished)

    async def step(self, *, wait_for_camera=True):
        async with self.lock:
            if self.game is None:
                return
            if self.clock() - self.created > 6 * 3600 and self.game.phase != "finished":
                self.game.end()
            else:
                self.game.tick()
        self._schedule_effects()
        if self.camera_enabled and self.game is not None and self.game.phase!='finished':
            if wait_for_camera:
                await self._camera()
            elif self.camera_task is None or self.camera_task.done():
                self.camera_task = asyncio.create_task(self._camera(), name='friday-werewolf-camera')
                self.camera_task.add_done_callback(self._camera_finished)
        else:
            if self.camera_task is not None and not self.camera_task.done():
                self.camera_task.cancel()
            self._clear_camera()
            await self._release_camera()

    async def _run(self):
        try:
            while self.game is not None:
                if self.clock() - self.created > 6 * 3600:
                    asyncio.create_task(self.close_game(), name="friday-werewolf-expire")
                    return
                await self.step(wait_for_camera=False)
                await asyncio.sleep(.5)
        finally:
            task = self.camera_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _camera_finished(task):
        if not task.cancelled():
            task.exception()

    def _clear_camera(self):
        self.camera_hints = []
        self.camera_candidates = []
        self.camera_process = None
        self.camera_source = None
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
        context = self.camera_context or session
        # Record before sending: a lost acknowledgement still owes this room an end.
        self.camera_hold=(owner,process,context,capability['scope'])
        result=await owner.party_camera(dict(action='renew',context=context,
            sequence=sequence,expected_scope=capability['scope'],not_after=self.clock()+1,
            camera=report['camera_id'],session=report['session_id'],frame_mono=capability['frame_mono']))
        return (isinstance(result,dict)and result.get('scope')==capability['scope']
                and result.get('context')==context and type(result.get('sequence'))is int and result['sequence']==sequence
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

    def _camera_binding_window(self):
        game = self.game
        if not self.camera_enabled or not self.session or game is None:
            return None
        if game.phase == 'lobby' or (game.phase in {'discussion', 'runoff_discussion'}
                and (game.paused or game.deadline is not None and self.clock() < game.deadline)):
            return game.phase, game.round
        return None

    def _camera_binding_seats(self):
        return [seat for seat, player in self.game.players.items() if self.game.phase == 'lobby' or player.alive]

    async def _camera(self):
        async with self.camera_read_lock:
            return await self._refresh_camera()

    async def _refresh_camera(self):
        self.camera_generation += 1
        generation = self.camera_generation
        if self.embodied is None or not self.game or not self.camera_enabled or self.game.phase=='finished':
            self._clear_camera()
            return
        owner, game, session = self.embodied, self.game, self.session
        process = getattr(owner, "process", None)
        if process is None or process.returncode is not None:
            self._clear_camera()
            return
        def current():
            return (generation == self.camera_generation and self.game is game and self.session == session
                    and game.phase != 'finished' and self.camera_enabled and self.embodied is owner
                    and getattr(owner, 'process', None) is process and process.returncode is None)

        began = self.clock()
        try:
            async with asyncio.timeout(1):
                report = await owner.request("/api/status", timeout=1)
        except (ValueError, OSError, TimeoutError, httpx.HTTPError):
            if generation == self.camera_generation:
                self._clear_camera()
            return
        if not current():
            if generation == self.camera_generation:
                self._clear_camera()
            return
        sample = camera_observation(report)
        if sample is None:
            self._clear_camera()
            return
        source = sample['camera'], sample['session']
        if self.camera_source != source:
            self._clear_camera()
        try:
            async with asyncio.timeout(1):
                held=await self._hold_camera(report,game,session,process)
        except(ValueError,OSError,TimeoutError,httpx.HTTPError):
            if generation == self.camera_generation:
                self._clear_camera()
            return
        if not current():
            if generation == self.camera_generation:
                self._clear_camera()
            return
        now = self.clock()
        elapsed = now - began
        remaining = 1 - sample['age_ms'] / 1000 - elapsed
        if not held or not 0 <= elapsed < 1 or remaining <= 0:
            self._clear_camera()
            return
        self.camera_candidates = [dict(camera=sample['camera'], session=sample['session'], track=track)
                                  for track in sample['people']]
        self.camera_hints = camera_advice(sample, self.bindings, game.players)
        self.camera_until, self.camera_process, self.camera_source = now + remaining, process, source
        return generation, sample

    async def camera_view(self, game_session, phase=None, round_number=None):
        if game_session != self.session or self.game is None:
            raise GameError('room_changed')
        window = self._camera_binding_window()
        if ((phase is None) != (round_number is None) or window is None
                or window != (('lobby', self.game.round) if phase is None else (phase, round_number))):
            raise GameError('camera_binding_unavailable')
        owner, game, session = self.embodied, self.game, self.session
        process = getattr(owner, 'process', None)
        if self.camera_view_busy or process is None or process.returncode is not None:
            return dict(available=False)
        began = self.clock()
        self.camera_view_busy = True
        def current():
            return (self.game is game and self.session == session and self._camera_binding_window() == window
                    and self.embodied is owner and getattr(owner, 'process', None) is process
                    and process.returncode is None)

        try:
            async with asyncio.timeout(1):
                report = await owner.party_frame()
            if not current():
                return dict(available=False)
            sample = camera_picture(report)
            elapsed = self.clock() - began
            if sample is None or not 0 <= elapsed < 1:
                return dict(available=False)
            async with asyncio.timeout(1):
                held=await self._hold_camera(report,game,session,process)
            elapsed=self.clock()-began
            if not held or not current() or not 0 <= elapsed < 1:
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
            _, replay = self._host_admission(proof, "bind", seat)
            if replay:
                return self.host()
            window = self._camera_binding_window()
            if window is None or seat not in self._camera_binding_seats():
                raise GameError("camera_binding_unavailable")
            game, room, revision = self.game, self.session, self.game.revision
        sample = await self._camera()
        async with self.lock:
            admission, replay = self._host_admission(proof, "bind", seat)
            if replay:
                return self.host()
            if self.game is not game or self.session != room or game.revision != revision:
                raise GameError("phase_changed")
            if self._camera_binding_window() != window or seat not in self._camera_binding_seats():
                raise GameError("camera_binding_unavailable")
            value = dict(camera=camera, session=session, track=track)
            if (sample is None or sample[0] != self.camera_generation or not self._camera_current()
                    or value not in self.camera_candidates
                    or any(v == (camera, session, track) for s, v in self.bindings.items() if s != seat)):
                raise GameError("camera_track_unavailable")
            if self.bindings.get(seat) != (camera, session, track):
                self.bindings[seat] = (camera, session, track)
                self.camera_binding_revision += 1
                if game.phase == 'lobby':
                    game.revision += 1
                self.camera_hints = camera_advice(sample[1], self.bindings, game.players)
            self._remember_host(admission)
            return self.host()

    async def unbind(self, seat, camera, session, track, *, proof=None):
        async with self.lock:
            admission, replay = self._host_admission(proof, 'unbind', seat)
            if replay:
                return self.host()
            if self._camera_binding_window() is None or seat not in self._camera_binding_seats():
                raise GameError('camera_binding_unavailable')
            if self.bindings.get(seat) != (camera, session, track):
                raise GameError('camera_binding_changed')
            del self.bindings[seat]
            self.camera_binding_revision += 1
            if self.game.phase == 'lobby':
                self.game.revision += 1
            self.camera_hints = [dict(h, pointing_at=None) if h['pointing_at'] == seat else h
                                 for h in self.camera_hints if h['seat'] != seat]
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
            self.seat_recoveries.clear()
            self.join_key = None
            self.camera_generation += 1
            self._clear_camera()
            self.camera_enabled = False
            self.camera_context = None
            if self.camera_task is not None and not self.camera_task.done():
                self.camera_task.cancel()
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
                    tasks=set(self.effect_tasks)|{task for task in (self.pump,self.camera_task) if task is not None}
                    for task in tasks:
                        if not task.done() and not task.cancelling():task.cancel()
                    await asyncio.shield(asyncio.gather(*tasks,return_exceptions=True))
                    if self.gateway:
                        await self.gateway.close()
                finally:
                    self.game = self.gateway = None
                    self.pump = self.effect_task = self.camera_task = None
                    self.origin = None
                    self.effects.lights_active = False
                    self.light_bindings = {}
                    self.effects.narration = False
                    self.retries.clear()
                    self.choice_versions.clear()
                    self.bindings.clear()
                    self.camera_hints = []
                    self.camera_candidates = []
                    self.last_effect_revision = None
                    self.effect_runs.clear()
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
            if request.url.path in {'/api/player', '/api/ready', '/api/identity', '/api/choice', '/api/speaker-done'}:
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
    @app.get("/phone/recover")
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

    @app.get('/api/invitation')
    async def invitation(request: Request):
        if not owner.public_access(token(request)) or owner.origin != origin:
            raise HTTPException(403, detail='public_access_required')
        result = owner.display_invitation()
        if result is None:
            raise HTTPException(404, detail='invitation_unavailable')
        return result

    @app.post("/api/join")
    async def join(payload: Join):
        return await owner.join(payload)

    @app.post("/api/recover-seat")
    async def recover_seat(payload: SeatClaim):
        return await owner.claim_seat_recovery(payload, origin)

    @app.post("/api/lobby")
    async def lobby(payload: Lobby):
        return await owner.lobby(payload.code)

    @app.get("/api/player")
    async def player(request: Request):
        return await owner.private(token(request), request.headers.get('x-friday-choice'))

    @app.post("/api/ready")
    async def ready(payload: Ready, request: Request):
        return await owner.ready(token(request), payload.ready, payload.lobby_revision)

    @app.post("/api/identity")
    async def confirm_identity(payload: ConfirmIdentity, request: Request):
        return await owner.confirm_identity(token(request), payload.revision)

    @app.post("/api/speaker-done")
    async def finish_speaking(payload: FinishSpeaking, request: Request):
        return await owner.finish_speaking(token(request), payload.revision)

    @app.post("/api/choice")
    async def choice(payload: Choice, request: Request):
        return await owner.choose(token(request), payload)

    return app
