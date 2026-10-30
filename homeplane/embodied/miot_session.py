"""Renew only the saved Mi Home grant; never open a camera or widen its scope."""
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from .paths import PRIVATE, configuration
from .ptz_control import atomic, read_lease

logging.getLogger('miot').setLevel(logging.CRITICAL)
AUTH_ERRORS={-10005,-10020,-10021,-10030}


def authorization_error(error):
    return getattr(error,'code',None) in AUTH_ERRORS or getattr(error,'error_code',None) in AUTH_ERRORS


class MiSession:
    def __init__(self,directory=PRIVATE,*,oauth_factory=None,http_factory=None,clock=time.time):
        self.directory,self.clock=directory,clock
        self.oauth_factory,self.http_factory=oauth_factory,http_factory
        self.path=directory/'miot-session.json'

    def oauth(self,saved,uuid=None):
        # The SDK's existing registered return page is retained. No arbitrary redirect URL.
        redirect=saved.get('oauth_redirect_uri');url=urlsplit(redirect or '')
        if (url.scheme,url.netloc,url.path,url.query,url.fragment)!=('https','mico.api.mijia.tech','/login_redirect','',''):
            raise ValueError('mijia_authorization_configuration')
        factory=self.oauth_factory
        if factory is None:
            from miot.cloud import MIoTOAuth2Client
            factory=MIoTOAuth2Client
        return factory(redirect_uri=redirect,cloud_server=saved['cloud_server'],uuid=uuid or saved['oauth_uuid'])

    def http(self,**options):
        factory=self.http_factory
        if factory is None:
            from miot.cloud import MIoTHttpClient
            factory=MIoTHttpClient
        return factory(**options)

    def claim(self):
        fd=os.open(self.directory/'miot-session.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:os.close(fd);raise
        return fd

    async def current(self):
        saved=read_lease(self.path)
        if saved.get('expires_ts',0)>self.clock()+60:return saved
        fd=self.claim()
        try:
            saved=read_lease(self.path)
            if saved.get('expires_ts',0)>self.clock()+60:return saved
            failure=self.directory/'miot-renewal-failure.json'
            if failure.exists():
                prior=read_lease(failure)
                if prior.get('generation')==hashlib.sha256(self.path.read_bytes()).hexdigest() and prior.get('until',0)>self.clock():
                    raise ValueError('mijia_authorization_required' if prior.get('authorization') else 'mijia_connection_unavailable')
            generation=hashlib.sha256(self.path.read_bytes()).hexdigest();oauth=self.oauth(saved)
            try:
                value=await asyncio.wait_for(oauth.refresh_access_token_async(saved['refresh_token']),20)
                updated={**saved,**{key:getattr(value,key)for key in ('access_token','refresh_token','expires_ts')}}
                if not updated['access_token'] or not updated['refresh_token'] or updated['expires_ts']<=self.clock():raise ValueError('invalid_renewal')
                if hashlib.sha256(self.path.read_bytes()).hexdigest()!=generation:raise ValueError('mijia_session_changed')
                atomic(self.path,updated);failure.unlink(missing_ok=True);return updated
            except Exception as error:
                auth=authorization_error(error)
                atomic(failure,dict(generation=generation,authorization=auth,until=self.clock()+(86400 if auth else 15)))
                raise ValueError('mijia_authorization_required' if auth else 'mijia_connection_unavailable')from None
            finally:await oauth.deinit_async()
        finally:os.close(fd)

    async def check(self,cameras):
        try:
            saved=await self.current();api=self.http(cloud_server=saved['cloud_server'],access_token=saved['access_token'])
            try:
                await asyncio.wait_for(api.get_homes_async(),15)
                from .miot_spec import camera_property
                from miot.types import MIoTGetPropertyParam
                refs=[]
                for camera in cameras:
                    try:
                        siid,piid=camera_property(camera,'camera_power','read');refs.append((camera,siid,piid))
                    except ValueError:pass
                values=await asyncio.wait_for(api.get_props_async([MIoTGetPropertyParam(did=c['device']['did'],siid=s,piid=p)for c,s,p in refs]),15) if refs else []
                states=[]
                for c,s,p in refs:
                    rows=[r for r in values if str(r.get('did'))==str(c['device']['did']) and (r.get('siid'),r.get('piid'))==(s,p)]
                    enabled=rows[0].get('value') if len(rows)==1 and rows[0].get('code')==0 else None
                    states.append(dict(camera=c['id'],enabled=enabled if type(enabled)is bool else None))
                return dict(state='ready',authorization='valid',cameras=states,checked_at=self.clock(),basis='cloud_and_property_readback')
            finally:await api.deinit_async()
        except Exception as error:
            auth=authorization_error(error) or str(error)=='mijia_authorization_required'
            return dict(state='authorization_required' if auth else 'unreachable',authorization='expired' if auth else 'unknown',cameras=[],checked_at=self.clock())

    async def begin(self):
        saved=read_lease(self.path);uuid=secrets.token_hex(16);oauth=self.oauth(saved,uuid)
        try:
            result=dict(uuid=uuid,state=oauth.state,expires=self.clock()+300,generation=hashlib.sha256(self.path.read_bytes()).hexdigest())
            fd=self.claim()
            try:atomic(self.directory/'miot-authorization-pending.json',result)
            finally:os.close(fd)
            return dict(authorization_url=oauth.gen_auth_url(skip_confirm=False),expires_at=result['expires'],return_mode='registered_return_page')
        finally:await oauth.deinit_async()

    async def finish(self,returned_url):
        if not isinstance(returned_url,str) or len(returned_url)>4096:raise ValueError('mijia_return_invalid')
        url=urlsplit(returned_url)
        if (url.scheme,url.netloc,url.path,url.fragment)!=('https','mico.api.mijia.tech','/login_redirect',''):raise ValueError('mijia_return_invalid')
        query=parse_qs(url.query);fd=self.claim()
        try:
            path=self.directory/'miot-authorization-pending.json';pending=read_lease(path);saved=read_lease(self.path)
            if (pending.get('used') or pending['expires']<self.clock() or len(query.get('code',[]))!=1 or len(query.get('state',[]))!=1
                    or not secrets.compare_digest(query['state'][0],pending['state'])
                    or hashlib.sha256(self.path.read_bytes()).hexdigest()!=pending['generation']):raise ValueError('mijia_authorization_expired')
            # A code exchange with an unknown outcome is never silently replayed.
            atomic(path,dict(pending,used=True));oauth=self.oauth(saved,pending['uuid'])
            try:
                tokens=await asyncio.wait_for(oauth.get_access_token_async(query['code'][0]),20)
                api=self.http(cloud_server=saved['cloud_server'],access_token=tokens.access_token)
                try:account=await asyncio.wait_for(api.get_user_info_async(),15)
                finally:await api.deinit_async()
                if str(account.uid)!=str(saved.get('user_info',{}).get('uid')):raise ValueError('mijia_account_changed')
                if hashlib.sha256(self.path.read_bytes()).hexdigest()!=pending['generation']:raise ValueError('mijia_session_changed')
                updated={**saved,**{key:getattr(tokens,key)for key in ('access_token','refresh_token','expires_ts')},'oauth_uuid':pending['uuid']}
                atomic(self.path,updated);(self.directory/'miot-renewal-failure.json').unlink(missing_ok=True);path.unlink(missing_ok=True)
                return dict(state='ready',authorization='valid')
            finally:await oauth.deinit_async()
        finally:os.close(fd)


async def run():
    import sys
    request=json.loads(sys.stdin.read(8192));owner=MiSession();action=request.get('action')
    if action=='status':return await owner.check(configuration()['cameras'])
    if action=='begin':return await owner.begin()
    if action=='finish':return await owner.finish(request['return_url'])
    raise ValueError('mijia_connection_request_invalid')


if __name__=='__main__':
    try:print(json.dumps(asyncio.run(run())))
    except Exception:print(json.dumps(dict(state='unavailable',code='mijia_connection_unavailable')))
