"""ASGI-only phone/TV/host acceptance; no LAN listener or household I/O."""

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from homeplane.werewolf_api import create_router
from homeplane.werewolf_effects import HostEffects, SceneLights
from homeplane.werewolf_service import Choice, Join, WerewolfService, phone_app
from homeplane.werewolf_service import PhoneGateway
from tests.test_homeplane_actions import close
from tests.test_homeplane_device_controls import rig


class SilentLights:
    def __init__(self):
        self.calls = []
        self.session = None
    async def choices(self):
        return []
    async def select(self, session, targets):
        self.session = session
        self.calls.append(("select", targets))
    async def scene(self, night, revision, current):
        if current():
            self.calls.append(("scene", night))
    async def restore(self, *, current=lambda: True):
        if current():
            self.calls.append(("restore",))
            self.session = None
        return self.status()
    def status(self):
        return dict(selected=0, recovery_pending=False, receipts=[])


class NoNetwork:
    opened = closed = 0
    async def open(self, address, owner):
        type(self).opened += 1
        return "http://192.168.50.20:12345"
    async def close(self):
        type(self).closed += 1


async def owner():
    effects = HostEffects(SilentLights())
    service = WerewolfService(effects, gateway_factory=NoNetwork, addresses=lambda: ["192.168.50.20"])
    await service.create(6, address="192.168.50.20")
    return service


