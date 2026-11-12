"""Trusted timezone source and flow composition; no network or real home data."""
from zoneinfo import ZoneInfoNotFoundError

import httpx
import pytest

from homeplane.ha import HAReadOnlyAdapter
from homeplane.api import create_app
from homeplane.simulator import Simulator


@pytest.mark.asyncio
@pytest.mark.parametrize('config,expected', [
    ({'time_zone': 'America/New_York'}, 'America/New_York'),
    ({'time_zone': 'Asia/Shanghai'}, 'Asia/Shanghai'),
    ({'time_zone': 'Not/A_Zone'}, None), ({'time_zone': 8}, None), ({}, None),
])
async def test_timezone_is_validated_from_explicit_ha_source(config, expected):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=config)
    adapter = HAReadOnlyAdapter('http://ha.test', 'SYNTHETIC', 'fixture-home', transport=httpx.MockTransport(handle))
    try:
        assert await adapter.household_timezone() == expected
        assert [r.url.path for r in requests] == ['/api/config']
        assert requests[0].headers['authorization'] == 'Bearer SYNTHETIC'
    finally:
        await adapter.close()


class OfflineARL:
    async def status(self, **_kwargs):
        return {'status':'not_running','ready':False,'metadata_freshness':'current'}


@pytest.mark.asyncio
async def test_flow_uses_household_metadata_without_host_timezone(tmp_path):
    class Household(Simulator):
        calls = 0
        async def household_timezone(self):
            self.calls += 1
            return 'America/New_York'
    adapter = Household()
    app = create_app(adapter, db_path=tmp_path/'traces.sqlite', settings_path=tmp_path/'settings.json',
                     arl_runtime=OfflineARL())
    async with app.router.lifespan_context(app):
        flow = app.state.home_flow
        result = await flow.bootstrap()
        session = flow.sessions[result.session_id]
        assert flow._context(session).household_timezone == 'America/New_York'
        assert adapter.calls == 1
        assert session.household_timezone == 'America/New_York'


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, 'Invalid/Source', ZoneInfoNotFoundError('missing')])
async def test_invalid_or_missing_metadata_stays_unresolved(tmp_path, value):
    class Household(Simulator):
        async def household_timezone(self):
            if isinstance(value, Exception):
                raise value
            return value
    app = create_app(Household(), db_path=tmp_path/'traces.sqlite', settings_path=tmp_path/'settings.json',
                     arl_runtime=OfflineARL())
    async with app.router.lifespan_context(app):
        flow = app.state.home_flow
        result = await flow.bootstrap()
        assert flow._context(flow.sessions[result.session_id]).household_timezone is None
