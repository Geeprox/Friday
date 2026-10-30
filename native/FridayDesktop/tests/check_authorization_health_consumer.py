"""Actual desktop ASGI authorization/resources, synthetic owned inputs only."""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('auth_health_fixture', HERE/'check_local_consumers.py')
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)


class AuthorizationConsumerChecks(fixture.LocalConsumerChecks):
    async def test_current_health_failure_reaches_actual_authorization_page(self):
        await self.connect()
        old_health = self.wrapper.adapter.health()
        with patch.object(self.wrapper.adapter, 'health', return_value={**old_health, 'authentication_available':False}):
            report = (await self.client.get('/desktop/status')).json()
            self.assertEqual(report['connection'],'failed')
            self.assertEqual(report['failure'],'ha_authorization_rejected')
            self.assertIsNone(self.wrapper.failure)  # Successful original construction.
            page = await self.client.get('/desktop/authorization')
        self.assertEqual(page.status_code,200)
        description = page.text.split('<p>',1)[1].split('</p>',1)[0]
        self.assertIn('家庭授权暂不可用',description)
        self.assertIn('尚不能断定 token 过期',description)
        self.assertNotIn('拒绝了现有授权',description)  # No captured 401/403 here.
        self.assertFalse((self.data/'absent-new-ha-credential.json').exists())

    async def test_actual_rejection_evidence_keeps_precise_authorization_description(self):
        self.wrapper.failure = 'ha_authorization_rejected'
        self.wrapper.failure_evidence = {'httpReads':[{'status':401}]}
        page = await self.client.get('/desktop/authorization')
        description = page.text.split('<p>',1)[1].split('</p>',1)[0]
        self.assertIn('Home Assistant 拒绝了现有授权',description)
        self.assertIn('尚不能断定 token 过期',description)
        self.assertIn('摄像头、语音和家居控制按本机配置与当前授权启用',page.text)
        self.assertIn('其余设备写入仍关闭',page.text)

    async def test_production_asgi_serves_changed_resource_bytes_without_effects(self):
        await self.connect()
        root = Path(__file__).resolve().parents[3]
        for route,rel in [('/static/app.js','homeplane/static/app.js'),
                          ('/static/copy-layout.js','homeplane/static/copy-layout.js'),
                          ('/static/home-conversation.js','homeplane/static/home-conversation.js'),
                          ('/static/file-steward.js','homeplane/static/file-steward.js')]:
            response = await self.client.get(route)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.content,(root/rel).read_bytes())
        entry = await self.client.get('/')
        self.assertIn('data-console-read-only="true"',entry.text)
        self.assertIn('/static/copy-layout.js',entry.text)
        self.assertEqual((await self.client.post('/api/actions/execute',json={})).status_code,409)
        self.assertFalse((self.data/'absent-new-ha-credential.json').exists())

    async def test_malformed_diagnostic_evidence_does_not_claim_rejection_or_fail_page(self):
        self.wrapper.failure = 'ha_authorization_rejected'
        for evidence in [None, {'httpReads':None}, {'httpReads':[{'status':{'SYNTHETIC':'private'}}]},
                         {'httpReads':[{'status':'401'}]}, {'httpReads':[None]}]:
            self.wrapper.failure_evidence = evidence
            page = await self.client.get('/desktop/authorization')
            self.assertEqual(page.status_code,200)
            description = page.text.split('<p>',1)[1].split('</p>',1)[0]
            self.assertIn('家庭授权暂不可用',description)
            self.assertNotIn('拒绝了现有授权',description)


if __name__ == '__main__':
    names = [name for name in AuthorizationConsumerChecks.__dict__ if name.startswith('test_')]
    suite = unittest.TestSuite(AuthorizationConsumerChecks(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
