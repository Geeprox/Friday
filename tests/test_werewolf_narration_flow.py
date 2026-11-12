"""Real game/effect/controller flow with silent output and inert HA transport."""

import asyncio
from contextlib import asynccontextmanager
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
import httpx

from homeplane.ha_control import HADeviceWriter
from homeplane.werewolf_api import create_router
from homeplane.werewolf_effects import HostEffects, SceneLights
from homeplane.werewolf_service import Join, WerewolfService
from tests.test_homeplane_actions import close
from tests.test_homeplane_device_controls import rig
from tests.test_werewolf_service import begin_play


@asynccontextmanager
async def party(tmp_path, *, clock=time.monotonic):
    core, rows, writes, *_ = rig(tmp_path)
    lights = SceneLights(core, asyncio.Lock(), tmp_path / 'scene.json')
    spoken = []

    async def speak(text, *, current):
        if current():
            spoken.append(text)
        return dict(status='accepted', physical_audio_verified=False)

    effects = HostEffects(lights, speak=speak, speech_status=lambda: dict(quiet=False, configured=True))
    service = WerewolfService(effects, clock=clock)
    try:
        target = (await lights.choices())[0]['id']
        await service.create(6, lights=[target], narration=True)
        service.pump.cancel()
        await asyncio.gather(service.pump, return_exceptions=True)
        service.pump = None
        tokens = {}
        for seat in range(1, 7):
            joined = await service.join(Join(code=service.join_code, seat=seat, name=f'合成玩家{seat}'))
            tokens[seat] = joined['token']
            await service.ready(joined['token'], True)
        yield SimpleNamespace(core=core, rows=rows, writes=writes, lights=lights,
                              effects=effects, service=service, spoken=spoken, target=target, tokens=tokens)
    finally:
        await service.close()
        await close(core)


