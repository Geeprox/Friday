"""Decisive business counterexamples through the actual desktop/domain consumer.
Synthetic owned fixtures only; no networks, credentials, models or effects.
"""
import asyncio
import importlib.util
import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

APP = Path(sys.argv[sys.argv.index("--app") + 1]).resolve() if "--app" in sys.argv else None
if APP:
    product = APP / "Contents/Resources/product"
    sys.path[:0] = [str(product / "vendor"), str(product / "app")]

spec = importlib.util.spec_from_file_location("business_base", Path(__file__).with_name("check_desktop_consumer.py"))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
if APP:
    consumer = APP / "Contents/Resources/backend/desktop_backend.py"
    actual_spec = importlib.util.spec_from_file_location("business_packaged_backend", consumer)
    actual = importlib.util.module_from_spec(actual_spec)
    sys.modules[actual_spec.name] = actual
    actual_spec.loader.exec_module(actual)
    base.backend = actual
    assert actual.PRODUCT.resolve() == product.resolve()
    import homeplane.ha as actual_ha
    assert Path(actual_ha.__file__).resolve() == (product / "app/homeplane/ha.py").resolve()
    print("CONSUMER: actual packaged backend and homeplane source", APP)
from homeplane.ha import HAReadOnlyAdapter
from homeplane.home_flow import Operation
from homeplane.query_contracts import HomeQuery
from homeplane.core_rules import CoreRuleProposal

