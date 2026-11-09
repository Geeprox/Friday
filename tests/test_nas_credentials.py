"""Synthetic credentials and inert transports only; no household secret or NAS IO."""
import asyncio
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.nas_connection.credentials import LocalNASCredentials
from homeplane.nas_connection.state import StateStore
from homeplane.settings import SettingsService
from tests.test_household_timezone_wiring import OfflineARL
from test_homeplane_nas_connection import CREDS,SECRET,FakeTransport,config,wait_for

PURPOSE='friday.nas.read-only.v1'
ORIGIN='http://127.0.0.1:8740'

@pytest.mark.asyncio
async def test_default_private_owner_to_real_manager_read_chain_without_network(tmp_path):
    from homeplane.nas_connection import NASConnectionManager
    settings=SettingsService(tmp_path/'settings.json')
    assert not settings._local_nas_credentials.directory.exists()
    fake=FakeTransport()
    manager=NASConnectionManager(settings.nas_connection_state_path,
        credential_resolver=settings.resolve_nas_credential,transport_factory=lambda:fake)
    try:
        await manager.configure(config(),expected_generation=0)
        receipt=settings.save_nas_credential(CREDS.username,CREDS.password,expected_generation=1)
        assert receipt=={'saved':True,'secret_values_returned':False,'connection_attempted':False}
        assert not fake.calls
        await manager.resume(expected_generation=1);await wait_for(manager)
        page=await manager.list_page(config().source_ref)
        assert page.entries and fake.calls[:2]==['connect','list']
        assert SECRET not in manager.status().model_dump_json()
        assert SECRET not in json.dumps(settings.get_public())
    finally:await manager.close()

@pytest.mark.parametrize('change',[
    {'host':'other.invalid'},{'port':1445},{'share':'other'},
    {'root':'other'},{'source_ref':'other'},{'require_encryption':False},
    {'credential_ref':'other'},
])
def test_credentials_cannot_follow_a_changed_endpoint_or_scope(tmp_path,change):
    owner=LocalNASCredentials(tmp_path/'credentials/nas');cfg=config()
    owner.save(cfg,CREDS)
    assert owner.resolve(cfg,purpose=PURPOSE)==CREDS
    assert owner.resolve(cfg.model_copy(update=change),purpose=PURPOSE) is None
    assert owner.resolve(cfg,purpose='other-purpose') is None

def test_private_permissions_symlink_hardlink_and_oversize_fail_closed(tmp_path):
    owner=LocalNASCredentials(tmp_path/'credentials/nas');cfg=config();owner.save(cfg,CREDS)
    file=next(owner.directory.glob('*.json'))
    assert stat.S_IMODE(file.stat().st_mode)==0o600
    assert stat.S_IMODE(owner.directory.stat().st_mode)==0o700
    file.chmod(0o644);assert owner.resolve(cfg,purpose=PURPOSE) is None
    file.chmod(0o600);hard=owner.directory/'hard';os.link(file,hard)
    assert owner.resolve(cfg,purpose=PURPOSE) is None
    hard.unlink();content=file.read_bytes();file.unlink()
    external=tmp_path/'external';external.write_bytes(content);file.symlink_to(external)
    assert owner.resolve(cfg,purpose=PURPOSE) is None
    file.unlink();file.write_bytes(b'x'*65537);file.chmod(0o600)
    assert owner.resolve(cfg,purpose=PURPOSE) is None

def test_symlinked_private_directory_is_never_followed(tmp_path):
    elsewhere=tmp_path/'elsewhere';elsewhere.mkdir(mode=0o700)
    (tmp_path/'credentials').symlink_to(elsewhere,target_is_directory=True)
    owner=LocalNASCredentials(tmp_path/'credentials/nas')
    with pytest.raises(OSError):owner.save(config(),CREDS)
    assert not list(elsewhere.iterdir())

def test_credentials_http_origin_generation_and_error_redaction(tmp_path):
    settings=SettingsService(tmp_path/'settings.json')
    state=StateStore(settings.nas_connection_state_path);state.save(config(),1,None);state.release()
    app=create_app(settings_service=settings,db_path=tmp_path/'trace.sqlite',arl_runtime=OfflineARL(),background_enabled=False)
    body={'expected_generation':1,'username':CREDS.username,'password':SECRET}
    with TestClient(app,base_url=ORIGIN) as client:
        assert client.post('/api/settings/nas/credential',json=body).status_code==403
        stale=client.post('/api/settings/nas/credential',json={**body,'expected_generation':2},headers={'Origin':ORIGIN})
        assert stale.status_code==409 and SECRET not in stale.text
        invalid=client.post('/api/settings/nas/credential',json={**body,'password':SECRET*300},headers={'Origin':ORIGIN})
        assert invalid.status_code==422 and SECRET not in invalid.text
        result=client.post('/api/settings/nas/credential',json=body,headers={'Origin':ORIGIN})
        assert result.status_code==200 and result.json()['connection_attempted'] is False
        assert SECRET not in result.text
        assert client.get('/api/settings/nas/connection').json()['state']=='disabled'
    assert asyncio.run(settings.resolve_nas_credential(config().credential_ref))==CREDS
    assert SECRET not in ''.join(p.read_text() for p in (tmp_path/'logs').glob('*.jsonl'))

def test_foreign_credential_owner_is_not_shadowed(tmp_path):
    async def foreign(*args,**kwargs):return CREDS
    settings=SettingsService(tmp_path/'settings.json',nas_credential_resolver=foreign)
    with pytest.raises(ValueError,match='external_credential_owner'):
        settings.save_nas_credential(CREDS.username,CREDS.password,expected_generation=1)
    assert not settings._local_nas_credentials.directory.exists()
