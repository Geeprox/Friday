"""Saved-grant recovery and explicit reauthorization, with all cloud IO mocked."""
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from homeplane.embodied.miot_session import MiSession
from homeplane.embodied.ptz_control import atomic


def rig(tmp_path,*,expired=False):
    calls=[];fault={'refresh':None,'uid':'same-account'}
    tokens=SimpleNamespace(access_token='new-private-access',refresh_token='new-private-refresh',expires_ts=8000)
    class OAuth:
        def __init__(self,**kwargs):self.state='nonce:'+kwargs['uuid'];calls.append(('oauth',kwargs))
        async def refresh_access_token_async(self,token):
            calls.append(('renew',token))
            if fault['refresh']:raise fault['refresh']
            return tokens
        def gen_auth_url(self,**kwargs):return 'https://account.xiaomi.com/oauth2/authorize?'+urlencode({'state':self.state})
        async def get_access_token_async(self,code):calls.append(('exchange',code));return tokens
        async def deinit_async(self):pass
    class HTTP:
        def __init__(self,**kwargs):calls.append(('http',kwargs))
        async def get_user_info_async(self):return SimpleNamespace(uid=fault['uid'])
        async def get_homes_async(self):return {'saved-home':{}}
        async def deinit_async(self):pass
    saved=dict(access_token='old-private-access',refresh_token='old-private-refresh',expires_ts=0 if expired else 5000,
        oauth_redirect_uri='https://mico.api.mijia.tech/login_redirect',cloud_server='cn',oauth_uuid='saved-device',user_info={'uid':'same-account'})
    atomic(tmp_path/'miot-session.json',saved)
    return MiSession(tmp_path,oauth_factory=OAuth,http_factory=HTTP,clock=lambda:1000),calls,fault,saved


@pytest.mark.asyncio
async def test_saved_grant_renews_once_without_camera_or_account_expansion(tmp_path):
    owner,calls,_,saved=rig(tmp_path,expired=True)
    first=await owner.current();assert first['user_info']==saved['user_info'] and first['oauth_uuid']==saved['oauth_uuid']
    assert (await owner.current())==first
    assert len([c for c in calls if c[0]=='renew'])==1
    assert (tmp_path/'miot-session.json').stat().st_mode&0o077==0
    assert not any(c[0]=='http' for c in calls)


@pytest.mark.asyncio
async def test_permanent_rejection_is_distinct_from_network_and_does_not_retry(tmp_path):
    owner,calls,fault,saved=rig(tmp_path,expired=True)
    class Rejected(Exception):code=-10021
    fault['refresh']=Rejected('private upstream response')
    for _ in range(2):
        with pytest.raises(ValueError,match='mijia_authorization_required'):await owner.current()
    assert len([c for c in calls if c[0]=='renew'])==1
    assert json.loads((tmp_path/'miot-session.json').read_text())==saved
    assert 'private upstream response' not in (tmp_path/'miot-renewal-failure.json').read_text()


@pytest.mark.asyncio
async def test_authorization_is_nonce_bound_one_time_and_preserves_same_account(tmp_path):
    owner,calls,_,saved=rig(tmp_path)
    started=await owner.begin();assert started['authorization_url'].startswith('https://account.xiaomi.com/')
    pending=json.loads((tmp_path/'miot-authorization-pending.json').read_text())
    returned=saved['oauth_redirect_uri']+'?'+urlencode({'state':pending['state'],'code':'private-once-code'})
    result=await owner.finish(returned);assert result=={'state':'ready','authorization':'valid'}
    updated=json.loads((tmp_path/'miot-session.json').read_text());assert updated['user_info']==saved['user_info']
    assert updated['oauth_uuid']!=saved['oauth_uuid']
    assert all('private-once-code' not in path.read_text() for path in tmp_path.glob('*.json'))
    with pytest.raises((ValueError,OSError)):await owner.finish(returned)
    assert len([c for c in calls if c[0]=='exchange'])==1


@pytest.mark.asyncio
async def test_foreign_redirect_nonce_or_account_cannot_replace_session(tmp_path):
    owner,calls,fault,saved=rig(tmp_path);await owner.begin()
    pending=json.loads((tmp_path/'miot-authorization-pending.json').read_text())
    with pytest.raises(ValueError):await owner.finish('https://example.invalid/?code=secret&state=wrong')
    with pytest.raises(ValueError):await owner.finish(saved['oauth_redirect_uri']+'?code=secret&state=wrong')
    assert not any(c[0]=='exchange' for c in calls)
    fault['uid']='different-account'
    with pytest.raises(ValueError,match='mijia_account_changed'):
        await owner.finish(saved['oauth_redirect_uri']+'?'+urlencode({'state':pending['state'],'code':'one-code'}))
    assert json.loads((tmp_path/'miot-session.json').read_text())==saved
