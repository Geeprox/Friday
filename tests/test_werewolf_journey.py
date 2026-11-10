"""Complete original host/phone/TV/effects chain; ASGI and HA are inert fakes."""
import asyncio
from contextlib import AsyncExitStack
from functools import partial
import json

from fastapi import FastAPI
import httpx
import pytest

from homeplane.werewolf import WerewolfGame
from homeplane.werewolf_api import create_router
from homeplane.werewolf_effects import HostEffects, SceneLights
from homeplane.werewolf_service import WerewolfService, phone_app
from tests.test_homeplane_actions import close
from tests.test_homeplane_device_controls import rig
from tests.test_werewolf import Clock, FixedRoles
from tests.test_werewolf_service import NoNetwork


@pytest.mark.asyncio
@pytest.mark.parametrize('count',[6,9,12])
async def test_complete_private_phone_match_public_host_and_scoped_lights(tmp_path,count):
    core, rows, writes, *_=rig(tmp_path)
    lights=SceneLights(core,asyncio.Lock(),tmp_path/'scene.json')
    spoken=[]
    async def speak(text, *, current):
        spoken.append(text);return dict(status='accepted',physical_audio_verified=False)
    effects=HostEffects(lights,speak=speak,speech_status=lambda:dict(quiet=False,configured=True))
    service=WerewolfService(effects,gateway_factory=NoNetwork,addresses=lambda:['192.168.50.20'],
        clock=Clock(),game_factory=partial(WerewolfGame,rng=FixedRoles()))
    script=set()
    try:
        target=(await lights.choices())[0]['id']
        await service.create(count,address='192.168.50.20',lights=[target],narration=True)
        service.pump.cancel();await asyncio.gather(service.pump,return_exceptions=True);service.pump=None
        party=phone_app(service,service.origin)
        host_app=FastAPI();host_app.include_router(create_router(core,None,None,tmp_path,owner=service))
        async with AsyncExitStack() as stack:
            host=await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=host_app),base_url='http://host.invalid',headers={'Origin':'http://host.invalid'}))
            tv=await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=party),base_url=service.origin,headers={'Authorization':'Bearer '+service.public_token}))
            phones={}
            for seat in range(1,count+1):
                phone=await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=party,client=(f'192.168.50.{seat+50}',1000+seat)),base_url=service.origin))
                joined=await phone.post('/api/join',json=dict(code=service.join_code,seat=seat,name=f'合成玩家{seat}'))
                assert joined.status_code==200
                phone.headers['Authorization']='Bearer '+joined.json()['token']
                assert (await phone.post('/api/ready',json=dict(ready=True))).status_code==200
                phones[seat]=phone
            assert (await host.post('/api/games/werewolf/control',json=dict(action='start',session=service.session,revision=service.game.revision,event_id=f'SYNTHETIC-start-{count}'))).status_code==200
            for step in range(64):
                await asyncio.gather(service.effect_task,return_exceptions=False)
                public=(await tv.get('/api/public')).json()['game']
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
            assert (await host.post('/api/games/werewolf/control',json=dict(action='close',session=service.session,revision=service.game.revision,event_id=f'SYNTHETIC-close-{count}'))).json()['active'] is False
            assert (await tv.get('/api/public')).status_code==403
            assert all([(await phone.get('/api/player')).status_code==403 for phone in phones.values()])
    finally:
        await service.close()
        await close(core)
