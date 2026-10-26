"""No-grant metadata/landing checks with only generated local authorization files."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('authorization_backend', ROOT / 'backend/desktop_backend.py')
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)
import httpx
from homeplane.settings import SettingsConfig

class AuthorizationMetadataChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'settings.json'
        self.original = self.root / 'original-authorization.json'
        self.new = self.root / 'new-authorization.json'
        self.config.write_text('{}')
        self.original.write_text('{}')
        self.patches = [patch.object(backend, name, value) for name, value in {
            'BASE': self.root, 'CONFIG': self.config, 'CREDENTIAL': self.original,
            'NEW_CREDENTIAL': self.new, 'config': lambda: SettingsConfig(ha_url='http://test.invalid'),
        }.items()]
        for p in self.patches:
            p.start()
        (self.root / 'READ-ONLY-HA-STATUS.json').write_text(json.dumps({
            'credentialMtimeNS': self.original.stat().st_mtime_ns, 'configMtimeNS': self.config.stat().st_mtime_ns,
            'failure': 'ha_authorization_rejected', 'evidence': {'source': 'generated_prior_probe',
            'httpReads': [{'resource': '/api/states', 'status': 401, 'method': 'GET'}], 'credentialRefreshAttempted': False}}))

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    async def test_cached_rejection_does_not_claim_a_new_grant_failed_or_connected(self):
        old = backend.DesktopService()
        self.assertEqual(old.connection, 'failed')
        self.assertIsNone(backend.authorization_revision())
        self.new.write_text('{}')
        new = backend.DesktopService()
        self.assertEqual(new.connection, 'not_connected')
        self.assertIsNone(new.failure)
        self.assertIsNotNone(backend.authorization_revision())
        self.assertEqual(self.original.read_text(), '{}')

    async def test_revision_is_only_a_change_hint_not_credential_validity(self):
        self.new.write_text('{}')
        first = backend.authorization_revision()
        self.assertIsNotNone(first)
        facts = backend.connection_configuration()
        self.assertFalse(facts['credentialAvailable'])
        self.assertNotIn('access_token', json.dumps(facts))
        self.new.write_text('{"generated":"new"}')
        self.assertNotEqual(backend.authorization_revision(), first)

    async def test_landing_requires_user_click_and_does_not_create_authorization(self):
        service = backend.DesktopService()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url=backend.ORIGIN) as client:
            r = await client.get('/desktop/authorization')
            self.assertEqual(r.status_code, 200)
            self.assertIn('在 Home Assistant 中重新登录并授权', r.text)
            self.assertIn(".onclick=async", r.text)
            self.assertIn('尚不能断定 token 过期', r.text)
            self.assertFalse(self.new.exists())
            service.failure = 'ha_unreachable'
            r = await client.get('/desktop/authorization')
            self.assertNotIn('拒绝了现有授权', r.text)
            self.assertFalse(self.new.exists())

if __name__ == '__main__':
    unittest.main(verbosity=2)
