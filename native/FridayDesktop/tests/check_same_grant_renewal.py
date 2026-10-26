"""Offline HA-shaped token responses and the actual production adapter; synthetic secrets only."""
import asyncio
import contextlib
import importlib.util
import io
import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('renewal_backend', ROOT/'backend/desktop_backend.py')
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)
import httpx
from friday.home import private_json

URL = 'http://SYNTHETIC-ha.invalid:8123'


def blocked(*_args, **_kwargs):
    raise AssertionError('Offline renewal fixture forbids sockets and DNS')


class RenewalChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/'SYNTHETIC-grant.json'
        self.calls, self.response, self.status = [], None, 200
        self.before_response = None
        self.renewal = backend.SameGrantRenewal()
        self.settings = backend.SettingsConfig(ha_url=URL, ha_instance='SYNTHETIC-instance')
        client_class = httpx.AsyncClient
        async def transport(request):
            self.calls.append((request.method, request.url.path, request))
            if request.url.path == '/auth/token':
                form = parse_qs(request.content.decode())
                self.assertEqual(form['grant_type'], ['refresh_token'])
                self.assertEqual(set(form), {'grant_type', 'refresh_token', 'client_id'})
                if self.before_response:
                    await self.before_response()
                value = self.response if self.response is not None else {
                    'access_token': 'SYNTHETIC-renewed-access', 'token_type': 'Bearer', 'expires_in': 1800}
                return httpx.Response(self.status, json=value)
            self.assertEqual(request.method, 'GET', 'An external effect reached transport')
            self.assertIn(request.url.path, {'/api/states', '/api/services'})
            return httpx.Response(200, json=[] if request.url.path == '/api/states' else [])
        def client(**kwargs):
            return client_class(transport=httpx.MockTransport(transport), **kwargs)
        def credentials(settings, http):
            value = backend.Credentials(type('Config', (), {'ha_url': settings.ha_url,
                                        'ha_token_env': 'FRIDAY_NO_ENV_CREDENTIALS'})(), http)
            value.path = self.path
            return value
        self.patches = [patch.object(backend, 'config', lambda: self.settings),
                        patch.object(backend, 'credentials', credentials),
                        patch.object(backend.httpx, 'AsyncClient', client),
                        patch.object(backend.HAReadOnlyAdapter, '_inventory', AsyncMock())]
        self.patches += [patch.object(socket.socket, n, blocked) for n in ('connect', 'connect_ex', 'bind', 'listen')]
        self.patches += [patch.object(socket, n, blocked) for n in ('create_connection', 'getaddrinfo')]
        for p in self.patches:
            p.start()
        self.adapters = []
        self.write(expires_at=time.time()-1)

    def write(self, **changes):
        value = {'ha_url': URL, 'access_token': 'SYNTHETIC-saved-access',
                 'refresh_token': 'SYNTHETIC-same-grant-refresh', 'client_id': 'SYNTHETIC-client',
                 'expires_at': time.time()+1800, 'expires_in': 1800}
        value.update(changes)
        private_json(self.path, value)
        self.path.chmod(0o600)
        return value

    async def adapter(self):
        _, adapter, snapshot = await backend.real_adapter(self.renewal)
        self.adapters.append(adapter)
        self.assertEqual(snapshot.mode, 'ha-read-only')
        self.assertEqual(adapter.health()['transport'], 'online')
        return adapter

    async def asyncTearDown(self):
        for adapter in self.adapters:
            await adapter.close()
        for p in reversed(self.patches):
            p.stop()
        self.directory.cleanup()

    def refresh_count(self):
        return sum(method == 'POST' for method, _, _ in self.calls)

    async def test_expired_initial_token_renews_same_grant_before_HA_GET(self):
        before = json.loads(self.path.read_text())
        adapter = await self.adapter()
        self.assertEqual([(m, p) for m, p, _ in self.calls],
                         [('POST', '/auth/token'), ('GET', '/api/states'), ('GET', '/api/services')])
        after = json.loads(self.path.read_text())
        self.assertEqual(after['refresh_token'], before['refresh_token'])
        self.assertEqual(after['client_id'], before['client_id'])
        self.assertEqual(after['ha_url'], before['ha_url'])
        self.assertGreater(after['expires_at'], time.time()+1700)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(r.headers['Authorization'] == 'Bearer SYNTHETIC-renewed-access'
                            for m, _, r in self.calls if m == 'GET'))
        self.assertEqual(adapter._desktop_http_reads[0], {'resource': '/auth/token', 'status': 200, 'method': 'POST'})

    async def test_valid_access_and_later_provider_renewal_without_old_cache(self):
        self.write()
        adapter = await self.adapter()
        self.assertEqual(self.refresh_count(), 0)
        self.write(expires_at=time.time()+30)
        await adapter.snapshot()
        self.assertEqual(self.refresh_count(), 1)
        self.assertEqual(self.calls[-2][2].headers['Authorization'], 'Bearer SYNTHETIC-renewed-access')
        await adapter.snapshot()
        self.assertEqual(self.refresh_count(), 1)

    async def test_concurrent_refresh_is_one_post(self):
        # Actual adapter token provider owns the same renewal coordinator.
        self.write()
        adapter = await self.adapter()
        self.write(expires_at=time.time()-1)
        values = await asyncio.gather(*[adapter._token_provider() for _ in range(12)])
        self.assertEqual(values, ['SYNTHETIC-renewed-access']*12)
        self.assertEqual(self.refresh_count(), 1)

    async def test_rejected_refresh_no_HA_GET_no_burst_until_generation_changes(self):
        before = self.path.read_bytes()  # Synthetic fixture bytes only.
        self.status, self.response = 400, {'error': 'invalid_grant'}
        for _ in range(3):
            with self.assertRaises(backend.RealHomeFailure) as caught:
                await self.adapter()
            self.assertEqual(str(caught.exception), 'ha_authorization_rejected')
        self.assertEqual(self.refresh_count(), 1)
        self.assertFalse(any(m == 'GET' for m, _, _ in self.calls))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.renewal.refresh_suppressed)
        self.status, self.response = 200, None
        self.write(expires_at=time.time()-1, refresh_token='SYNTHETIC-replaced-grant')
        await self.adapter()
        self.assertEqual(self.refresh_count(), 2)

    async def test_no_refresh_capability_refuses_without_token_endpoint(self):
        self.write(expires_at=time.time()-1, refresh_token=None)
        with self.assertRaises(backend.RealHomeFailure) as caught:
            await self.adapter()
        self.assertEqual(str(caught.exception), 'ha_authorization_rejected')
        self.assertFalse(self.calls)

    async def test_malformed_response_never_overwrites_existing_grant(self):
        for value in [{}, {'access_token': 'SYNTHETIC-new', 'expires_in': True},
                      {'access_token': 'SYNTHETIC-new', 'expires_in': 1800, 'client_id': 'SYNTHETIC-other'},
                      {'access_token': 'SYNTHETIC-new', 'expires_in': 1800, 'token_type': 4}]:
            with self.subTest(response_shape=list(value)):
                self.write(expires_at=time.time()-1)
                self.renewal = backend.SameGrantRenewal()
                self.response = value
                before = self.path.read_bytes()
                with self.assertRaises(backend.RealHomeFailure):
                    await self.adapter()
                self.assertEqual(self.path.read_bytes(), before)

    async def test_narrow_refresh_request_gate_and_no_other_POST(self):
        adapter = await self.adapter()
        before = len(self.calls)
        normal = {'grant_type': 'refresh_token', 'refresh_token': 'SYNTHETIC-same-grant-refresh',
                  'client_id': 'SYNTHETIC-client'}
        for path, form in [('/api/services/light/turn_on', normal),
                           ('/auth/token', {**normal, 'grant_type': 'authorization_code'}),
                           ('/auth/token', {**normal, 'scope': 'SYNTHETIC-extra'}),
                           ('/auth/token', {**normal, 'client_id': 'SYNTHETIC-other'}),
                           ('/auth/token', {**normal, 'refresh_token': 'SYNTHETIC-other'})]:
            with self.subTest(path=path, fields=list(form)):
                with self.assertRaises(ValueError):
                    await adapter._client.post(URL+path, data=form)
        self.assertEqual(len(self.calls), before)

    async def test_generation_replacement_during_refresh_is_preserved(self):
        replacement = None
        async def replace():
            nonlocal replacement
            self.write(access_token='SYNTHETIC-user-replacement', refresh_token='SYNTHETIC-user-refresh')
            replacement = self.path.read_bytes()
        self.before_response = replace
        with self.assertRaises(backend.RealHomeFailure):
            await self.adapter()
        self.assertEqual(self.path.read_bytes(), replacement)
        self.before_response = None
        await self.adapter()
        self.assertEqual(self.refresh_count(), 1)
        self.assertEqual(self.calls[-2][2].headers['Authorization'], 'Bearer SYNTHETIC-user-replacement')

    async def test_cancelled_refresh_does_not_persist_and_can_recover(self):
        entered, release = asyncio.Event(), asyncio.Event()
        before = self.path.read_bytes()
        async def wait():
            entered.set()
            await release.wait()
        self.before_response = wait
        caller = asyncio.create_task(self.adapter())
        await asyncio.wait_for(entered.wait(), 1)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(self.path.read_bytes(), before)
        self.before_response = None
        await self.adapter()
        self.assertEqual(self.refresh_count(), 2)

    async def test_transient_refresh_failure_cools_down_then_recovers(self):
        clock = [1000.0]
        self.status, self.response = 503, {'error': 'SYNTHETIC-temporary-unavailable'}
        with patch.object(backend.time, 'monotonic', lambda: clock[0]):
            for _ in range(2):
                with self.assertRaises(backend.RealHomeFailure) as caught:
                    await self.adapter()
                self.assertEqual(str(caught.exception), 'ha_http_error')
            self.assertEqual(self.refresh_count(), 1)
            clock[0] += 61
            self.status, self.response = 200, None
            await self.adapter()
        self.assertEqual(self.refresh_count(), 2)

    async def test_provider_refresh_rotation_retains_owner_and_scope(self):
        self.response = {'access_token': 'SYNTHETIC-renewed-access', 'refresh_token': 'SYNTHETIC-rotated-refresh',
                         'expires_in': 1800, 'token_type': 'Bearer'}
        await self.adapter()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['refresh_token'], 'SYNTHETIC-rotated-refresh')
        self.assertEqual(saved['client_id'], 'SYNTHETIC-client')
        self.assertEqual(saved['ha_url'], URL)
        self.assertEqual(self.refresh_count(), 1)

    async def test_probe_reports_actual_refresh_without_secret_values(self):
        output = io.StringIO()
        with patch.object(backend, 'NEW_CREDENTIAL', self.path), \
                patch.object(backend, 'arl_status', AsyncMock(return_value={'status': 'SYNTHETIC-unavailable'})), \
                contextlib.redirect_stdout(output):
            await backend.probe()
        result = json.loads(output.getvalue())
        self.assertTrue(result['grantRefreshAttempted'])
        self.assertFalse(result['grantRefreshSuppressed'])
        self.assertFalse(result['credentialValuesExported'])
        self.assertNotIn('SYNTHETIC-same-grant-refresh', output.getvalue())
        self.assertNotIn('SYNTHETIC-renewed-access', output.getvalue())

    async def test_short_refresh_ttl_is_rejected_once_and_suppressed(self):
        for ttl in [1, 30, 60]:
            with self.subTest(expires_in=ttl):
                self.write(expires_at=time.time()-1)
                self.renewal = backend.SameGrantRenewal()
                self.response = {'access_token': 'SYNTHETIC-short-access',
                                 'token_type': 'Bearer', 'expires_in': ttl}
                before = self.path.read_bytes()
                offset, count = len(self.calls), self.refresh_count()
                results = await asyncio.gather(*[self.adapter() for _ in range(12)], return_exceptions=True)
                self.assertTrue(all(isinstance(value, backend.RealHomeFailure) for value in results))
                self.assertTrue(all(str(value) == 'ha_configuration_or_protocol_error' for value in results))
                self.assertEqual(self.refresh_count()-count, 1)
                self.assertFalse(any(method == 'GET' for method, _, _ in self.calls[offset:]))
                self.assertEqual(self.path.read_bytes(), before)
                self.assertTrue(self.renewal.refresh_suppressed)
        # The persistent provider of an already-connected adapter has the same bound.
        self.write()
        self.response = None
        self.renewal = backend.SameGrantRenewal()
        adapter = await self.adapter()
        self.write(expires_at=time.time()-1)
        self.response = {'access_token': 'SYNTHETIC-short-access', 'token_type': 'Bearer', 'expires_in': 30}
        before = self.path.read_bytes()
        offset, count = len(self.calls), self.refresh_count()
        results = await asyncio.gather(*[adapter._token_provider() for _ in range(12)], return_exceptions=True)
        self.assertTrue(all(isinstance(value, RuntimeError) for value in results))
        self.assertEqual(self.refresh_count()-count, 1)
        self.assertFalse(any(method == 'GET' for method, _, _ in self.calls[offset:]))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.renewal.refresh_suppressed)

    async def test_normal_1800_ttl_waiters_share_one_refresh(self):
        self.write()
        adapter = await self.adapter()
        self.write(expires_at=time.time()-1)
        self.response = {'access_token': 'SYNTHETIC-renewed-access', 'token_type': 'Bearer', 'expires_in': 1800}
        async def yield_to_waiters():
            await asyncio.sleep(0)
        self.before_response = yield_to_waiters
        count = self.refresh_count()
        values = await asyncio.gather(*[adapter._token_provider() for _ in range(12)])
        self.assertEqual(values, ['SYNTHETIC-renewed-access']*12)
        self.assertEqual(self.refresh_count()-count, 1)
        self.assertFalse(self.renewal.refresh_suppressed)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['expires_in'], 1800)
        self.assertEqual(saved['client_id'], 'SYNTHETIC-client')
        self.assertEqual(saved['ha_url'], URL)
        self.assertGreater(saved['expires_at'], time.time()+1700)


if __name__ == '__main__':
    unittest.main(verbosity=2)
