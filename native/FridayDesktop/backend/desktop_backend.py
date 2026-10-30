"""Friday desktop service: existing Console over explicit real read-only ingress.

No Simulator fallback, credential export, new grant, device control or runtime bootstrap.
Only the current saved grant can renew its access token at the configured HA token endpoint.
The login agent owns persistent mode. Compatibility callers may still use stdin EOF.
"""
import argparse
import contextvars
import asyncio
import functools
import json
import math
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

PRODUCT = Path(__file__).resolve().parent.parent / 'product'
if not PRODUCT.exists():
    PRODUCT = Path(__file__).resolve().parent.parent / 'Resources/product'
sys.path[:0] = [str(PRODUCT / 'vendor'), str(PRODUCT / 'app')]
# Optional runtime lives outside immutable App resources. No models/SDK load on import.
USER_SUPPORT = Path.home() / 'Library/Application Support'
BASE = Path(os.environ.get('FRIDAY_HOME', USER_SUPPORT / 'Friday')).expanduser().absolute()
os.environ.setdefault('FRIDAY_EMBODIED_DATA', str(BASE / 'data/embodied'))
os.environ['FRIDAY_EMBODIED_DATA'] = str(Path(os.environ['FRIDAY_EMBODIED_DATA']).expanduser().resolve())

import httpx
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse
from homeplane.arl import ARLRuntime
from homeplane.ha import HAReadOnlyAdapter
from homeplane.settings import SettingsConfig, SettingsService
from friday.home import Credentials

from homeplane.runtime_log import RuntimeLog, SafeServerHandler, asgi_observed, observe

CONFIG = Path(os.environ.get('FRIDAY_SETTINGS', BASE / 'data/homeplane/settings.json'))
CREDENTIAL = BASE / 'data/credentials/home-assistant.json'
NEW_CREDENTIAL = BASE / 'data/desktop/credentials/home-assistant.json'
STATE = BASE / 'data/desktop'
ORIGIN = 'http://127.0.0.1:8740'
_device_control = contextvars.ContextVar('friday_desktop_embodied_control',default=False)
READ_ONLY_PREPARATION_PATHS = frozenset({
    '/api/query', '/api/actions/plan', '/api/actions/dry-run',
    '/api/rules/core/plans', '/api/rules/core/plans/trial',
    '/api/rules/text/plan', '/api/rules/plans/simulate',
    '/api/rules/analysis/reviews', '/api/rules/analysis/cancel',
    '/api/archive/playback/cancel',
    '/api/archive/resolve', '/api/archive/playback',
    '/api/home/evidence/query', '/api/home/evidence/detail',
    '/api/home/recordings/stop',
    '/api/files/query', '/api/files/settings/query',
    '/api/diagnostics/export',
})
# RuleIR permits 12 KiB; retain a bounded envelope for this existing preview.
READ_ONLY_REQUEST_BYTES = {'/api/rules/core/plans': 16384}
PEOPLE_WRITE_PATHS = frozenset('/api/people/' + name for name in (
    'name', 'merge', 'split', 'correct', 'undo', 'voice-review', 'live/register', 'live/forget', 'live/name-candidate',
    'live/candidate-group','live/candidate-group/undo'))
HELD_GET_PATHS = frozenset({'/api/settings/ha/discover', '/api/settings/miloco/models'})
HELD_GET_PREFIXES = ('/api/archive/media/', '/api/home/recordings/media/', '/api/family-media/originals/')


CONNECTION_PHASES = frozenset({'configuration', 'saved_login', 'home_read', 'workspace', 'startup'})


class RealHomeFailure(RuntimeError):
    def __init__(self, code, http_reads, phase='home_read'):
        self.http_reads = http_reads
        self.connection_phase = phase if phase in CONNECTION_PHASES else 'home_read'
        super().__init__(code)


def config():
    if CONFIG.is_symlink() or CONFIG.stat().st_size > 16384:
        raise ValueError('invalid_config')
    return SettingsConfig.model_validate(json.loads(CONFIG.read_text()).get('config', {}))


def credentials(settings, client):
    saved = Credentials(SimpleNamespace(ha_url=settings.ha_url,
                                        ha_token_env='FRIDAY_NO_ENV_CREDENTIALS'), client)
    saved.path = NEW_CREDENTIAL if NEW_CREDENTIAL.exists() else CREDENTIAL
    return saved


def saved_authorization(settings):
    """One bounded, private projection for native and embedded Settings consumers.

    Presence is not current server acceptance. Never expose file names, token
    values or private exception text; this function does no refresh or network IO.
    """
    saved = credentials(settings, None)
    try:
        generation = SameGrantRenewal.generation(saved.path)
        value = json.loads(saved.path.read_text())
        if generation != SameGrantRenewal.generation(saved.path) or not isinstance(value, dict):
            return False, 'unavailable'
        if value.get('ha_url') != settings.ha_url:
            return False, 'unavailable'
        expiry = value.get('expires_at')
        if (not isinstance(value.get('access_token'), str) or not value['access_token']
                or type(expiry) not in {int, float} or not math.isfinite(expiry)):
            return False, 'unavailable'
        if expiry < time.time() + 60 and not all(
                isinstance(value.get(key), str) and value[key] for key in ('refresh_token', 'client_id')):
            return False, 'login_required'
        return True, 'saved_record_available'
    except FileNotFoundError:
        return False, 'login_required'
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False, 'unavailable'


def connection_configuration():
    try:
        settings = config()
        available, authorization = saved_authorization(settings)
        return {'configurationAvailable': bool(settings.ha_url),
                'credentialAvailable': available,
                'authorizationState': authorization,
                'authorizationRevision': authorization_revision(),
                'networkHost': httpx.URL(settings.ha_url).host if settings.ha_url else None,
                'networkPort': httpx.URL(settings.ha_url).port or (443 if settings.ha_url and settings.ha_url.startswith('https:') else 80)}
    except (OSError, ValueError, KeyError, TypeError):
        return {'configurationAvailable': False, 'credentialAvailable': False,
                'authorizationState': 'configuration_required',
                'networkHost': None, 'networkPort': 80}


def authorization_revision():
    """Change hint only; the real provider read still validates authorization."""
    try:
        if NEW_CREDENTIAL.is_symlink():
            return None
        info = NEW_CREDENTIAL.stat()
        return f'{info.st_mtime_ns:x}-{info.st_ino:x}'
    except OSError:
        return None


def failure_code(error):
    if isinstance(error, RuntimeError) and str(error) in {
            'ha_authorization_rejected', 'ha_unreachable', 'ha_read_timeout',
            'ha_http_error', 'ha_configuration_or_protocol_error'}:
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        return 'ha_authorization_rejected' if error.response.status_code in {401, 403} else 'ha_http_error'
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return 'ha_read_timeout'
    if isinstance(error, (OSError, httpx.RequestError)):
        return 'ha_unreachable'
    return 'ha_configuration_or_protocol_error'


