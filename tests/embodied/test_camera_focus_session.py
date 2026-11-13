"""Original worker observation calls and director policy over synthetic sources."""

import ast
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeplane.embodied.camera_director import CameraDirector
from tests.embodied.test_camera_director import clear, person, sample
from tests.embodied.test_speech_owner import rig as rig
from tests.embodied.test_voice_admission import voice as voice

WORKER = Path(__file__).resolve().parents[2] / "homeplane/embodied/worker.py"


@pytest.fixture
def camera(voice, monkeypatch):
    h = voice.home
    h.enabled = h.armed = h.selected = True
    h.camera_session = "source-a"
    h.ensure_voice = Mock()
    h.refresh_voice = Mock()
    h.curtain.close = AsyncMock()
    h.visible_tracks = {7}
    return voice


def hint(session="source-a", *, track=7, at=None):
    return dict(track=track, at=time.monotonic() if at is None else at, session_id=session)


async def frame(camera, origin, session, *, active=True, registration="locked", people=()):
    """Execute the original producer's call, rather than a second observation API."""
    h = camera.home
    now = time.monotonic()
    tree = ast.parse(WORKER.read_text())
    fleet = SimpleNamespace(cameras={h.camera_id: {"owner": None}},
                            update=Mock(return_value=[]), voice=Mock(return_value=h.audio_allowed))
    safety = SimpleNamespace(sound_requested=Mock(return_value=False), offer=Mock())
    scope = dict(home=h, time=time, interaction_enabled=active, fleet=fleet, safety=safety,
                 PRIVATE=Path(camera.audio.private))
    if origin == "primary":
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Await)
                 and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
                 and isinstance(n.value.func.value, ast.Name) and n.value.func.value.id == "home"
                 and n.value.func.attr == "observe"]
        assert len(calls) == 1
        method = ast.AsyncFunctionDef(name="receive", args=ast.arguments(
            posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=[ast.Expr(value=calls[0])], decorator_list=[])
        scope.update(people=list(people), detection={"faces": []},
                     layout={"registration": {"status": registration}}, now=now, capture=None,
                     SELECTED={"id": h.camera_id}, identities=SimpleNamespace(session=session),
                     voice_captured_mono=now)
        code = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(code, str(WORKER), "exec"), scope)
        await scope["receive"]()
    else:
        assert origin == "secondary"
        functions = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                     and n.name == "secondary_observation"]
        assert len(functions) == 1
        process = SimpleNamespace(stdin=None)
        scope.update(secondary_homes={h.camera_id: h}, SELECTED={"id": "cam_2222222222"},
                     secondary_frame_current=lambda observation, owner: owner is process,
                     practice=SimpleNamespace(observe=Mock()), wall=SimpleNamespace(observe=Mock()),
                     dispatch_interactions=AsyncMock())
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(WORKER), "exec"), scope)
        await scope["secondary_observation"](dict(camera=h.camera_id, session=session,
            people=list(people), faces=[], hands=[], registration={"status": registration},
            now=now, captured_mono=now, scene_visible=True), process)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["primary", "secondary"])
async def test_replacement_source_retires_old_hint_and_voice_lease(camera, origin):
    h = camera.home
    h.camera_focus = hint()
    epoch = h.epoch
    current = h._voice_guard(camera.text, time.monotonic(), epoch, True, camera.policy, 7)
    assert current()
    await frame(camera, origin, "source-b")
    assert h.epoch > epoch
    assert h.camera_session == "source-b" and h.camera_focus is None
    assert not current()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["primary", "secondary"])
async def test_same_source_keeps_completed_lease_when_microphone_moves(camera, origin):
    h = camera.home
    original = h.camera_focus = hint()
    h.audio_allowed = False
    epoch = h.epoch
    current = h._voice_guard(camera.text, time.monotonic(), epoch, True, camera.policy, 7)
    await frame(camera, origin, "source-a")
    assert h.epoch == epoch and h.camera_focus is original and current()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["primary", "secondary"])
@pytest.mark.parametrize("session", [None, "", True])
async def test_unknown_frame_session_does_not_arm_input_or_reuse_focus(camera, origin, session):
    camera.home.camera_focus = hint()
    await frame(camera, origin, session)
    assert not camera.home.selected
    assert camera.home.camera_focus is None


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["primary", "secondary"])
async def test_deselecting_observed_input_discards_its_focus(camera, origin):
    camera.home.camera_focus = hint()
    await frame(camera, origin, "source-a", active=False)
    assert not camera.home.selected and camera.home.camera_focus is None


