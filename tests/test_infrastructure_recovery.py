"""Original supervision and Safety API, inert worker; never opens household input."""
import asyncio
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter,FastAPI

from homeplane.infrastructure import HouseholdInfrastructure,ServicePolicy
from homeplane.embodied import safety_api
from homeplane.embodied.safety import SafetyPolicy, SafetyRuntime, SafetyStore
from tests.embodied.test_camera_audio import actual
from tests.test_household_infrastructure import service


async def eventually(predicate):
    async def check():
        while not predicate():await asyncio.sleep(.001)
    await asyncio.wait_for(check(),1)


@pytest.mark.asyncio
async def test_explicit_retry_wakes_the_existing_owner_and_preserves_the_policy(tmp_path):
    worker=service();attempts=[]
    async def ensure(**kwargs):
        attempts.append(kwargs)
        if len(attempts)==1:raise OSError('SIMULATED unavailable')
    worker.ensure_background=ensure
    owner=HouseholdInfrastructure(worker,tmp_path/'policy.json',admitted=lambda:True,interval=60)
    try:
        await owner.configure(ServicePolicy(enabled=True));await owner.start()
        original_task=owner.task;await eventually(lambda:owner.failure is not None)
        policy_bytes=owner.path.read_bytes();report=owner.report()
        assert report['recovery']['automatic'] and report['recovery']['can_retry']
        assert report['recovery']['retry_after_seconds']>0
        await owner.retry(expected_revision=owner.policy.revision)
        await eventually(lambda:owner.failure is None and len(attempts)==2)
        assert owner.task is original_task and owner.path.read_bytes()==policy_bytes
        assert attempts==[dict(interaction=True,open_privacy=False)]*2
        assert not owner.report()['recovery']['can_retry']
        await owner.retry(expected_revision=owner.policy.revision)
        await asyncio.sleep(.01);assert len(attempts)==2
    finally:await owner.close()


@pytest.mark.asyncio
async def test_old_retry_after_pause_cannot_reenable_or_wake_capture(tmp_path):
    worker=service();worker.ensure_background.side_effect=OSError('SIMULATED')
    owner=HouseholdInfrastructure(worker,tmp_path/'policy.json',admitted=lambda:True,interval=60)
    try:
        await owner.configure(ServicePolicy(enabled=True));await owner.start()
        await eventually(lambda:owner.failure is not None);old=owner.policy.revision
        await owner.configure(owner.policy.model_copy(update={'enabled':False}))
        with pytest.raises(ValueError,match='source_conflict'):await owner.retry(expected_revision=old)
        with pytest.raises(ValueError):await owner.retry(expected_revision=owner.policy.revision)
        await asyncio.sleep(.01)
        assert worker.capture_enabled is False and worker.ensure_background.await_count==1
        assert not owner.report()['recovery']['automatic'] and not owner.report()['recovery']['can_retry']
    finally:await owner.close()


@pytest.mark.asyncio
async def test_repeated_manual_retry_cannot_remove_backoff_repeatedly(tmp_path):
    worker=service();worker.ensure_background.side_effect=OSError('SIMULATED')
    owner=HouseholdInfrastructure(worker,tmp_path/'policy.json',admitted=lambda:True,interval=60)
    try:
        await owner.configure(ServicePolicy(enabled=True));await owner.start();await eventually(lambda:owner.failure is not None)
        await owner.retry(expected_revision=owner.policy.revision)
        await eventually(lambda:worker.ensure_background.await_count==2)
        assert not owner.report()['recovery']['can_retry']
        for _ in range(5):await owner.retry(expected_revision=owner.policy.revision)
        await asyncio.sleep(.01);assert worker.ensure_background.await_count==2
    finally:await owner.close()


@pytest.mark.asyncio
async def test_cancelled_waiting_retry_cannot_wake_a_later_owner(tmp_path):
    worker=service();worker.ensure_background.side_effect=OSError('SIMULATED')
    owner=HouseholdInfrastructure(worker,tmp_path/'policy.json',admitted=lambda:True,interval=60)
    try:
        await owner.configure(ServicePolicy(enabled=True));await owner.start();await eventually(lambda:owner.failure is not None)
        async with owner.lock:
            task=asyncio.create_task(owner.retry(expected_revision=owner.policy.revision));await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        await asyncio.sleep(.01);assert worker.ensure_background.await_count==1
    finally:await owner.close()


