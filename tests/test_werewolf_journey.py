"""Complete original host/phone/TV/effects chain; ASGI and HA are inert fakes."""
import asyncio
from contextlib import AsyncExitStack
from functools import partial
import json
from unittest.mock import patch

from fastapi import FastAPI
import httpx
import pytest

from homeplane.werewolf import SPEAKER_SECONDS, WerewolfGame
from homeplane.werewolf_api import create_router
from homeplane.werewolf_effects import HostEffects, SceneLights
from homeplane.werewolf_service import WerewolfService, phone_app
from tests.test_homeplane_actions import close
from tests.test_homeplane_device_controls import rig
from tests.test_werewolf import Clock, FixedRoles
from tests.test_werewolf_camera import Embodied
from tests.test_werewolf_service import NoNetwork


async def check_speaking_turn(service, host, tv, phones, camera, spoken, writes):
    await service.step()
    advisory = (await tv.get('/api/public')).json()
    assert advisory['camera_hints'][0]['seat'] == 4 and advisory['camera_hints'][0]['hand_raised']
    assert advisory['game']['speaker_seat'] is None
    deadline, lights_before, announcements_before = service.game.deadline, len(writes), len(spoken)
    request = dict(action='speaker', seat=4, session=service.session, revision=advisory['game']['revision'],
                   event_id='SYNTHETIC-speaker-first')
    path = '/api/games/werewolf/control'
    called = await host.post(path, json=request)
    assert called.status_code == 200 and called.json()['game']['speaker_seat'] == 4
    await service.effect_task
    assert spoken[announcements_before:] == ['请 4 号发言。']
    assert (await host.post(path, json=request)).json()['game'] == called.json()['game']
    stale = await host.post(path, json={**request, 'seat': 5, 'event_id': 'SYNTHETIC-speaker-stale'})
    assert stale.status_code == 409 and stale.json()['detail'] == 'phase_changed'
    assert len(writes) == lights_before and service.game.deadline == deadline
    camera.report['tracking_current'] = False
    await service.step()
    report = (await tv.get('/api/public')).json()
    assert report['game'] == called.json()['game'] and report['camera_hints'] == []
    assert 'speaker_seats' not in report
    assert all('role' not in player for player in report['game']['players'])
    for phone in (phones[4], phones[5]):
        private = (await phone.get('/api/player')).json()
        assert private['public'] == report['game'] and private['actions'] == []
    await host.post(path, json=dict(action='speaker', seat=None, session=service.session,
                                    revision=report['game']['revision'], event_id='SYNTHETIC-speaker-clear'))
    await service.effect_task
    assert service.game.speaker_seat is None and spoken[announcements_before:] == ['请 4 号发言。', '继续自由发言。']
    assert len(writes) == lights_before and service.game.deadline == deadline


async def check_rematch(service, host, tv, phones, lights, lamp, spoken, writes):
    previous = (await tv.get('/api/public')).json()['game']
    room = service.host()
    old_seat, old_choice = next(iter(service.retries))
    path = '/api/games/werewolf/control'

    def command(action, event):
        return dict(action=action, session=service.session, revision=service.game.revision, event_id=event)

    original_writes = len(writes)
    with patch.object(lights.core.actions, 'binding_fingerprint', return_value='f'*64):
        changed = await host.post(path, json=command('rematch','SYNTHETIC-changed-lamp'))
    assert changed.status_code == 409 and changed.json()['detail'] == 'scene_light_binding_changed'
    assert (await tv.get('/api/public')).json()['game'] == previous and len(writes) == original_writes

    lamp.update(state='on')
    lamp['attributes']['brightness'] = 123
    request = command('rematch', 'SYNTHETIC-second-match')
    joined = await host.post(path, json=request)
    assert joined.status_code == 200, joined.text
    next_room = joined.json()
    lobby = (await tv.get('/api/public')).json()['game']
    assert next_room['session']==room['session'] and next_room['phone_url']==room['phone_url']
    assert next_room['display_url']==room['display_url'] and next_room['join_code']==room['join_code']
    assert lobby['phase']=='lobby' and lobby['revision']>previous['revision']
    assert lobby['discussion_mode']==previous['discussion_mode'] and 'speaking_turn' not in lobby
    assert lobby['phase_name']=='重新准备'
    assert lobby['host_script']['id']>previous['host_script']['id']
    assert not lobby['vote_result'] and lobby['winner'] is None and lobby['round']==0
    assert all(player['alive'] and not player['ready'] and 'role' not in player for player in lobby['players'])
    assert [(p['seat'],p['name']) for p in lobby['players']]==[(p['seat'],p['name']) for p in previous['players']]
    await service.effect_task
    assert spoken[-1]==lobby['host_script']['text'] and len(writes)==original_writes

    private = (await phones[old_seat].get('/api/player', headers={'X-Friday-Choice':old_choice})).json()
    assert private['role'] is None and private['submitted'] is None and private['choice_version']==0
    assert private['choice_receipt']==dict(event_id=old_choice,recorded=False)
    stale = await phones[1].post('/api/ready',json=dict(ready=True,lobby_revision=0))
    assert stale.status_code==409 and stale.json()['detail']=='phase_changed'
    for phone in phones.values():
        accepted = await phone.post('/api/ready',json=dict(ready=True,lobby_revision=lobby['lobby_revision']))
        assert accepted.status_code==200
    replayed = (await host.post(path,json=request)).json()
    assert replayed['game']['phase']=='lobby' and all(p['ready'] for p in replayed['game']['players'])

    deal = (await host.post(path,json=command('start','SYNTHETIC-second-deal'))).json()['game']
    assert deal['phase']=='identity' and deal['identities_confirmed']==0
    assert all('role' not in p for p in (await tv.get('/api/public')).json()['game']['players'])
    for phone in phones.values():
        assert (await phone.post('/api/identity',json=dict(revision=deal['revision']))).status_code==200
    await host.post(path,json=command('advance','SYNTHETIC-second-night'))
    await service.effect_task
    assert service.game.phase=='wolves' and lamp['attributes']['brightness']==46
    await host.post(path,json=command('end','SYNTHETIC-second-end'))
    await service.effect_task
    assert lamp['state']=='on' and lamp['attributes']['brightness']==123 and not lights.records


