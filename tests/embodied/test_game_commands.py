"""Original game handler with pure rules; no worker import or device startup."""
import pytest
from fastapi import HTTPException

from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_game_controls import game_handler, request


def scope(game):
    return dict(expected_context=game.public.get('control_context'),
                expected_revision=game.public.get('control_revision'))


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['new_round','new_worker','pause'])
async def test_retired_game_command_cannot_change_current_round(change):
    game=MotionGames();game.reset('ski');old=scope(game)
    if change=='new_round':game.reset('ski')
    elif change=='new_worker':game=MotionGames();game.reset('ski')
    else:game.set_paused(True)
    current=game.public.copy();control,start,enable=game_handler(game)
    with pytest.raises(HTTPException) as error:
        await control(request(dict(mode='off',**old)))
    assert error.value.status_code==409
    assert game.public==current
    start.assert_not_awaited();enable.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_or_boolean_game_scope_is_not_an_admission():
    game=MotionGames();game.reset('ski');control,_,_=game_handler(game)
    for extra in ({},{**scope(game),'expected_revision':True}):
        with pytest.raises(HTTPException) as error:await control(request(dict(mode='off',**extra)))
        assert error.value.status_code==409 and game.mode=='ski'


@pytest.mark.asyncio
async def test_service_command_does_not_retry_when_a_peer_changes_the_game_after_read():
    from homeplane.embodied.service import EmbodiedService
    game=MotionGames();game.reset('quest');control,_,_=game_handler(game);calls=[]
    async def send(path,data=None):
        calls.append(path)
        if path=='/api/status':
            before=game.public.copy();game.reset('ski');return {'game':before,'owned':True,'game_intent':'a'*64}
        return await control(request(data))
    owner=EmbodiedService()
    owner.request=send
    async def status():return await send('/api/status')
    owner.status=status
    try:
        with pytest.raises(HTTPException) as error:
            await owner.game_command({'mode':'off'},expected_intent='a'*64)
    finally:
        await owner.client.aclose()
    assert error.value.status_code==409 and game.mode=='ski'
    assert calls==['/api/status','/api/game']


@pytest.mark.asyncio
async def test_game_revision_changes_only_for_control_intent_not_observation_frames():
    from tests.embodied.test_motion_games import LOCK, human
    game=MotionGames();initial=scope(game)
    game.select('ski');selected=scope(game);assert selected!=initial
    control,_,_=game_handler(game)
    started=await control(request(dict(mode='ski',action='start',**selected)))
    token=scope(game)
    for i in range(35):game.update([human()],LOCK,i*.05)
    assert scope(game)==token and started['round_id']==game.round_id
    await control(request(dict(mode='ski',action='pause',**token)))
    assert scope(game)!=token