class SameGrantRenewal:
    """Renew only the current saved grant; suppress failed refresh bursts."""
    def __init__(self):
        self.lock = asyncio.Lock()
        self.failure = None
        self.active_generation = None
        self.refresh_attempted = False
        self.refresh_suppressed = False

    @staticmethod
    def generation(path):
        info = path.stat()
        if path.is_symlink() or info.st_size > 16384 or info.st_mode & 0o077:
            raise ValueError('invalid_saved_credential')
        return (str(path.absolute()), info.st_ino, info.st_mtime_ns, info.st_size)

    @observe('ha.auth', upstream='ha')
    async def token(self, saved):
        async with self.lock:
            generation = self.generation(saved.path)
            self.active_generation = generation
            self.refresh_suppressed = False
            if self.failure and self.failure[0] == generation:
                _, code, retry_at = self.failure
                if retry_at is None or time.monotonic() < retry_at:
                    self.refresh_suppressed = True
                    raise RuntimeError(code)
            value = json.loads(saved.path.read_text())
            expiry = value.get('expires_at')
            if type(expiry) not in {int, float} or not math.isfinite(expiry):
                raise ValueError('invalid_saved_credential_expiry')
            if expiry < time.time()+60 and not (value.get('refresh_token') and value.get('client_id')):
                self.failure = (generation, 'ha_authorization_rejected', None)
                raise RuntimeError('ha_authorization_rejected')
            try:
                token = await saved.token(allow_refresh=True)
                if not isinstance(token, str) or not token:
                    raise ValueError('invalid_saved_token')
            except httpx.HTTPStatusError as error:
                rejected = error.response.status_code in {400, 401, 403}
                code = 'ha_authorization_rejected' if rejected else 'ha_http_error'
                self.failure = (generation, code, None if rejected else time.monotonic()+60)
                raise RuntimeError(code) from None
            except (httpx.RequestError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
                code = failure_code(error)
                self.failure = (generation, code, time.monotonic()+60)
                raise RuntimeError(code) from None
            self.failure = None
            return token


@observe('ha.connect', upstream='ha')
async def real_adapter(renewal=None):
    device_control=_device_control.get()
    settings = config()
    if not settings.ha_url:
        raise ValueError('ha_not_configured')
    client = httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False)
    renewal = renewal or SameGrantRenewal()
    saved = credentials(settings, client)
    reads = []
    refresh_url = httpx.URL(settings.ha_url.rstrip('/')+'/auth/token')
    async def only_read_or_renew(request):
        if request.method == 'GET':
            return
        if device_control and request.method=='POST':
            permitted={'/api/services/light/turn_on','/api/services/light/turn_off','/api/services/switch/turn_on','/api/services/switch/turn_off','/api/services/cover/set_cover_position','/api/services/cover/open_cover','/api/services/cover/close_cover','/api/services/climate/set_hvac_mode','/api/services/climate/set_temperature',
                       '/api/services/fan/turn_on','/api/services/fan/turn_off','/api/services/fan/set_percentage',
                       '/api/services/lock/lock','/api/services/lock/unlock','/api/services/number/set_value','/api/services/select/select_option',
                       '/api/services/climate/set_fan_mode','/api/services/climate/turn_on','/api/services/climate/turn_off',
                       '/api/services/cover/stop_cover','/api/services/cover/set_cover_tilt_position',
                       '/api/services/media_player/turn_on','/api/services/media_player/turn_off',
                       '/api/services/media_player/volume_set','/api/services/media_player/volume_mute',
                       '/api/services/media_player/media_play','/api/services/media_player/media_pause'}
            if request.url.path in permitted and str(request.url).startswith(settings.ha_url.rstrip('/')+'/api/services/') and len(request.content)<=4096:
                payload=json.loads(request.content)
                if isinstance(payload,dict) and isinstance(payload.get('entity_id'),str):return
        if (request.method != 'POST' or request.url != refresh_url
                or request.headers.get('content-type') != 'application/x-www-form-urlencoded'
                or len(request.content) > 16384):
            raise ValueError('grant_or_external_write_forbidden')
        form = parse_qs(request.content.decode(), strict_parsing=True, keep_blank_values=True)
        current = json.loads(saved.path.read_text())
        if (set(form) != {'grant_type', 'refresh_token', 'client_id'}
                or form['grant_type'] != ['refresh_token']
                or any(len(values) != 1 or not values[0] for values in form.values())
                or form['refresh_token'] != [current.get('refresh_token')]
                or form['client_id'] != [current.get('client_id')]
                or renewal.active_generation != renewal.generation(saved.path)):
            raise ValueError('grant_or_external_write_forbidden')
        renewal.refresh_attempted = True
        request.extensions['friday_credential_generation'] = renewal.active_generation
    async def record(response):
        reads.append({'resource': response.request.url.path if response.request.url.path in {'/api/states', '/api/services', refresh_url.path} else 'other',
                      'status': response.status_code, 'method': response.request.method})
        if response.request.method == 'POST' and response.request.url == refresh_url and 200 <= response.status_code < 300:
            if response.status_code != 200:
                raise ValueError('invalid_refresh_response')
            await response.aread()
            if len(response.content) > 16384:
                raise ValueError('invalid_refresh_response')
            value = response.json()
            if (not isinstance(value, dict)
                    or set(value)-{'access_token', 'refresh_token', 'token_type', 'expires_in'}
                    or not isinstance(value.get('access_token'), str) or not value['access_token']
                    or type(value.get('expires_in')) is not int or not 60 < value['expires_in'] <= 86400
                    or not isinstance(value.get('token_type', 'Bearer'), str)
                    or value.get('token_type', 'Bearer').lower() != 'bearer'
                    or ('refresh_token' in value and (not isinstance(value['refresh_token'], str) or not value['refresh_token']))
                    or response.request.extensions['friday_credential_generation'] != renewal.generation(saved.path)):
                raise ValueError('invalid_refresh_response')
    client.event_hooks = {'request': [only_read_or_renew], 'response': [record]}
    phase = 'saved_login'
    try:
        if not saved.connected():
            raise ValueError('ha_credentials_unavailable')
        token = await renewal.token(saved)
        if not isinstance(token, str) or not token:
            raise ValueError('invalid_saved_token')
        phase = 'home_read'
        instance = settings.ha_instance
        if not instance:
            import hashlib
            instance = 'configured-' + hashlib.sha256(settings.ha_url.encode()).hexdigest()[:16]
        adapter = HAReadOnlyAdapter(settings.ha_url, token, instance, client=client,
                                   token_provider=functools.partial(renewal.token, saved))
        adapter._owned_client = True
        async with asyncio.timeout(15):
            snapshot = await adapter.snapshot()
        adapter._desktop_http_reads = reads
        if snapshot.mode != 'ha-read-only' or adapter.health()['transport'] != 'online':
            failure = 'ha_authorization_rejected' if any(item['status'] in {401, 403} for item in reads) else 'ha_unreachable'
            raise RealHomeFailure(failure, reads)
        return settings, adapter, snapshot
    except BaseException as error:
        await client.aclose()
        if not isinstance(error, (RealHomeFailure, asyncio.CancelledError)):
            raise RealHomeFailure(failure_code(error), reads, phase=phase) from None
        raise


