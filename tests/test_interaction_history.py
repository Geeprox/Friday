"""用合成事件验证手势历史的时间、规则快照和页面展示，不启动识别进程。"""

import ast
import asyncio
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied import interaction
from homeplane.embodied.interaction_fleet import InteractionFleet

ROOT = Path(__file__).resolve().parents[1]


def test_event_captures_wall_time_and_rule_before_configuration_changes(monkeypatch):
    binding = interaction.Binding(id='a' * 32, gesture='hand.open_palm', target='lamp',
                                  action='on', scope='camera', camera='cam_1111111111', enabled=True)
    config = interaction.Configuration(revision=3, enabled=True, bindings=[binding])
    monkeypatch.setattr(interaction, 'load', lambda: config)
    monkeypatch.setattr(interaction.time, 'time', lambda: 1794024000.25)
    engine = interaction.InteractionEngine(binding.camera, 'synthetic-session')
    args = ([{'id': 7, 'joints': {}}], [{'person_id': 7, 'gesture': 'open_palm'}],
            7, {'status': 'locked'}, True)
    assert engine.update(*args, 10) == []
    event, = engine.update(*args, 10.8)
    binding.gesture = 'hand.ok'
    binding.action = 'off'
    config.revision = 4
    assert event['triggered_at'] == 1794024000.25
    assert event['observed_mono'] == 10.8
    assert (event['binding'], event['revision'], event['gesture'], event['scope'], event['action']) == (
        'a' * 32, 3, 'hand.open_palm', 'camera', 'on')


@pytest.mark.asyncio
async def test_delayed_receipt_keeps_trigger_time_and_rule_metadata(monkeypatch):
    from homeplane.embodied import bridge
    from homeplane.embodied.practice import RecognitionPractice

    waiting, release = asyncio.Event(), asyncio.Event()

    async def command(*args, **kwargs):
        waiting.set()
        await release.wait()
        return {'status': 'reported_match', 'acknowledged': True, 'reached_target': True}

    monkeypatch.setattr(bridge, 'command', AsyncMock(side_effect=command))
    nodes = ast.parse((ROOT / 'homeplane/embodied/worker.py').read_text()).body
    source_guard = next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == 'interaction_source_current')
    dispatch = next(n for n in nodes if isinstance(n, ast.AsyncFunctionDef) and n.name == 'dispatch_interactions')
    fleet, tasks = InteractionFleet(), []
    monkeypatch.setattr(fleet, 'current', lambda *_: True)
    scope = dict(time=SimpleNamespace(time=lambda: 1794024035, monotonic=lambda: 20),
                 interaction_enabled=True, SELECTED={'id': 'synthetic-camera'},
                 state={'status': 'live'}, identities=SimpleNamespace(session='synthetic-session'),
                 games=SimpleNamespace(mode='off', control_context='synthetic-game', control_revision=0),
                 practice=RecognitionPractice(),
                 home=SimpleNamespace(armed=True, spawn=lambda coro: tasks.append(asyncio.create_task(coro))),
                 secondary_homes={}, fleet=fleet, wall=SimpleNamespace(notice=lambda *_: None))
    exec(compile(ast.Module(body=[source_guard, dispatch], type_ignores=[]), '<合成手势分发>', 'exec'), scope)
    event = dict(camera='synthetic-camera', session='synthetic-session', track=7,
                 target='lamp', action='on', event_id='synthetic-history',
                 binding='a' * 32, revision=3, gesture='hand.open_palm', scope='home',
                 triggered_at=1794024000.25, observed_mono=20)
    try:
        await scope['dispatch_interactions']([event])
        await asyncio.wait_for(waiting.wait(), 1)
        assert not fleet.public(20)['events']
        release.set()
        await asyncio.gather(*tasks)
        receipt, = fleet.public(20)['events']
        assert receipt['at'] == 1794024035
        assert receipt['triggered_at'] == 1794024000.25
        for key in ('camera', 'target', 'action', 'binding', 'revision', 'gesture', 'scope'):
            assert receipt[key] == event[key]
        assert receipt['acknowledged'] and receipt['reached_target']
        assert not {'session', 'track', 'observed_mono', 'event_id'} & receipt.keys()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_synthetic_history_console():
    node = shutil.which('node')
    if node is None:
        pytest.skip('页面验证需要 Node.js')
    result = subprocess.run([node, str(ROOT / 'tests/interaction_history_fixture.cjs')],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
