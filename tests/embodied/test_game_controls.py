"""Production handler bodies with pure game rules and explicitly inert I/O ports.

The worker module is not imported: it owns native camera/model startup globals.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from homeplane.embodied.api import GameControl
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.service import EmbodiedService
from tests.embodied.test_motion_games import LOCK, human


def handler(path, name, **ports):
    tree = ast.parse(Path(path).read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    node.decorator_list = []
    namespace = {'Request':object, 'HTTPException':HTTPException, **ports}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), path, 'exec'), namespace)
    return namespace[name]


def request(data, origin='http://127.0.0.1:8860', *, game=None):
    if game is not None and isinstance(data,dict):
        data=dict(data,expected_context=game.public['control_context'],expected_revision=game.public['control_revision'])
    return SimpleNamespace(headers={'host':'127.0.0.1:8860', 'origin':origin}, json=AsyncMock(return_value=data))


def game_handler(game):
    start, enable = AsyncMock(), AsyncMock()
    control = handler('homeplane/embodied/worker.py', 'game_control', games=game,
                      practice=SimpleNamespace(active=lambda:False), start=start,
                      home=SimpleNamespace(enable=enable))
    return control, start, enable


@pytest.mark.asyncio
async def test_product_stop_payload_reaches_worker_reset_without_stopping_perception():
    game = MotionGames()
    game.reset('ski')
    control, start, enable = game_handler(game)
    calls = []
    async def send(path, data=None):
        calls.append((path, data))
        if path=='/api/status':return {'game':game.public}
        return await control(request(data))
    owner=EmbodiedService()
    owner.request=send
    owner._check_game_intent=AsyncMock(return_value=dict(owned=True,game=dict(game.public)))
    stop = handler('homeplane/embodied/api.py', 'stop_game', service=owner,GameControl=GameControl)
    try:
        result = await stop(GameControl(expected_intent='a'*64))
    finally:
        await owner.client.aclose()
    owner._check_game_intent.assert_awaited_once_with('a'*64)
    assert calls==[('/api/game',dict(mode='off',expected_context=game.public['control_context'],expected_revision=game.control_revision-1))]
    assert result['mode'] == 'off' and game.mode == 'off'
    start.assert_not_awaited()
    enable.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_freezes_game_and_holds_gestures_then_resumes_without_catchup():
    game = MotionGames()
    game.reset('ski')
    control, start, enable = game_handler(game)
    for i in range(30):
        game.update([human()], LOCK, i*.05)
    game.distance = 12
    old_elapsed, old_mode = game.elapsed, game.selected_mode
    paused = await control(request({'action':'pause', 'mode':'ski'},game=game))
    assert paused['manually_paused'] is True
    for i in range(100):
        state = game.update([human()], LOCK, 10+i*.1, [{'person_id':1,'gesture':'yeah'}])
    assert state['status'] == 'paused' and game.distance == 12
    assert game.selected_mode == old_mode and game.elapsed == old_elapsed
    resumed = await control(request({'action':'resume', 'mode':'ski'},game=game))
    assert resumed['manually_paused'] is False
    game.update([human()], LOCK, 1000)
    assert game.elapsed == old_elapsed
    start.assert_not_awaited()
    enable.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('data', [[], {'action':[]}, {'action':'unexpected'}, {'action':'pause','mode':'tennis'}])
async def test_invalid_or_stale_game_control_has_no_effect(data):
    game = MotionGames()
    game.reset('ski')
    control, start, enable = game_handler(game)
    with pytest.raises(HTTPException) as error:
        await control(request(data,game=game))
    assert error.value.status_code == 400
    assert game.mode == 'ski' and game.manually_paused is False
    start.assert_not_awaited()
    enable.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_origin_cannot_pause_or_restart():
    game = MotionGames()
    game.reset('ski')
    control, start, _ = game_handler(game)
    with pytest.raises(HTTPException) as error:
        await control(request({'action':'pause','mode':'ski'}, 'http://elsewhere.invalid',game=game))
    assert error.value.status_code == 403 and game.manually_paused is False
    start.assert_not_awaited()


def test_game_gesture_cycle_stays_out_of_household_control():
    game = MotionGames()
    game.select('hiit')
    for i in range(25):
        game.gesture_control([human()], [{'person_id':1,'gesture':'yeah'}], LOCK, i*.05)
    assert game.selected_mode == 'quest' and game.mode == 'off'


@pytest.mark.asyncio
async def test_stopped_or_household_context_requires_new_explicit_game_admission():
    game = MotionGames()
    control, start, enable = game_handler(game)
    for mode in ['home', 'ski']:
        game.select(mode)
        if mode == 'ski':
            await control(request({'mode':'off'},game=game))
        for label in ['yeah', 'ok']:
            for i in range(45):
                game.update([human()], LOCK, i*.05, [{'person_id':1,'gesture':label}])
        assert game.game_input_enabled is False
        assert game.mode == 'off' and game.selected_mode == mode
    game.select('tennis')
    assert game.game_input_enabled is True
    start.assert_not_awaited()
    enable.assert_not_awaited()
