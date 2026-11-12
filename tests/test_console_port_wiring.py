"""Declared loopback origins, complete mocked OAuth and hostile-header rejection."""

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homeplane import ha_setup
from homeplane.api import create_app
from homeplane.settings import SettingsService


@pytest.mark.parametrize('port', [8740, 18744])
def test_declared_port_binds_every_oauth_step_and_rejects_spoofed_headers(tmp_path, monkeypatch, port):
    origin = f'http://127.0.0.1:{port}'
    config = tmp_path/'settings.json'
    config.write_text(json.dumps({'ha_url':'http://ha.fixture.invalid:8123'}))
    credential = tmp_path/'credential.json'
    monkeypatch.setattr(ha_setup, 'CREDENTIAL_PATH', credential)
    calls = []
    def exchange(request):
        calls.append(request)
        return httpx.Response(200, json={'access_token':'SYNTHETIC', 'refresh_token':'SYNTHETIC',
                                         'expires_in':1800, 'token_type':'Bearer'})
    app = FastAPI()
    ha_setup.install_oauth(app, config, base_url=origin, transport=httpx.MockTransport(exchange))
    with TestClient(app, base_url=origin, follow_redirects=False) as client:
        assert client.get('/api/settings/ha/status').status_code == 200
        for headers in ({'Origin':'http://evil.invalid'}, {'Origin':origin,'Host':'evil.invalid'},
                        {'Origin':origin,'Host':'127.0.0.1:9999'}, {'Origin':origin,'Sec-Fetch-Site':'cross-site'}):
            assert client.post('/api/settings/ha/connect', headers=headers).status_code == 403
        connected = client.post('/api/settings/ha/connect', headers={'Origin':origin})
        query = parse_qs(urlsplit(connected.json()['authorization_url']).query)
        assert query['client_id'] == [origin] and query['redirect_uri'] == [origin+ha_setup.CALLBACK]
        assert not calls
        result = client.get(ha_setup.CALLBACK, params={'code':'SYNTHETIC-CODE','state':query['state'][0]},
                            headers={'Sec-Fetch-Site':'cross-site'})
        assert result.status_code == 303
        assert parse_qs(calls[0].content.decode())['client_id'] == [origin]
        assert client.get(ha_setup.CALLBACK, params={'code':'SYNTHETIC-CODE','state':query['state'][0]}).status_code == 400
        assert len(calls) == 1


@pytest.mark.parametrize('origin', ['http://evil.invalid:18744','https://127.0.0.1:18744',
                                  'http://127.0.0.1:18744/path','http://user@127.0.0.1:18744',
                                  'http://127.0.0.1:0'])
def test_only_explicit_loopback_origins_are_accepted(tmp_path, origin):
    with pytest.raises(ValueError):
        ha_setup.install_oauth(FastAPI(), tmp_path/'settings.json', base_url=origin)


@pytest.mark.parametrize('port', [80, 8740, 18744])
def test_actual_console_alias_keeps_the_declared_origin_and_privacy_boundary(tmp_path, port):
    authority = '' if port == 80 else f':{port}'
    origin = 'http://127.0.0.1' + authority
    alias = 'localhost' + authority
    settings = SettingsService(tmp_path/'settings.json', token_path=tmp_path/'absent-token.json')
    app = create_app(db_path=tmp_path/'synthetic-traces.sqlite', settings_service=settings, console_port=port)
    with TestClient(app, base_url=origin, follow_redirects=False) as client:
        response = client.get('/?code=SYNTHETIC-NOT-FORWARDED',
                              headers={'Host':alias, 'X-Forwarded-Host':'evil.invalid'})
        assert response.status_code == 303 and response.headers['location'] == origin + '/'
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert client.head('/', headers={'Host':alias}).status_code == 200
        assert client.get('/', headers={'Host':'localhost:9999'}).status_code == 200
        assert client.post('/api/settings/ha/connect',
                           headers={'Host':alias, 'Origin':'http://' + alias}).status_code == 403
        for headers in ({}, {'Origin':'http://evil.invalid'},
                        {'Origin':origin, 'Sec-Fetch-Site':'cross-site'}):
            assert client.put('/api/settings', json={}, headers=headers).status_code == 403
        assert client.get('/api/settings', headers={'Sec-Fetch-Site':'cross-site'}).status_code == 403
        assert not settings.path.exists()


@pytest.mark.parametrize('port', [0, 65536, True, '18744'])
def test_invalid_console_ports_fail_before_any_runtime_is_created(port):
    with pytest.raises(ValueError, match='invalid_console_port'):
        create_app(console_port=port)


def test_cli_passes_same_declared_port_to_console_and_listener(monkeypatch, tmp_path):
    import uvicorn

    from homeplane import api, cli
    calls = []
    adapter, application = object(), object()
    monkeypatch.setenv('FRIDAY_INTELLIGENCE_DIRECTORY', str(tmp_path / 'intelligence'))
    monkeypatch.setattr('sys.argv', ['home','serve','--port','18744'])
    monkeypatch.setattr(cli, 'adapter_from_env', lambda _args: adapter)
    def build(value, **kwargs):
        assert value is adapter
        calls.append(('create',kwargs))
        return application
    def serve(value, **kwargs):
        assert value is application
        calls.append(('serve',kwargs))
    monkeypatch.setattr(api, 'create_app', build)
    monkeypatch.setattr(uvicorn, 'run', serve)
    cli.main()
    assert calls[0] == ('create', {'console_port':18744, 'intelligence_directory':tmp_path / 'intelligence'})
    assert calls[1][1]['port'] == 18744 and calls[1][1]['host'] == '127.0.0.1'
    assert calls[1][1]['proxy_headers'] is False
