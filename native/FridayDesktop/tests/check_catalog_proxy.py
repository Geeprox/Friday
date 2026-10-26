"""Actual desktop ASGI boundary; local catalog writes never imply device scope."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
import httpx
from fastapi import FastAPI

spec=importlib.util.spec_from_file_location('catalog_proxy_backend',Path(__file__).resolve().parents[1]/'backend/desktop_backend.py')
backend=importlib.util.module_from_spec(spec);sys.modules[spec.name]=backend;spec.loader.exec_module(backend)

class CatalogProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_local_catalog_origin_only_and_no_device_scope(self):
        app=FastAPI();calls=[]
        @app.post('/api/home/automations/catalog')
        async def save():
            calls.append('local-catalog');return {'stored':True}
        app.state.core=SimpleNamespace(actions=SimpleNamespace(policy_report=lambda:{'enabled':False,'configured':False}))
        wrapper=backend.DesktopService.__new__(backend.DesktopService)
        wrapper.runtime_log=backend.RuntimeLog()
        wrapper.inner=app;wrapper.authorization_paths=set();wrapper.planning_authorizer=None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapper),base_url=backend.ORIGIN) as client:
            for origin in (None,'https://outside.invalid'):
                r=await client.post('/api/home/automations/catalog',json={},headers={} if origin is None else {'Origin':origin})
                self.assertEqual(r.status_code,403)
            allowed=await client.post('/api/home/automations/catalog',json={},headers={'Origin':backend.ORIGIN})
            self.assertEqual(allowed.status_code,200);self.assertEqual(calls,['local-catalog'])
            for path in ('/api/home/automations/execute','/api/actions/execute'):
                denied=await client.post(path,json={},headers={'Origin':backend.ORIGIN})
                self.assertEqual(denied.status_code,409)
            self.assertEqual(calls,['local-catalog'])

if __name__=='__main__':unittest.main(verbosity=2)
