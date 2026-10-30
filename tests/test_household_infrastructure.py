"""No devices/models: real coordinator and persistence, fake worker boundary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.infrastructure import HouseholdInfrastructure, ServicePolicy
from homeplane.embodied.household import DeviceCommands, supported_actions, summarize_outcomes
from homeplane.actions import ActionController
from homeplane.people.live import IdentityStore
from homeplane.recognition.contracts import Embedding
from homeplane.recognition.artifacts import SFACE_SHA256


def service():
    return SimpleNamespace(configured=lambda: True, ensure_background=AsyncMock(),
        set_interaction=AsyncMock(), stop=AsyncMock(), sync_people=AsyncMock())


@pytest.mark.asyncio
async def test_background_runs_without_http_request_and_recovers(tmp_path):
    worker=service(); worker.ensure_background.side_effect=[OSError('private'),None]
    owner=HouseholdInfrastructure(worker,tmp_path/'intent.json',admitted=lambda:True,interval=.001)
    await owner.configure(ServicePolicy(enabled=True))
    await owner.start()
    for _ in range(50):
        if owner.failure:break
        await asyncio.sleep(.001)
    assert owner.report()['failure']=='service_unavailable'
    assert 'private' not in str(owner.report())
    owner.retry_at=0
    for _ in range(50):
        if worker.ensure_background.await_count>=2:break
        await asyncio.sleep(.001)
    assert owner.failure is None
    await owner.close()
    n=worker.ensure_background.await_count
    await asyncio.sleep(.005)
    assert worker.ensure_background.await_count==n


@pytest.mark.asyncio
async def test_pause_survives_restart_and_resume_is_revision_fenced(tmp_path):
    path=tmp_path/'intent.json';worker=service()
    owner=HouseholdInfrastructure(worker,path,admitted=lambda:True)
    await owner.configure(ServicePolicy(enabled=False))
    worker.stop.assert_awaited_once()
    reopened=HouseholdInfrastructure(worker,path,admitted=lambda:True)
    await reopened.reconcile();worker.ensure_background.assert_not_awaited()
    with pytest.raises(ValueError,match='source_conflict'):
        await reopened.configure(ServicePolicy())
    await reopened.configure(ServicePolicy(revision=1,enabled=True))
    await reopened.reconcile();worker.ensure_background.assert_awaited_once_with(interaction=True,open_privacy=False)
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_pause_interactions_keeps_recognition_and_setup_is_headless(tmp_path):
    worker=service();prepare=AsyncMock()
    owner=HouseholdInfrastructure(worker,tmp_path/'intent.json',admitted=lambda:True,prepare=prepare)
    await owner.configure(ServicePolicy(enabled=True,interaction=False));await owner.reconcile()
    worker.stop.assert_not_awaited();worker.ensure_background.assert_awaited_once_with(interaction=False,open_privacy=False)
    prepare.assert_awaited_once()


@pytest.mark.asyncio
async def test_unadmitted_or_corrupt_policy_never_starts_models(tmp_path):
    worker=service();path=tmp_path/'intent.json';path.write_text('{bad')
    owner=HouseholdInfrastructure(worker,path,admitted=lambda:True)
    await owner.reconcile();worker.ensure_background.assert_not_awaited()
    assert owner.failure=='invalid_config'
    other=HouseholdInfrastructure(worker,tmp_path/'absent',admitted=lambda:False)
    await other.reconcile();worker.ensure_background.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_auto_open_permission_cannot_reopen_manual_shutters(tmp_path):
    path=tmp_path/'intent.json';worker=service()
    # Load the exact old on-disk shape, bypassing the current model validator.
    path.write_text('{"enabled":true,"auto_open_cameras":true}')
    owner=HouseholdInfrastructure(worker,path,admitted=lambda:True)
    await owner.reconcile()
    worker.ensure_background.assert_awaited_once_with(interaction=True,open_privacy=False)
    worker.ensure_background.reset_mock()
    await owner.configure(ServicePolicy(enabled=True).model_copy(update={'auto_open_cameras':True}))
    reopened=HouseholdInfrastructure(worker,path,admitted=lambda:True)
    await reopened.reconcile()
    worker.ensure_background.assert_awaited_once_with(interaction=True,open_privacy=False)
    assert reopened.policy.auto_open_cameras is False
    assert reopened.report()['requires_open_window'] is False


@pytest.mark.asyncio
async def test_saved_pause_fences_direct_starts_even_if_cleanup_fails(tmp_path):
    worker=service()
    owner=HouseholdInfrastructure(worker,tmp_path/'intent.json',admitted=lambda:True)
    assert worker.capture_enabled is False
    await owner.configure(ServicePolicy(enabled=True))
    assert worker.capture_enabled is True
    worker.stop.side_effect=ValueError('cleanup_pending')
    await owner.configure(ServicePolicy(revision=1,enabled=False))
    assert worker.capture_enabled is False and owner.report()['failure']=='service_unavailable'
    reopened=HouseholdInfrastructure(worker,owner.path,admitted=lambda:True)
    assert not worker.capture_enabled and not reopened.policy.enabled


@pytest.mark.asyncio
async def test_readback_waits_for_async_report_without_resending():
    writer=SimpleNamespace(readback=AsyncMock(side_effect=[{'power':'off'},{'power':'on'}]))
    result=await ActionController._settle(None,writer,{'power':'on'})
    assert result=={'power':'on'} and writer.readback.await_count==2


def test_unknown_send_never_becomes_acknowledged_success():
    result=summarize_outcomes([dict(status='unknown_outcome',write_attempted=True,acknowledged=False)])
    assert result['status']=='unknown_outcome' and not result['acknowledged'] and not result['reached_target']
    assert not summarize_outcomes([])['reached_target']


def test_capabilities_choose_supported_cover_and_climate_actions():
    curtain=dict(kind='cover',capabilities=['cover.state'])
    assert supported_actions(curtain)==['open','close']
    assert DeviceCommands.parameters(curtain,'open')==('cover.state',{'state':'open'})
    climate=dict(kind='climate',capabilities=['climate.mode'],capability_specs={'climate.mode':{'choices':['off','heat']}})
    assert supported_actions(climate)==['off','mode:heat']
    assert DeviceCommands.parameters(climate,'on')==('climate.mode',{'hvac_mode':'heat'})
    climate['capability_specs']['climate.mode']['choices']=['off','cool','heat']
    with pytest.raises(ValueError,match='climate_mode_required'):DeviceCommands.parameters(climate,'on')


def test_merge_and_undo_change_background_identity_without_deleting_templates(tmp_path):
    private=tmp_path/'private';private.mkdir(mode=0o700)
    store=IdentityStore(private/'people.sqlite')
    embedding=Embedding('face',SFACE_SHA256,(1.,)+(0.,)*127,False)
    try:
        store.enroll('first',[embedding]*3);store.enroll('second',[embedding]*3)
        assert store.match(embedding)[1]=='ambiguous'
        store.sync_people([SimpleNamespace(person_id='first',name='First',merged_into='second'),SimpleNamespace(person_id='second',name='Second',merged_into=None)])
        assert store.match(embedding)[0]=='second' and set(store.registered())=={'second'}
        assert store.labels()=={'second':'Second'}
        store.sync_people([SimpleNamespace(person_id='first',name='First',merged_into=None),SimpleNamespace(person_id='second',name='Second',merged_into=None)])
        assert store.match(embedding)[1]=='ambiguous'
        assert store.labels()=={'first':'First','second':'Second'}
    finally:store.close()

@pytest.mark.asyncio
async def test_gesture_expiring_in_command_queue_never_reaches_plan():
    lock=asyncio.Lock();await lock.acquire()
    core=SimpleNamespace(snapshot=AsyncMock(),actions=SimpleNamespace(plan=AsyncMock()))
    commands=DeviceCommands(core,lock,None)
    commands.inventory=AsyncMock(return_value=[dict(id='lamp',kind='light',controllable=True,capabilities=['light.set'])])
    fresh=True
    task=asyncio.create_task(commands.command(None,'on','event',target='lamp',dispatch_guard=lambda:fresh))
    await asyncio.sleep(0)
    fresh=False;lock.release()
    result=await task
    assert not result['write_attempted']
    assert result['outcomes'][0]['code']=='stale_interaction'
    core.snapshot.assert_not_awaited();core.actions.plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_install_requires_explicit_recognition_enable(tmp_path):
    worker = service()
    owner = HouseholdInfrastructure(worker, tmp_path/'intent.json', admitted=lambda: True)
    await owner.reconcile()
    assert owner.report()['policy']['enabled'] is False
    worker.ensure_background.assert_not_awaited()
