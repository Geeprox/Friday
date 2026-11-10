"""Camera-to-party evidence and timer continuity, without camera/model/network I/O."""

import asyncio
from contextlib import asynccontextmanager
import copy
from types import SimpleNamespace

import pytest

from homeplane.werewolf_effects import HostEffects
from homeplane.werewolf_service import Join, WerewolfService
from tests.test_werewolf import Clock, FixedRoles
from tests.test_werewolf_service import NoNetwork, SilentLights


def observation():
    return dict(status='live',scene_visible=True,tracking_current=True,frame_age_ms=20,
        party_camera=dict(protocol=1,scope='a'*32,revision=0,active=False,frame_mono=0.),
        camera_id='cam_1234567890',session_id='SYNTHETIC',
        people=[dict(id=track,box=dict(x=.1+index*.45,y=.1,w=.3,h=.8,confidence=.95),
            joints=dict(nose=dict(x=.25+index*.45,y=.4,confidence=.95),
                leftWrist=dict(x=.2+index*.45,y=.1,confidence=.95)))for index,track in enumerate([7,8])],
        room=dict(registration=dict(status='locked'),
            seats=[dict(id=1,track=7,active=True),dict(id=2,track=8,active=True)],
            pointing=[dict(person_id=7,target=2,confirmed=True,ambiguous=False)]))


class Embodied:
    def __init__(self):
        self.process=SimpleNamespace(returncode=None)
        self.report=observation()

    async def request(self,*args,**kwargs):
        return copy.deepcopy(self.report)

    async def party_camera(self,payload):
        return dict(context=payload['context'],scope='a'*32,sequence=payload['sequence'],active=payload['action']=='renew')


@asynccontextmanager
async def party(*,start=True):
    clock=Clock();embodied=Embodied()
    def game_factory(count,**kwargs):
        from homeplane.werewolf import WerewolfGame
        return WerewolfGame(count,rng=FixedRoles(),**kwargs)
    service=WerewolfService(HostEffects(SilentLights()),embodied=embodied,clock=clock,
        gateway_factory=NoNetwork,addresses=lambda:['192.168.50.20'],game_factory=game_factory)
    try:
        await service.create(6,address='192.168.50.20',camera=True)
        service.pump.cancel();await asyncio.gather(service.pump,return_exceptions=True);service.pump=None
        for seat in range(1,7):
            joined=await service.join(Join(code=service.join_code,seat=seat,name=f'合成{seat}'))
            await service.ready(joined['token'],True)
        await service.bind(1,'cam_1234567890','SYNTHETIC',7)
        await service.bind(2,'cam_1234567890','SYNTHETIC',8)
        if start:
            await service.control('start')
            while service.game.phase!='discussion':await service.control('advance')
        await service._camera()
        yield service,embodied,clock
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_only_bound_fresh_bodies_supply_advisory_hand_and_pointing():
    async with party()as(service,_,_):
        hints=service.public()['camera_hints']
        assert hints[0]==dict(seat=1,hand_raised=True,pointing_at=2,advisory=True,vote_submitted=False)
        assert hints[1]['seat']==2 and hints[1]['pointing_at']is None
        assert not service.game.choices


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['document','room','registration','tracking','covered','age_bool','age_nan',
    'age_infinite','people','boolean_track','duplicate_track','bad_box','seats','pointing'])
async def test_invalid_camera_payload_clears_advice_and_cannot_break_the_host(fault):
    async with party()as(service,embodied,_):
        report=embodied.report
        if fault=='document':embodied.report=[]
        elif fault=='room':report['room']='invalid'
        elif fault=='registration':report['room']['registration']='invalid'
        elif fault=='tracking':report['tracking_current']=False
        elif fault=='covered':report['scene_visible']=False
        elif fault.startswith('age_'):report['frame_age_ms']={'age_bool':True,'age_nan':float('nan'),'age_infinite':float('inf')}[fault]
        elif fault=='people':report['people']=None
        elif fault=='boolean_track':report['people'][0]['id']=True
        elif fault=='duplicate_track':report['people'].append(copy.deepcopy(report['people'][0]))
        elif fault=='bad_box':report['people'][0]['box']['x']=float('nan')
        else:report['room'][fault]=None
        await service.step()
        assert service.public()['camera_hints']==[] and service.host()['camera_candidates']==[]
        assert not service.game.choices


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['boolean_confidence','nan_y','outside_image','missing_x','joints_not_object'])
async def test_unusable_joints_never_claim_a_hand_raise(fault):
    async with party()as(service,embodied,_):
        joints=embodied.report['people'][0]['joints']
        if fault=='boolean_confidence':joints['nose']['confidence']=True
        elif fault=='nan_y':joints['leftWrist']['y']=float('nan')
        elif fault=='outside_image':joints['leftWrist']['y']=-1
        elif fault=='missing_x':joints['leftWrist'].pop('x')
        else:embodied.report['people'][0]['joints']=[]
        await service._camera()
        assert not next(h for h in service.camera_hints if h['seat']==1)['hand_raised']