def arl_runtime():
    try:
        location = config().arl_home
    except (OSError, ValueError, KeyError, TypeError):
        location = None
    return ARLRuntime(lambda: Path(location) if location else USER_SUPPORT / 'ARL')


def prepare_private_workspace():
    """Create or tighten only the app-owned desktop state directory.

    Existing mkdir(mode=0700) does not change an earlier 0755 directory.
    Keep owner/path checks and use a pinned directory fd; never relax private
    storage checks or alter the contents/modes of configuration/login files.
    """
    if STATE.is_symlink() or any(parent.is_symlink() for parent in STATE.parents):
        raise ValueError('desktop_private_workspace_required')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError('desktop_private_workspace_required')
        current = STATE.lstat()
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('desktop_workspace_changed')
        if stat.S_IMODE(before.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
        after = os.fstat(descriptor)
        if stat.S_IMODE(after.st_mode) != 0o700:
            raise ValueError('desktop_private_workspace_required')
    finally:
        os.close(descriptor)


def local_people_catalog():
    """Open only an owner-local dedicated metadata DB, with no sample seeding."""
    from homeplane.people import PeopleCatalog
    from homeplane.people_feature_bridge import MAX_PEOPLE_DATABASE_BYTES, qualify_people_catalog
    from homeplane.store import local_database_path
    requested = STATE / 'people.sqlite'
    if requested.is_symlink() or any(p.is_symlink() for p in requested.parents):
        raise ValueError('people_private_database_required')
    directory = STATE.stat()
    if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise ValueError('people_private_directory_required')
    path = local_database_path(requested)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_size > MAX_PEOPLE_DATABASE_BYTES):
            raise ValueError('people_private_database_required')
    finally:
        os.close(fd)
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        current = path.stat()
        if path.is_symlink() or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('people_database_changed')
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and tables != {'people_state', 'people_undo'}:
            raise ValueError('people_dedicated_connection_required')
        catalog = PeopleCatalog(connection)
        qualify_people_catalog(catalog)
        return catalog
    except BaseException:
        connection.close()
        raise


def arl_connection_state(report):
    """Current health only; cached readiness cannot qualify a current connection."""
    freshness = report.get('metadata_freshness', 'unknown')
    if report.get('metadata_refresh_pending') is True:
        return 'refreshing'
    if freshness == 'last_known':
        return 'stale'
    if freshness not in {'current', 'unavailable'}:
        return 'unknown'
    status = report.get('status')
    if status == 'ready' and report.get('ready') is True and freshness == 'current':
        return 'connected'
    return {'disconnected': 'disconnected', 'not_running': 'not_running',
            'sdk_missing': 'sdk_missing', 'user_disabled': 'disabled',
            'unsafe_home': 'unsafe_configuration', 'starting': 'starting'}.get(status, 'unknown')


async def arl_status(runtime=None):
    runtime = runtime or arl_runtime()
    report = await runtime.status(start=False, timeout=3)
    return {'status': report['status'], 'sdkVersion': report.get('sdk_version'),
            'ready': report.get('ready') is True, 'inferenceAttempted': False,
            'metadataFreshness': report.get('metadata_freshness', 'unknown'),
            'refreshPending': report.get('metadata_refresh_pending') is True,
            'refreshCode': report.get('metadata_refresh_code', 'none'),
            'refreshSource': report.get('metadata_refresh_source', 'sdk'),
            'connectionState': arl_connection_state(report),
            'taskReadiness': 'unverified', 'identityVerified': False,
            'modelCapabilityVerified': False}


