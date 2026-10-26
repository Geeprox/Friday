"""Necessary invariants for a real read-only desktop, not simulated product acceptance."""
import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('desktop_backend', ROOT / 'backend/desktop_backend.py')
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)


class ReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_cached_credential_never_refreshes(self):
        class Client:
            async def post(self, *_args, **_kwargs):
                raise AssertionError('Grant refresh attempted')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'authorization.json'
            path.write_text(json.dumps({'ha_url': 'http://test.invalid', 'access_token': 'synthetic-token',
                                        'refresh_token': 'synthetic-refresh', 'expires_at': 0}))
            before = path.read_bytes()
            saved = backend.Credentials(SimpleNamespace(ha_url='http://test.invalid', ha_token_env='UNUSED'), Client())
            saved.path = path
            self.assertEqual(await saved.token(allow_refresh=False), 'synthetic-token')
            self.assertEqual(path.read_bytes(), before)

    async def test_missing_home_is_unavailable_and_controls_are_closed(self):
        import httpx
        service = backend.DesktopService()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url='http://127.0.0.1:8740',
                                     headers={'Origin': 'http://127.0.0.1:8740'}) as client:
            result = await client.get('/api/snapshot')
            self.assertEqual(result.status_code, 503)
            self.assertFalse(result.json()['simulated'])
            result = await client.post('/api/actions/execute', json={})
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json()['code'], 'desktop_read_only')
            result = await client.post('/desktop/connect', headers={'Origin': 'https://example.invalid'})
            self.assertEqual(result.status_code, 403)

    async def test_exact_upstream_sdk_is_importable(self):
        import arl
        import importlib.metadata
        self.assertEqual(arl.PROTOCOL, 'arl/1')
        self.assertEqual(importlib.metadata.version('arl-runtime'), '0.1.1')
        from arl.friday_planning_client import plan_removal, ReviewedRemoval
        self.assertTrue(callable(plan_removal) and callable(ReviewedRemoval))


if __name__ == '__main__':
    unittest.main(verbosity=2)