@pytest.mark.asyncio
async def test_two_conflicting_pointing_hands_do_not_pick_the_last_target():
    async with party()as(service,embodied,_):
        embodied.report['room']['pointing'].append(dict(person_id=7,target=1,confirmed=True,ambiguous=False))
        await service._camera()
        assert next(h for h in service.camera_hints if h['seat']==1)['pointing_at']is None


@pytest.mark.asyncio
async def test_camera_advice_expires_without_requiring_another_frame_poll():
    async with party()as(service,_,clock):
        assert service.public()['camera_hints']
        clock.now+=1.1
        assert service.public()['camera_hints']==[] and service.host()['camera_candidates']==[]


@pytest.mark.asyncio
async def test_request_transit_time_is_part_of_the_camera_freshness_budget():
    async with party()as(service,embodied,clock):
        async def delayed(*args,**kwargs):
            clock.now+=.6;return dict(embodied.report,frame_age_ms=500)
        embodied.request=delayed
        await service._camera()
        assert service.camera_hints==[] and service.camera_candidates==[]


@pytest.mark.asyncio
async def test_camera_request_has_its_own_timeout_and_next_phase_still_runs():
    async with party()as(service,embodied,clock):
        async def stuck(*args,**kwargs):
            await asyncio.Event().wait()
        embodied.request=stuck
        await asyncio.wait_for(service._camera(),1.4)
        assert service.camera_hints==[]
        embodied.request=lambda *args,**kwargs:asyncio.sleep(0,result=observation())
        clock.now=service.game.deadline+.01
        await service.step()
        assert service.game.phase=='vote'


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['room_closed','worker_replaced','camera_disabled'])
async def test_late_camera_response_cannot_revive_retired_advice(change):
    async with party()as(service,embodied,_):
        entered=asyncio.Event();release=asyncio.Event();report=copy.deepcopy(embodied.report)
        async def request(*args,**kwargs):
            entered.set();await release.wait();return report
        embodied.request=request;task=asyncio.create_task(service._camera())
        try:
            await asyncio.wait_for(entered.wait(),1)
            if change=='room_closed':await service.close_game()
            elif change=='worker_replaced':embodied.process=SimpleNamespace(returncode=None)
            else:service.camera_enabled=False
            release.set();await task
            assert service.camera_hints==[] and service.camera_candidates==[]
        finally:
            release.set();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_newer_camera_response_wins_over_an_older_inflight_request():
    async with party()as(service,embodied,_):
        entered=asyncio.Event();release=asyncio.Event();old=copy.deepcopy(embodied.report);calls=0
        async def request(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:entered.set();await release.wait();return old
            current=copy.deepcopy(old)
            for p in current['people']:p['joints']['leftWrist']['y']=.6
            return current
        embodied.request=request;task=asyncio.create_task(service._camera())
        try:
            await asyncio.wait_for(entered.wait(),1)
            await service._camera()
            release.set();await task
            assert service.camera_hints and all(not h['hand_raised']for h in service.camera_hints)
        finally:
            release.set();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_bad_camera_payload_does_not_stop_automatic_phase_deadlines():
    async with party()as(service,embodied,clock):
        calls=asyncio.Queue()
        async def malformed(*args,**kwargs):
            calls.put_nowait(None);return dict(embodied.report,room=dict(registration='invalid'))
        embodied.request=malformed;service.pump=asyncio.create_task(service._run())
        await asyncio.wait_for(calls.get(),1);await asyncio.sleep(0)
        assert not service.pump.done()
        clock.now=service.game.deadline+.01
        await asyncio.wait_for(calls.get(),1)
        assert service.game.phase=='vote' and not service.pump.done()


@pytest.mark.asyncio
async def test_original_bytetrack_body_reaches_explicit_party_binding():
    from homeplane.embodied.stable_tracks import StableTracks
    from tests.embodied.test_tracking_continuity import frame
    tracker=StableTracks();people=[]
    for now in [0,.05,.1,.15]:people=tracker.update(frame(),now)
    async with party(start=False)as(service,embodied,_):
        service.bindings.clear();embodied.report.update(people=people,tracking_current=tracker.input_valid)
        await service.bind(1,'cam_1234567890','SYNTHETIC',people[0]['id'])
        await service._camera()
        assert service.camera_hints[0]['seat']==1 and service.camera_hints[0]['hand_raised']
        tracker.update(dict(boxes=[dict(x=float('nan'))],poses=[]),.2)
        embodied.report['tracking_current']=tracker.input_valid
        await service._camera()
        assert service.camera_hints==[] and not service.game.choices
