"""用静音子进程替身验证本机播报的设置、狼人杀入口和撤销。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from homeplane.actions import ActionController
from homeplane.core import HomeCore
from homeplane.embodied import airplay, audio_output, speech, speech_mac
from homeplane.embodied.paths import configuration
from homeplane.embodied.speech_selection import save_output
from homeplane.local_camera_home import LocalCameraHome
from homeplane.store import TraceStore
from homeplane.werewolf_api import create_router
from tests.test_homeplane_actions import close


class InertSpeech:
    def __init__(self, held):
        self.held, self.returncode = held, None
        self.finished, self.input_received = asyncio.Event(), asyncio.Event()
        self.stdin = Mock()
        self.stdin.write.side_effect = lambda _: self.input_received.set()
        self.stdin.close.side_effect = self.end_of_input
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()

    def end_of_input(self):
        if not self.held and self.stdin.write.called:
            self.finish(0)

    def finish(self, code):
        if self.returncode is None:
            self.returncode = code
            self.finished.set()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)

    async def wait(self):
        await self.finished.wait()
        return self.returncode


@pytest.fixture
def local(tmp_path, monkeypatch):
    state = SimpleNamespace(
        private=tmp_path, held=False, children=[], spawned=asyncio.Event(), release=asyncio.Event(),
    )
    state.release.set()

    async def spawn(*command, **options):
        assert command == ('/usr/bin/say', '-f', '-')
        assert options['stdin'] == asyncio.subprocess.PIPE
        process = InertSpeech(state.held)
        state.children.append(process)
        state.spawned.set()
        await state.release.wait()
        return process

    state.spawn = AsyncMock(side_effect=spawn)
    state.ha = AsyncMock(side_effect=AssertionError('本机播报不能请求 HA'))
    for module in (audio_output, speech, airplay):
        monkeypatch.setattr(module, 'PRIVATE', tmp_path)
    for module in (audio_output, speech):
        monkeypatch.setattr(module, 'configuration', lambda: configuration(tmp_path / 'config.json'))
    monkeypatch.setattr(speech, 'VOICE_PAUSED', tmp_path / 'voice-paused')
    monkeypatch.setattr(speech.HA, 'arequest', state.ha)
    monkeypatch.setattr(speech_mac, 'available', lambda: True)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', state.spawn)
    return state


@pytest.mark.asyncio
async def test_choose_mac_with_no_camera_or_ha_and_play_only_explicit_test(local):
    app, router = FastAPI(), APIRouter()
    audio_output.install(router)
    app.include_router(router)
    base = '/api/embodied/speech-output'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
        before = (await client.get(base)).json()
        assert before['mac_available'] and not before['configured']
        selected = await client.post(base, json={'output':'mac'})
        assert selected.status_code == 200 and selected.json()['configured'] and selected.json()['output'] == 'mac'
        assert not (local.private / 'config.json').exists()
        assert (local.private / 'speech-output.json').stat().st_mode & 0o777 == 0o600
        assert (await client.post(base + '/test', json={'output':'homepod'})).status_code == 409
        local.spawn.assert_not_awaited()
        result = await client.post(base + '/test', json={'output':'mac'})
        assert result.json() == {'status':'accepted', 'backend_completed':True, 'physical_audio_verified':False}
        local.children[0].stdin.write.assert_called_once_with('这是一段播报试听。'.encode())
        assert (await client.post(base, json={'output':'panel'})).json()['output'] == 'panel'
        assert (await client.post(base + '/test', json={'output':'mac'})).status_code == 409
    assert local.spawn.await_count == 1
    local.ha.assert_not_awaited()


@pytest.mark.asyncio
async def test_werewolf_host_uses_the_selected_mac_without_a_camera_or_ha(local):
    save_output(local.private, 'mac')
    core = HomeCore(LocalCameraHome(mijia_selected=True), TraceStore(local.private / 'home.sqlite'))
    core.actions = ActionController(core, local.private / 'no-device-authority.json')
    router = create_router(core, asyncio.Lock(), None, local.private)
    owner = router.game_owner
    owner.addresses = lambda: []
    app = FastAPI()
    app.include_router(router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local',
                                     headers={'Origin':'http://local'}) as client:
            base = '/api/games/werewolf'
            context = (await client.get(base + '/context')).json()
            assert context['speech']['output'] == 'mac' and context['speech']['configured']
            local.spawn.assert_not_awaited()
            result = await client.post(base + '/create', json={'players':6, 'narration':True})
            assert result.is_success, result.text
            await owner.step()
            if owner.effect_task is not None:
                await owner.effect_task
            host = (await client.get(base + '/host')).json()
            public_text = host['game']['host_script']['text']
            local.children[0].stdin.write.assert_called_once_with(public_text.encode())
            assert host['effects']['voice'] == 'requested' and not host['effects']['physical_audio_verified']
        local.spawn.assert_awaited_once()
        local.ha.assert_not_awaited()
        assert not (local.private / 'config.json').exists()
    finally:
        await owner.close()
        await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize('moment', ['before_input', 'while_playing'])
async def test_quiet_revokes_the_mac_child_and_prevents_a_second_output(local, moment):
    local.held = True
    save_output(local.private, 'mac')
    if moment == 'before_input':
        local.release.clear()
    speaking = asyncio.create_task(speech.Speaker().say('合成主持提示'))
    try:
        await asyncio.wait_for(local.spawned.wait(), 2)
        process = local.children[0]
        if moment == 'while_playing':
            await asyncio.wait_for(process.input_received.wait(), 2)
        assert (await speech.Speaker().say('并发合成提示'))['status'] == 'busy'
        (local.private / 'voice-paused').touch()
        local.release.set()
        receipt = await asyncio.wait_for(speaking, 2)
        assert receipt['status'] == ('paused' if moment == 'before_input' else 'unconfirmed')
        assert process.returncode == -15
        assert process.stdin.write.call_count == (0 if moment == 'before_input' else 1)
        assert (await speech.Speaker().say('安静模式期间的合成提示'))['status'] == 'paused'
        local.spawn.assert_awaited_once()
        local.ha.assert_not_awaited()
    finally:
        local.release.set()
        if not speaking.done():
            speaking.cancel()
            await asyncio.gather(speaking, return_exceptions=True)
