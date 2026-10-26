"""Current desktop login/recovery consumers, synthetic private records only."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

APP = Path(sys.argv[sys.argv.index('--app') + 1]).resolve() if '--app' in sys.argv else None
fixture_path = Path(__file__).with_name('check_local_consumers.py')
if APP:
    import types
    product = APP / 'Contents/Resources/product'
    consumer = APP / 'Contents/Resources/backend/desktop_backend.py'
    assert APP.name == 'Friday.app' and consumer.is_file()
    sys.path[:0] = [str(product / 'vendor'), str(product / 'app')]
    # Reuse fixture behavior while loading only the selected package's consumer.
    source = fixture_path.read_text()
    substitutions = {
        'ROOT = Path(__file__).resolve().parents[3]': 'ROOT = Path(' + repr(str(product / 'app')) + ')',
        'ROOT / "native/FridayDesktop/backend/desktop_backend.py"': 'Path(' + repr(str(consumer)) + ')',
    }
    for old, new in substitutions.items():
        assert source.count(old) == 1
        source = source.replace(old, new)
    fixture = types.ModuleType('connection_fixture')
    fixture.__file__ = str(fixture_path)
    sys.modules[fixture.__name__] = fixture
    exec(compile(source, str(fixture_path), 'exec'), fixture.__dict__)
    assert Path(fixture.backend.__file__).resolve() == consumer
    assert fixture.backend.PRODUCT.resolve() == product
    for name in ('homeplane.api', 'homeplane.ha', 'homeplane.settings', 'friday.home'):
        assert Path(sys.modules[name].__file__).resolve().is_relative_to(product / 'app'), name
    print('CONSUMER: actual packaged backend and domains', APP, flush=True)
else:
    spec = importlib.util.spec_from_file_location('connection_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fixture
    spec.loader.exec_module(fixture)
backend = fixture.backend

class ConnectionChecks(fixture.LocalConsumerChecks):
    def saved(self, path, **change):
        record = {'ha_url':'http://synthetic.invalid:8123', 'access_token':'SYNTHETIC-ACCESS',
                  'refresh_token':'SYNTHETIC-REFRESH', 'client_id':'SYNTHETIC-CLIENT', 'expires_at':time.time()+3600}
        record.update(change)
        path.write_text(json.dumps(record))
        os.chmod(path, 0o600)
        return record

    async def test_workspace_symlink_never_tightens_a_foreign_target(self):
        target = self.data / 'foreign-target'
        target.mkdir(mode=0o755)
        os.chmod(target, 0o755)
        backend.STATE.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            backend.prepare_private_workspace()
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    async def test_existing_app_workspace_is_tightened_without_changing_user_files(self):
        backend.STATE.mkdir(mode=0o755)
        os.chmod(backend.STATE, 0o755)
        keep = backend.STATE / 'settings.json'
        keep.write_text('{"schema_version":1,"revision":0,"config":{}}')
        before = keep.read_bytes()
        await self.connect()
        self.assertEqual(backend.STATE.stat().st_mode & 0o777, 0o700)
        self.assertEqual(keep.read_bytes(), before)
        self.assertTrue((backend.STATE / 'people.sqlite').exists())
        self.assertTrue((await self.client.get('/desktop/status')).json()['homeReadable'])

    async def test_native_and_settings_use_same_selected_saved_login(self):
        self.saved(backend.CREDENTIAL)
        with patch.object(backend, 'config', lambda: fixture.SettingsConfig(ha_url='http://synthetic.invalid:8123')):
            native = (await self.client.get('/desktop/status')).json()
            settings = (await self.client.get('/api/settings/ha/status')).json()
        self.assertTrue(native['credentialAvailable'])
        self.assertEqual(native['credentialAvailable'], settings['credential_available'])
        self.assertEqual(settings['authorization_state'], 'saved_record_available')
        self.assertFalse(settings['home_readable'])
        self.assertEqual(settings['server_acceptance'], 'not_checked')
        self.assertNotIn('SYNTHETIC-ACCESS', json.dumps(settings))
        self.assertNotIn(str(backend.CREDENTIAL), json.dumps(settings))

    async def test_incomplete_or_invalid_new_record_does_not_fall_back_or_claim_ready(self):
        self.saved(backend.CREDENTIAL)
        with patch.object(backend, 'config', lambda: fixture.SettingsConfig(ha_url='http://synthetic.invalid:8123')):
            for value in [None, 'invalid']:
                self.saved(backend.NEW_CREDENTIAL, expires_at=value)
                native = (await self.client.get('/desktop/status')).json()
                public = (await self.client.get('/api/settings/ha/status')).json()
                self.assertFalse(native['credentialAvailable'])
                self.assertEqual(native['credentialAvailable'], public['credential_available'])
                self.assertTrue(backend.CREDENTIAL.exists())
            self.saved(backend.NEW_CREDENTIAL, expires_at=time.time()-1, refresh_token='')
            self.assertFalse((await self.client.get('/api/settings/ha/status')).json()['credential_available'])
            self.saved(backend.NEW_CREDENTIAL, expires_at=time.time()-1)
            self.assertTrue((await self.client.get('/api/settings/ha/status')).json()['credential_available'])
            self.saved(backend.NEW_CREDENTIAL)
            os.chmod(backend.NEW_CREDENTIAL, 0o644)
            self.assertFalse((await self.client.get('/api/settings/ha/status')).json()['credential_available'])

    async def test_first_rejection_remains_observed_when_workspace_was_not_created(self):
        async def failed(_renewal):
            raise backend.RealHomeFailure('ha_authorization_rejected', [{'status':401}], phase='home_read')
        with patch.object(backend, 'real_adapter', failed):
            await self.client.post('/desktop/connect')
        self.assertIsNone(self.wrapper.inner)
        public = (await self.client.get('/api/settings/ha/status')).json()
        self.assertEqual(public['server_acceptance'], 'unavailable')
        self.assertFalse(public['home_readable'])
        self.assertEqual(public['connection_phase'], 'home_read')

    async def test_saved_login_failure_reports_only_fixed_phase_and_usable_recovery(self):
        async def failed(_renewal):
            raise backend.RealHomeFailure('ha_configuration_or_protocol_error', [], phase='saved_login')
        with patch.object(backend,'real_adapter',failed):
            report = (await self.client.post('/desktop/connect')).json()
        self.assertEqual(report['failureEvidence']['phase'],'saved_login')
        page = await self.client.get('/desktop/authorization')
        self.assertIn('本机保存的登录记录暂不可用',page.text)
        self.assertIn('id="retry"',page.text)
        self.assertIn('id="login"',page.text)
        self.assertNotIn('SYNTHETIC-ACCESS',page.text)
        self.assertEqual(page.headers['cache-control'],'no-store')
        denied = await self.client.get('/api/settings/ha/status',headers={'Origin':'https://invalid.example'})
        self.assertEqual(denied.status_code,403)

    async def test_local_workspace_error_is_not_presented_as_server_authorization_rejection(self):
        with patch.object(backend,'local_people_catalog',side_effect=ValueError('SYNTHETIC-PRIVATE-DETAIL')):
            report = (await self.client.post('/desktop/connect')).json()
        self.assertEqual(report['failureEvidence']['phase'],'workspace')
        page = await self.client.get('/desktop/authorization')
        self.assertIn('本机工作区准备未完成',page.text)
        self.assertNotIn('SYNTHETIC-PRIVATE-DETAIL',page.text)
        self.assertNotIn('Home Assistant 拒绝了现有授权',page.text)
        self.assertTrue(self.adapters[-1].closed)

if __name__ == '__main__':
    names = ['test_workspace_symlink_never_tightens_a_foreign_target',
             'test_existing_app_workspace_is_tightened_without_changing_user_files',
             'test_native_and_settings_use_same_selected_saved_login',
             'test_incomplete_or_invalid_new_record_does_not_fall_back_or_claim_ready',
             'test_first_rejection_remains_observed_when_workspace_was_not_created',
             'test_saved_login_failure_reports_only_fixed_phase_and_usable_recovery',
             'test_local_workspace_error_is_not_presented_as_server_authorization_rejection',
             'test_actual_construction_mounts_empty_people_and_unconfigured_files']
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(ConnectionChecks(n) for n in names))
    sys.exit(not result.wasSuccessful())