@pytest.mark.asyncio
async def test_phone_private_cards_public_tv_and_no_host_or_household_routes():
    service = await owner()
    app = phone_app(service, service.origin)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=service.origin) as client:
            tokens = []
            for seat in range(1, 7):
                joined = await client.post("/api/join", json=dict(code=service.join_code, seat=seat, name=f"玩家{seat}"))
                assert joined.status_code == 200
                token = joined.json()["token"]
                tokens.append(token)
                assert joined.json()["player"]["role"] is None
                assert (await client.post("/api/ready", json={"ready": True}, headers={"Authorization": "Bearer " + token})).status_code == 200
            await service.control("start")
            public = await client.get("/api/public", headers={"Authorization": "Bearer " + service.public_token})
            assert public.status_code == 200
            assert all("role" not in p for p in public.json()["game"]["players"])
            assert "join_code" not in public.json() and "camera_candidates" not in public.json()
            assert "receipts" not in public.json()["effects"]["lights"]
            for token in tokens:
                response = await client.get("/api/player", headers={"Authorization": "Bearer " + token})
                assert response.status_code == 200 and response.json()["role"]
                assert "no-store" in response.headers["cache-control"]
                assert "camera=()" in response.headers["permissions-policy"]
            # Display/other credentials cannot read somebody else's private card.
            assert (await client.get("/api/player", headers={"Authorization": "Bearer " + service.public_token})).status_code == 403
            assert (await client.get("/api/player?token=" + tokens[0])).status_code == 403
            assert (await client.get("/api/player", headers={"Authorization": "Bearer " + "a" * 64})).status_code == 403
            for path in ("/api/games/werewolf/control", "/api/embodied/start", "/api/actions/execute"):
                assert (await client.post(path, json={"action": "end"})).status_code == 404
            assert (await client.get("/api/public", headers={"Host": "evil.example"})).status_code == 403
            assert (await client.post("/api/ready", json={"ready": True}, headers={"Origin": "https://evil.example"})).status_code == 403
            assert (await client.post("/api/ready", content=b"a" * 9000)).status_code == 413
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_lobby_tokens_revoked_and_choice_retries_are_idempotent():
    service = await owner()
    try:
        old = await service.join(Join(code=service.join_code, seat=1, name="旧手机"))
        await service.control("remove", seat=1)
        with pytest.raises(ValueError, match="access_required"):
            await service.private(old["token"])
        credentials = {}
        for seat in range(1, 7):
            joined = await service.join(Join(code=service.join_code, seat=seat, name=str(seat)))
            credentials[seat] = joined["token"]
            await service.ready(joined["token"], True)
        await service.control("start")
        wolf = next(s for s, p in service.game.players.items() if p.role == "wolf")
        choice = Choice(action="attack", target=6, revision=service.game.revision, event_id="retry_event_0001")
        first = await service.choose(credentials[wolf], choice)
        assert first["submitted"] == ("attack", 6)
        await service.control("advance")
        await service.choose(credentials[wolf], choice)  # Lost ACK, already committed.
        assert not service.game.choices  # No choice leaks into the next phase.
        with pytest.raises(ValueError, match="event_conflict"):
            await service.choose(credentials[wolf], choice.model_copy(update={"target": 5}))
        public_token = service.public_token
        await service.close_game()
        assert service.public() == {"active": False} and not service.public_access(public_token)
        with pytest.raises(ValueError, match="access_required"):
            await service.private(credentials[wolf])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_host_command_origin_and_listener_not_opened_on_inspection():
    service = WerewolfService(HostEffects(SilentLights()), gateway_factory=NoNetwork, addresses=lambda: [])
    app = FastAPI()
    app.include_router(create_router(None, None, None, ".", owner=service))
    before = NoNetwork.opened
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/games/werewolf/host")).json() == {"active": False}
            assert NoNetwork.opened == before
            assert (await client.post("/api/games/werewolf/create", json={"players": 6})).status_code == 403
            created = await client.post("/api/games/werewolf/create", json={"players": 6}, headers={"Origin": "http://testserver"})
            assert created.status_code == 200
            assert NoNetwork.opened == before  # Local-only lobby requested.
            assert (await client.post("/api/games/werewolf/create", json={"players": 6}, headers={"Origin": "http://testserver"})).status_code == 409
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_gateway_join_budget_and_strict_inputs():
    service = await owner()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=phone_app(service, service.origin)), base_url=service.origin) as client:
            for _ in range(10):
                response = await client.post("/api/join", json=dict(code="000000", seat=1, name="test"))
                assert response.status_code == 409
            assert (await client.post("/api/join", json=dict(code=service.join_code, seat=1, name="test"))).status_code == 429
        with pytest.raises(ValueError):
            Join(code=service.join_code, seat=True, name="test")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_shared_address_player_polling_isolated_by_verified_credential():
    service = await owner()
    try:
        tokens = [(await service.join(Join(code=service.join_code, seat=i, name=f'SIMULATED {i}')))['token']
                  for i in range(1, 7)]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=phone_app(service, service.origin)),
                                    base_url=service.origin) as client:
            # Six phones behind one address exceed the old shared 150/min
            # budget during an ordinary minute of one-second polling.
            for token in tokens:
                for _ in range(30):
                    response = await client.get('/api/player', headers={'Authorization':'Bearer '+token})
                    assert response.status_code == 200
            for _ in range(120):
                assert (await client.get('/api/player', headers={'Authorization':'Bearer '+tokens[0]})).status_code == 200
            limited = await client.get('/api/player', headers={'Authorization':'Bearer '+tokens[0]})
            assert limited.status_code == 429 and 1 <= int(limited.headers['retry-after']) <= 60
            assert limited.headers['cache-control'] == 'no-store'
            assert (await client.get('/api/player', headers={'Authorization':'Bearer '+tokens[1]})).status_code == 200
            # A changing invalid credential cannot manufacture fresh buckets.
            for i in range(150):
                invalid = await client.get('/api/player', headers={'Authorization':'Bearer '+format(i,'064x')})
                assert invalid.status_code == 403
            assert (await client.get('/api/player', headers={'Authorization':'Bearer '+'f'*64})).status_code == 429
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_narrator_only_uses_public_script_and_preserves_quiet_mode():
    calls = []
    async def speak(text, *, current):
        calls.append(text)
        return {"status": "accepted"}
    status = dict(quiet=True, configured=True)
    effects = HostEffects(SilentLights(), speak=speak, speech_status=lambda: status)
    effects.narration = True
    public = dict(phase="wolves", paused=False, revision=1, announcements=[dict(id=1, text="狼人请行动。")])
    await effects.apply(public, lambda: True)
    assert not calls and effects.voice_state == "quiet"
    status["quiet"] = False
    public["announcements"].append(dict(id=2, text="预言家请行动。"))
    await effects.apply(public, lambda: True)
    await effects.apply(public, lambda: True)
    assert calls == ["预言家请行动。"]