@pytest.mark.asyncio
async def test_closing_home_clears_the_source_and_focus(camera):
    camera.home.camera_focus = hint()
    await camera.home.close()
    assert camera.home.camera_session is None and camera.home.camera_focus is None


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["primary", "secondary"])
@pytest.mark.parametrize("phase", ["transcribe", "delegate"])
async def test_old_decode_or_plan_cannot_actuate_after_source_replacement(camera, origin, phase):
    if phase == "delegate":
        camera.text = "Friday，把玄关灯处理一下"
    original = camera.home.request

    async def worker(kind, **kwargs):
        result = await original(kind, **kwargs)
        if kind == phase:
            await frame(camera, origin, "source-b")
        return result

    camera.home.request = worker
    await camera.home.transcribe(b"SYNTHETIC-old-source", 7, time.monotonic(), True,
                                 epoch=camera.home.epoch)
    command = camera.home.state["voice_command"]
    assert command is None or command["state"] == "expired"
    camera.device.assert_not_awaited()
    camera.home.command.assert_not_awaited()
    assert not camera.audio.calls and camera.home.camera_focus is None


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["Friday", "小福"])
async def test_bare_configured_wake_frames_current_source_without_a_command(camera, word):
    camera.policy.wake_word = word
    camera.home.request = AsyncMock(return_value=dict(
        type='wake', voice_revision=camera.policy.revision, latency_ms=1))
    await camera.home.transcribe(b"SYNTHETIC-bare-wake", None, time.monotonic())
    assert camera.home.camera_focus["track"] == 7
    assert camera.home.camera_focus["session_id"] == "source-a"
    assert camera.home.state["voice_command"] is None
    camera.home.request.assert_awaited_once()
    assert camera.home.request.await_args.args == ('transcribe',) and not camera.audio.calls
    camera.device.assert_not_awaited()


@pytest.mark.asyncio
async def test_bare_wake_worker_flag_cannot_become_a_device_command(camera):
    camera.text = "Friday"
    camera.worker_admission = dict(admitted=True, wake_word=True)
    await camera.home.transcribe(b"SYNTHETIC-empty-intent", 7, time.monotonic(), True)
    camera.device.assert_not_awaited()
    camera.home.command.assert_not_awaited()
    assert "delegate" not in camera.requests


@pytest.mark.parametrize("focus", [
    dict(track=2, at=2, session_id="old"), dict(track=2, at=2),
    dict(track=2, at=None, session_id="first"), dict(track=2, at=True, session_id="first"),
    dict(track=2, at=float("nan"), session_id="first"),
])
def test_director_does_not_choose_a_target_from_unbound_or_invalid_focus(focus):
    d = CameraDirector()
    clear(d)
    assert d.choose(sample(2, people=[person(1, .1), person(2, .75)]), 2, focus=focus) is None
    assert d.phase == "target_ambiguous"


def test_director_accepts_the_matching_source_focus():
    d = CameraDirector()
    clear(d)
    assert d.choose(sample(2, people=[person(1, .1), person(2, .75)]), 2,
                    focus=dict(track=2, at=2, session_id="first")) == "right"


@pytest.mark.parametrize("session,expected", [("old", True), ("first", False)])
def test_pending_move_only_observes_new_focus_from_its_current_source(session, expected):
    d = CameraDirector()
    clear(d)
    assert d.choose(sample(1, people=[person(1)]), 1) == "right"
    current = sample(1.2, people=[person(1), person(2, .1)])
    assert d.decision_current(current, 1.2, "right",
        focus=dict(track=2, at=1.2, session_id=session)) is expected


def test_new_stream_requires_its_own_fresh_view_evidence():
    d = CameraDirector()
    clear(d)
    assert d.choose(sample(1, people=[person(1)]), 1) == "right"
    assert d.choose(sample(2, people=[person(2)], session="replacement"), 2) is None
    assert d.pending_decision is None
    assert d.choose(sample(2.2, people=[person(2)], session="replacement"), 2.2) == "right"


def test_original_worker_source_retirement_clears_the_focus(camera):
    h = camera.home
    h.camera_focus = hint()
    tree = ast.parse(WORKER.read_text())
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {"clear_camera", "clear_interactions"}]
    scope = dict(home=h, SELECTED={"id": h.camera_id}, secondary_homes={},
                 safety=SimpleNamespace(clear=Mock()), fleet=SimpleNamespace(clear=Mock()))
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(WORKER), "exec"), scope)
    scope["clear_camera"](h.camera_id)
    assert not h.selected and h.camera_session is None and h.camera_focus is None


def test_new_source_during_game_keeps_the_last_game_frame_barrier():
    d = CameraDirector()
    clear(d)
    held = dict(sample(2, people=[person(1)], session="replacement"), game={"status": "playing"})
    assert d.choose(held, 2) is None
    assert d.choose(dict(held, game={"status": "off"}), 2.1) is None
    assert d.choose(sample(2.2, people=[person(1)], session="replacement"), 2.2) is None
    assert d.choose(sample(2.4, people=[person(1)], session="replacement"), 2.4) == "right"
