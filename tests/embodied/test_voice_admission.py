"""Original voice routing and Speaker with synthetic ASR/intent and silent HA.

No camera, microphone, model, household control or audible output is started.
The existing speech fixture exercises the real cancellable HTTP/output owner.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied import home_service
from homeplane.embodied.intent import address
from homeplane.embodied.interaction_fleet import InteractionFleet
from tests.embodied.test_speech_owner import rig as rig


@pytest.fixture
def voice(tmp_path, monkeypatch, rig):
    policy = SimpleNamespace(wake_word="Friday", revision=0)
    state = SimpleNamespace(
        policy=policy,
        text="Friday，打开玄关灯",
        attending=True,
        owner=7,
        after_asr=lambda: None,
        after_plan=lambda: None,
        after_effect=lambda: None,
        worker_admission=None,
        requests=[],
    )
    monkeypatch.setattr(home_service, "DATA", tmp_path)
    monkeypatch.setattr(home_service, "VOICE_INPUT_PAUSED", tmp_path / "voice-input-paused")
    home = home_service.HomeService(
        camera_id="cam_1111111111", voice_preferences=lambda: state.policy
    )
    home.selected = home.armed = True
    home.camera_session = "SYNTHETIC-voice-source"
    home.last_video = time.monotonic()
    home.attention.latest = {7: True}
    home.speaker = rig.speaker()
    receipt = dict(status="accepted", acknowledged=True, reached_target=False)

    async def effect(*args, **kwargs):
        state.after_effect()
        return dict(receipt)

    home.command = AsyncMock(side_effect=effect)
    device = AsyncMock(side_effect=effect)
    monkeypatch.setattr(home_service.bridge, "command", device)

    async def worker(kind, **kwargs):
        state.requests.append(kind)
        if kind == "transcribe":
            admitted = state.worker_admission or address(
                state.text, state.attending, wake_word=policy.wake_word
            )
            state.after_asr()
            return dict(text=state.text, voice_revision=policy.revision, address=admitted, laya={})
        assert kind == "delegate", "Only the synthetic ASR and intent ports are allowed"
        state.after_plan()
        curtain = "窗帘" in state.text
        # Contract-shaped stand-in for the separate planner, never a model call.
        return dict(
            type="task_result", simulated=False, provider="SYNTHETIC-intent",
            plan=dict(is_command=True, route="curtain" if curtain else "home",
                      device="selected_cover" if curtain else "玄关灯",
                      action="open" if curtain else "on", reply="正在处理。"),
        )

    home.request = worker
    state.home, state.device, state.audio = home, device, rig
    return state


async def speak(voice):
    await voice.home.transcribe(
        b"SYNTHETIC-admitted-utterance", voice.owner, time.monotonic(), voice.attending
    )
    return voice.home.state["voice_command"]


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", ["Friday", "小福"])
@pytest.mark.parametrize("target", ["玄关灯", "窗帘"])
@pytest.mark.parametrize("phase", ["asr", "intent"])
async def test_explicit_wake_survives_optional_visual_track_loss(voice, wake, target, phase):
    voice.policy.wake_word = wake
    voice.text = f"{wake}，打开{target}" if phase == "asr" else f"{wake}，把{target}处理一下"
    setattr(voice, "after_asr" if phase == "asr" else "after_plan",
            lambda: voice.home.attention.latest.clear())
    command = await speak(voice)
    assert command["state"] == "accepted", command
    assert command["receipt"] == dict(status="accepted", acknowledged=True, reached_target=False)
    assert voice.device.await_count + voice.home.command.await_count == 1
    assert voice.home.speaker.state["status"] == "accepted"
    assert voice.home.speaker.state["physical_audio_verified"] is False
    assert [str(r.url.path) for r in voice.audio.calls] == ["/api/services/notify/send_message"]
    assert json.loads(voice.audio.calls[0].content)["entity_id"] == "notify.synthetic"
    assert voice.requests == (["transcribe"] if phase == "asr" else ["transcribe", "delegate"])


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["玄关灯", "窗帘"])
async def test_explicit_wake_without_visual_owner_still_uses_original_ports(voice, target):
    voice.text, voice.attending, voice.owner = f"Friday，打开{target}", False, None
    voice.home.attention.latest.clear()
    command = await speak(voice)
    assert command["state"] == "accepted"
    assert voice.device.await_count + voice.home.command.await_count == 1
    assert len(voice.audio.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["asr", "intent"])
async def test_gaze_only_request_still_expires_after_track_loss(voice, phase):
    voice.text = "打开玄关灯" if phase == "asr" else "把玄关灯处理一下"
    setattr(voice, "after_asr" if phase == "asr" else "after_plan",
            lambda: voice.home.attention.latest.clear())
    command = await speak(voice)
    assert command["state"] == "expired"
    voice.device.assert_not_awaited()
    voice.home.command.assert_not_awaited()
    assert not voice.audio.calls
    assert voice.requests == (["transcribe"] if phase == "asr" else ["transcribe", "delegate"])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["asr", "intent"])
async def test_input_pause_blocks_late_dispatch_even_before_worker_cleanup(voice, phase):
    if phase == "intent":
        voice.text = "Friday，把玄关灯处理一下"
    setattr(voice, "after_asr" if phase == "asr" else "after_plan",
            lambda: home_service.VOICE_INPUT_PAUSED.touch())
    command = await speak(voice)
    assert command["state"] == "expired"
    voice.device.assert_not_awaited()
    voice.home.command.assert_not_awaited()
    assert not voice.audio.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["asr", "intent"])
async def test_microphone_handoff_keeps_the_completed_utterances_own_lease(voice, phase):
    fleet = InteractionFleet()
    camera, other = voice.home.camera_id, "cam_2222222222"
    fleet.session(camera, "SYNTHETIC-first")
    fleet.session(other, "SYNTHETIC-second")
    assert fleet.voice(camera, time.monotonic(), False, people=1, session="SYNTHETIC-first")
    voice.attending, voice.owner = False, None
    if phase == "intent":
        voice.text = "Friday，把玄关灯处理一下"

    def handoff():
        now = time.monotonic()
        assert fleet.voice(other, now, True, people=1, session="SYNTHETIC-second")
        voice.home.audio_allowed = fleet.voice_current(camera, now)
        assert voice.home.audio_allowed is False

    setattr(voice, "after_asr" if phase == "asr" else "after_plan", handoff)
    command = await speak(voice)
    assert command["state"] == "accepted"
    voice.device.assert_awaited_once()
    assert len(voice.audio.calls) == 1


@pytest.mark.asyncio
async def test_worker_admission_flag_cannot_replace_actual_addressing(voice):
    voice.text, voice.attending, voice.owner = "打开玄关灯", False, None
    voice.worker_admission = dict(admitted=True, wake_word=True)
    command = await speak(voice)
    assert command["state"] == "expired"
    assert voice.requests == ["transcribe"]
    voice.device.assert_not_awaited()
    assert not voice.audio.calls


@pytest.mark.asyncio
async def test_pause_after_device_ack_preserves_execution_receipt_but_stops_reply(voice):
    voice.after_effect = lambda: home_service.VOICE_INPUT_PAUSED.touch()
    command = await speak(voice)
    assert command["state"] == "accepted" and not command["receipt"]["reached_target"]
    voice.device.assert_awaited_once()
    assert voice.home.speaker.state["status"] == "expired"
    assert not voice.audio.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("gaze", [False, True])
async def test_worker_wake_flag_cannot_publish_a_camera_focus_cue(voice, gaze):
    voice.text, voice.attending = "打开玄关灯", gaze
    voice.home.visible_tracks = {7}
    voice.worker_admission = dict(admitted=True, wake_word=True)
    command = await speak(voice)
    assert voice.home.camera_focus is None
    if gaze:
        assert command["state"] == "accepted"
        voice.device.assert_awaited_once()
    else:
        assert command["state"] == "expired"
        voice.device.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_pause_during_asr_cannot_publish_a_camera_focus_cue(voice):
    voice.home.visible_tracks = {7}
    voice.after_asr = lambda: home_service.VOICE_INPUT_PAUSED.touch()
    command = await speak(voice)
    assert command["state"] == "expired"
    assert voice.home.camera_focus is None
    voice.device.assert_not_awaited()
    assert not voice.audio.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", [set(), {7}, {7, 8}])
async def test_actual_wake_frames_only_a_unique_visible_person(voice, visible):
    voice.home.visible_tracks = visible
    command = await speak(voice)
    assert command["state"] == "accepted"
    if len(visible) == 1:
        assert voice.home.camera_focus["track"] == 7
        assert 0 <= time.monotonic() - voice.home.camera_focus["at"] < 1
    else:
        assert voice.home.camera_focus is None