@pytest.mark.asyncio
async def test_reading_safety_is_silent_and_retry_rejoins_original_background_recovery(tmp_path,monkeypatch):
    monkeypatch.setattr(safety_api,'PRIVATE',tmp_path/'safety')
    worker=service();worker.process=SimpleNamespace(returncode=None);broken=[True]
    async def ensure(**kwargs):
        assert kwargs==dict(interaction=True,open_privacy=False)
        if broken[0]:raise OSError('SIMULATED unavailable')
    worker.ensure_background=AsyncMock(side_effect=ensure)
    async def request(path):
        assert path=='/api/safety/status'
        if broken[0]:raise httpx.ReadTimeout('SIMULATED')
        return dict(cameras=[dict(camera='SIMULATED-camera',fresh=True)],recording={'state':'ready'},
                    policy_revision=runtime.policy_cache.revision)
    worker.request=request
    owner=HouseholdInfrastructure(worker,tmp_path/'policy.json',admitted=lambda:True,interval=60)
    router=APIRouter();runtime=None
    try:
        await owner.configure(ServicePolicy(enabled=True));await owner.start();await eventually(lambda:owner.failure is not None)
        runtime=safety_api.install(router,worker,infrastructure=owner)
        runtime.store.configure(SafetyPolicy(enabled=True))
        app=FastAPI();app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://SIMULATED')as client:
            state=(await client.get('/api/embodied/safety')).json()
            assert state['worker']['status']=='unavailable' and state['supervision']['recovery']['automatic']
            assert worker.ensure_background.await_count==1
            revision=state['supervision']['policy']['revision'];broken[0]=False
            response=await client.post('/api/embodied/safety/retry',json={'expected_revision':revision})
            assert response.status_code==200
            await eventually(lambda:owner.failure is None)
            state=(await client.get('/api/embodied/safety')).json()
            assert state['worker']['status']=='running' and state['worker']['cameras'][0]['fresh']
            assert worker.ensure_background.await_count==2
    finally:
        await owner.close()
        if runtime is not None:await runtime.close()


@pytest.mark.asyncio
async def test_safety_status_fences_worker_replacement_and_old_monitoring_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    monkeypatch.setattr(safety_api, 'configured_camera_sources', lambda: [])
    store = SafetyStore(tmp_path / 'safety.sqlite')
    store.configure(SafetyPolicy(enabled=True))
    worker = SafetyRuntime(store)
    handler = actual('homeplane/embodied/worker.py', 'safety_status', {'safety': worker})
    service = SimpleNamespace(process=SimpleNamespace(returncode=None))
    replace = False

    async def request(path):
        assert path == '/api/safety/status'
        result = await handler()
        if replace:
            service.process = SimpleNamespace(returncode=None)
        return result

    async def observation():
        now = monotonic()
        worker.capture_frame('synthetic', 'b' * 32, 'a' * 64, now)
        assert worker.offer('synthetic', 'session', [], now, now, stable=True, source_context='a' * 64,
                            capture_session='b' * 32)
        await worker.observation_task

    service.request = request
    router = APIRouter()
    console = safety_api.install(router, service)
    app = FastAPI()
    app.include_router(router)
    try:
        await worker.sync_policy()
        await observation()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
            path = '/api/embodied/safety'
            ready = (await client.get(path)).json()
            assert ready['worker']['cameras'] == [dict(camera='synthetic', fresh=True)]
            new_policy = store.configure(store.read_policy().model_copy(update={'announce': True}))
            stale = (await client.get(path)).json()
            assert stale['policy']['revision'] == new_policy.revision
            assert stale['worker']['status'] == 'waiting_policy' and stale['worker']['cameras'] == []
            assert stale['worker']['waiting_observations'] == 0
            await worker.sync_policy()
            await observation()
            assert (await client.get(path)).json()['worker']['status'] == 'running'
            replace = True
            switched = (await client.get(path)).json()
            assert switched['worker'] == dict(status='unavailable', cameras=[])
    finally:
        await worker.close()
        await console.close()


@pytest.mark.asyncio
async def test_retry_without_a_live_admitted_owner_never_starts_one(tmp_path):
    worker=service();owner=HouseholdInfrastructure(worker,tmp_path/'policy.json',admitted=lambda:False)
    try:
        await owner.configure(ServicePolicy(enabled=True))
        with pytest.raises(ValueError):await owner.retry(expected_revision=owner.policy.revision)
        assert owner.task is None;worker.ensure_background.assert_not_awaited()
    finally:await owner.close()


@pytest.mark.asyncio
async def test_default_console_passes_its_actual_owner_to_safety_and_reads_do_not_start_it(tmp_path):
    from homeplane.api import create_app
    from homeplane.embodied.service import EmbodiedService
    worker=EmbodiedService();worker._spawn_guarded_worker=AsyncMock(side_effect=AssertionError('no models or capture'))
    app=create_app(db_path=tmp_path/'SIMULATED.sqlite',settings_path=tmp_path/'settings.json',embodied_service=worker)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8740')as client:
            response=await client.get('/api/embodied/safety');assert response.status_code==200
            state=response.json();assert state['supervision']==app.state.infrastructure.report()
            revision=state['supervision']['policy']['revision']
            assert (await client.post('/api/embodied/safety/retry',json={'expected_revision':revision})).status_code==403
            headers={'origin':'http://127.0.0.1:8740'}
            assert (await client.post('/api/embodied/safety/retry',json={'expected_revision':revision},headers=headers)).status_code==409
            for value in [True,'0',-1]:
                assert (await client.post('/api/embodied/safety/retry',json={'expected_revision':value},headers=headers)).status_code==422
            worker._spawn_guarded_worker.assert_not_awaited()