class DesktopService:
    def __init__(self, *, nas_credential_resolver=None, runtime_log=None,
                 arl_planning_authorizer=None, arl_planning_executor=None,
                 query_adoption_config=None):
        # Trusted host composition only. Neither HTTP nor runtime metadata can
        # establish an execution policy, verified peer or household read scope.
        if (arl_planning_authorizer is None) != (arl_planning_executor is None) or any(
                value is not None and not callable(value)
                for value in (arl_planning_authorizer, arl_planning_executor)):
            raise ValueError('complete_planning_policy_required')
        if query_adoption_config is not None:
            from homeplane.query import HomeQueryAdoptionConfig
            if not isinstance(query_adoption_config, HomeQueryAdoptionConfig):
                raise ValueError('invalid_query_adoption_config')
        self.planning_authorizer = arl_planning_authorizer
        self.planning_executor = arl_planning_executor
        self.query_adoption_config = query_adoption_config
        self.runtime_log = runtime_log or RuntimeLog(BASE / 'logs')
        self.runtime_log.record('service.construct', 'start')
        self.nas_credential_resolver = nas_credential_resolver
        self.inner = None
        self.lifespan = None
        self.adapter = None
        self.connection = 'not_connected'
        self.failure = None
        self.failure_evidence = None
        self._last_health_result = None
        self._cleanup_task = None
        self._cleanup_state = 'idle'
        self.provider_reads = []
        self.renewal = SameGrantRenewal()
        try:
            previous = json.loads((BASE / 'READ-ONLY-HA-STATUS.json').read_text())
            if (not NEW_CREDENTIAL.exists()
                    and previous['credentialMtimeNS'] == CREDENTIAL.stat().st_mtime_ns
                    and previous['configMtimeNS'] == CONFIG.stat().st_mtime_ns
                    and previous['failure'] == 'ha_authorization_rejected'):
                self.connection = 'failed'
                self.failure = previous['failure']
                self.failure_evidence = previous['evidence']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.entity_count = 0
        self.gate = asyncio.Lock()
        self.arl_runtime = arl_runtime()
        self.arl_task = None
        self.home_task = None
        self.persistent = False
        self.arl = {'status': 'not_checked', 'sdkVersion': '0.1.1', 'ready': False, 'inferenceAttempted': False}
        self.metadata = FastAPI()
        self.metadata.add_api_route('/desktop/status', self.status, methods=['GET'])
        self.metadata.add_api_route('/desktop/capabilities', self.capabilities, methods=['GET'])
        self.metadata.add_api_route('/desktop/connect', self.connect, methods=['POST'])
        self.metadata.add_api_route('/desktop/disconnect', self.disconnect, methods=['POST'])
        self.metadata.add_api_route('/desktop/authorization', self.authorization_page, methods=['GET'])
        self.metadata.add_api_route('/desktop/setup', self.setup_page, methods=['GET'])
        self.metadata.add_api_route('/desktop/setup/assets/{name}', self.setup_asset, methods=['GET'])
        self.metadata.add_api_route('/api/settings/setup', self.setup_status, methods=['GET'])
        self.metadata.add_api_route('/api/settings/setup', self.setup_save, methods=['POST'])
        self.metadata.add_api_route('/api/settings/ha/discover', self.setup_discover, methods=['GET'])
        self.metadata.add_api_route('/api/settings/camera/probe', self.setup_camera_probe, methods=['POST'])
        from homeplane.discovery import HADiscovery
        self.setup_discovery = HADiscovery()
        self.metadata.add_api_route('/api/diagnostics/runtime', self.runtime_diagnostics, methods=['GET'])
        from homeplane import ha_setup
        ha_setup.CREDENTIAL_PATH = NEW_CREDENTIAL
        self.metadata.add_api_route('/api/settings/ha/status', self.authorization_status, methods=['GET'])
        ha_setup.install_oauth(self.metadata, CONFIG)
        self.authorization_paths = {'/api/settings/ha/connect', '/api/settings/ha/status',
                                    '/api/settings/setup', '/api/settings/ha/discover',
                                    '/api/settings/camera/probe', ha_setup.CALLBACK}
        self.runtime_log.record('service.construct', 'result')

    async def setup_page(self):
        from homeplane import onboarding
        return FileResponse(Path(onboarding.__file__).with_name('static') / 'connection-setup.html',
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    async def setup_asset(self, name: str):
        if name not in {'style.css', 'interface.css', 'connection-setup.css', 'connection-setup.js', 'connection-setup-page.js'}:
            return JSONResponse({'code': 'not_found'}, status_code=404)
        from homeplane import onboarding
        return FileResponse(Path(onboarding.__file__).with_name('static') / name, headers={'Cache-Control': 'no-store'})

    def setup_settings(self):
        return SettingsService(CONFIG, token_path=BASE / 'data/miloco/config.json')

    async def setup_status(self):
        from homeplane.onboarding import setup_report
        settings = self.setup_settings()
        health = {'transport': 'offline'}
        camera = False
        if self.inner:
            health = self.inner.state.home_experience.connection_health()
            if getattr(self.inner.state.core.adapter, 'url', None) != (settings.config.ha_url or '').rstrip('/'):
                health = {'transport': 'configuration_changed'}
            camera = self.inner.state.embodied.configured()
        return JSONResponse(setup_report(settings, health, camera_configured=camera, desktop=True,
                            control_enabled=bool(self.inner and self.inner.state.core.actions.policy_report()['enabled'])),
                            headers={'Cache-Control': 'no-store'})

    async def setup_save(self, request: Request):
        from homeplane.onboarding import SetupConfiguration
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2048:
                return JSONResponse({'code': 'request_budget_exceeded'}, status_code=413)
        try:
            command = SetupConfiguration.model_validate_json(body)
            patch = command.model_dump(exclude_unset=True)
            self.setup_settings().update(patch)
            camera_patch = {key: value for key, value in patch.items() if key.startswith('camera_')}
            if camera_patch:
                SettingsService(STATE / 'settings.json').update(camera_patch)
                if self.inner:
                    self.inner.state.settings.update(camera_patch)
        except (ValueError, OSError):
            return JSONResponse({'code': 'setup_configuration_invalid'}, status_code=400)
        return await self.setup_status()

    async def setup_discover(self):
        return await self.setup_discovery.discover()

    async def setup_camera_probe(self):
        return await self.setup_settings().probe_camera()

    async def capabilities(self):
        # Public SDK discovery is availability metadata, never account/app
        # authorization. Do not start ARL, register a peer or request a grant.
        report = await self.arl_runtime.status(start=False, timeout=3)
        codex = next((row for row in report.get('providers', [])
                      if row.get('name') == 'codex_app_server'), {})
        tool = next((row for row in report.get('tools', [])
                     if row.get('name') == 'friday.rule-removal.plan.v1'), {})
        model_ready = codex.get('availability') == 'ready' and 'gpt-6.1-sol' in codex.get('models', [])
        tool_available = tool.get('availability') == 'adapter'
        policy = self.planning_authorizer is not None and self.planning_executor is not None
        source = self.inner.state.rule_analysis.context().available if self.inner else False
        planning_status = ('policy_not_configured' if not policy else
                           'codex_unavailable' if not model_ready else
                           'planning_contract_unavailable' if not tool_available else
                           'owned_source_unavailable' if not source else 'review_required')
        from homeplane.query import HomeQueryAdoption
        query = self.inner.state.query_adoption if self.inner else None
        query_status = query.status() if query else HomeQueryAdoption.unconfigured_status()
        return JSONResponse({
            'runtime_ready': report.get('ready') is True,
            'runtime_status': report.get('status'),
            'metadata_freshness': report.get('metadata_freshness', 'unknown'),
            'metadata_refresh_pending': report.get('metadata_refresh_pending') is True,
            'metadata_refresh_code': report.get('metadata_refresh_code', 'none'),
            'metadata_refresh_source': report.get('metadata_refresh_source', 'sdk'),
            'metadata_only': True,
            'codex': {'availability': codex.get('availability', 'not_discovered'),
                      'model_available': model_ready},
            'planning': {'status': planning_status, 'policy_configured': policy,
                         'source_available': source, 'tool_available': tool_available,
                         'provider': 'codex_app_server', 'model': 'gpt-6.1-sol',
                         'explicit_review_required': True, 'action_authority': False},
            'query': {**query_status, 'console_readable': self.inner is not None and self.connection_failure() is None},
            'model_inference_attempted': False,
            'device_writes': 'disabled',
        }, headers={'Cache-Control': 'no-store'})

    async def authorization_status(self):
        facts = connection_configuration()
        return JSONResponse({
            'configured': facts['configurationAvailable'],
            'credential_available': facts['credentialAvailable'],
            'authorization_state': facts['authorizationState'],
            'home_readable': self.inner is not None and self.connection_failure() is None and self.connection == 'connected',
            'connection_phase': self.failure_evidence.get('phase') if isinstance(self.failure_evidence, dict) and self.failure_evidence.get('phase') in CONNECTION_PHASES else None,
            'server_acceptance': 'unavailable' if (
                self.connection_failure() or self.failure) == 'ha_authorization_rejected' else (
                    'not_checked' if self.inner is None else 'previously_observed'),
            'source_activation': 'explicit_required', 'device_writes': 'disabled',
        }, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    async def runtime_diagnostics(self):
        return JSONResponse(self.runtime_log.export(), headers={'Cache-Control':'no-store'})

    async def authorization_page(self):
        current_failure = self.connection_failure() or self.failure
        evidence = self.failure_evidence if isinstance(self.failure_evidence, dict) else {}
        reads = evidence.get('httpReads', [])
        recorded_rejection = isinstance(reads, list) and any(
            isinstance(row, dict) and type(row.get('status')) is int
            and row['status'] in {401, 403} for row in reads[-16:])
        if current_failure == 'ha_authorization_rejected':
            description = ('Home Assistant 拒绝了现有授权。请重新登录并确认授权；尚不能断定 token 过期。'
                           if recorded_rejection else
                           '当前家庭授权暂不可用。请重新登录并确认 Friday 的连接授权；尚不能断定 token 过期。')
        elif evidence.get('phase') in {'workspace', 'startup'}:
            description = '家庭读取之后，本机工作区准备未完成。重新登录通常不能解决此问题；请重试连接或查看本机诊断。'
        elif evidence.get('phase') == 'saved_login':
            description = '本机保存的登录记录暂不可用。请重新登录并确认授权；已有记录保留，不会自动删除。'
        elif current_failure in {'ha_unreachable', 'ha_read_timeout'}:
            description = '家庭网关暂时无法读取。请核对网络后重试；连接失败不代表需要重新登录。'
        else:
            description = '请在已配置的 Home Assistant 中登录并确认 Friday 的连接授权。'
        page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Friday · 连接已有家庭</title>
<style>body{font:16px -apple-system,BlinkMacSystemFont,sans-serif;max-width:680px;margin:64px auto;padding:24px;line-height:1.7}button{font:inherit;padding:10px 18px}p{color:#555}</style>
<h1>Friday · 连接已有家庭</h1>
<p>__AUTHORIZATION_DESCRIPTION__</p>
<p>下面的按钮会打开你已配置的 Home Assistant 登录与授权页面。由你亲自确认后，原有程序才交换授权，并将其存到本机新的 0600 私密文件。旧配置和旧凭据保留；不在聊天中提交密钥。</p>
<p>Friday 只读家庭状态和设备清单。已配置的同一 grant 可在正常到期窗口由原有程序有界续期 access token，不增加授权范围。游戏库的摄像头、语音和家居控制按本机配置与当前授权启用；其余设备写入仍关闭。</p>
<button id="retry">重新读取家庭</button> <button id="login">在 Home Assistant 中登录并授权</button><p id="result" role="status"></p>
<p><a href="/api/diagnostics/runtime">查看本机安全诊断</a></p>
<p>授权完成后回到 Friday 应用，点击“连接已有家庭”；若出现 Friday 本地网络权限弹窗，由你确认。</p>
<script>document.getElementById('retry').onclick=async()=>{const b=document.getElementById('retry');b.disabled=true;try{const r=await fetch('/desktop/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const v=await r.json();document.getElementById('result').textContent=v.homeReadable?'家庭只读连接已恢复，可以回到 Friday。':'家庭尚未可读，请核对连接状态或查看诊断。';}catch(e){document.getElementById('result').textContent='本机连接请求未完成，请稍后重试。';}finally{b.disabled=false;}};document.getElementById('login').onclick=async()=>{const b=document.getElementById('login');b.disabled=true;try{const r=await fetch('/api/settings/ha/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const v=await r.json();if(!r.ok||!v.authorization_url)throw Error();location.assign(v.authorization_url);}catch(e){document.getElementById('result').textContent='无法开始授权；旧凭据未删除。请检查已有家庭配置。';b.disabled=false;}};</script></html>'''
        return HTMLResponse(page.replace('__AUTHORIZATION_DESCRIPTION__', description),
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    def connection_failure(self):
        """Project already-observed health; status never probes or renews HA."""
        if self.inner is None:
            return None
        try:
            health = self.adapter.health()
            if not isinstance(health, dict) or health.get('mode') != 'ha-read-only':
                return 'ha_configuration_or_protocol_error'
            if (type(health.get('authentication_available')) is not bool
                    or type(health.get('observation_gap')) is not bool):
                return 'ha_configuration_or_protocol_error'
            if health.get('authentication_available') is False:
                return 'ha_authorization_rejected'
            if health.get('transport') != 'online':
                return 'ha_unreachable'
            if health.get('observation_gap') is True:
                return 'subscription_disconnected'
        except Exception:
            return 'ha_configuration_or_protocol_error'
        return None

    async def status(self):
        facts = connection_configuration()
        observed_failure = self.connection_failure()
        connection = 'failed' if observed_failure else self.connection
        failure = observed_failure or self.failure
        health_result = (connection, failure)
        if health_result != self._last_health_result:
            self._last_health_result = health_result
            self.runtime_log.record('desktop.health', 'result',
                                    result='unavailable' if failure else 'ok',
                                    code=failure or 'none', upstream='ha' if self.inner else 'local')
        return {**facts, 'service': 'running', 'connection': connection,
                'failure': failure, 'source': 'ha-read-only' if self.inner else 'not_connected',
                'failureEvidence': self.failure_evidence,
                'homeReadable': self.inner is not None and not observed_failure and self.connection == 'connected',
                'providerReads': self.provider_reads,
                'entityCount': self.entity_count, 'deviceWrites': 'scoped' if self.inner and self.inner.state.core.actions.policy_report()['enabled'] else 'disabled',
                'arl': self.arl, 'simulated': False, 'pid': os.getpid(),
                'lifetime': 'persistent' if self.persistent else 'parent'}

    @observe('desktop.arl', upstream='arl')
    async def refresh_arl(self):
        while True:
            await asyncio.sleep(3)
            self.arl = await arl_status(self.arl_runtime)

    @observe('desktop.connect', upstream='ha')
    async def connect(self):
        (STATE / 'connection-paused').unlink(missing_ok=True)
        return await self._connect()

    async def restore_home(self):
        """Restore saved intent without a UI; failed authentication waits for change.

        Existing HA streams own their reconnect. Never churn camera sessions or
        replay commands to recover an upstream transport gap.
        """
        attempt = 0
        rejected_revision = None
        while True:
            facts = connection_configuration()
            revision = facts.get('authorizationRevision')
            if (self.inner is None and not (STATE / 'connection-paused').exists()
                    and facts.get('configurationAvailable') and facts.get('credentialAvailable')
                    and (rejected_revision is None or revision != rejected_revision)):
                try:
                    await self._connect()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.runtime_log.record('desktop.health', 'result', result='unavailable', code='service_unavailable')
                if self.failure == 'ha_authorization_rejected':
                    rejected_revision = revision
                attempt = 0 if self.inner is not None else min(attempt + 1, 6)
            await asyncio.sleep(min(60, 2 ** attempt) if attempt else 5)

    async def _connect(self):
        if self.gate.locked() or self._cleanup_state in {'running', 'failed'}:
            return JSONResponse({'code': 'connection_in_progress'}, status_code=409)
        async with self.gate:
            if self.inner is not None:
                selected_url = (self.setup_settings().config.ha_url or '').rstrip('/')
                current_url = getattr(self.adapter, 'url', None)
                if self.connection_failure() is None and current_url == selected_url:
                    return await self.status()
                # Only the explicit connect request replaces an unhealthy owner.
                # A passive status read never closes or reconnects it.
                try:
                    await self.close_inner()
                except BaseException as error:
                    self.connection, self.failure = 'failed', failure_code(error)
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    return await self.status()
            self.connection, self.failure = 'connecting', None
            self.failure_evidence = None
            self.provider_reads = []
            self.renewal.refresh_attempted = False
            adapter = lifespan = people = None
            phase = 'configuration'
            lifespan_entered = False
            try:
                from homeplane.embodied.service import EmbodiedService
                integration=EmbodiedService()
                # Household control is independent from optional camera/ML setup.
                controls=(Path(os.environ['FRIDAY_EMBODIED_DATA'])/'private/action-authority.json').is_file()
                grant_context=_device_control.set(controls)
                try:settings, adapter, snapshot = await real_adapter(self.renewal)
                finally:_device_control.reset(grant_context)
                phase = 'workspace'
                from homeplane.api import create_app
                prepare_private_workspace()
                local_settings = SettingsService(
                    STATE / 'settings.json', token_path=STATE / 'camera-not-configured.json',
                    nas_credential_resolver=self.nas_credential_resolver,
                )
                # Keep the saved NAS and camera sources on real-home entry.
                if not local_settings.path.exists():
                    local_settings.config = settings.model_copy()
                else:
                    local_settings.config = local_settings.config.model_copy(update={
                        'ha_url': settings.ha_url, 'ha_instance': settings.ha_instance})
                def closed_geek(*_args, **_kwargs):
                    raise RuntimeError('desktop_read_only')
                people = local_people_catalog()
                inner = create_app(adapter, db_path=STATE / 'traces.sqlite', settings_service=local_settings,
                                   authority_path=Path(os.environ['FRIDAY_EMBODIED_DATA'])/'private/action-authority.json' if controls else STATE/'no-device-authority.json', geek_launcher=closed_geek,
                                   geek_readonly_directory=Path(os.environ['FRIDAY_EMBODIED_DATA']).parent/'homeplane',
                                   arl_runtime=self.arl_runtime,
                                   arl_planning_authorizer=self.planning_authorizer,
                                   arl_planning_executor=self.planning_executor,
                                   query_adoption_config=self.query_adoption_config,
                                   people_catalog=people, file_steward_enabled=True,
                                   family_media_indexed_kinds=('image', 'video', 'audio', 'other'),
                                   nas_credential_resolver=local_settings.resolve_nas_credential,
                                   console_read_only=not controls, runtime_log=self.runtime_log, embodied_service=integration,
                                   background_enabled=self.persistent)
                lifespan = inner.router.lifespan_context(inner)
                phase = 'startup'
                await lifespan.__aenter__()
                lifespan_entered = True
                self.inner, self.lifespan, self.adapter = inner, lifespan, adapter
                self._cleanup_task, self._cleanup_state = None, 'idle'
                self.entity_count = len(snapshot.entities)
                self.provider_reads = getattr(adapter, '_desktop_http_reads', [])
                self.connection = 'connected'
            except BaseException as error:
                if lifespan_entered:
                    await lifespan.__aexit__(type(error), error, error.__traceback__)
                elif lifespan is None:
                    if people is not None:
                        from homeplane.people_feature_bridge import close_people_catalog
                        close_people_catalog(people)
                    if adapter is not None:
                        await adapter.close()
                self.inner = self.lifespan = self.adapter = None
                self.connection, self.failure = 'failed', failure_code(error)
                self.failure_evidence = {'source': 'cached_credential_failure' if self.renewal.refresh_suppressed else
                                                   'provider_http' if isinstance(error, RealHomeFailure) else 'domain_or_transport',
                                         'httpReads': error.http_reads if isinstance(error, RealHomeFailure) else [],
                                          'phase': error.connection_phase if isinstance(error, RealHomeFailure) else phase,
                                          'credentialRefreshAttempted': self.renewal.refresh_attempted,
                                          'credentialRefreshSuppressed': self.renewal.refresh_suppressed}
                if isinstance(error, asyncio.CancelledError):
                    raise
            return await self.status()

    @observe('desktop.close', upstream='local')
    async def close(self):
        async with self.gate:
            await self.close_inner()

    async def close_inner(self):
        """One owned exit per lifespan; consumed or failed exits are never retried."""
        if self._cleanup_state == 'failed':
            raise RuntimeError('desktop_cleanup_incomplete')
        if self._cleanup_task is None:
            owner = (self.inner, self.lifespan, self.adapter)
            if all(value is None for value in owner):
                return

            async def finish_owner():
                try:
                    if owner[1] is not None:
                        await owner[1].__aexit__(None, None, None)
                    elif owner[2] is not None:
                        await owner[2].close()
                except BaseException:
                    self._cleanup_state = 'failed'
                    raise
                # Only this complete exit can release its captured resource owner.
                if (self.inner is not owner[0] or self.lifespan is not owner[1]
                        or self.adapter is not owner[2]):
                    self._cleanup_state = 'failed'
                    raise RuntimeError('desktop_cleanup_incomplete')
                self.inner = self.lifespan = self.adapter = None
                self.connection, self.entity_count = 'not_connected', 0
                self.provider_reads = []
                self._cleanup_state = 'succeeded'

            self._cleanup_state = 'running'
            self._cleanup_task = asyncio.create_task(finish_owner())
        task = self._cleanup_task
        cancelled = False
        # Keep the caller's gate until its owned cleanup finishes, even after
        # repeated caller cancellations. Shield never restarts __aexit__.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        if task.cancelled():
            self._cleanup_state = 'failed'
            raise asyncio.CancelledError
        error = task.exception()
        if error is not None:
            raise error
        if cancelled:
            raise asyncio.CancelledError

    @observe('desktop.disconnect', upstream='local')
    async def disconnect(self):
        if self.gate.locked() or self._cleanup_state in {'running', 'failed'}:
            return JSONResponse({'code': 'connection_in_progress'}, status_code=409)
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        (STATE / 'connection-paused').touch(mode=0o600)
        await self.close()
        return await self.status()

    async def read_only_flow(self, scope, receive, send):
        """Forward the existing session/read/preview consumer without granting effects."""
        if self.inner is None:
            await JSONResponse({'code': 'home_not_connected', 'simulated': False}, status_code=503)(scope, receive, send)
            return
        if scope['path'] == '/api/home-flow/turn':
            from homeplane.home_flow_contracts import FlowRequest, MAX_REQUEST_BYTES
            body = bytearray()
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    return
                chunk = event.get('body', b'')
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    await JSONResponse({'code': 'flow_request_budget'}, status_code=413)(scope, receive, send)
                    return
                body.extend(chunk)
                if not event.get('more_body', False):
                    break
            try:
                request = FlowRequest.model_validate_json(bytes(body))
            except ValueError:
                await JSONResponse({'code': 'invalid_flow_request'}, status_code=422)(scope, receive, send)
                return
            flow = self.inner.state.home_flow
            session = flow.sessions.get(request.session_id)
            operation = session.operations.get(request.operation_id) if session else None
            lane = operation.lane if operation else flow._lane(request)
            if (request.kind != 'cancel' and
                    (request.kind not in {'turn', 'status'} or lane not in {'control', 'rules', 'why'})):
                await JSONResponse({'code': 'desktop_read_only', 'device_writes': 'disabled'}, status_code=409)(scope, receive, send)
                return
            consumed = False
            original_receive = receive
            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await original_receive()
            receive = replay
        await self.inner(scope, receive, send)

    async def read_only_preparation(self, scope, receive, send, *, reviewed_analysis=False):
        """Bounded existing reads, local previews and owned receipt cancellation.

        The original domain keeps source, permission, revision and ownership
        checks. The ordinary preview routes grant no execution, inference or
        original-media access. Explicit playback creates only a protected preview;
        the media guard below requires a completed, currently owned preview lease.
        A trusted policy can separately forward the typed
        analysis approval reference; the domain owns its exact review checks.
        """
        if self.inner is None:
            await JSONResponse({'code': 'home_not_connected', 'simulated': False}, status_code=503)(scope, receive, send)
            return
        from pydantic import TypeAdapter
        from homeplane import api
        from homeplane.file_steward.contracts import StewardPageRequest
        from homeplane.file_steward.settings_contracts import StewardSettingsRequest
        schemas = {
            '/api/query': api.HomeQuery,
            '/api/actions/plan': api.LightPlanRequest | api.DevicePlanRequest,
            '/api/actions/dry-run': api.ActionRequest,
            '/api/rules/core/plans': api.CoreRuleProposal,
            '/api/rules/core/plans/trial': api.RulePlanRef,
            '/api/rules/text/plan': api.RuleTextRequest,
            '/api/rules/plans/simulate': api.RulePlanRef,
            '/api/rules/analysis/reviews': api.RuleAnalysisRequest,
            '/api/rules/analysis/cancel': api.RuleAnalysisApproval,
            '/api/archive/playback/cancel': api.ArchivePlaybackCancel,
            '/api/archive/resolve': api.ArchiveResolveRequest,
            '/api/archive/playback': api.ArchivePlaybackRequest,
            '/api/files/query': StewardPageRequest,
            '/api/files/settings/query': StewardSettingsRequest,
            '/api/diagnostics/export': api.DiagnosticsExportRequest,
        }
        if scope['path'] in {'/api/home/evidence/query', '/api/home/evidence/detail'}:
            from homeplane.event_evidence import EvidenceDetail, EvidenceQuery
            schemas.update({'/api/home/evidence/query': EvidenceQuery,
                            '/api/home/evidence/detail': EvidenceDetail})
        if scope['path'] == '/api/home/recordings/stop':
            from homeplane.home_experience import RecordingStopRequest
            schemas['/api/home/recordings/stop'] = RecordingStopRequest
        if reviewed_analysis:
            # Only a complete trusted policy opens this typed approval-ref route.
            # The original domain checks the immutable review/digest/deadline.
            if scope['path'] != '/api/rules/analysis/analyze' or self.planning_authorizer is None or self.planning_executor is None:
                await JSONResponse({'code': 'desktop_read_only'}, status_code=409)(scope, receive, send)
                return
            schemas['/api/rules/analysis/analyze'] = api.RuleAnalysisApproval
        budget = READ_ONLY_REQUEST_BYTES.get(scope['path'], 8192)
        body = bytearray()
        while True:
            event = await receive()
            if event['type'] == 'http.disconnect':
                return
            chunk = event.get('body', b'')
            if len(body) + len(chunk) > budget:
                await JSONResponse({'code': 'desktop_request_budget'}, status_code=413)(scope, receive, send)
                return
            body.extend(chunk)
            if not event.get('more_body', False):
                break
        try:
            request = TypeAdapter(schemas[scope['path']]).validate_json(bytes(body))
        except ValueError:
            await JSONResponse({'code': 'invalid_desktop_read_request'}, status_code=422)(scope, receive, send)
            return
        if scope['path'] == '/api/query' and (
                request.source != 'home' or request.kind not in {'state', 'devices', 'history'}):
            await JSONResponse({'code': 'desktop_read_only', 'device_writes': 'disabled'}, status_code=409)(scope, receive, send)
            return
        consumed = False
        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()
        await self.inner(scope, replay, send)

    def protected_preview_media(self, path, headers):
        """Grant no original access: only exact, same-origin owned preview leases."""
        if self.inner is None or not (
                headers.get(b'origin') == ORIGIN.encode()
                or headers.get(b'sec-fetch-site') == b'same-origin'):
            return False
        archive = re.fullmatch(r'/api/archive/media/([a-f0-9]{64})', path)
        recording = re.fullmatch(r'/api/home/recordings/media/([a-f0-9]{48})', path)
        try:
            if archive:
                return self.inner.state.archive_player.protected_preview(archive[1]) is True
            if recording:
                return self.inner.state.home_experience.recordings.protected_preview(recording[1]) is True
        except (AttributeError, OSError, ValueError, TypeError):
            pass
        return False

    @asgi_observed
    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    self.arl = await arl_status(self.arl_runtime)
                    self.arl_task = asyncio.create_task(self.refresh_arl())
                    if self.persistent:
                        self.home_task = asyncio.create_task(self.restore_home())
                    await send({'type': 'lifespan.startup.complete'})
                elif event['type'] == 'lifespan.shutdown':
                    if self.home_task:
                        self.home_task.cancel()
                        await asyncio.gather(self.home_task, return_exceptions=True)
                    if self.arl_task:
                        self.arl_task.cancel()
                        try:
                            await self.arl_task
                        except asyncio.CancelledError:
                            pass
                    await self.close()
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        path = scope.get('path', '')
        if scope['type'] == 'http':
            headers = dict(scope.get('headers', []))
            origin = headers.get(b'origin')
            if headers.get(b'host') != b'127.0.0.1:8740' or origin not in {None, ORIGIN.encode()}:
                await JSONResponse({'code': 'invalid_local_origin'}, status_code=403)(scope, receive, send)
                return
            if path == '/' and self.inner is None:
                await RedirectResponse('/desktop/setup' + ('?ha=failed' if b'ha=failed' in scope.get('query_string', b'') else ''))(scope, receive, send)
                return
            if path.startswith('/desktop/') or path in self.authorization_paths or path == '/api/diagnostics/runtime':
                if scope['method'] != 'GET' and origin != ORIGIN.encode():
                    await JSONResponse({'code': 'origin_required'}, status_code=403)(scope, receive, send)
                    return
                await self.metadata(scope, receive, send)
                return
            if scope['method']=='POST' and self.inner is not None and (
                path.startswith('/api/embodied/') or path in PEOPLE_WRITE_PATHS
                or path.startswith('/api/home/automations/records/') and self.inner.state.core.actions.policy_report().get('enabled')
                or path=='/api/home/storage/prepare'
                or path in {'/api/rules/core/plans/apply','/api/rules/core/plans/rollback','/api/rules/core/runtime/mode'} and self.inner.state.core.actions.policy_report().get('enabled')
                or path in {'/api/home/layout/preview','/api/home/layout/import','/api/home/layout/device-position','/api/home/layout/restore'}
                or path in {'/api/home/control','/api/home/preferences','/api/home/recordings/play','/api/home/interactions','/api/home/presentation','/api/home/automations/catalog'}
            ) and self.inner is not None:
                if origin != ORIGIN.encode():
                    await JSONResponse({'code':'origin_required'},status_code=403)(scope,receive,send)
                    return
                await self.inner(scope,receive,send)
                return
            policy=self.inner.state.core.actions.policy_report() if self.inner else {}
            if scope['method']=='POST' and self.inner is not None and (
                policy.get('enabled') and (path.startswith('/api/home-flow/') or path in {'/api/actions/execute','/api/actions/reconcile'})
                or policy.get('configured') and path in {'/api/settings/actions/pause','/api/settings/actions/resume'}
            ):
                if origin != ORIGIN.encode():
                    await JSONResponse({'code':'origin_required'},status_code=403)(scope,receive,send)
                    return
                await self.inner(scope,receive,send)
                return
            if scope['method'] == 'POST' and path in {
                '/api/home-flow/bootstrap', '/api/home-flow/context', '/api/home-flow/turn',
                '/api/home-flow/cancel', '/api/home-flow/dispose',
            }:
                if origin != ORIGIN.encode():
                    await JSONResponse({'code': 'origin_required'}, status_code=403)(scope, receive, send)
                    return
                await self.read_only_flow(scope, receive, send)
                return
            if scope['method'] == 'POST' and path == '/api/rules/analysis/analyze' and self.planning_authorizer is not None:
                if origin != ORIGIN.encode():
                    await JSONResponse({'code': 'origin_required'}, status_code=403)(scope, receive, send)
                    return
                await self.read_only_preparation(scope, receive, send, reviewed_analysis=True)
                return
            if scope['method'] == 'POST' and path in READ_ONLY_PREPARATION_PATHS:
                if origin != ORIGIN.encode():
                    await JSONResponse({'code': 'origin_required'}, status_code=403)(scope, receive, send)
                    return
                await self.read_only_preparation(scope, receive, send)
                return
            if scope['method'] not in {'GET', 'HEAD'}:
                await JSONResponse({'code': 'desktop_read_only', 'device_writes': 'disabled'}, status_code=409)(scope, receive, send)
                return
            if (path in HELD_GET_PATHS or path.startswith(HELD_GET_PREFIXES)) and not self.protected_preview_media(path, headers):
                await JSONResponse({'code': 'desktop_read_only', 'device_writes': 'disabled'}, status_code=409)(scope, receive, send)
                return
        if self.inner is not None:
            await self.inner(scope, receive, send)
        elif scope['type'] == 'http':
            if path == '/':
                await HTMLResponse('<!doctype html><html lang="zh"><meta charset="utf-8"><title>Friday</title>'
                                   '<h1>Friday</h1><p>家庭尚未连接。请在 Friday 应用中连接已有家庭。</p>'
                                   '<p>此页面没有模拟家庭，也不会执行设备控制。</p>'
                                   '<p><a href="/api/diagnostics/runtime">查看本机运行诊断</a></p></html>')(scope, receive, send)
            else:
                await JSONResponse({'code': 'home_not_connected', 'simulated': False}, status_code=503)(scope, receive, send)
        else:
            await send({'type': 'websocket.close', 'code': 1008})


async def probe():
    renewal = SameGrantRenewal()
    result = {'configuration': connection_configuration(), 'arl': await arl_status(),
              'credentialValuesExported': False, 'grantRefreshAttempted': False,
              'deviceControlAttempted': False, 'originalRepositoryWrites': 0}
    adapter = None
    try:
        _, adapter, snapshot = await real_adapter(renewal)
        result['home'] = {'status': 'connected_read_only', 'entityCount': len(snapshot.entities),
                          'mode': snapshot.mode, 'health': adapter.health(), 'httpReads': adapter._desktop_http_reads}
        assert snapshot.mode == 'ha-read-only'
    except Exception as error:
        result['home'] = {'status': 'failed', 'code': failure_code(error), 'simulated': False,
                          'errorSource': 'provider_http' if isinstance(error, RealHomeFailure) else 'domain_or_transport',
                          'httpReads': error.http_reads if isinstance(error, RealHomeFailure) else []}
    finally:
        if adapter is not None:
            await adapter.close()
        result['grantRefreshAttempted'] = renewal.refresh_attempted
        result['grantRefreshSuppressed'] = renewal.refresh_suppressed
    print(json.dumps(result))


async def serve(runtime_session=None, *, persistent=False):
    # launchd starts agents in /. The existing local database policy is rooted
    # in the owner's data/ directory, so establish that private working root
    # before constructing any owner, independently of the GUI's working dir.
    prepare_private_workspace()
    os.chdir(BASE)
    import logging
    import uvicorn
    service = DesktopService(runtime_log=RuntimeLog(BASE / 'logs', session=runtime_session))
    service.persistent = persistent
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        logger = logging.getLogger(name)
        logger.handlers = [SafeServerHandler(service.runtime_log)]
        logger.propagate = False
    server = uvicorn.Server(uvicorn.Config(service, host='127.0.0.1', port=8740, access_log=False,
                                          proxy_headers=False, loop='asyncio', http='h11', log_level='warning',
                                          log_config=None))
    loop = asyncio.get_running_loop()
    if not persistent:
        os.set_blocking(0, False)
    def parent_input():
        if os.read(0, 4096) == b'':
            loop.remove_reader(0)
            server.should_exit = True
    if not persistent:
        loop.add_reader(0, parent_input)
    try:
        await server.serve()
    finally:
        if not persistent:
            loop.remove_reader(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--runtime-session')
    parser.add_argument('--persistent', action='store_true')
    args = parser.parse_args()
    asyncio.run(probe() if args.probe else serve(args.runtime_session, persistent=args.persistent))