async def check_ordered_turns(service, host, tv, phones, spoken, writes, first):
    current, next_seat = first['speaker_seat'], first['speaking_turn']['next_seat']
    third = next(p['seat'] for p in first['players'] if p['alive'] and p['seat'] not in {current, next_seat})
    before_writes = len(writes)
    assert current == 1 and next_seat == 2 and first['seconds_left'] == SPEAKER_SECONDS
    path = '/api/speaker-done'
    request = dict(revision=first['revision'])
    assert (await tv.post(path, json=request)).status_code == 403
    denied = await phones[next_seat].post(path, json=request)
    assert denied.status_code == 409 and denied.json()['detail'] == 'speaker_unavailable'

    async def control(action, state):
        return await host.post('/api/games/werewolf/control', json=dict(
            action=action, session=service.session, revision=state['revision'], event_id=f'SYNTHETIC-turn-{action}'))

    service.clock.now += 7
    paused = (await control('pause', first)).json()['game']
    assert paused['paused'] and paused['seconds_left'] == SPEAKER_SECONDS - 7
    service.clock.now += 60
    await service.step()
    assert (await tv.get('/api/public')).json()['game'] == paused
    assert (await phones[current].post(path,json=dict(revision=paused['revision']))).status_code == 409
    resumed = (await control('resume', paused)).json()['game']
    assert resumed['speaker_seat'] == current and resumed['seconds_left'] == paused['seconds_left']
    request = dict(revision=resumed['revision'])
    finished = await asyncio.gather(*(phones[current].post(path, json=request) for _ in range(2)))
    assert sorted(r.status_code for r in finished) == [200, 409]
    handoff = (await tv.get('/api/public')).json()['game']
    assert handoff['revision'] == resumed['revision'] + 1
    assert handoff['speaker_seat'] == next_seat and handoff['seconds_left'] == SPEAKER_SECONDS
    assert handoff['speaking_turn'] == dict(position=2,total=first['speaking_turn']['total'],next_seat=third)
    assert (await phones[next_seat].get('/api/player')).json()['public'] == handoff
    await service.effect_task
    assert spoken[-1] == f'请 {next_seat} 号发言。'
    service.clock.now += SPEAKER_SECONDS
    await service.step()
    timed = (await tv.get('/api/public')).json()['game']
    assert timed['speaker_seat'] == third and timed['seconds_left'] == SPEAKER_SECONDS
    assert timed['revision'] == handoff['revision'] + 1
    assert not timed['vote_result'] and len(writes) == before_writes
    await service.effect_task
    assert spoken[-1] == f'请 {third} 号发言。'
    return timed


