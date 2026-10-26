"""Real create_app async-generator exit; no network, credential or process."""
import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('cleanup_fixture', SOURCE/'native/FridayDesktop/tests/check_local_consumers.py')
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)

class CleanupChecks(fixture.LocalConsumerChecks):
    async def asyncTearDown(self):
        # In injected last-close failures every preceding real close completed.
        # Dispose only the test's remaining SQLite handle after all assertions;
        # never retry the consumed lifespan or count this disposal as product GO.
        if getattr(self.wrapper, '_cleanup_state', None) == 'failed':
            self.assertTrue(self.test_store_disposed)
            await self.client.aclose()
            for p in reversed(self.patches):
                p.stop()
            self.directory.cleanup()
        else:
            await super().asyncTearDown()

    async def failed_exit(self, error):
        old = await self.connect()
        owner = self.wrapper.lifespan
        store = old.state.core.store
        original_close = store.close
        calls = []
        def fail_last():
            calls.append('store')
            raise error
        try:
            with patch.object(store, 'close', fail_last), patch.object(self.wrapper.adapter, 'health', return_value={'mode':'ha-read-only'}):
                first = asyncio.create_task(self.wrapper.connect())
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await first
                else:
                    response = await first
                    self.assertEqual(response['connection'], 'failed')
                    self.assertNotIn('SYNTHETIC-PRIVATE',fixture.json.dumps(response))
                self.assertIs(self.wrapper.inner, old)
                self.assertIs(self.wrapper.lifespan, owner)
                self.assertEqual(calls, ['store'])
                self.assertTrue(self.adapters[0].closed)
                self.assertFalse(old.state.home_flow.tasks)
                self.assertFalse(old.state.home_flow.bootstrap_tasks)
                with self.assertRaises(fixture.sqlite3.ProgrammingError):
                    old.state.people.connection.execute('SELECT 1')
                store.connection.execute('SELECT 1')
                for _ in range(2):
                    again = await self.client.post('/desktop/connect')
                    self.assertEqual(again.status_code, 409, again.text)
                    self.assertEqual(again.json()['code'], 'connection_in_progress')
                    self.assertIs(self.wrapper.inner, old)
                    self.assertIs(self.wrapper.lifespan, owner)
                    self.assertEqual(len(self.adapters), 1)
                    self.assertEqual(calls, ['store'])
                stopped = await self.client.post('/desktop/disconnect')
                self.assertEqual(stopped.status_code,409)
        finally:
            original_close()
            self.test_store_disposed = True

    async def test_failed_consumed_generator_remains_held_on_second_connect(self):
        await self.failed_exit(RuntimeError('SYNTHETIC-PRIVATE-CLOSE'))

    async def test_cancelled_consumed_generator_remains_held_on_second_connect(self):
        await self.failed_exit(asyncio.CancelledError())

    async def test_repeated_caller_cancel_waits_for_single_real_generator_exit(self):
        old = await self.connect()
        owner = self.wrapper.lifespan
        adapter = self.wrapper.adapter
        entered, release = asyncio.Event(), asyncio.Event()
        real_close = old.state.home_flow.close
        calls = []
        async def pause_real_close():
            calls.append('flow')
            entered.set()
            await release.wait()
            await real_close()
        task = None
        try:
            with patch.object(old.state.home_flow, 'close', pause_real_close), patch.object(adapter, 'health', return_value={'mode':'ha-read-only'}):
                task = asyncio.create_task(self.wrapper.connect())
                await asyncio.wait_for(entered.wait(),2)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    again = await self.client.post('/desktop/connect')
                    self.assertEqual(again.status_code,409)
                    self.assertTrue(self.wrapper.gate.locked())
                    self.assertIs(self.wrapper.inner,old)
                    self.assertIs(self.wrapper.lifespan,owner)
                    self.assertFalse(adapter.closed)
                    old.state.people.connection.execute('SELECT 1')
                    old.state.core.store.connection.execute('SELECT 1')
                    self.assertEqual(len(self.adapters),1)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(calls,['flow'])
            self.assertFalse(self.wrapper.gate.locked())
            self.assertIsNone(self.wrapper.inner)
            self.assertTrue(adapter.closed)
            self.assertFalse(old.state.home_flow.tasks)
            self.assertFalse(old.state.home_flow.sessions)
            self.assertFalse(old.state.home_flow.bootstrap_tasks)
            for connection in [old.state.people.connection,old.state.core.store.connection]:
                with self.assertRaises(fixture.sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')
            new = await self.connect()
            self.assertIsNot(new,old)
            self.assertEqual(len(self.adapters),2)
        finally:
            release.set()
            if task is not None and not task.done():
                await asyncio.gather(task,return_exceptions=True)

if __name__ == '__main__':
    names = sys.argv[1:] or [n for n in CleanupChecks.__dict__ if n.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(CleanupChecks(n) for n in names))
    raise SystemExit(not result.wasSuccessful())