@pytest.mark.asyncio
async def test_camera_hints_require_explicit_binding_and_cannot_cast_votes():
    class Embodied:
        process = SimpleNamespace(returncode=None)
        report = dict(status="live", scene_visible=True, tracking_current=True, frame_age_ms=30, camera_id="cam_1234567890", session_id="SYNTHETIC",
                      room=dict(registration=dict(status="locked"), seats=[], pointing=[]),
                      people=[dict(id=7, box=dict(x=.1,y=.05,w=.4,h=.9,confidence=.95),
                          joints=dict(nose=dict(x=.3,y=.4,confidence=.9),leftWrist=dict(x=.2,y=.1,confidence=.9)))])
        async def request(self, *_args, **_kwargs):
            return self.report
    service = await owner()
    service.embodied = Embodied()
    service.camera_enabled = True
    try:
        await service.join(Join(code=service.join_code, seat=1, name="玩家"))
        await service.bind(1, "cam_1234567890", "SYNTHETIC", 7)
        await service._camera()
        assert service.camera_hints[0]["hand_raised"]
        assert service.camera_hints[0]["vote_submitted"] is False and not service.game.choices
        service.game.phase = "wolves"
        assert service.public()["camera_hints"] == []
        service.embodied.report["frame_age_ms"] = 2000
        await service._camera()
        assert service.camera_hints == []
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_selected_lights_use_real_controller_path_and_restore_respects_manual_changes(tmp_path, manual):
    core, rows, calls, *_ = rig(tmp_path)
    try:
        lights = SceneLights(core, asyncio.Lock(), tmp_path / "scene-recovery.json")
        choices = await lights.choices()
        assert len(choices) == 1
        target = choices[0]["id"]
        await lights.select("SYNTHETIC", [target])
        assert not calls
        await lights.scene(True, 1, lambda: True)
        assert len(calls) == 1 and json.loads(calls[0].content)["brightness"] == 46
        row = next(r for r in rows if r["entity_id"] == "light.simulated")
        assert row["state"] == "on"
        if manual:
            row["attributes"]["brightness"] = 220
        result = await lights.restore()
        assert result["recovery_pending"] is False
        if manual:
            assert row["attributes"]["brightness"] == 220 and len(calls) == 1
            assert result["receipts"][-1]["status"] == "left_unchanged"
        else:
            assert row["state"] == "off" and len(calls) == 2
            assert result["receipts"][-1]["status"] == "reported_match"
        assert not (tmp_path / "scene-recovery.json").exists()
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_restart_preserves_recovery_and_does_not_write_until_explicit_restore(tmp_path):
    core, _, calls, *_ = rig(tmp_path)
    path = tmp_path / "scene.json"
    try:
        first = SceneLights(core, asyncio.Lock(), path)
        target = (await first.choices())[0]["id"]
        await first.select("SYNTHETIC", [target])
        await first.scene(True, 1, lambda: True)
        second = SceneLights(core, asyncio.Lock(), path)
        assert second.status()["recovery_pending"] and len(calls) == 1
        service = WerewolfService(HostEffects(second))
        await service.close()
        assert len(calls) == 1  # Merely opening/closing the app never restores old lights.
        await second.restore()
        assert len(calls) == 2 and not path.exists()
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_uncertain_delayed_light_send_keeps_recovery_until_observed(tmp_path):
    core, rows, calls, fault, *_ = rig(tmp_path)
    path = tmp_path / 'scene.json'
    try:
        lights = SceneLights(core, asyncio.Lock(), path)
        target = (await lights.choices())[0]['id']
        await lights.select('SYNTHETIC', [target])
        fault['send'] = 'cancel'
        with pytest.raises(asyncio.CancelledError):
            await lights.scene(True, 1, lambda: True)
        result = await lights.restore()
        assert result['recovery_pending'] and path.exists() and len(calls) == 1
        row = next(r for r in rows if r['entity_id'] == 'light.simulated')
        row['state'] = 'on'
        row['attributes']['brightness'] = 46  # Delayed synthetic HA application.
        fault['send'] = None
        result = await lights.restore()
        assert not result['recovery_pending'] and row['state'] == 'off' and len(calls) == 2
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_denied_day_scene_keeps_night_ownership_for_restoration(tmp_path):
    core, rows, calls, fault, *_ = rig(tmp_path)
    try:
        lights = SceneLights(core, asyncio.Lock(), tmp_path / 'scene.json')
        target = (await lights.choices())[0]['id']
        await lights.select('SYNTHETIC', [target])
        await lights.scene(True, 1, lambda: True)
        fault['send'] = 'deny'
        await lights.scene(False, 2, lambda: True)
        fault['send'] = None
        result = await lights.restore()
        assert not result['recovery_pending'] and len(calls) == 3
        assert next(r for r in rows if r['entity_id']=='light.simulated')['state'] == 'off'
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_phase_after_cancelled_send_keeps_original_light_recovery_responsibility(tmp_path):
    core, rows, calls, fault, *_ = rig(tmp_path)
    path = tmp_path/'scene.json'
    try:
        lights = SceneLights(core, asyncio.Lock(), path)
        target = (await lights.choices())[0]['id']
        await lights.select('SYNTHETIC', [target])
        fault['send'] = 'cancel'
        with pytest.raises(asyncio.CancelledError):
            await lights.scene(True, 1, lambda: True)
        assert lights.status()['recovery_pending']
        fault['send'] = None
        await lights.scene(False, 2, lambda: True)
        assert not lights.records[target].get('released') and len(calls)==1
        assert lights.status()['recovery_pending'] and path.exists()
        row = next(r for r in rows if r['entity_id']=='light.simulated')
        row.update(state='on');row['attributes']['brightness']=46
        await lights.restore()
        assert row['state']=='off' and len(calls)==2 and not path.exists()
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_service_pause_resume_cannot_release_uncertain_night_scene(tmp_path):
    core, rows, calls, fault, *_ = rig(tmp_path)
    lights = SceneLights(core, asyncio.Lock(), tmp_path/'scene.json')
    service = WerewolfService(HostEffects(lights))
    try:
        target = (await lights.choices())[0]['id']
        await service.create(6, lights=[target])
        service.pump.cancel();await asyncio.gather(service.pump, return_exceptions=True);service.pump=None
        for seat in range(1,7):
            joined = await service.join(Join(code=service.join_code, seat=seat, name=f'合成玩家{seat}'))
            await service.ready(joined['token'], True)
        fault['send']='cancel'
        await service.control('start')
        await asyncio.gather(service.effect_task, return_exceptions=True)
        await service.control('pause')
        await asyncio.gather(service.effect_task, return_exceptions=True)
        fault['send']=None
        await service.control('resume')
        await asyncio.gather(service.effect_task, return_exceptions=True)
        assert not lights.records[target].get('released') and len(calls)==1
        assert service.public()['effects']['lights']['recovery_pending']
        row=next(r for r in rows if r['entity_id']=='light.simulated')
        row.update(state='on');row['attributes']['brightness']=46
        await service.close_game()
        assert row['state']=='off' and len(calls)==2
    finally:
        fault['send']=None
        await service.close()
        await close(core)


