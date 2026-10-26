"""Outer desktop HTTP to the existing read/preview domain; generated data only."""
import asyncio
import importlib.util
import json
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

spec = importlib.util.spec_from_file_location('read_fixture', Path(__file__).with_name('check_desktop_consumer.py'))
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)
backend, api = fixture.backend, fixture.api


class ReadChecks(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ConsumerChecks.asyncSetUp
    asyncTearDown = fixture.ConsumerChecks.asyncTearDown

    async def query(self, **fields):
        response = await self.client.post('/api/query', json={'kind': 'state', 'rooms': ['living-room'], **fields})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_actual_ui_three_query_kinds_and_metadata(self):
        capabilities = ['temperature', 'humidity', 'light.state', 'climate.state',
                        'cover.state', 'lock.state', 'fan.state', 'contact.transition']
        for fields in [dict(capabilities=capabilities), dict(kind='devices'),
                       dict(kind='history', start=(datetime.now(UTC)-timedelta(hours=1)).isoformat(),
                            end=datetime.now(UTC).isoformat())]:
            report = await self.query(limit=20, **fields)
            self.assertIn(report['status'], {'ok', 'empty', 'partial'})
            self.assertEqual(report['source'], 'home')
            self.assertTrue(report['simulated'])
            self.assertFalse(report['execution_authority'])
            self.assertFalse(report['household_media_sent'])
            self.assertEqual(report['permission_scope']['subject'], 'console')
            self.assertEqual(report['permission_scope']['boundary'], 'loopback_console')
            self.assertIsNone(report['confidence'])
            if report['kind'] != 'history':
                self.assertTrue(report['items'])
        state = await self.query(entities=['sensor.living.temperature'], capabilities=['temperature'])
        self.assertEqual(state['items'][0]['value'], 24.5)
        self.assertEqual(state['items'][0]['as_of_basis'], 'canonical_resolution_not_hardware_sample')

    async def test_cursor_selection_and_source_change_remain_domain_owned(self):
        first = await self.query(limit=1)
        self.assertTrue(first['next_cursor'])
        second = await self.query(limit=1, cursor=first['next_cursor'])
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        changed = await self.query(limit=1, rooms=['hall'], cursor=first['next_cursor'])
        self.assertEqual(changed['code'], 'query_context_changed')
        self.inner.state.queries.core.adapter.instance_id = 'SYNTHETIC-source-switched'
        changed = await self.query(limit=1, cursor=first['next_cursor'])
        self.assertEqual(changed['code'], 'query_context_changed')
        invalid = await self.query(cursor='SYNTHETIC-invalid-cursor')
        self.assertEqual(invalid['code'], 'invalid_query_cursor')
        self.assertFalse(invalid['items'])

    async def test_no_client_grant_and_original_permission_refusals(self):
        request_scope = api.ReadScope
        for change, expected in [({'permitted': False}, 'read_permission_required'),
                                 ({'all_home': False}, 'outside_read_scope'),
                                 ({'history': False}, 'history_permission_required')]:
            with patch.object(api, 'ReadScope', lambda **values: replace(request_scope(**values), **change)):
                fields = {'kind': 'history', 'start': (datetime.now(UTC)-timedelta(hours=1)).isoformat(),
                          'end': datetime.now(UTC).isoformat()} if 'history' in change else {}
                report = await self.query(**fields)
                self.assertEqual(report['code'], expected)
                self.assertEqual(report['status'], 'refused')
                self.assertFalse(report['items'])
        response = await self.client.post('/api/query', json={
            'kind': 'state', 'rooms': ['living-room'], 'scope': {'all_home': True}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('all_home', response.text)

    async def test_source_failure_deadline_and_caller_cancellation(self):
        core = self.inner.state.queries.core
        with patch.object(core, 'snapshot', AsyncMock(side_effect=RuntimeError('SYNTHETIC-private-fault'))):
            report = await self.query()
            self.assertEqual(report['code'], 'query_source_unavailable')
            self.assertNotIn('private-fault', json.dumps(report))
        entered, release = asyncio.Event(), asyncio.Event()
        snapshot = core.snapshot
        async def wait_snapshot():
            entered.set()
            await release.wait()
            return await snapshot()
        with patch.object(core, 'snapshot', wait_snapshot):
            report = await self.query(timeout_ms=10)
            self.assertEqual(report['code'], 'query_deadline_exceeded')
            entered.clear()
            caller = asyncio.create_task(self.query())
            await asyncio.wait_for(entered.wait(), 1)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
        report = await self.query()
        self.assertNotEqual(report['status'], 'refused')
        self.assertFalse(self.inner.state.queries.gate.locked())

    async def test_chunked_exact_body_and_disconnect_before_domain(self):
        real_inner = self.wrapper.inner
        forwarded = []
        async def observe(scope, receive, send):
            event = await receive()
            forwarded.append(event['body'])
            consumed = False
            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return event
                return await receive()
            await real_inner(scope, replay, send)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': '/api/query', 'raw_path': b'/api/query',
                 'query_string': b'', 'root_path': '', 'server': ('127.0.0.1', 8740),
                 'client': ('127.0.0.1', 1), 'headers': [(b'host', b'127.0.0.1:8740'),
                 (b'origin', backend.ORIGIN.encode()), (b'content-type', b'application/json')]}
        body = b'{ "kind": "state", "rooms": ["living-room"] }'
        events = asyncio.Queue()
        for event in [{'type': 'http.request', 'body': body[:17], 'more_body': True},
                      {'type': 'http.request', 'body': body[17:], 'more_body': False}]:
            events.put_nowait(event)
        messages = []
        async def send(event):
            messages.append(event)
        try:
            self.wrapper.inner = observe
            await self.wrapper(scope, events.get, send)
            self.assertEqual(forwarded, [body])
            self.assertEqual(messages[0]['status'], 200)
            self.wrapper.inner = AsyncMock()
            events.put_nowait({'type': 'http.request', 'body': body[:5], 'more_body': True})
            events.put_nowait({'type': 'http.disconnect'})
            messages.clear()
            await self.wrapper(scope, events.get, send)
            self.wrapper.inner.assert_not_awaited()
            self.assertFalse(messages)
        finally:
            self.wrapper.inner = real_inner

    async def test_existing_preview_and_owned_cancel_routes_without_effects(self):
        snapshot = (await self.client.get('/api/snapshot')).json()
        expiry = (datetime.now(UTC)+timedelta(seconds=30)).isoformat()
        dry = await self.client.post('/api/actions/dry-run', json={
            'target': 'light.hall', 'desired': {'power': 'on'}, 'expected_version': snapshot['version'],
            'expires_at': expiry, 'idempotency_key': 'SYNTHETIC-dry-preview'})
        self.assertEqual(dry.status_code, 200, dry.text)
        self.assertEqual(dry.json()['code'], 'dry_run_only')
        plan = await self.client.post('/api/actions/plan', json={
            'target': 'light.hall', 'desired': {'power': 'on'}, 'expected_state_version': 'SYNTHETIC-version',
            'expires_at': expiry, 'idempotency_key': 'SYNTHETIC-action-preview'})
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertFalse(plan.json()['real_write_attempted'])
        context = (await self.client.get('/api/rules/text/context')).json()
        preview = await self.client.post('/api/rules/text/plan', json={
            'text': '以后回家不开空调', 'expected_context': context['fingerprint'],
            'expires_at': expiry, 'idempotency_key': 'SYNTHETIC-rule-preview'})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()['status'], 'planned')
        trial = await self.client.post('/api/rules/plans/simulate', json={'plan_id': preview.json()['plan']['id']})
        self.assertEqual(trial.status_code, 200, trial.text)
        context = (await self.client.get('/api/rules/analysis/context')).json()
        review = await self.client.post('/api/rules/analysis/reviews', json={
            'text': '以后回家不开空调', 'expected_context': context['fingerprint'],
            'expires_at': expiry, 'idempotency_key': 'SYNTHETIC-analysis-preview'})
        self.assertEqual(review.status_code, 200, review.text)
        self.assertFalse(review.json()['model_inference_attempted'])
        cancelled = await self.client.post('/api/rules/analysis/cancel', json={
            'review_id': review.json()['id'], 'approval_digest': review.json()['approval_digest']})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()['code'], 'analysis_cancelled_before_call')
        self.assertFalse(cancelled.json()['planner_call_attempted'])
        revoked = await self.client.post('/api/archive/playback/cancel', json={'plan_id': 'a'*64})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertFalse(revoked.json()['device_write_attempted'])
        # Real-source preparation remains subject to the original authority check.
        rule = self.inner.state.rules.backend.records()[0].rule.model_dump(mode='json')
        rule.update(simulated=False, execution={**rule['execution'], 'preferred_backend': 'home_core'})
        core = await self.client.post('/api/rules/core/plans', json={
            'rule': rule, 'expected_context': '0'*64, 'expires_at': expiry,
            'idempotency_key': 'SYNTHETIC-unavailable-core'})
        self.assertEqual(core.status_code, 400, core.text)
        self.assertEqual(core.json()['code'], 'invalid_request')
        trial = await self.client.post('/api/rules/core/plans/trial', json={'plan_id': 'SYNTHETIC-unknown'})
        self.assertEqual(trial.status_code, 404, trial.text)
        self.assertEqual(trial.json()['code'], 'not_found')
        self.assertNotEqual(trial.json()['code'], 'desktop_read_only')
        self.assertFalse(self.inner.state.queries.core.adapter.managed_actions)

    async def test_full_mutation_registry_origin_budget_and_held_gets(self):
        flow = {'/api/home-flow/bootstrap', '/api/home-flow/context', '/api/home-flow/turn',
                '/api/home-flow/cancel', '/api/home-flow/dispose'}
        checked = 0
        for route in self.inner.routes:
            path = getattr(route, 'path', '')
            if (not path.startswith('/api/') or path in flow
                    or path in backend.READ_ONLY_PREPARATION_PATHS or path in self.wrapper.authorization_paths):
                continue
            path = path.replace('{name}', 'SYNTHETIC').replace('{plan_id}', 'a'*64)
            for method in (getattr(route, 'methods', set()) or set()) - {'GET', 'HEAD', 'OPTIONS'}:
                response = await self.client.request(method, path, json={})
                self.assertEqual(response.status_code, 409, (method, path, response.text))
                self.assertEqual(response.json()['code'], 'desktop_read_only')
                checked += 1
        self.assertGreater(checked, 0)  # Every current held route was asserted individually above.
        for path in sorted(backend.READ_ONLY_PREPARATION_PATHS):
            response = await self.client.post(path, content=b'{}')
            self.assertEqual(response.status_code, 422, (path, response.text))
            response = await self.client.post(path, content=b'x'*(backend.READ_ONLY_REQUEST_BYTES.get(path, 8192) + 1))
            self.assertEqual(response.status_code, 413, path)
            response = await self.client.post(path, json={}, headers={'Origin': 'https://example.invalid'})
            self.assertEqual(response.status_code, 403, path)
        for path in ['/api/settings/ha/discover', '/api/settings/miloco/models',
                     '/api/archive/media/'+'a'*64, '/api/family-media/originals/'+'a'*64]:
            for method in ['GET', 'HEAD']:
                self.assertEqual((await self.client.request(method, path)).status_code, 409)
        family = {'kind': 'evidence', 'source': 'family_media', 'source_ref': 'SYNTHETIC-source',
                  'expected_context': 'a'*64, 'evidence_refs': ['family-media:'+'a'*64]}
        self.assertEqual((await self.client.post('/api/query', json=family)).status_code, 409)
        evidence = {'kind': 'evidence', 'rooms': ['living-room'], 'evidence_refs': ['SYNTHETIC-event'],
                    'start': (datetime.now(UTC)-timedelta(hours=1)).isoformat(), 'end': datetime.now(UTC).isoformat()}
        self.assertEqual((await self.client.post('/api/query', json=evidence)).status_code, 409)
        response = await self.client.post('/api/query', json={'kind': 'state', 'rooms': ['living-room']},
                                          headers={'Origin': ''})
        self.assertEqual(response.status_code, 403)
        real_inner = self.wrapper.inner
        self.wrapper.inner = None
        try:
            response = await self.client.post('/api/query', json={'kind': 'state', 'rooms': ['living-room']})
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json()['simulated'])
        finally:
            self.wrapper.inner = real_inner


if __name__ == '__main__':
    unittest.main(verbosity=2)
