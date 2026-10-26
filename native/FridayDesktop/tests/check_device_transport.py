"""Offline actual desktop HTTP boundary: HA service ACK is not an OAuth response."""
import importlib.util
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('device_transport_backend',ROOT/'backend/desktop_backend.py')
backend=importlib.util.module_from_spec(spec);sys.modules[spec.name]=backend;spec.loader.exec_module(backend)
import httpx
from friday.home import private_json
URL='http://synthetic-ha.invalid:8123'
RealClient=httpx.AsyncClient

class TransportChecks(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.directory=tempfile.TemporaryDirectory();self.path=Path(self.directory.name)/'SYNTHETIC-grant.json';self.calls=[]
  private_json(self.path,dict(ha_url=URL,access_token='SYNTHETIC-access',refresh_token='SYNTHETIC-refresh',client_id='SYNTHETIC-client',expires_at=time.time()+1800,expires_in=1800))
  async def transport(request):
   self.calls.append((request.method,request.url.path));return httpx.Response(200,json=[])
  def client(**kwargs):return RealClient(transport=httpx.MockTransport(transport),**kwargs)
  def credentials(settings,http):
   value=backend.Credentials(SimpleNamespace(ha_url=URL,ha_token_env='FRIDAY_NO_ENV_CREDENTIALS'),http);value.path=self.path;return value
  def blocked(*args,**kwargs):raise AssertionError('No real network permitted by transport fixture')
  self.patches=[patch.object(backend,'config',lambda:backend.SettingsConfig(ha_url=URL,ha_instance='SYNTHETIC')),patch.object(backend,'credentials',credentials),patch.object(backend.httpx,'AsyncClient',client),patch.object(backend.HAReadOnlyAdapter,'_inventory',AsyncMock())]
  self.patches += [patch.object(socket.socket,n,blocked) for n in ('connect','connect_ex','bind','listen')]
  self.patches += [patch.object(socket,n,blocked) for n in ('create_connection','getaddrinfo')]
  for p in self.patches:p.start()
  self.adapter=None

 async def open(self,enabled):
  token=backend._device_control.set(enabled)
  try:_,self.adapter,_=await backend.real_adapter()
  finally:backend._device_control.reset(token)
  return self.adapter._client

 async def asyncTearDown(self):
  if self.adapter is not None:await self.adapter.close()
  for p in reversed(self.patches):p.stop()
  self.directory.cleanup()

 async def test_accepted_light_service_array_keeps_ack_and_never_enters_refresh_validation(self):
  client=await self.open(True);before=self.path.read_bytes()
  response=await client.post(URL+'/api/services/light/turn_on',json={'entity_id':'light.synthetic'})
  self.assertEqual(response.status_code,200);self.assertEqual(response.json(),[])
  self.assertEqual(self.path.read_bytes(),before)

 async def test_state_only_cover_open_close_reach_the_actual_desktop_transport(self):
  client=await self.open(True)
  for operation in ('open_cover','close_cover'):
   response=await client.post(URL+'/api/services/cover/'+operation,json={'entity_id':'cover.synthetic'})
   self.assertEqual(response.status_code,200)
   self.assertIn(('POST','/api/services/cover/'+operation),self.calls)

 async def test_no_control_context_still_rejects_service_posts_before_transport(self):
  client=await self.open(False);before=len(self.calls)
  with self.assertRaises(ValueError):await client.post(URL+'/api/services/light/turn_on',json={'entity_id':'light.synthetic'})
  self.assertEqual(len(self.calls),before)

 async def test_unregistered_service_or_different_origin_never_reaches_transport(self):
  client=await self.open(True);before=len(self.calls)
  for url in (URL+'/api/services/system/restart','http://other-synthetic.invalid:8123/api/services/light/turn_on'):
   with self.assertRaises(ValueError):await client.post(url,json={'entity_id':'light.synthetic'})
  self.assertEqual(len(self.calls),before)

if __name__=='__main__':unittest.main(verbosity=2)