@pytest.mark.asyncio
async def test_close_revokes_phone_access_even_if_light_journal_cannot_be_saved(tmp_path, monkeypatch):
    core, rows, calls, *_ = rig(tmp_path)
    lights=SceneLights(core, asyncio.Lock(), tmp_path/'scene.json')
    effects=HostEffects(lights)
    service=WerewolfService(effects, gateway_factory=NoNetwork, addresses=lambda:['192.168.50.20'])
    try:
        target=(await lights.choices())[0]['id']
        await service.create(6, address='192.168.50.20', lights=[target])
        joined=await service.join(Join(code=service.join_code,seat=1,name='合成玩家'))
        token=service.public_token;gateway=service.gateway;closed_before=gateway.closed
        def cannot_save(): raise OSError('synthetic full disk')
        with monkeypatch.context() as patch:
            patch.setattr(lights,'_save',cannot_save)
            result=await service.close_game()
        assert result['active'] is False and service.game is None and service.gateway is None
        assert service.pump is None and service.effect_task is None and not service.credentials
        assert not service.public_access(token) and gateway.closed==closed_before+1
        assert lights.status()['recovery_pending'] and target in lights.records
        with pytest.raises(ValueError,match='player_access_required'):
            await service.private(joined['token'])
        await lights.restore()
        assert not lights.status()['recovery_pending']
    finally:
        await service.close()
        await close(core)


