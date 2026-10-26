"""A second UI reads the existing owner without acquiring its connection."""
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from homeplane.api import create_app
from homeplane.geek_background import MARKER_NAME
from homeplane.geek_background_contracts import BackgroundInstallation
from homeplane.geek_daemon import GeekDaemon, atomic_private
from homeplane.geek_setup import GeekSetup
from homeplane.settings import SettingsService
from test_homeplane_geek_background import launcher, settle, URL, ORIGIN


@pytest.mark.asyncio
async def test_separate_console_reads_owner_and_cannot_reconfigure_it():
    with tempfile.TemporaryDirectory(prefix='g37-',dir='/tmp') as directory:
        root=Path(directory).resolve(); owner=root/'owner'; owner.mkdir()
        settings=SettingsService(owner/'settings.json');settings.update({'geek_url':URL})
        GeekSetup(owner).save('SYNTHETIC-pin')
        atomic_private(owner/MARKER_NAME,BackgroundInstallation(use_for_connections=True).model_dump_json())
        processes=[];daemon=GeekDaemon(owner,launcher=launcher(processes,snapshot=True))
        await daemon.start()
        try:
            await daemon.connect(URL);await settle()
            ui=root/'ui';ui.mkdir()
            app=create_app(db_path=ui/'traces.sqlite',settings_path=ui/'settings.json',geek_readonly_directory=owner)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
                    report=(await client.get('/api/settings/geek/session')).json()
                    assert report['status']=='online' and report['session_live']
                    inventory=(await client.get('/api/settings/geek/inventory')).json()
                    assert inventory['status']=='ready' and len(inventory['graphs'])==1
                    for operation in ('connect','disconnect','background/install','credential'):
                        result=await client.post('/api/settings/geek/'+operation,headers={'origin':ORIGIN},json={'passcode':'NEW-SYNTHETIC-pin'} if operation=='credential' else {})
                        assert result.status_code==403
                    result=await client.put('/api/settings',headers={'origin':ORIGIN},json={'geek_url':'http://192.168.50.2:8086'})
                    assert result.status_code==403
                    public=(await client.get('/api/settings')).json()
                    assert public['geek_background_enabled'] and public['geek_managed_separately']
                    assert 'SYNTHETIC-pin' not in json.dumps(public)
            assert len(processes)==1 and processes[0].returncode is None
            assert daemon.intent.keep_connected
            assert not (ui/'geek-passcode').exists()
        finally:await daemon.close()