async def until(predicate):
    async with asyncio.timeout(.5):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancelled_close_request_still_restores_lights_while_camera_releases(tmp_path):
    async with party(tmp_path) as table:
        service = table.service
        await begin_play(service)
        await service.effect_task
        lamp = next(row for row in table.rows if row['entity_id'] == 'light.simulated')
        assert lamp['state'] == 'on' and lamp['attributes']['brightness'] == 46
        entered, released = asyncio.Event(), asyncio.Event()

        async def release_camera(command):
            assert command['action'] == 'end'
            assert command['context'] == body['session']
            entered.set()
            await released.wait()

        process = SimpleNamespace(returncode=None)
        service.embodied = SimpleNamespace(process=process, party_camera=release_camera)
        service.camera_hold = (service.embodied, process, service.session, 'a' * 32)
        app = FastAPI()
        app.include_router(create_router(None, None, None, tmp_path, owner=service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                     headers={'Origin': 'http://test'}) as client:
            body = dict(action='close', session=service.session, revision=service.game.revision,
                        event_id='SYNTHETIC-close-while-camera-releases')
            request = asyncio.create_task(client.post('/api/games/werewolf/control', json=body))
            replacement = None
            try:
                await asyncio.wait_for(entered.wait(), 1)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert (await client.get('/api/games/werewolf/host')).json() == {'active': False}
                await until(lambda: lamp['state'] == 'off' and not table.lights.records)
                assert len(table.writes) == 2
                replacement = asyncio.create_task(client.post('/api/games/werewolf/create', json={'players': 6}))
                await until(service.lock.locked)
                assert not replacement.done()
                released.set()
                new_room = await asyncio.wait_for(replacement, 1)
                assert new_room.status_code == 200 and new_room.json()['session'] != body['session']
                assert lamp['state'] == 'off' and len(table.writes) == 2
            finally:
                released.set()
                await asyncio.gather(request, return_exceptions=True)
                if replacement is not None:
                    await asyncio.gather(replacement, return_exceptions=True)
                await service.close()
            assert not table.lights.status()['recovery_pending']
            assert len(table.writes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['quiet', 'unconfigured', 'status_error', 'busy', 'muted'])
async def test_current_prompt_recovers_without_advancing_or_replaying_lights(tmp_path, blocked):
    now = [100.]
    ready = False
    checks, attempts = [], []
    async with party(tmp_path, clock=lambda: now[0]) as table:
        def status():
            checks.append(now[0])
            if blocked == 'status_error' and not ready:
                raise OSError('读取合成输出状态失败')
            return dict(quiet=blocked == 'quiet' and not ready, configured=blocked != 'unconfigured' or ready)

        async def speak(text, *, current):
            assert current()
            attempts.append(text)
            if blocked in {'busy', 'muted'} and not ready:
                return {'status': blocked}
            table.spoken.append(text)
            return {'status': 'accepted'}

        table.effects.speech_status, table.effects.speak = status, speak
        await begin_play(table.service)
        await table.service.effect_task
        assert not table.spoken
        phase = table.service.game.public()
        original_checks, original_attempts, original_writes = len(checks), len(attempts), len(table.writes)

        ready = True
        now[0] += .5
        await table.service.step()
        assert len(checks) == original_checks and len(attempts) == original_attempts
        now[0] += 2
        await table.service.step()
        await table.service.effect_task
        assert table.spoken == [phase['host_script']['text']]
        assert len(table.writes) == original_writes
        assert table.service.game.revision == phase['revision']
        app = FastAPI()
        app.include_router(create_router(None, None, None, tmp_path, owner=table.service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            host = (await client.get('/api/games/werewolf/host')).json()
        assert host['game']['host_script'] == phase['host_script']
        assert host['effects']['voice'] == 'requested' and host['effects']['physical_audio_verified'] is False

        now[0] += 3
        await table.service.step()
        await table.service.effect_task
        assert len(attempts) == original_attempts + 1 and len(table.spoken) == 1


@pytest.mark.asyncio
async def test_recovery_skips_retired_and_uncertain_prompts(tmp_path):
    now = [100.]
    quiet = True
    attempts = []
    async with party(tmp_path, clock=lambda: now[0]) as table:
        table.effects.speech_status = lambda: dict(quiet=quiet, configured=True)

        async def uncertain(text, *, current):
            assert current()
            attempts.append(text)
            return {'status': 'unconfirmed'}

        table.effects.speak = uncertain
        await begin_play(table.service)
        await table.service.effect_task
        retired = table.service.game.public()['host_script']
        assert not attempts
        quiet = False
        now[0] = table.service.game.deadline + .1
        await table.service.step()
        await table.service.effect_task
        current = table.service.game.public()['host_script']
        assert current != retired and attempts == [current['text']]
        assert table.effects.voice_state == 'unconfirmed'

        now[0] += 3
        await table.service.step()
        await table.service.control('pause')
        await table.service.control('resume')
        await table.service.effect_task
        assert attempts == [current['text']]


@pytest.mark.asyncio
async def test_paused_before_first_output_can_narrate_the_current_phase_on_resume(tmp_path):
    async with party(tmp_path) as table:
        await begin_play(table.service)
        # No effect task has run yet; the host pauses before any output attempt.
        await table.service.control('pause')
        await table.service.effect_task
        assert not table.spoken
        await table.service.control('resume')
        await table.service.effect_task
        assert table.spoken == [table.service.game.public()['host_script']['text']]


@pytest.mark.asyncio
async def test_lobby_readiness_does_not_interrupt_unchanged_public_prompt(tmp_path, monkeypatch):
    async with party(tmp_path) as table:
        speaking, release = asyncio.Event(), asyncio.Event()
        speak = table.effects.speak
        attempts = []

        async def slow_prompt(text, *, current):
            attempts.append(text)
            speaking.set()
            await release.wait()
            return await speak(text, current=current)

        monkeypatch.setattr(table.effects, 'speak', slow_prompt)
        try:
            await table.service.step()
            await asyncio.wait_for(speaking.wait(), 1)
            await table.service.ready(table.tokens[1], False)
            await table.service.step()
            release.set()
            await asyncio.wait_for(table.service.effect_task, 1)
            prompt = table.service.game.public()['host_script']['text']
            assert attempts == table.spoken == [prompt]
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['scene', 'restore'])
async def test_slow_light_io_does_not_hold_the_current_public_narration(tmp_path, monkeypatch, stage):
    async with party(tmp_path) as table:
        entered, release = asyncio.Event(), asyncio.Event()
        read = table.lights._read

        async def slow_read(*args, **kwargs):
            entered.set()
            await release.wait()
            return await read(*args, **kwargs)

        if stage == 'restore':
            await begin_play(table.service)
            await table.service.effect_task
            table.spoken.clear()
        with monkeypatch.context() as patch:
            patch.setattr(table.lights, '_read', slow_read)
            try:
                if stage == 'scene':
                    await begin_play(table.service)
                else:
                    await table.service.control('end')
                await asyncio.wait_for(entered.wait(), .5)
                await until(lambda: bool(table.spoken))
                assert table.spoken == [table.service.game.public()['host_script']['text']]
                assert not table.service.effect_task.done()
            finally:
                release.set()
                await table.service.effect_task
        assert table.writes
        assert all(json.loads(write.content)['entity_id'] == 'light.simulated' for write in table.writes)


@pytest.mark.asyncio
async def test_next_phase_narrates_before_previous_light_cleanup_finishes(tmp_path, monkeypatch):
    """旧阶段的灯光仍在收尾时，主持可以读出当前阶段的公共提示。"""
    async with party(tmp_path) as table:
        reading, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        read = table.lights._read

        async def read_with_slow_cleanup(*args, **kwargs):
            if not reading.is_set():
                reading.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            return await read(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(table.lights, '_read', read_with_slow_cleanup)
            try:
                await begin_play(table.service)
                previous = table.service.effect_task
                await asyncio.wait_for(reading.wait(), .5)
                first = table.service.game.public()['host_script']['text']
                await until(lambda: table.spoken == [first])

                while table.service.game.phase in {'wolves', 'seer', 'witch'}:
                    await table.service.control('advance')
                assert table.service.game.phase == 'dawn'
                second = table.service.game.public()['host_script']['text']
                assert second != first
                await asyncio.wait_for(cleaning.wait(), .5)
                await until(lambda: table.spoken == [first, second])
                assert not previous.done() and not table.writes
                assert not table.service.effect_task.done()

                release.set()
                await asyncio.wait_for(table.service.effect_task, .5)
                assert [json.loads(write.content)['brightness'] for write in table.writes] == [179]
            finally:
                release.set()
                await asyncio.gather(*table.service.effect_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('advance_to', ['seer', 'dawn'])
async def test_phase_change_keeps_claimed_light_write_until_readback(tmp_path, monkeypatch, advance_to):
    async with party(tmp_path) as table:
        sent, release = asyncio.Event(), asyncio.Event()
        send = HADeviceWriter.send
        scene = table.lights.scene
        scenes = []

        async def observed_scene(night, revision, current):
            scenes.append(night)
            await scene(night, revision, current)

        async def delay_night_receipt(writer, desired):
            outcome = await send(writer, desired)
            if desired.get('brightness') == 46:
                sent.set()
                await release.wait()
            return outcome

        monkeypatch.setattr(HADeviceWriter, 'send', delay_night_receipt)
        monkeypatch.setattr(table.lights, 'scene', observed_scene)
        try:
            await begin_play(table.service)
            assert table.service.game.phase == 'wolves'
            await asyncio.wait_for(sent.wait(), 1)
            while table.service.game.phase != advance_to:
                await table.service.control('advance')
            script = table.service.game.public()['host_script']['text']
            await until(lambda: script in table.spoken)
            assert len(table.writes) == 1 and not table.service.effect_task.done()
            release.set()
            await asyncio.wait_for(table.service.effect_task, 1)

            while table.service.game.phase in {'wolves', 'seer', 'witch'}:
                await table.service.control('advance')
                await asyncio.wait_for(table.service.effect_task, 1)
            assert table.service.game.phase == 'dawn'
            assert scenes == [True, False]
            writes = [json.loads(call.content)['brightness'] for call in table.writes]
            assert writes == [46, 179], table.lights.status()
            assert not table.lights.status()['recovery_pending']
            await table.service.control('end')
            await asyncio.wait_for(table.service.effect_task, 1)
            assert not table.lights.journal.exists()
            row = next(row for row in table.rows if row['entity_id'] == 'light.simulated')
            assert row['state'] == 'off'
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['end', 'close'])
async def test_ending_or_closing_restores_lights_before_previous_speech_cleanup(tmp_path, monkeypatch, action):
    """结束或关闭后，扬声器收尾不延误灯光恢复，同一扬声器仍串行。"""
    async with party(tmp_path) as table:
        speaking, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        speak = table.effects.speak

        async def speak_with_slow_cleanup(text, *, current):
            receipt = await speak(text, current=current)
            if not speaking.is_set():
                speaking.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            return receipt

        closing = None
        row = next(row for row in table.rows if row['entity_id'] == 'light.simulated')
        with monkeypatch.context() as patch:
            patch.setattr(table.effects, 'speak', speak_with_slow_cleanup)
            try:
                await begin_play(table.service)
                previous = table.service.effect_task
                first = table.service.game.public()['host_script']['text']
                await asyncio.wait_for(speaking.wait(), .5)
                await until(lambda: table.effects.last_scene is True)
                assert row['attributes']['brightness'] == 46

                if action == 'end':
                    await table.service.control('advance')
                    await asyncio.wait_for(cleaning.wait(), .5)
                    assert not table.service.effect_task.done()
                    await table.service.control('end')
                else:
                    closing = asyncio.create_task(table.service.close_game())
                await asyncio.wait_for(cleaning.wait(), .5)
                await until(lambda: not table.lights.records)
                assert row['state'] == 'off' and not table.lights.journal.exists()
                assert table.spoken == [first] and not previous.done()

                release.set()
                if closing is not None:
                    assert (await asyncio.wait_for(closing, .5))['active'] is False
                    assert table.spoken == [first]
                else:
                    await asyncio.wait_for(table.service.effect_task, .5)
                    assert table.spoken == [first, table.service.game.public()['host_script']['text']]
            finally:
                release.set()
                tasks = set(table.service.effect_tasks)
                if closing is not None:
                    tasks.add(closing)
                await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_restore_requests_share_the_original_recovery_responsibility(tmp_path, monkeypatch):
    async with party(tmp_path) as table:
        await begin_play(table.service)
        await table.service.effect_task
        read = table.lights._read
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def slow_read(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = await read(*args, **kwargs)
            # Hold only the initial snapshots so two old restores would both
            # retain the same responsibility before either commits its release.
            if calls <= 2:
                entered.set()
                await release.wait()
            return result

        tasks = []
        with monkeypatch.context() as patch:
            patch.setattr(table.lights, '_read', slow_read)
            try:
                tasks.append(asyncio.create_task(table.lights.restore()))
                await asyncio.wait_for(entered.wait(), .5)
                tasks.append(asyncio.create_task(table.lights.restore()))
                await asyncio.sleep(0)
                release.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                assert all(isinstance(result, dict) for result in results), results
                assert all(not result['recovery_pending'] for result in results)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        assert not table.lights.records and not table.lights.journal.exists()
        assert next(row for row in table.rows if row['entity_id'] == 'light.simulated')['state'] == 'off'


@pytest.mark.asyncio
async def test_interrupted_scene_is_reconciled_even_when_the_next_phase_returns_to_day(tmp_path, monkeypatch):
    async with party(tmp_path) as table:
        await begin_play(table.service)
        await table.service.effect_task
        while table.service.game.phase != 'discussion':
            await table.service.control('advance')
            await table.service.effect_task
        row = next(row for row in table.rows if row['entity_id'] == 'light.simulated')
        assert row['attributes']['brightness'] == 179
        scene = table.lights.scene
        entered, release = asyncio.Event(), asyncio.Event()

        async def after_reported_scene(night, revision, current):
            await scene(night, revision, current)
            if night:
                entered.set()
                await release.wait()

        with monkeypatch.context() as patch:
            patch.setattr(table.lights, 'scene', after_reported_scene)
            try:
                await table.service.control('advance')  # Discussion -> vote.
                await table.service.effect_task
                await table.service.control('advance')
                await table.service.effect_task
                assert table.service.game.phase == 'vote_reveal' and row['attributes']['brightness'] == 179
                await table.service.control('advance')
                await asyncio.wait_for(entered.wait(), .5)
                assert row['attributes']['brightness'] == 46
                while table.service.game.phase in {'wolves', 'seer', 'witch'}:
                    await table.service.control('advance')
                await asyncio.wait_for(table.service.effect_task, .5)
                assert table.service.game.phase == 'dawn'
                assert row['attributes']['brightness'] == 179
            finally:
                release.set()
                await asyncio.gather(*table.service.effect_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('replaced', ['scene', 'room'])
async def test_queued_restore_api_cannot_restore_a_replaced_scene_or_room(tmp_path, replaced):
    async with party(tmp_path) as table:
        await begin_play(table.service)
        await table.service.effect_task
        await table.service.control('end')
        await table.service.effect_task
        # Retain a real pending scene for the restoration port to own.
        await table.lights.select('SYNTHETIC-recovery-scene', [table.target])
        await table.lights.scene(True, 100, lambda: True)
        app = FastAPI()
        app.include_router(create_router(table.core, None, None, tmp_path, owner=table.service))
        waiting = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://host.invalid',
                                    headers={'Origin': 'http://host.invalid'}) as client:
            await table.lights.operation_lock.acquire()
            try:
                waiting = asyncio.create_task(client.post('/api/games/werewolf/restore-lights'))
                await asyncio.sleep(0)
                before = len(table.writes)
                previous = table.lights.session if replaced == 'scene' else table.service.session
                if replaced == 'scene':
                    table.lights.session = 'replacement-scene'
                else:
                    table.service.session = 'replacement-room'
            finally:
                table.lights.operation_lock.release()
            response = await asyncio.wait_for(waiting, .5)
            assert response.status_code == 409
            assert len(table.writes) == before and table.target in table.lights.records
            if replaced == 'scene':
                table.lights.session = previous
            else:
                table.service.session = previous
        await table.lights.restore()


@pytest.mark.asyncio
async def test_light_failure_leaves_recovery_visible_without_suppressing_the_public_prompt(tmp_path, monkeypatch):
    async with party(tmp_path) as table:
        async def cannot_save():
            raise OSError('SYNTHETIC unavailable scene storage')
        # Keep the normal port signature, with failure before a device send.
        async def scene(*args, **kwargs):
            await cannot_save()
        monkeypatch.setattr(table.lights, 'scene', scene)
        await begin_play(table.service)
        await table.service.effect_task
        assert table.spoken == [table.service.game.public()['host_script']['text']]
        assert table.service.public()['effects']['lights']['recovery_pending']
        assert not table.writes


@pytest.mark.asyncio
async def test_pause_cleans_up_both_outputs_and_never_replays_an_uncertain_voice_send(tmp_path, monkeypatch):
    async with party(tmp_path) as table:
        reading, speaking, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        read = table.lights._read

        async def slow_read(*args, **kwargs):
            reading.set()
            await release.wait()
            return await read(*args, **kwargs)

        async def slow_speak(text, *, current):
            table.spoken.append(text)
            speaking.set()
            await release.wait()
            return {'status': 'accepted'}

        with monkeypatch.context() as patch:
            patch.setattr(table.lights, '_read', slow_read)
            patch.setattr(table.effects, 'speak', slow_speak)
            try:
                await begin_play(table.service)
                await asyncio.wait_for(asyncio.gather(reading.wait(), speaking.wait()), .5)
                await table.service.control('pause')
                await asyncio.wait_for(table.service.effect_task, .5)
                assert table.effects.voice_state == 'unconfirmed' and not table.writes
                release.set()
                await table.service.control('resume')
                await asyncio.wait_for(table.service.effect_task, .5)
                assert len(table.spoken) == 1 and len(table.writes) == 1
                assert table.effects.voice_state == 'unconfirmed'
            finally:
                release.set()
                await asyncio.gather(*table.service.effect_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_restore_api_rechecks_room_after_an_awaited_light_read(tmp_path, monkeypatch):
    async with party(tmp_path) as table:
        await begin_play(table.service)
        await table.service.effect_task
        await table.service.control('end')
        await table.service.effect_task
        await table.lights.select('SYNTHETIC-recovery-scene', [table.target])
        await table.lights.scene(True, 100, lambda: True)
        read = table.lights._read
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_snapshot(*args, **kwargs):
            result = await read(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        app = FastAPI()
        app.include_router(create_router(table.core, None, None, tmp_path, owner=table.service))
        previous = table.service.session
        with monkeypatch.context() as patch:
            patch.setattr(table.lights, '_read', delayed_snapshot)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://host.invalid',
                                        headers={'Origin': 'http://host.invalid'}) as client:
                task = asyncio.create_task(client.post('/api/games/werewolf/restore-lights'))
                try:
                    await asyncio.wait_for(entered.wait(), .5)
                    before = len(table.writes)
                    table.service.session = 'replacement-room'
                    release.set()
                    response = await asyncio.wait_for(task, .5)
                    assert response.status_code == 409 and len(table.writes) == before
                    assert table.target in table.lights.records
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    table.service.session = previous
        await table.lights.restore()


@pytest.mark.asyncio
async def test_duplicate_restore_api_returns_restored_after_the_first_request_completes(tmp_path, monkeypatch):
    async with party(tmp_path) as table:
        await begin_play(table.service)
        await table.service.effect_task
        await table.service.control('end')
        await table.service.effect_task
        await table.lights.select('SYNTHETIC-recovery-scene', [table.target])
        await table.lights.scene(True, 100, lambda: True)
        read = table.lights._read
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_snapshot(*args, **kwargs):
            result = await read(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        app = FastAPI()
        app.include_router(create_router(table.core, None, None, tmp_path, owner=table.service))
        tasks = []
        with monkeypatch.context() as patch:
            patch.setattr(table.lights, '_read', delayed_snapshot)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://host.invalid',
                                        headers={'Origin': 'http://host.invalid'}) as client:
                try:
                    tasks.append(asyncio.create_task(client.post('/api/games/werewolf/restore-lights')))
                    await asyncio.wait_for(entered.wait(), .5)
                    tasks.append(asyncio.create_task(client.post('/api/games/werewolf/restore-lights')))
                    await asyncio.sleep(0)
                    before = len(table.writes)
                    release.set()
                    responses = await asyncio.gather(*tasks)
                    assert [response.status_code for response in responses] == [200, 200]
                    assert all(not response.json()['recovery_pending'] for response in responses)
                    assert len(table.writes) == before + 1
                finally:
                    release.set()
                    await asyncio.gather(*tasks, return_exceptions=True)