@pytest.mark.asyncio
async def test_cancelled_narration_never_stays_speaking_or_replays_same_announcement():
    entered=asyncio.Event();never=asyncio.Event();calls=[]
    async def speak(text, *, current):
        calls.append(text);entered.set();await never.wait()
    effects=HostEffects(SilentLights(),speak=speak,speech_status=lambda:dict(quiet=False,configured=True))
    effects.narration=True
    public=dict(phase='wolves',paused=False,revision=1,announcements=[dict(id=1,text='狼人请行动。')])
    task=asyncio.create_task(effects.apply(public,lambda:True))
    await asyncio.wait_for(entered.wait(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert effects.voice_state=='unconfirmed'
    await effects.apply(public,lambda:True)
    assert calls==['狼人请行动。']


@pytest.mark.asyncio
async def test_proven_unsent_scene_can_recompute_for_the_next_phase(tmp_path, monkeypatch):
    core, rows, calls, *_=rig(tmp_path)
    lights=SceneLights(core,asyncio.Lock(),tmp_path/'scene.json')
    entered=asyncio.Event();never=asyncio.Event()
    try:
        target=(await lights.choices())[0]['id']
        await lights.select('SYNTHETIC',[target])
        async def before_claim(*args,**kwargs):
            entered.set();await never.wait()
        with monkeypatch.context() as patch:
            patch.setattr(core.actions,'execute',before_claim)
            task=asyncio.create_task(lights.scene(True,1,lambda:True))
            await asyncio.wait_for(entered.wait(),1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        assert not calls and not lights.status()['recovery_pending']
        await lights.scene(False,2,lambda:True)
        assert len(calls)==1 and json.loads(calls[0].content)['brightness']==179
        await lights.restore()
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_next_effect_waits_for_the_old_effect_cleanup():
    service=await owner()
    events=[];entered=asyncio.Event();release=asyncio.Event();never=asyncio.Event()
    try:
        service.pump.cancel();await asyncio.gather(service.pump,return_exceptions=True);service.pump=None
        for seat in range(1,7):
            joined=await service.join(Join(code=service.join_code,seat=seat,name=f'合成{seat}'))
            await service.ready(joined['token'],True)
        async def apply(public,current):
            if public['paused']:
                events.append('paused');return
            try:
                entered.set();await never.wait()
            finally:
                events.append('cleaning');await release.wait();events.append('cleaned')
        service.effects.apply=apply
        await service.control('start')
        await asyncio.wait_for(entered.wait(),1)
        await service.control('pause')
        await asyncio.sleep(0);await asyncio.sleep(0)
        assert 'cleaning' in events and 'paused' not in events
        release.set();await service.effect_task
        assert events.index('cleaned')<events.index('paused')
    finally:
        release.set();await service.close()


@pytest.mark.asyncio
async def test_cancelled_room_close_still_revokes_and_closes_phone_gateway():
    service=await owner()
    entered=asyncio.Event();cleaning=asyncio.Event();never=asyncio.Event()
    gateway=service.gateway;before=gateway.closed
    try:
        service.pump.cancel();await asyncio.gather(service.pump,return_exceptions=True)
        async def pump():
            try:
                entered.set();await never.wait()
            finally:
                cleaning.set();await never.wait()
        service.pump=asyncio.create_task(pump())
        await asyncio.wait_for(entered.wait(),1)
        closing=asyncio.create_task(service.close_game())
        await asyncio.wait_for(cleaning.wait(),1)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):await closing
        assert gateway.closed==before+1 and service.gateway is None and service.game is None
        assert service.pump is None and not service.credentials and service.public_token is None
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('record',[
    'invalid',{},dict(binding='a'*64,original={'power':'on','brightness':True},expected={'power':'off'},status='sending'),
    *[dict(binding='a'*64,original={'power':'off'},expected={'power':'off'},status='selected',attempt=value)
      for value in ['invalid',True,-1]],
])
async def test_malformed_recovery_record_is_preserved_and_never_breaks_status(tmp_path,record):
    path=tmp_path/'scene.json'
    data=json.dumps(dict(version=1,session='SYNTHETIC',records={'synthetic':record}))
    path.write_text(data)
    lights=SceneLights(None,None,path)
    assert lights.status()['recovery_pending'] and not lights.records
    with pytest.raises(ValueError,match='scene_recovery_file_unreadable'):await lights.restore()
    assert path.read_text()==data


@pytest.mark.asyncio
@pytest.mark.parametrize('stored',[[],None,1,'invalid'])
async def test_malformed_recovery_document_does_not_prevent_service_initialization(tmp_path,stored):
    path=tmp_path/'scene.json';data=json.dumps(stored);path.write_text(data)
    lights=SceneLights(None,None,path)
    assert lights.status()['recovery_pending'] and not lights.records
    with pytest.raises(ValueError,match='scene_recovery_file_unreadable'):await lights.restore()
    assert path.read_text()==data


@pytest.mark.asyncio
async def test_gateway_closes_socket_even_when_its_server_task_was_already_cancelled():
    gateway=PhoneGateway();closed=[]
    gateway.task=asyncio.get_running_loop().create_future();gateway.task.cancel()
    gateway.sock=SimpleNamespace(close=lambda:closed.append(True))
    with pytest.raises(asyncio.CancelledError):await gateway.close()
    assert closed==[True] and gateway.sock is None and gateway.task is None
