"""Public metadata and actual local worker cache; synthetic SDK only, no home reads."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('refresh_base',Path(__file__).with_name('check_home_connection_consumer.py'))
base=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=base
spec.loader.exec_module(base)
fixture,backend=base.fixture,base.backend
from homeplane.arl import ARLError,ARLRuntime

class MetadataPeer:
    def __init__(self):
        self.calls=0;self.error=None;self.block=False
        self.entered=threading.Event();self.release=threading.Event()
        self.version='0.1.1-SYNTHETIC'
    def rpc(self,method):
        assert method=='overview'
        self.calls+=1
        if self.block:
            self.entered.set()
            if not self.release.wait(1):raise AssertionError('Synthetic worker release missing')
        if self.error:raise ARLError(self.error)
        return {'schema_version':1,'protocol':'arl/1','version':self.version,
                'health':{'live':True,'ready':True},
                'policy':{'local_inference_enabled':False,'identity_verifier_configured':True},
                'providers':[{'name':'codex_app_server','availability':'disabled','models':[],'paid':True}],
                'capabilities':[]}

class RefreshChecks(fixture.LocalConsumerChecks):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.home=self.data/'SYNTHETIC-ARL';self.home.mkdir(mode=0o700)
        self.peer=MetadataPeer();self.runtime=self.wrapper.arl_runtime
        self.runtime.home=lambda:self.home
        self.runtime.settings_client_factory=lambda _home:self.peer
        self.runtime.client_factory=lambda *_:(_ for _ in ()).throw(AssertionError('No task/launcher authorized'))
        self.owned=[]

    async def worker(self,timeout=1):
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        def operation(stop):
            entered.set()
            try:
                if not release.wait(1):raise AssertionError('Synthetic release missing')
                if stop.is_set():raise ARLError('expired')
                return True
            finally:finished.set()
        task=asyncio.create_task(self.runtime._bounded(operation,timeout))
        self.owned.append((task,release,finished))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        return task,release,finished

    async def settle(self,task,release,finished):
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(await asyncio.to_thread(finished.wait,1))
        limit=time.monotonic()+1
        while self.runtime._busy.locked() and time.monotonic()<limit:await asyncio.sleep(.001)
        self.assertFalse(self.runtime._busy.locked(),'Actual owned worker failed to release its lock')

    async def asyncTearDown(self):
        for task,release,finished in self.owned:await self.settle(task,release,finished)
        self.peer.release.set()
        await super().asyncTearDown()

    async def test_local_busy_keeps_last_metadata_and_native_capability_projection(self):
        warm=await self.runtime.status();self.assertTrue(warm['ready'])
        task,release,finished=await self.worker()
        for _ in range(3):
            current=await self.runtime.status()
            self.assertTrue(current['ready']);self.assertEqual(current['status'],'ready')
            self.assertEqual(current['metadata_freshness'],'last_known')
            self.assertEqual(current['metadata_refresh_source'],'local_worker')
            self.assertTrue(current['metadata_refresh_pending'])
        self.assertEqual(self.peer.calls,1,'Busy status must not queue or call the SDK')
        self.wrapper.arl=await backend.arl_status(self.runtime)
        native=(await self.client.get('/desktop/status')).json()['arl']
        self.assertTrue(native['ready']);self.assertTrue(native['refreshPending'])
        self.assertEqual(native['refreshSource'],'local_worker')
        caps=(await self.client.get('/desktop/capabilities')).json()
        self.assertTrue(caps['runtime_ready'])
        self.assertEqual(caps['codex']['availability'],'disabled')
        self.assertFalse(caps['planning']['policy_configured'])
        self.assertFalse(caps['query']['configured'])
        self.assertFalse(caps['model_inference_attempted'])
        self.assertEqual(self.peer.calls,1)
        await self.settle(task,release,finished)
        fresh=await self.runtime.status();self.assertEqual(fresh['metadata_freshness'],'current')
        self.assertEqual(self.peer.calls,2)

    async def test_busy_without_previous_state_is_explicitly_unknown(self):
        task,release,finished=await self.worker()
        caps=(await self.client.get('/desktop/capabilities')).json()
        self.assertFalse(caps['runtime_ready'])
        self.assertEqual(caps['metadata_freshness'],'unknown')
        self.assertEqual(caps['metadata_refresh_source'],'local_worker')
        self.assertTrue(caps['metadata_refresh_pending'])
        self.assertEqual(self.peer.calls,0)
        await self.settle(task,release,finished)

    async def test_confirmed_disconnect_replaces_ready_and_busy_never_resurrects_it(self):
        self.assertTrue((await self.runtime.status())['ready'])
        self.peer.error='disconnected'
        failed=await self.runtime.status()
        self.assertFalse(failed['ready']);self.assertEqual(failed['metadata_freshness'],'unavailable')
        task,release,finished=await self.worker()
        cached=await self.runtime.status()
        self.assertFalse(cached['ready']);self.assertEqual(cached['status'],'disconnected')
        self.assertEqual(cached['metadata_freshness'],'last_known')
        await self.settle(task,release,finished)
        self.peer.error=None
        self.assertTrue((await self.runtime.status())['ready'])

    async def test_unknown_sdk_failure_preserves_state_without_claiming_local_busy(self):
        self.assertTrue((await self.runtime.status())['ready'])
        self.peer.error='invalid_overview'
        report=await self.runtime.status()
        self.assertTrue(report['ready']);self.assertEqual(report['metadata_refresh_source'],'sdk')
        self.assertEqual(report['metadata_refresh_code'],'invalid_overview')
        self.peer.error='runtime_busy'
        remote=await self.runtime.status()
        self.assertEqual(remote['metadata_refresh_source'],'sdk')

    async def test_timeout_and_caller_cancel_keep_gate_until_child_finishes(self):
        self.assertTrue((await self.runtime.status())['ready'])
        for cancelled in [False,True]:
            task,release,finished=await self.worker(timeout=1 if cancelled else .03)
            if cancelled:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            else:
                with self.assertRaisesRegex(ARLError,'deadline_exceeded'):await task
            self.assertTrue(self.runtime._busy.locked())
            cached=await self.runtime.status()
            self.assertTrue(cached['ready']);self.assertEqual(cached['metadata_refresh_source'],'local_worker')
            await self.settle(task,release,finished)
            fresh=await self.runtime.status();self.assertEqual(fresh['metadata_freshness'],'current')

    async def test_profile_change_and_ABA_never_publish_old_metadata(self):
        for aba,negative in [(False,False),(True,False),(False,True),(True,True)]:
            peer=MetadataPeer();active=[self.home]
            runtime=ARLRuntime(lambda:active[0],settings_client_factory=lambda _home:peer)
            self.assertTrue((await runtime.status())['ready'])
            peer.block=True
            old=asyncio.create_task(runtime.status(timeout=.5))
            self.assertTrue(await asyncio.to_thread(peer.entered.wait,1))
            try:
                other=self.data/('SYNTHETIC-other-'+str(aba)+'-'+str(negative));other.mkdir(mode=0o700)
                active[0]=other
                changed=await runtime.status()
                self.assertEqual(changed['metadata_freshness'],'unknown');self.assertFalse(changed['ready'])
                if aba:
                    active[0]=self.home
                    back=await runtime.status()
                    self.assertEqual(back['metadata_freshness'],'unknown');self.assertFalse(back['ready'])
                if negative:peer.error='disconnected'
                peer.release.set()
                stale=await old
                self.assertEqual(stale['metadata_refresh_code'],'discarded_configuration_changed')
                self.assertEqual(stale['metadata_freshness'],'unknown')
                peer.block=False;peer.error=None;peer.version='0.1.1-SYNTHETIC-new'
                fresh=await runtime.status()
                self.assertEqual(fresh['runtime_version'],peer.version)
                self.assertEqual(fresh['metadata_freshness'],'current')
            finally:
                peer.release.set();await asyncio.gather(old,return_exceptions=True)

    async def test_metadata_is_copied_and_cannot_be_used_as_authorization(self):
        warm=await self.runtime.status()
        warm['providers'][0]['models'].append('SYNTHETIC-poison')
        task,release,finished=await self.worker()
        first=await self.runtime.status()
        self.assertEqual(first['providers'][0]['models'],[])
        first['providers'][0]['models'].append('SYNTHETIC-poison')
        again=await self.runtime.status()
        self.assertEqual(again['providers'][0]['models'],[])
        self.assertFalse(again['action_authority']);self.assertFalse(again['inference_attempted'])
        await self.settle(task,release,finished)

if __name__=='__main__':
    names=['test_local_busy_keeps_last_metadata_and_native_capability_projection','test_busy_without_previous_state_is_explicitly_unknown','test_confirmed_disconnect_replaces_ready_and_busy_never_resurrects_it','test_unknown_sdk_failure_preserves_state_without_claiming_local_busy','test_timeout_and_caller_cancel_keep_gate_until_child_finishes','test_profile_change_and_ABA_never_publish_old_metadata','test_metadata_is_copied_and_cannot_be_used_as_authorization']
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(RefreshChecks(name) for name in names))
    sys.exit(not result.wasSuccessful())
