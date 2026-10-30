"""Finite source qualification: actual desktop construction, explicit synthetic input.

No saved credentials, Simulator, network, worker, registration or installation.
"""
import asyncio
import importlib.util
import json
import os
import socket
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "local_consumers_backend", ROOT / "native/FridayDesktop/backend/desktop_backend.py"
)
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)
import httpx
from homeplane import api, ha_setup
from homeplane.arl import ARLRuntime
from homeplane.contracts import Snapshot
from homeplane.nas_connection.contracts import NASConnectionConfig, NASCredentials
from homeplane.nas_connection.manager import NASConnectionManager
from homeplane.people_feature_bridge import MAX_PEOPLE_DATABASE_BYTES, close_people_catalog
from homeplane.settings import SettingsConfig, SettingsService

ORIGIN = "http://127.0.0.1:8740"


def blocked(*_args, **_kwargs):
    raise AssertionError("Synthetic source checks forbid sockets, DNS and subprocesses")


class SyntheticReadOnlyAdapter:
    """Protocol stub, deliberately not a Simulator or a live HA adapter."""
    def __init__(self):
        self.closed = False
        self.snapshot_value = Snapshot(
            mode="ha-read-only", version="SYNTHETIC:local-consumers",
            generated_at=datetime.now(timezone.utc), entities=[], observations=[],
            states=[], rules=[], events=[],
            limitations=["Explicit synthetic contract fixture; no real household qualification"],
        )

    async def snapshot(self):
        if self.closed:
            raise AssertionError("Snapshot after adapter close")
        return self.snapshot_value

    def health(self):
        return {'mode': 'ha-read-only', 'transport': 'offline' if self.closed else 'online',
                'authentication_available': True, 'observation_gap': self.closed}

    async def close(self):
        self.closed = True

    async def set_state(self, *_a, **_k):
        raise AssertionError("Device write attempted")

    async def call_service(self, *_a, **_k):
        raise AssertionError("Device service attempted")


class LocalConsumerChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name).resolve()
        self.adapters = []

        async def synthetic_real_adapter(_renewal):
            adapter = SyntheticReadOnlyAdapter()
            self.adapters.append(adapter)
            return SettingsConfig(ha_url="http://synthetic.invalid:8123"), adapter, await adapter.snapshot()

        self.patches = [patch.object(backend, name, value) for name, value in {
            "BASE": self.data, "CONFIG": self.data / "absent-config.json",
            "CREDENTIAL": self.data / "absent-ha-credential.json",
            "NEW_CREDENTIAL": self.data / "absent-new-ha-credential.json",
            "STATE": self.data / "desktop", "config": lambda: SettingsConfig(),
            "arl_runtime": lambda: ARLRuntime(lambda: self.data / "absent-arl"),
            "real_adapter": synthetic_real_adapter,
        }.items()]
        self.patches += [patch.object(socket.socket, name, blocked)
                         for name in ("connect", "connect_ex", "bind", "listen")]
        self.patches += [patch.object(socket, name, blocked)
                         for name in ("getaddrinfo", "create_connection")]
        self.patches += [patch.object(asyncio, "create_subprocess_exec", blocked),
                         patch.object(asyncio, "create_subprocess_shell", blocked),
                         patch.object(api, "DEFAULT_HOME", self.data / "absent-arl"),
                         patch.object(ha_setup, "CREDENTIAL_PATH", self.data / "absent-oauth.json")]
        for p in self.patches:
            p.start()
        self.wrapper = backend.DesktopService()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.wrapper), base_url=ORIGIN,
            headers={"Origin": ORIGIN},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.wrapper.close()
        for p in reversed(self.patches):
            p.stop()
        self.directory.cleanup()

    async def connect(self):
        response = await self.client.post("/desktop/connect")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["connection"], "connected", response.text)
        self.assertEqual(response.json()["deviceWrites"], "disabled")
        return self.wrapper.inner

    async def test_actual_construction_mounts_empty_people_and_unconfigured_files(self):
        inner = await self.connect()
        self.assertIsNotNone(inner.state.people)
        self.assertIsNotNone(inner.state.file_steward)
        html = await self.client.get("/")
        self.assertEqual(html.status_code, 200)
        for flag in ("data-people-enabled", "data-file-steward-enabled", "data-console-read-only"):
            self.assertIn(flag + '="true"', html.text)
        people = await self.client.get("/api/people")
        self.assertEqual(people.status_code, 200, people.text)
        self.assertEqual(people.json()["samples"], [])
        self.assertEqual(people.json()["people"], [])
        self.assertIsNone(people.json()["last_run"])
        context = await self.client.get("/api/files/context")
        self.assertEqual(context.status_code, 200, context.text)
        value = context.json()
        self.assertFalse(value["catalog"]["simulated"])
        self.assertFalse(value["catalog"]["source_online"])
        self.assertEqual(value["session"]["reason"], "connection_required")
        self.assertEqual(value["session"]["label"], "需连接 NAS")
        self.assertFalse(value["executor_enabled"])
        self.assertFalse(any(p["enabled"] for p in value["providers"]))
        self.assertFalse((backend.STATE / "nas-connection.json").exists())
        settings = await self.client.get("/api/settings")
        self.assertEqual(settings.json()["credentials"]["nas"], "private_resolver_not_configured")

    async def test_two_typed_reads_reach_original_domain_and_context_fence(self):
        await self.connect()
        context = (await self.client.get("/api/files/context")).json()["catalog"]["fingerprint"]
        query = await self.client.post("/api/files/query", json={"expected_context": context, "limit": 1})
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(query.json()["items"], [])
        stale = await self.client.post("/api/files/query", json={"expected_context": "0" * 64})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertNotEqual(stale.json().get("code"), "desktop_read_only")
        settings = await self.client.post("/api/files/settings/query", json={
            "expected_context": context, "sample_item_id": "a" * 64,
        })
        self.assertEqual(settings.status_code, 409, settings.text)
        self.assertIn("detail", settings.json())  # Actual catalog rejects the nonexistent item.
        self.assertNotEqual(settings.json().get("code"), "desktop_read_only")

    async def test_exact_read_routes_keep_origin_budget_schema_and_effect_gates(self):
        await self.connect()
        for path in ("/api/files/query", "/api/files/settings/query"):
            self.assertEqual((await self.client.post(path, json={}, headers={"Origin": "https://invalid.example"})).status_code, 403)
            self.assertEqual((await self.client.post(path, content=b"x" * 8193)).status_code, 413)
            self.assertEqual((await self.client.post(path, json={})).status_code, 422)
        # Owner-local People edits retain their original typed domain gate;
        # read-only household transport does not grant device/file effects.
        for path in ('/api/people/name', '/api/people/undo'):
            self.assertEqual((await self.client.post(path,json={},headers={'Origin':'https://invalid.example'})).status_code,403)
            response=await self.client.post(path,json={})
            self.assertEqual(response.status_code,422,(path,response.text))
        for path in ("/api/files/settings/preview",
                     "/api/files/query/extra", "/api/files/jobs/preview", "/api/files/queue/review",
                     "/api/files/relocations/review", "/api/files/reconcile",
                     "/api/settings/nas/connection", "/api/actions/execute",
                     "/api/rules/analysis/approve", "/api/cameras/media/start"):
            response = await self.client.post(path, json={})
            self.assertEqual(response.status_code, 409, (path, response.text))
            self.assertEqual(response.json()["code"], "desktop_read_only")
        for path in ("/api/settings/ha/discover", "/api/settings/miloco/models",
                     "/api/archive/media/a", "/api/family-media/originals/a"):
            self.assertEqual((await self.client.get(path)).status_code, 409, path)

    async def test_close_reopen_preserves_metadata_and_reaps_owned_resources(self):
        inner = await self.connect()
        people = inner.state.people
        # A synthetic metadata revision, not a recognition run or user write route.
        people._change(0, lambda _state: None, undo=False)
        await self.wrapper.close()
        self.assertTrue(self.adapters[0].closed)
        self.assertFalse(inner.state.home_flow.tasks)
        self.assertFalse(inner.state.home_flow.sessions)
        self.assertFalse(inner.state.home_flow.bootstrap_tasks)
        with self.assertRaises(sqlite3.ProgrammingError):
            people.connection.execute("SELECT 1")
        again = await self.connect()
        self.assertEqual(again.state.people.page().revision, 1)
        self.assertEqual(again.state.people.page().samples, [])

    async def test_constructor_failure_closes_created_people_and_adapter(self):
        catalogs = []
        original = backend.local_people_catalog
        def capture():
            value = original()
            catalogs.append(value)
            return value
        with patch.object(backend, "local_people_catalog", capture), patch.object(
                api, "create_app", side_effect=ValueError("synthetic constructor failure")):
            result = await self.client.post("/desktop/connect")
        self.assertEqual(result.json()["connection"], "failed")
        self.assertTrue(self.adapters[0].closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            catalogs[0].connection.execute("SELECT 1")
        self.assertIsNone(self.wrapper.inner)

    async def test_startup_cancel_closes_people_and_adapter_before_propagating(self):
        entered = asyncio.Event()
        catalogs = []
        original = api.create_app
        def capture(*args, **kwargs):
            catalogs.append(kwargs["people_catalog"])
            return original(*args, **kwargs)
        async def wait_start(_self):
            entered.set()
            await asyncio.Future()
        with patch.object(api, "create_app", capture), patch.object(NASConnectionManager, "start", wait_start):
            task = asyncio.create_task(self.wrapper.connect())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.adapters[0].closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            catalogs[0].connection.execute("SELECT 1")
        self.assertIsNone(self.wrapper.inner)

    async def test_private_storage_rejects_foreign_schema_without_mutation(self):
        backend.STATE.mkdir(mode=0o700)
        path = backend.STATE / "people.sqlite"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE unrelated (body TEXT)")
        connection.commit()
        connection.close()
        path.chmod(0o600)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "dedicated_connection"):
            backend.local_people_catalog()
        self.assertEqual(path.read_bytes(), before)

    async def test_private_storage_rejects_symlink_hardlink_permissions_and_capacity(self):
        backend.STATE.mkdir(mode=0o700)
        path = backend.STATE / "people.sqlite"
        target = self.data / "synthetic-target"
        target.write_bytes(b"SYNTHETIC: unchanged")
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            backend.local_people_catalog()
        self.assertEqual(target.read_bytes(), b"SYNTHETIC: unchanged")
        path.unlink()
        target.chmod(0o600)
        os.link(target, path)
        with self.assertRaises(ValueError):
            backend.local_people_catalog()
        self.assertEqual(target.read_bytes(), b"SYNTHETIC: unchanged")
        path.unlink()
        path.touch(mode=0o644)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            backend.local_people_catalog()
        path.chmod(0o600)
        with path.open("wb") as stream:
            stream.truncate(MAX_PEOPLE_DATABASE_BYTES + 1)
        with self.assertRaises(ValueError):
            backend.local_people_catalog()
        path.unlink()
        backend.STATE.chmod(0o755)
        with self.assertRaises(ValueError):
            backend.local_people_catalog()

    async def test_resolver_is_explicit_purpose_bound_and_never_public_or_persisted(self):
        callback = AsyncMock(return_value=NASCredentials("SYNTHETIC-NAS-USER", "SYNTHETIC-NAS-SECRET"))
        default = SettingsService(self.data / "default-settings.json")
        self.assertIsNone(await default.resolve_nas_credential("opaque_nas_ref"))
        service = SettingsService(self.data / "isolated-settings.json", nas_credential_resolver=callback)
        for ref in (None, "", "../ha-credential", "a" * 129, "ref:purpose"):
            self.assertIsNone(await service.resolve_nas_credential(ref))
        callback.assert_not_called()
        value = await service.resolve_nas_credential("opaque_nas_ref")
        self.assertIsInstance(value, NASCredentials)
        callback.assert_awaited_once_with("opaque_nas_ref", purpose="friday.nas.read-only.v1")
        public = json.dumps(service.get_public())
        self.assertIn("opaque_reference_only", public)
        for private in ("opaque_nas_ref", "SYNTHETIC-NAS-USER", "SYNTHETIC-NAS-SECRET"):
            self.assertNotIn(private, public)
        self.assertFalse(service.path.exists())
        self.wrapper.nas_credential_resolver = callback
        inner = await self.connect()
        self.assertIs(inner.state.settings._nas_credential_resolver, callback)
        self.assertIsNone(inner.state.nas_connection.config)
        self.assertEqual(callback.await_count, 1)  # No default NAS activation during construction.

    async def test_resolver_rejects_private_failures_and_invalid_results(self):
        for result in (None, {"username": "SYNTHETIC", "password": "SYNTHETIC"},
                       NASCredentials("", "SYNTHETIC"), NASCredentials("SYNTHETIC", b"SYNTHETIC")):
            service = SettingsService(self.data / "not-written.json", nas_credential_resolver=AsyncMock(return_value=result))
            self.assertIsNone(await service.resolve_nas_credential("opaque_ref"))
        service = SettingsService(self.data / "not-written.json", nas_credential_resolver=AsyncMock(
            side_effect=RuntimeError("SYNTHETIC-PRIVATE-ERROR-TEXT")))
        self.assertIsNone(await service.resolve_nas_credential("opaque_ref"))
        self.assertNotIn("SYNTHETIC", json.dumps(service.get_public()))
        with self.assertRaises(ValueError):
            SettingsService(self.data / "not-written.json", nas_credential_resolver={})

    async def test_resolver_cancel_propagates_without_retry(self):
        entered = asyncio.Event()
        async def wait(ref, *, purpose):
            entered.set()
            await asyncio.Future()
        callback = AsyncMock(side_effect=wait)
        service = SettingsService(self.data / "not-written.json", nas_credential_resolver=callback)
        task = asyncio.create_task(service.resolve_nas_credential("opaque_ref"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(callback.await_count, 1)

    async def test_existing_nas_manager_consumes_only_synthetic_injected_seam(self):
        # Fake transport tests the original owner domain, with no real worker/socket.
        connected = asyncio.Event()
        closed = asyncio.Event()
        calls = []
        class SyntheticTransport:
            async def connect(self, config, credentials):
                calls.append((config.source_ref, credentials))
                connected.set()
                return {"reachable": True, "authenticated": True, "readable": True}
            async def close(self):
                closed.set()
        callback = AsyncMock(return_value=NASCredentials("SYNTHETIC", "SYNTHETIC"))
        service = SettingsService(self.data / "not-written.json", nas_credential_resolver=callback)
        manager = NASConnectionManager(self.data / "synthetic-nas-state.json",
            credential_resolver=service.resolve_nas_credential, transport_factory=SyntheticTransport)
        try:
            await manager.configure(NASConnectionConfig(source_ref="SYNTHETIC", transport="smb",
                host="synthetic.invalid", share="synthetic", credential_ref="opaque_nas_ref", enabled=True),
                expected_generation=0)
            await asyncio.wait_for(connected.wait(), 2)
            self.assertEqual(manager.status().state, "online")
            self.assertEqual(len(calls), 1)
            self.assertIsInstance(calls[0][1], NASCredentials)
            callback.assert_awaited_once_with("opaque_nas_ref", purpose="friday.nas.read-only.v1")
            self.assertNotIn("password", manager.status().model_dump_json())
        finally:
            await manager.close()
        self.assertTrue(closed.is_set())
        self.assertIsNone(manager._task)
        self.assertIsNone(manager._transport)


if __name__ == "__main__":
    unittest.main(verbosity=2)
