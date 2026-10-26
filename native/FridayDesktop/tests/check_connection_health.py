"""Finite actual ASGI/HA-read checks. All input is explicitly synthetic.

Sockets, DNS and subprocesses are forbidden. No saved credential or user data.
"""
import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("connection_fixture", HERE / "check_local_consumers.py")
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)
backend, httpx = fixture.backend, fixture.httpx
cleanup_spec = importlib.util.spec_from_file_location('real_cleanup_checks', HERE/'check_consumed_lifespan.py')
real_cleanup = importlib.util.module_from_spec(cleanup_spec)
sys.modules[cleanup_spec.name] = real_cleanup
cleanup_spec.loader.exec_module(real_cleanup)
from homeplane.ha import HAReadOnlyAdapter


class HealthChecks(fixture.LocalConsumerChecks):
    # The runner below selects only these finite additions and two integration controls.
    async def test_actual_http_auth_failure_and_recovery_reach_status(self):
        await self.connect()
        previous = self.wrapper.adapter
        response_code = 200
        reads = []
        def reply(request):
            reads.append(request.url.path)
            return httpx.Response(response_code, json=[])
        adapter = HAReadOnlyAdapter("http://synthetic.invalid", "SYNTHETIC-NOT-A-GRANT", "SYNTHETIC",
                                    transport=httpx.MockTransport(reply))
        adapter._inventory = AsyncMock(return_value=None)  # No WebSocket/provider fixture.
        try:
            self.wrapper.adapter = adapter
            await adapter.snapshot()
            self.assertEqual((await self.client.get("/desktop/status")).json()["connection"], "connected")
            for code in (401, 403):
                response_code = code
                await adapter.snapshot()
                self.assertFalse(adapter.health()["authentication_available"])
                before = len(reads)
                result = (await self.client.get("/desktop/status")).json()
                self.assertEqual(result["connection"], "failed")
                self.assertEqual(result["failure"], "ha_authorization_rejected")
                self.assertEqual(len(reads), before)  # Passive status does not contact HA.
                self.assertFalse(result["simulated"])
                self.assertEqual(result["deviceWrites"], "disabled")
            response_code = 200
            await adapter.snapshot()
            self.assertTrue(adapter.health()["authentication_available"])
            self.assertEqual((await self.client.get("/desktop/status")).json()["connection"], "connected")
        finally:
            self.wrapper.adapter = previous
            await adapter.close()

    async def test_actual_transport_failure_stale_evidence_and_safe_transition_logs(self):
        await self.connect()
        previous = self.wrapper.adapter
        failing = False
        def reply(request):
            if failing:
                raise httpx.ConnectError("SYNTHETIC-PRIVATE-URL-AND-SECRET", request=request)
            return httpx.Response(200, json=[])
        adapter = HAReadOnlyAdapter("http://synthetic.invalid", "SYNTHETIC-NOT-A-GRANT", "SYNTHETIC",
                                    transport=httpx.MockTransport(reply))
        adapter._inventory = AsyncMock(return_value=None)
        try:
            self.wrapper.adapter = adapter
            await adapter.snapshot()
            await self.client.get("/desktop/status")
            failing = True
            await adapter.snapshot()
            self.assertEqual(adapter.health()["transport"], "offline")
            for _ in range(3):
                result = (await self.client.get("/desktop/status")).json()
                self.assertEqual(result["failure"], "ha_unreachable")
                self.assertEqual(result["connection"], "failed")
            export = (await self.client.get("/api/diagnostics/runtime")).json()
            text = fixture.json.dumps(export)
            self.assertNotIn("SYNTHETIC-PRIVATE", text)
            rows = [row for row in export["events"] if row["stage"] == "desktop.health" and row["code"] == "ha_unreachable"]
            self.assertEqual(len(rows), 1)
        finally:
            self.wrapper.adapter = previous
            await adapter.close()

    async def test_health_gap_and_invalid_contract_are_not_connected(self):
        await self.connect()
        good = self.wrapper.adapter.health()
        for value, expected in [
            ({**good, "observation_gap": True}, "subscription_disconnected"),
            ({**good, "authentication_available": "true"}, "ha_configuration_or_protocol_error"),
            ({"mode": "ha-read-only"}, "ha_configuration_or_protocol_error"),
            (None, "ha_configuration_or_protocol_error"),
        ]:
            with patch.object(self.wrapper.adapter, "health", return_value=value):
                result = (await self.client.get("/desktop/status")).json()
                self.assertEqual(result["connection"], "failed")
                self.assertEqual(result["failure"], expected)
        with patch.object(self.wrapper.adapter, "health", side_effect=RuntimeError("SYNTHETIC-PRIVATE")):
            self.assertEqual(self.wrapper.connection_failure(), "ha_configuration_or_protocol_error")

    async def test_healthy_reconnect_is_idempotent(self):
        inner = await self.connect()
        for _ in range(2):
            await self.connect()
        self.assertIs(self.wrapper.inner, inner)
        self.assertEqual(len(self.adapters), 1)

    async def test_unhealthy_reconnect_reaps_original_domain_before_new_provider(self):
        old = await self.connect()
        adapter = self.wrapper.adapter
        original_provider = backend.real_adapter
        async def checked_provider(renewal):
            self.assertTrue(adapter.closed)
            self.assertFalse(old.state.home_flow.tasks)
            self.assertFalse(old.state.home_flow.bootstrap_tasks)
            with self.assertRaises(fixture.sqlite3.ProgrammingError):
                old.state.people.connection.execute("SELECT 1")
            return await original_provider(renewal)
        with patch.object(adapter, "health", return_value={**adapter.health(), "observation_gap": True}), \
             patch.object(backend, "real_adapter", checked_provider):
            new = await self.connect()
        self.assertIsNot(new, old)
        self.assertEqual(len(self.adapters), 2)

    async def asyncTearDown(self):
        if getattr(self.wrapper, '_cleanup_state', None) == 'failed':
            self.assertTrue(self.test_store_disposed)
            await self.client.aclose()
            for p in reversed(self.patches):
                p.stop()
            self.directory.cleanup()
        else:
            await super().asyncTearDown()

    async def test_cleanup_failure_keeps_owner_and_does_not_create_provider(self):
        await real_cleanup.CleanupChecks.failed_exit(self, RuntimeError('SYNTHETIC-PRIVATE-CLEANUP'))

    async def test_cleanup_cancel_keeps_gate_until_owner_cleanup_completes(self):
        await real_cleanup.CleanupChecks.test_repeated_caller_cancel_waits_for_single_real_generator_exit(self)


if __name__ == "__main__":
    names = [name for name in HealthChecks.__dict__ if name.startswith("test_")]
    names += ["test_actual_construction_mounts_empty_people_and_unconfigured_files",
              "test_exact_read_routes_keep_origin_budget_schema_and_effect_gates"]
    suite = unittest.TestSuite(HealthChecks(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
