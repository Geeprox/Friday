"""Actual desktop wrapper + existing ASGI/Console with synthetic owned data only."""
import hashlib
import importlib.util
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('consumer_backend', ROOT / 'backend/desktop_backend.py')
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)
import httpx
from homeplane import api, ha_setup
from homeplane.settings import SettingsConfig
from homeplane.simulator import Simulator

ORIGIN = 'http://127.0.0.1:8740'

def blocked(*_args, **_kwargs):
    raise AssertionError('Offline consumer check forbids sockets and DNS')

class ConsumerChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name).resolve()
        self.patches = [patch.object(backend, name, value) for name, value in {
            'BASE': self.data, 'CONFIG': self.data / 'settings.json',
            'CREDENTIAL': self.data / 'absent-credential.json',
            'NEW_CREDENTIAL': self.data / 'new-credential.json',
            'STATE': self.data / 'desktop', 'config': lambda: SettingsConfig(),
        }.items()]
        self.patches += [patch.object(socket.socket, name, blocked) for name in ('connect', 'connect_ex', 'bind', 'listen')]
        self.patches += [patch.object(socket, name, blocked) for name in ('getaddrinfo', 'create_connection')]
        self.patches += [patch.object(api, 'DEFAULT_HOME', self.data / 'absent-arl'),
                         patch.object(ha_setup, 'CREDENTIAL_PATH', self.data / 'new-credential.json')]
        for p in self.patches:
            p.start()
        self.wrapper = backend.DesktopService()
        class GeneratedAdapter(Simulator):
            async def set_state(self, *_a, **_k):
                raise AssertionError('Device write attempted')
            async def call_service(self, *_a, **_k):
                raise AssertionError('Device service attempted')
        self.inner = api.create_app(GeneratedAdapter(), db_path=self.data / 'traces.sqlite',
                                    settings_path=self.data / 'inner-settings.json',
                                    authority_path=self.data / 'absent-authority.json',
                                    flow_household_timezone='Asia/Shanghai')
        self.lifespan = self.inner.router.lifespan_context(self.inner)
        await self.lifespan.__aenter__()
        self.wrapper.inner = self.inner
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.wrapper), base_url=ORIGIN,
                                        headers={'Origin': ORIGIN})

    async def asyncTearDown(self):
        await self.client.aclose()
        self.wrapper.inner = None
        await self.lifespan.__aexit__(None, None, None)
        self.assertFalse(self.inner.state.home_flow.tasks)
        self.assertFalse(self.inner.state.home_flow.sessions)
        self.assertFalse(self.inner.state.home_flow.bootstrap_tasks)
        for p in reversed(self.patches):
            p.stop()
        self.directory.cleanup()

    async def ready(self):
        r = await self.client.post('/api/home-flow/bootstrap')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['code'], 'flow_ready')
        return r.json()['session_id']

    def request(self, session, **fields):
        return {'session_id': session, 'turn_id': 'turn:one', 'operation_id': 'operation:one',
                'idempotency_key': 'key:one', **fields}

    async def test_existing_widget_assets_and_read_query_reach_actual_domain(self):
        html = await self.client.get('/')
        self.assertEqual(html.status_code, 200)
        self.assertIn('/static/app.js', html.text)
        entry = await self.client.get('/static/app.js')
        self.assertIn('home-conversation.js', entry.text)
        for name in ('home-conversation.js', 'home-conversation.css'):
            pin=hashlib.sha256((Path(__file__).resolve().parents[3]/'homeplane/static'/name).read_bytes()).hexdigest()
            r = await self.client.get('/static/' + name)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(hashlib.sha256(r.content).hexdigest(), pin)
        session = await self.ready()
        r = await self.client.post('/api/home-flow/turn', json=self.request(session, text='为什么书房灯开着', lane='why'))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(r.json()['phase'], ['result', 'clarify', 'select', 'unavailable', 'refused'])
        self.assertNotEqual(r.json()['code'], 'desktop_read_only')
        self.assertEqual(r.json()['input_kind'], 'console_text')

    async def test_session_context_cancel_dispose_keep_original_owner(self):
        session = await self.ready()
        r = await self.client.post('/api/home-flow/context', json={'session_id': session})
        self.assertEqual(r.status_code, 200)
        r = await self.client.post('/api/home-flow/cancel', json=self.request(session, kind='cancel'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['phase'], 'cancelled')
        r = await self.client.post('/api/home-flow/dispose', json={'session_id': session})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(session, self.inner.state.home_flow.sessions)

    async def test_rule_preparation_reaches_original_without_model_or_write(self):
        session = await self.ready()
        r = await self.client.post('/api/home-flow/turn', json=self.request(session, text='以后我回家不要自动开空调'))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(r.json()['phase'], ['preview', 'select', 'refused', 'unavailable', 'result'])
        self.assertFalse((self.data / 'new-credential.json').exists())

    async def test_effects_mode_and_playback_remain_closed(self):
        session = await self.ready()
        for fields in [{'kind': 'approve', 'lane': 'control', 'plan_id': 'plan:one', 'approval_digest': 'a'*64},
                       {'kind': 'playback', 'lane': 'why'},
                       {'text': '我要睡觉'}, {'lane': 'mode', 'text': '晚安'},
                       {'kind': 'reconcile', 'plan_id': 'plan:one'},
                       {'kind': 'rollback', 'plan_id': 'plan:one'}]:
            r = await self.client.post('/api/home-flow/turn', json=self.request(session, **fields))
            self.assertEqual(r.status_code, 409, r.text)
        for path in ['/api/actions/execute', '/api/rules/analysis/approve', '/api/cameras/media/start']:
            r = await self.client.post(path, json={})
            self.assertEqual(r.status_code, 409)

    async def test_origin_budget_and_missing_home_fail_closed(self):
        r = await self.client.post('/api/home-flow/bootstrap', headers={'Origin': 'https://example.invalid'})
        self.assertEqual(r.status_code, 403)
        r = await self.client.post('/api/home-flow/bootstrap', headers={'Origin': ''})
        self.assertEqual(r.status_code, 403)
        r = await self.client.post('/api/home-flow/turn', content=b'x'*8193)
        self.assertEqual(r.status_code, 413)
        r = await self.client.post('/api/home-flow/turn', content=b'{}')
        self.assertEqual(r.status_code, 422)
        self.wrapper.inner = None
        r = await self.client.post('/api/home-flow/bootstrap')
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.json()['simulated'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
