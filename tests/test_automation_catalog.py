"""Catalog observation never becomes external execution authority."""
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from homeplane.automation_catalog import AutomationCatalog, Bundle, Import, create_router
from homeplane.runtime_log import RuntimeLog, binding


def bundle(**edit):
    entry = dict(key='observed:one', source='mijia', kind='automation',
                 name='PRIVATE synthetic household rule', observed_at=datetime.now(UTC).isoformat(),
                 evidence='ui_name_index')
    entry.update(edit)
    return Bundle.model_validate_json(json.dumps(dict(entries=[entry])))


def test_observation_round_trip_revision_and_log_privacy(tmp_path):
    owner = AutomationCatalog(tmp_path / 'catalog.json')
    journal = RuntimeLog(tmp_path / 'logs')
    with binding(journal):
        saved = owner.replace(Import(expected_revision=0, bundle=bundle()))
        assert owner.read() == saved
        with pytest.raises(ValueError, match='source_conflict'):
            owner.replace(Import(expected_revision=0, bundle=bundle()))
    assert saved.revision == 1
    assert os.stat(owner.path).st_mode & 0o777 == 0o600
    events = journal.export()['events']
    assert any(e['stage'] == 'automation.catalog.import' and e['event'] == 'result' for e in events)
    assert 'PRIVATE synthetic household rule' not in json.dumps(journal.export())
    assert owner.read().entries[0].source_id is None


@pytest.mark.parametrize('edit', [dict(enabled=True), dict(actions=['turn on']), dict(source_id='guessed-id'),
                                dict(observed_at=(datetime.now(UTC)+timedelta(days=1)).isoformat())])
def test_name_index_cannot_claim_enablement_actions_identity_or_future_freshness(edit):
    with pytest.raises(ValidationError):
        bundle(**edit)


def test_import_retains_corrupt_file_and_refuses_symlinks_and_budget(tmp_path):
    path=tmp_path/'catalog.json';path.write_text('{broken');path.chmod(0o600)
    owner=AutomationCatalog(path)
    with pytest.raises(ValueError, match='invalid_config'):owner.read()
    assert path.read_text() == '{broken'
    path.unlink();outside=tmp_path/'outside';outside.write_text('secret');path.symlink_to(outside)
    with pytest.raises(ValueError, match='invalid_config'):owner.replace(Import(expected_revision=0,bundle=bundle()))
    assert outside.read_text()=='secret'
    path.unlink();path.write_bytes(b'x'*(256*1024+1));path.chmod(0o600)
    with pytest.raises(ValueError, match='invalid_config'):owner.read()


def test_mode_links_must_reference_observed_entries():
    raw=bundle().model_dump(mode='json');raw['modes']=[dict(name='Sleep',entries=['absent'],chain=['one','two'])]
    with pytest.raises(ValidationError):Bundle.model_validate_json(json.dumps(raw))


@pytest.mark.asyncio
async def test_route_retains_catalog_when_native_source_unavailable_and_never_executes(tmp_path):
    async def failed_inventory():raise TimeoutError()
    manager=SimpleNamespace(records=lambda:[SimpleNamespace(rule=SimpleNamespace(id='owned:one',intent='Owned rule'),enabled=True)])
    app=FastAPI();app.include_router(create_router(tmp_path/'catalog.json',manager,failed_inventory))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test')as c:
        first=await c.get('/api/home/automations/catalog');assert first.json()['entries']==[]
        saved=await c.post('/api/home/automations/catalog',json={'expected_revision':0,'bundle':bundle().model_dump(mode='json')})
        assert saved.status_code==200, saved.text
        view=(await c.get('/api/home/automations/catalog')).json()
        assert view['entries'][0]['name'].startswith('PRIVATE')
        assert view['owned'][0]['key']=='owned:one'
        assert view['geek_connection']['status']=='unavailable'
        assert view['external_rules_intercepted'] is False and view['external_writes_supported'] is False
        assert view['mode_state']=='unknown'
        stale=await c.post('/api/home/automations/catalog',json={'expected_revision':0,'bundle':bundle().model_dump(mode='json')})
        assert stale.status_code==409
        malformed=await c.post('/api/home/automations/catalog',json={'expected_revision':1,'bundle':{'entries':[{'name':'Incomplete'}]}})
        assert malformed.status_code==422
        assert (await c.get('/api/home/automations/catalog')).json()['revision']==1


@pytest.mark.asyncio
async def test_real_app_catalog_budget_and_same_origin_fence(tmp_path):
    from homeplane.api import create_app
    app=create_app(db_path=tmp_path/'traces.sqlite',settings_path=tmp_path/'settings.json')
    raw=bundle().model_dump(mode='json')
    raw['entries']=[{**raw['entries'][0], 'key':f'observed:{n}', 'summary':'S'*380} for n in range(60)]
    payload=dict(expected_revision=0,bundle=raw)
    assert len(json.dumps(payload))>16384
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1:8740')as c:
        denied=await c.post('/api/home/automations/catalog',json=payload,headers={'Origin':'http://untrusted.invalid'})
        assert denied.status_code==403
        imported=await c.post('/api/home/automations/catalog',json=payload,headers={'Origin':'http://127.0.0.1:8740'})
        assert imported.status_code==200, imported.text
        view=(await c.get('/api/home/automations/catalog')).json()
        assert len(view['entries'])==60 and view['external_writes_supported']is False
        oversized=await c.post('/api/home/automations/catalog',content=b' '*270000,headers={'content-type':'application/json'})
        assert oversized.status_code==413
        unchanged=await c.post('/api/home/preferences',content=b' '*17000,headers={'content-type':'application/json'})
        assert unchanged.status_code==413
    app.state.core.actions.close();await app.state.core.adapter.close();app.state.core.store.close()