class BusinessChecks(base.ConsumerChecks):
    async def test_expired_cancel_waits_for_original_owned_child(self):
        sid = await self.ready()
        flow = self.inner.state.home_flow
        session = flow.sessions[sid]
        release, entered = asyncio.Event(), asyncio.Event()
        async def slow_child():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        child = asyncio.create_task(slow_child())
        await entered.wait()
        operation = Operation("control", task=child)
        session.operations["operation:one"] = operation
        session.deadline = flow.monotonic() - 1
        request = self.request(sid, kind="cancel", turn_id="cancel:first", idempotency_key="cancel:first")
        cancelled = asyncio.create_task(self.client.post("/api/home-flow/cancel", json=request))
        try:
            await asyncio.sleep(.03)
            self.assertFalse(cancelled.done(), "cancel returned before its expired session child settled")
            cleanup = operation.cancel_task
            self.assertIsNotNone(cleanup)
            release.set()
            result = await asyncio.wait_for(cancelled, 1)
            self.assertEqual(result.json()["phase"], "cancelled")
            again = await self.client.post("/api/home-flow/turn", json={**request,
                "turn_id":"cancel:again", "idempotency_key":"cancel:again"})
            self.assertEqual(again.json()["phase"], "cancelled")
            self.assertIs(operation.cancel_task, cleanup)
            self.assertTrue(child.done())
            refused = await self.client.post("/api/home-flow/turn", json=self.request(sid, kind="turn", text="灯的状态"))
            self.assertEqual(refused.json()["code"], "flow_session_unavailable")
        finally:
            release.set()
            await asyncio.gather(child, cancelled, return_exceptions=True)

    async def test_accepted_ha_instance_and_long_provisional_id_remain_queryable(self):
        original = self.inner.state.core.adapter
        for instance, external in [("家庭网关", "light.living"), ("home", "light." + "sensor_" * 30), ("home", "light.living")]:
            config = base.SettingsConfig(ha_instance=instance)
            adapter = HAReadOnlyAdapter("http://synthetic.invalid", "SYNTHETIC-NO-CREDENTIAL", config.ha_instance,
                client=base.httpx.AsyncClient(transport=base.httpx.MockTransport(lambda _: base.httpx.Response(500))))
            rows = [{"entity_id":external, "state":"on", "attributes":{"friendly_name":"客厅灯"}}]
            if instance != "home":
                adapter._registry = {external:{"id":"a" * 32}}
            async def projected():
                return adapter._project(rows, datetime.now(UTC), [], emit_snapshot=False)
            adapter.snapshot = projected
            self.inner.state.core.adapter = adapter
            try:
                snap = await adapter.snapshot()
                if instance == "home" and external == "light.living":
                    self.assertEqual(snap.entities[0].id, "ha:home:provisional:light.living")
                selected = HomeQuery(kind="state", entities=[snap.entities[0].id])
                report = await self.client.post("/api/query", json=selected.model_dump(mode="json"))
                self.assertEqual(report.status_code, 200, report.text)
                self.assertEqual(report.json()["code"], "selected_evidence", report.text)
                self.assertEqual(len(report.json()["items"]), 1)
                self.assertFalse(report.json()["simulated"])
                self.assertEqual(snap.entities[0].bindings[0].external_id, external)
                self.assertEqual((await adapter.snapshot()).entities[0].id, snap.entities[0].id)
            finally:
                self.inner.state.core.adapter = original
                await adapter.close()

    async def test_registry_facts_qualify_events_independently_of_opaque_id(self):
        from types import SimpleNamespace
        cases = [("home", "light.living", {}),
                 ("家庭网关", "light.living", {}),
                 ("home", "light." + "sensor_" * 30, {}),
                 ("家庭网关", "light.living", {"id":"a" * 32}),
                 ("provisional", "light.living", {"id":"a" * 32}),
                 ("home", "light.living", {"platform":"test", "unique_id":"stable-sensor"})]
        for instance, external, registration in cases:
            with self.subTest(instance=instance, length=len(external), registered=bool(registration)):
                adapter = HAReadOnlyAdapter("http://synthetic.invalid", "SYNTHETIC-NO-CREDENTIAL", instance,
                    client=base.httpx.AsyncClient(transport=base.httpx.MockTransport(lambda _: base.httpx.Response(500))))
                old = {"entity_id":external, "state":"off", "attributes":{}}
                new = {**old, "state":"on"}
                stream = SimpleNamespace(ready=True, gap=False, rows={external:new}, receipts={})
                adapter._stream = stream
                adapter._registry = {external:registration} if registration else {}
                adapter._registry_ok = True
                expected = bool(registration)
                try:
                    snapshot = adapter._project([old], datetime.now(UTC), [], emit_snapshot=False)
                    identity = snapshot.entities[0].id
                    self.assertEqual(any(c.access == "event" for c in snapshot.entities[0].capabilities), expected)
                    now = datetime.now(UTC)
                    await adapter._stream_change(stream, {"external":external, "old":old, "new":new,
                        "eligible":True, "late":False, "cursor":"SYNTHETIC:1", "fired_at":now, "received_at":now})
                    event = adapter._events[-1]
                    self.assertEqual(event.kind, "state-transition" if expected else "source-report")
                    self.assertEqual(event.payload["trigger_eligible"], expected)
                    self.assertEqual(event.payload["entity_id"], identity)
                    self.assertEqual(adapter._last.entities[0].id, identity)
                finally:
                    adapter._stream = None
                    await adapter.close()

    async def test_domain_valid_multiaction_preview_reaches_consumer(self):
        rule = {"id":"owned:preview", "revision":1, "intent":"玄关开门后的提醒",
            "trigger":{"type":"state_transition", "entity":"ha:home:door", "capability":"binary_sensor.state",
                       "from_value":"off", "to_value":"on"},
            "actions":[{"type":"feedback.speak", "id":f"feedback:{i}", "route":"local_tts", "endpoint":"endpoint:local", "text":"提醒" * 128} for i in range(12)],
            "execution":{"preferred_backend":"home_core"}, "simulated":False}
        request = {"rule":rule, "expected_context":"0" * 64, "idempotency_key":"bounded-preview",
                   "expires_at":(datetime.now(UTC) + timedelta(seconds=30)).isoformat()}
        CoreRuleProposal.model_validate(request)
        raw = json.dumps(request, ensure_ascii=False).encode()
        self.assertGreater(len(raw), 8192)
        self.assertLess(len(raw), 16384)
        result = await self.client.post("/api/rules/core/plans", content=raw,
                                        headers={"Content-Type":"application/json"})
        self.assertNotEqual(result.status_code, 413, result.text)
        self.assertEqual(result.status_code, 400, result.text)  # existing real-source/plan gate, not an effect
        held = await self.client.post("/api/rules/core/plans/apply", json=request)
        self.assertEqual(held.status_code, 409)
        oversized = await self.client.post("/api/rules/core/plans", content=b"x" * 16385)
        self.assertEqual(oversized.status_code, 413)

if __name__ == "__main__":
    suite = unittest.TestSuite(BusinessChecks(n) for n in [
        "test_expired_cancel_waits_for_original_owned_child",
        "test_accepted_ha_instance_and_long_provisional_id_remain_queryable",
        "test_domain_valid_multiaction_preview_reaches_consumer",
        "test_registry_facts_qualify_events_independently_of_opaque_id"])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