@pytest.mark.asyncio
@pytest.mark.parametrize('count,mode',[(6,'free'),(9,'free'),(12,'free'),(6,'turns'),(12,'turns')])
async def test_complete_private_phone_match_public_host_and_scoped_lights(tmp_path,count,mode):
    core, rows, writes, *_=rig(tmp_path)
    lights=SceneLights(core,asyncio.Lock(),tmp_path/'scene.json')
    spoken=[]
    async def speak(text, *, current):
        spoken.append(text);return dict(status='accepted',physical_audio_verified=False)
    effects=HostEffects(lights,speak=speak,speech_status=lambda:dict(quiet=False,configured=True))
    camera = Embodied() if count == 6 else None
    service=WerewolfService(effects,embodied=camera,gateway_factory=NoNetwork,addresses=lambda:['192.168.50.20'],
        clock=Clock(),game_factory=partial(WerewolfGame,rng=FixedRoles()))
    script=set()
    try:
        target=(await lights.choices())[0]['id']
        host_app=FastAPI();host_app.include_router(create_router(core,None,None,tmp_path,owner=service))
        async with AsyncExitStack() as stack:
            host=await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=host_app),base_url='http://host.invalid',headers={'Origin':'http://host.invalid'}))
            created=await host.post('/api/games/werewolf/create',json=dict(players=count,address='192.168.50.20',
                lights=[target],narration=True,camera=camera is not None,discussion_mode=mode))
            assert created.status_code==200 and created.json()['game']['discussion_mode']==mode
            service.pump.cancel();await asyncio.gather(service.pump,return_exceptions=True);service.pump=None
            party=phone_app(service,service.origin)
            tv=await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=party),base_url=service.origin,headers={'Authorization':'Bearer '+service.public_token}))
            phones={}
            for seat in range(1,count+1):
                phone=await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=party,client=(f'192.168.50.{seat+50}',1000+seat)),base_url=service.origin))
                joined=await phone.post('/api/join',json=dict(code=service.join_code,seat=seat,name=f'合成玩家{seat}'))
                assert joined.status_code==200
                phone.headers['Authorization']='Bearer '+joined.json()['token']
                assert (await phone.post('/api/ready',json=dict(ready=True))).status_code==200
                phones[seat]=phone
            if camera:
                # 六人局稍后会重新认牌，座位和镜头对应沿用原局。
                binding = await host.post('/api/games/werewolf/camera-bind', json=dict(
                    seat=4, camera=camera.report['camera_id'], session=camera.report['session_id'], track=7,
                    game_session=service.session, revision=service.game.revision, event_id='SYNTHETIC-bind-speaker'))
                assert binding.status_code == 200
            dealt = await host.post('/api/games/werewolf/control', json=dict(
                action='start', session=service.session, revision=service.game.revision, event_id=f'SYNTHETIC-start-{count}'))
            assert dealt.status_code == 200
            deal = (await tv.get('/api/public')).json()['game']
            assert deal['phase'] == 'identity' and deal['seconds_left'] is None
            assert deal['round'] == 0 and deal['identities_confirmed'] == 0
            assert all('role' not in p and 'identity_confirmed' not in p for p in deal['players'])
            await service.effect_task
            assert spoken[-1] == deal['host_script']['text'] and not writes
            service.clock.now += 120
            await service.step()
            assert (await tv.get('/api/public')).json()['game'] == deal

            async def first_night(event):
                return await host.post('/api/games/werewolf/control', json=dict(
                    action='advance', session=service.session, revision=deal['revision'], event_id=event))

            blocked = await first_night(f'SYNTHETIC-blocked-{count}')
            assert blocked.status_code == 409 and blocked.json()['detail'] == 'identities_pending'
            assert (await tv.post('/api/identity', json={'revision': deal['revision']})).status_code == 403
            if count == 6:
                old = deal['revision']
                for _ in range(2):
                    receipt = (await phones[1].post('/api/identity', json={'revision': old})).json()
                    assert receipt['identity_confirmed'] and receipt['public']['identities_confirmed'] == 1
                assert not (await phones[2].get('/api/player')).json()['identity_confirmed']
                reseated = await host.post('/api/games/werewolf/control', json=dict(
                    action='reseat', session=service.session, revision=old, event_id='SYNTHETIC-reseat-six'))
                assert reseated.status_code == 200 and reseated.json()['game']['phase'] == 'lobby'
                for phone in phones.values():
                    player = (await phone.get('/api/player')).json()
                    assert player['role'] is None and not player['ready']
                    assert (await phone.post('/api/ready', json={'ready': True,
                        'lobby_revision': service.game.lobby_revision})).status_code == 200
                redealt = await host.post('/api/games/werewolf/control', json=dict(
                    action='start', session=service.session, revision=service.game.revision, event_id='SYNTHETIC-redeal-six'))
                assert redealt.status_code == 200
                deal = redealt.json()['game']
                assert deal['identities_confirmed'] == 0
                late = await phones[1].post('/api/identity', json={'revision': old})
                assert late.status_code == 409 and late.json()['detail'] == 'phase_changed'
            for seat, phone in phones.items():
                private = (await phone.get('/api/player')).json()
                assert private['role'] and not private['identity_confirmed'] and not private['actions']
                confirmed = await phone.post('/api/identity', json={'revision': deal['revision']})
                assert confirmed.status_code == 200 and confirmed.json()['identity_confirmed']
                assert confirmed.json()['public']['identities_confirmed'] == seat
                assert confirmed.json()['public']['revision'] == deal['revision']
                assert confirmed.json()['public']['players'] == deal['players']
            assert not writes
            assert (await first_night(f'SYNTHETIC-first-night-{count}')).status_code == 200
            for step in range(100):
                await asyncio.gather(service.effect_task,return_exceptions=False)
                public=(await tv.get('/api/public')).json()['game']
                if public['phase'] == 'discussion' and public['round'] == 1:
                    if mode == 'turns' and public['speaking_turn']['position'] == 1:
                        public = await check_ordered_turns(service,host,tv,phones,spoken,writes,public)
                    elif mode == 'free' and camera:
                        await check_speaking_turn(service, host, tv, phones, camera, spoken, writes)
                        public = (await tv.get('/api/public')).json()['game']
                script.update(a['text']for a in public['announcements'])
                script.add(public['host_script']['text'])
                if public['phase']=='finished':break
                assert all('role'not in p and 'role_name'not in p for p in public['players'])
                private={seat:(await phone.get('/api/player')).json()for seat,phone in phones.items()}
                wolves=[s for s,p in private.items()if p['alive']and p['role']=='wolf']
                villagers=[s for s,p in private.items()if p['alive']and p['role']!='wolf']
                for seat,p in private.items():
                    if p['role']!='seer':assert 'checks'not in p
                    if p['role']!='wolf':assert 'team_choices'not in p
                    actions={a['action']:a['targets']for a in p['actions']}
                    action,chosen='pass',None
                    if 'attack'in actions:action,chosen='attack',max(villagers)
                    elif 'save'in actions:action,chosen='save',actions['save'][0]
                    elif 'check'in actions:action,chosen='check',wolves[0]
                    elif 'vote'in actions:
                        choices=[s for s in wolves if s in actions['vote']]
                        if choices:action,chosen='vote',choices[0]
                    if action not in actions:continue
                    response=await phones[seat].post('/api/choice',json=dict(action=action,target=chosen,
                        revision=public['revision'],event_id=f'SYNTHETIC_{count}_{step}_{seat}'))
                    assert response.status_code==200,response.text
                # All private choices remain absent from the public game.
                assert (await tv.get('/api/public')).json()['game']==public
                if public.get('speaking_turn', {}).get('position', 0) % 2 == 1:
                    assert (await phones[public['speaker_seat']].post('/api/speaker-done',
                        json=dict(revision=public['revision']))).status_code==200
                else:
                    assert (await host.post('/api/games/werewolf/control',json=dict(action='advance',session=service.session,revision=service.game.revision,event_id=f'SYNTHETIC-advance-{count}-{step}'))).status_code==200
            else:raise AssertionError('match did not complete within its phase budget')
            assert public['winner']=='village' and public['phase']=='finished'
            assert all('role'in p for p in public['players'])
            assert spoken and set(spoken)<=script
            assert spoken[-1]==public['host_script']['text']
            assert '号被放逐。 好人阵营获胜。'in spoken[-1]
            assert not lights.status()['recovery_pending'] and not lights.records
            assert next(row for row in rows if row['entity_id']=='light.simulated')['state']=='off'
            assert writes and all(json.loads(w.content)['entity_id']=='light.simulated'for w in writes)
            assert {json.loads(w.content).get('brightness')for w in writes}>={46,179}
            if count == 6:
                lamp = next(row for row in rows if row['entity_id']=='light.simulated')
                await check_rematch(service,host,tv,phones,lights,lamp,spoken,writes)
            assert (await host.post('/api/games/werewolf/control',json=dict(action='close',session=service.session,revision=service.game.revision,event_id=f'SYNTHETIC-close-{count}'))).json()['active'] is False
            assert (await tv.get('/api/public')).status_code==403
            assert all([(await phone.get('/api/player')).status_code==403 for phone in phones.values()])
    finally:
        await service.close()
        await close(core)
