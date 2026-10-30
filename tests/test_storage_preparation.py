import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.storage_preparation import StoragePreparation,mount_saved_url


def settings(reports):
    return SimpleNamespace(config=SimpleNamespace(nas_url='smb://nas.invalid/recordings',nas_path=None),
                           probe_nas=AsyncMock(side_effect=reports))


@pytest.mark.asyncio
async def test_existing_connection_is_reused_without_mount_or_credential_read():
    runner=AsyncMock();owner=StoragePreparation(settings([{'readable':True}]),runner=runner,platform='darwin')
    assert (await owner.prepare())['state']=='online'
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_page_preparation_uses_saved_url_in_bounded_no_ui_worker():
    runner=AsyncMock(return_value=(0,b'{"mounted":true}'))
    saved=settings([{'status':'mount_required'},{'readable':True}])
    result=await StoragePreparation(saved,runner=runner,platform='darwin').prepare()
    assert result==dict(state='online',readable=True,automatically_prepared=True)
    payload=json.loads(runner.call_args.kwargs['input'])
    assert payload=={'url':'smb://nas.invalid/recordings'}
    assert runner.call_args.kwargs['timeout']==25
    assert 'homeplane.storage_preparation' in repr(runner.call_args.args)


@pytest.mark.asyncio
async def test_missing_auth_is_reported_after_preparation_and_not_retried_in_a_loop():
    runner=AsyncMock(return_value=(0,b'{"mounted":false,"authentication_required":true}'))
    saved=settings([{'status':'mount_required'}]*3)
    owner=StoragePreparation(saved,runner=runner,platform='darwin')
    first=await owner.prepare();assert first['state']=='needs_authentication'
    assert await owner.prepare()==first
    runner.assert_awaited_once()


@pytest.mark.asyncio
async def test_simulator_does_not_mount_real_shares():
    runner=AsyncMock();owner=StoragePreparation(settings([{'status':'mount_required'}]),
        runner=runner,platform='darwin',simulated=lambda:True)
    assert (await owner.prepare())['readable'] is False
    runner.assert_not_called()


def test_url_credentials_and_arbitrary_protocols_are_rejected_before_os_calls():
    import sys
    if sys.platform!='darwin':pytest.skip('macOS input fence')
    for url in ('smb://name:secret@example.invalid/share','file:///tmp','https://nas.invalid/share'):
        with pytest.raises(ValueError):mount_saved_url(url)
