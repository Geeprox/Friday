"""Original party owner and camera director; synthetic observations, inert motor."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.catalog import MOTION_GAME_MODES
from homeplane.embodied.motion_games import MotionGames
from homeplane.werewolf_effects import HostEffects
from homeplane.werewolf_service import WerewolfService
from tests.test_werewolf_camera import observation
from tests.test_werewolf_service import SilentLights
from tests.embodied.test_camera_director import clear, person, sample
from homeplane.embodied.party_camera import PartyCamera


def source(at=10.0):
    return dict(observation(), _local_received=at)


def renew(holder, *, context="b" * 32, sequence=1, at=10.0):
    return dict(
        action="renew",
        context=context,
        expected_scope=holder.scope,
        sequence=sequence,
        not_after=at + 1,
        camera="cam_1234567890",
        session="SYNTHETIC",
        frame_mono=at,
    )


class Camera:
    def __init__(self):
        self.process = SimpleNamespace(returncode=None)
        self.report = observation()
        self.report["party_camera"] = dict(
            protocol=1, scope="a" * 32, revision=0, active=False, frame_mono=10.0
        )
        self.calls = []

    async def request(self, path, body=None, **kwargs):
        self.calls.append((path, copy.deepcopy(body)))
        if path == "/api/party-camera":
            return dict(
                context=body["context"],
                scope="a" * 32,
                sequence=body["sequence"],
                active=body["action"] == "renew",
            )
        return copy.deepcopy(self.report)

    async def party_camera(self, payload):
        return await self.request("/api/party-camera", payload)


@pytest.mark.asyncio
async def test_party_observation_acquires_a_scoped_hold_and_close_releases_it():
    camera = Camera()
    owner = WerewolfService(HostEffects(SilentLights()), embodied=camera, clock=lambda: 10.0)
    try:
        await owner.create(6, camera=True)
        owner.pump.cancel()
        await asyncio.gather(owner.pump, return_exceptions=True)
        owner.pump = None
        context = owner.session
        await owner._camera()
        holds = [body for path, body in camera.calls if path == "/api/party-camera"]
        assert len(holds) == 1 and holds[0]["action"] == "renew" and holds[0]["context"] == context
        assert holds[0]["camera"] == "cam_1234567890" and holds[0]["session"] == "SYNTHETIC"
        await owner.close_game()
        ends = [
            body for path, body in camera.calls if path == "/api/party-camera" and body["action"] == "end"
        ]
        assert len(ends) == 1 and ends[0]["context"] == context and ends[0]["sequence"] > holds[0]["sequence"]
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_same_room_rematch_gets_a_new_hold_after_the_previous_context_is_retired():
    camera, holder = Camera(), PartyCamera()
    camera.report['party_camera'] = holder.status(camera='cam_1234567890',session='SYNTHETIC',frame_mono=10.,now=10.)

    async def command(payload):
        return holder.command(payload, source(), 10.)

    camera.party_camera = command
    owner = WerewolfService(HostEffects(SilentLights()), embodied=camera, clock=lambda: 10.)
    try:
        await owner.create(6, camera=True)
        owner.pump.cancel()
        await asyncio.gather(owner.pump,return_exceptions=True)
        owner.pump = None
        room = owner.session
        await owner._camera()
        assert holder.current['context']==room
        await owner.control('end')
        assert holder.current is None and holder.requests[room]['closed']
        await owner.control('rematch')
        await owner._camera()
        assert owner.session==room and owner.camera_context!=room
        assert holder.current['context']==owner.camera_context and owner.host()['camera_available']
    finally:
        await owner.close()


def test_party_hold_freezes_auto_follow_even_when_the_motion_game_is_off():
    director = CameraDirector()
    clear(director)
    current = sample(10, people=[person(identity="SYNTHETIC")])
    current["party_camera"] = dict(scope="a" * 32, revision=1, active=True)
    assert director.choose(current, 10) is None
    assert director.phase == "game_held"


def test_party_begin_and_end_between_decision_and_dispatch_retires_the_old_motor_plan():
    director = CameraDirector()
    clear(director)
    before = sample(10, people=[person(identity="SYNTHETIC")])
    before["party_camera"] = dict(scope="a" * 32, revision=0, active=False)
    assert director.choose(before, 10) == "right"
    after = copy.deepcopy(before)
    after["party_camera"]["revision"] = 2
    assert not director.decision_current(after, 10.1, "right")


@pytest.mark.parametrize(
    "fault",
    [
        "camera",
        "session",
        "scope",
        "late",
        "future",
        "frame",
        "tracking",
        "covered",
        "motor",
        "settling",
        "missing",
    ],
)
def test_hold_requires_the_actual_fresh_settled_source(fault):
    holder = PartyCamera()
    body = renew(holder)
    current = source()
    options = {}
    if fault == "camera":
        body["camera"] = "cam_0987654321"
    elif fault == "session":
        body["session"] = "replaced"
    elif fault == "scope":
        body["expected_scope"] = "c" * 32
    elif fault == "late":
        body["not_after"] = 10.0
    elif fault == "future":
        body["not_after"] = 13.0
    elif fault == "frame":
        body["frame_mono"] = 8.0
    elif fault == "tracking":
        current["tracking_current"] = False
    elif fault == "covered":
        current["scene_visible"] = False
    elif fault == "motor":
        options["motor_busy"] = True
    elif fault == "settling":
        options["motor_last_sent"] = 9.0
    else:
        body["camera"] = None
    with pytest.raises(ValueError):
        holder.command(body, current, 10.0, **options)
    assert holder.current is None


def test_end_overtakes_an_inflight_renewal_and_cannot_release_the_next_room():
    holder = PartyCamera()
    body = renew(holder)
    holder.command(body, source(), 10.0)
    end = dict(action="end", context=body["context"], expected_scope=holder.scope, sequence=2, not_after=11.0)
    holder.command(end, {}, 10.0)
    with pytest.raises(ValueError, match="retired"):
        holder.command(body, source(), 10.0)
    second = renew(holder, context="d" * 32, sequence=3)
    holder.command(second, source(), 10.0)
    assert holder.command(end, {}, 10.0)["active"] is False
    assert holder.current["context"] == second["context"]


def test_lost_controller_expires_without_mutating_persistent_camera_intent():
    holder = PartyCamera()
    body = renew(holder)
    holder.command(body, source(), 10.0)
    epoch = holder.revision
    assert holder.window(12.9)[2]
    assert holder.window(13.0) == (holder.scope, epoch + 1, False)
    assert holder.current is None


def test_hold_bookkeeping_is_bounded_even_for_closed_rooms():
    holder = PartyCamera()
    for i in range(64):
        holder.command(
            dict(action="end", context=f"{i:032x}", expected_scope=holder.scope, sequence=1, not_after=11.0),
            {},
            10.0,
        )
    with pytest.raises(ValueError, match="capacity"):
        holder.command(renew(holder), source(), 10.0)
    holder.command(renew(holder, at=71.0), source(71.0), 71.0)
    assert len(holder.requests) == 1


@pytest.mark.asyncio
async def test_original_service_transport_worker_route_and_director_share_the_same_hold(
    tmp_path, monkeypatch
):
    import httpx
    import secrets
    from fastapi import FastAPI, Request, HTTPException
    from fastapi.responses import JSONResponse
    from homeplane.embodied import service as service_module
    from tests.embodied.test_review_motion import source_function

    clock = [10.0]
    holder = PartyCamera()
    current = source()

    def report():
        return dict(
            current,
            party_camera=holder.status(
                camera=current["camera_id"], session=current["session_id"], frame_mono=clock[0], now=clock[0]
            ),
        )

    scope = dict(
        Request=Request,
        HTTPException=HTTPException,
        party_camera=holder,
        games=MotionGames(),
        MOTION_GAME_MODES=MOTION_GAME_MODES,
        view=report,
        last_frame=10.0,
        motor_waiters={},
        motor_last_sent=-100.0,
        time=SimpleNamespace(monotonic=lambda: clock[0]),
    )
    route = source_function("homeplane/embodied/worker.py", "party_camera_control", scope)
    scope.update(
        KEY="SYNTHETIC-key",
        ALLOWED_IPS={"127.0.0.1"},
        secrets=secrets,
        JSONResponse=JSONResponse,
        configuration=lambda: dict(speaker_hosts=[], main_origin="http://parent.invalid"),
    )
    source_function("homeplane/embodied/worker.py", "authorized", scope)
    privacy = source_function("homeplane/embodied/worker.py", "privacy", scope)
    app = FastAPI()
    app.middleware("http")(privacy)
    app.add_api_route("/api/party-camera", route, methods=["POST"])
    app.get("/api/status")(report)
    app.get("/api/frame")(lambda: dict(report(), width=2, height=2, frame="/9j/2Q=="))
    monkeypatch.setattr(service_module, "PRIVATE", tmp_path)
    monkeypatch.setattr(service_module, "configuration", lambda: dict(viewer_port=31973))
    (tmp_path / "viewer-key").write_text("SYNTHETIC-key")
    (tmp_path / "control-key").write_text("SYNTHETIC-control-key")
    camera = service_module.EmbodiedService()
    await camera.client.aclose()
    camera.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False)
    camera.process = SimpleNamespace(returncode=None)
    camera.selected = current["camera_id"]
    owner = WerewolfService(HostEffects(SilentLights()), embodied=camera, clock=lambda: clock[0])
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:31973"
        ) as raw:
            assert (await raw.post("/api/party-camera", json=renew(holder))).status_code == 403
            assert (
                await raw.post(
                    "/api/party-camera?k=SYNTHETIC-key",
                    json=renew(holder),
                    headers={"Origin": "http://elsewhere.invalid"},
                )
            ).status_code == 403
        assert holder.current is None
        await owner.create(6, camera=True)
        owner.pump.cancel()
        await asyncio.gather(owner.pump, return_exceptions=True)
        owner.pump = None
        scope['games'].reset('tennis')
        await owner._camera()
        assert owner.host()['camera_reason'] == 'motion_game_camera_busy'
        assert not owner.camera_candidates and holder.current is None
        assert await owner.camera_view(owner.session) == dict(available=False, reason='motion_game_camera_busy')
        scope['games'].reset('off')
        await owner._camera()
        assert owner.host()['camera_reason'] is None
        assert owner.camera_candidates and holder.current["context"] == owner.session
        assert (await owner.camera_view(owner.session))['available'] is True
        director = CameraDirector()
        clear(director)
        view = dict(sample(10, people=[person(identity="SYNTHETIC")]), party_camera=report()["party_camera"])
        assert director.choose(view, 10.0) is None and director.phase == "game_held"
        await owner.close_game()
        assert holder.current is None
        view["party_camera"] = report()["party_camera"]
        assert director.choose(view, 10.0) is None
        for now in [10.2, 10.4]:
            clock[0] = now
            view["_local_received"] = now
            view["party_camera"] = report()["party_camera"]
            move = director.choose(view, now)
        assert move == "right"
    finally:
        await owner.close()
        await camera.client.aclose()


@pytest.mark.asyncio
async def test_lost_hold_ack_never_publishes_advice_and_close_still_ends_that_request():
    import httpx

    camera = Camera()
    original = camera.party_camera
    seen = []

    async def request(body):
        seen.append(copy.deepcopy(body))
        result = await original(body)
        if body["action"] == "renew":
            raise httpx.ReadTimeout("SYNTHETIC lost ack")
        return result

    camera.party_camera = request
    owner = WerewolfService(HostEffects(SilentLights()), embodied=camera, clock=lambda: 10.0)
    try:
        await owner.create(6, camera=True)
        owner.pump.cancel()
        await asyncio.gather(owner.pump, return_exceptions=True)
        owner.pump = None
        await owner._camera()
        assert not owner.camera_candidates and not owner.camera_hints
        await owner.close_game()
        assert [v["action"] for v in seen] == ["renew", "end"] and seen[1]["sequence"] > seen[0]["sequence"]
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_before_send", [False, True])
async def test_old_motor_plan_cannot_cross_the_original_dispatch_after_a_party_window(finish_before_send):
    from tests.embodied.test_motor_recovery import primary_scope

    scope, sent = primary_scope()
    now = scope["last_frame"]
    holder = PartyCamera()
    director = CameraDirector()
    clear(director)
    current = dict(
        sample(now, people=[person(identity="SYNTHETIC")]),
        party_camera=dict(scope=holder.scope, revision=0, active=False),
    )
    assert director.choose(current, now) == "right"
    holder.command(renew(holder, at=now), source(now), now)
    if finish_before_send:
        holder.command(
            dict(action="end", context="b" * 32, expected_scope=holder.scope, sequence=2, not_after=now + 1),
            {},
            now,
        )
    current["party_camera"] = holder.status(
        camera="cam_1234567890", session="SYNTHETIC", frame_mono=now, now=now
    )
    scope["state"].update(current)
    with pytest.raises(RuntimeError, match="stale_input"):
        await scope["motor_step"](
            "right",
            now,
            expected_intent="original",
            decision_current=lambda: director.decision_current(current, scope["time"].monotonic(), "right"),
        )
    assert sent == [] and not scope["motor_waiters"]


@pytest.mark.asyncio
@pytest.mark.parametrize("camera", ["primary", "secondary"])
@pytest.mark.parametrize("end_before_dispatch", [False, True])
async def test_queued_household_gestures_cannot_cross_a_party_hold_but_other_cameras_still_work(
    monkeypatch, camera, end_before_dispatch
):
    from collections import deque
    from unittest.mock import AsyncMock, Mock
    from homeplane.embodied import bridge
    from homeplane.embodied.motion_games import MotionGames
    from tests.embodied.test_game_camera_ownership import actual

    calls = AsyncMock(return_value={})
    monkeypatch.setattr(bridge, "command", calls)
    holder = PartyCamera()
    jobs = []
    scope = dict(
        SELECTED={"id": "primary"},
        games=MotionGames(),
        party_camera=holder,
        interaction_enabled=True,
        home=SimpleNamespace(armed=True, spawn=jobs.append),
        secondary_homes={"secondary": SimpleNamespace(armed=True)},
        interaction_source_current=lambda _: True,
        fleet=SimpleNamespace(current=lambda *_: True, receipts=deque()),
        practice=SimpleNamespace(suppresses=lambda _: False),
        wall=SimpleNamespace(notice=Mock()),
        time=SimpleNamespace(monotonic=lambda: 10.0, time=lambda: 1000.0),
    )
    dispatch = actual("dispatch_interactions", scope)
    event = dict(
        camera=camera,
        session="SYNTHETIC",
        action="on",
        target="SYNTHETIC-light",
        event_id="SYNTHETIC",
        binding="a" * 32,
        revision=1,
        observed_mono=10.0,
    )
    await dispatch([event])
    holder.command(renew(holder), source(), 10.0)
    if end_before_dispatch:
        holder.command(
            dict(action="end", context="b" * 32, expected_scope=holder.scope, sequence=2, not_after=11.0),
            {},
            10.0,
        )
    await asyncio.gather(*jobs)
    assert calls.await_count == (1 if camera == "secondary" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["legacy", "scope", "sequence", "inactive"])
async def test_unconfirmed_hold_never_exposes_people_as_usable_party_advice(fault):
    camera = Camera()
    original = camera.party_camera
    if fault == "legacy":
        camera.report.pop("party_camera")

    async def answer(body):
        result = await original(body)
        if fault == "scope":
            result["scope"] = "c" * 32
        elif fault == "sequence":
            result["sequence"] = True
        elif fault == "inactive":
            result["active"] = False
        return result

    camera.party_camera = answer
    owner = WerewolfService(HostEffects(SilentLights()), embodied=camera, clock=lambda: 10.0)
    try:
        await owner.create(6, camera=True)
        owner.pump.cancel()
        await asyncio.gather(owner.pump, return_exceptions=True)
        owner.pump = None
        await owner._camera()
        assert not owner.camera_candidates and not owner.camera_hints
    finally:
        await owner.close()


def test_a_party_between_two_frames_consumes_the_old_pose_and_allows_a_fresh_one(monkeypatch):
    from homeplane.embodied.interaction_fleet import InteractionFleet
    from tests.embodied.test_game_gesture_handoff import configure, step, person as pose

    configure(monkeypatch)
    fleet = InteractionFleet()
    holder = PartyCamera()
    game = ("SYNTHETIC-game", 0)
    for i in range(12):
        assert not step(fleet, i / 10, game_window=(game, holder.window(i / 10)[:2]))
    holder.command(renew(holder, at=1.2), source(1.2), 1.2)
    holder.command(
        dict(action="end", context="b" * 32, expected_scope=holder.scope, sequence=2, not_after=2.2), {}, 1.2
    )
    window = (game, holder.window(1.2)[:2])
    for i in range(12, 35):
        assert not step(fleet, i / 10, label="open_palm", pose=pose(raised=True), game_window=window)
    for i in range(35, 42):
        assert not step(fleet, i / 10, game_window=window)
    events = []
    for i in range(42, 61):
        events += step(fleet, i / 10, label="open_palm", pose=pose(raised=True), game_window=window)
    assert len(events) == 1
