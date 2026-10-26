"""Friday-only file streaming to one identified HomePod; never system audio."""

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import ipaddress
import json

from .paths import PRIVATE, configuration

_play_lock = asyncio.Lock()


class Unavailable(ValueError):
    pass


def _host(value):
    # pyatv 0.18's unicast scanner accepts IPv4 addresses only.
    address = ipaddress.IPv4Address(value)
    if not address.is_private or any((address.is_loopback, address.is_multicast,
                                     address.is_unspecified, address.is_link_local, address.is_reserved)):
        raise ValueError('homepod_local_ipv4_required')
    return str(address)


def _target():
    path = PRIVATE / 'homepod.json'
    if path.is_symlink() or path.stat().st_size > 16384 or path.stat().st_mode & 0o077:
        raise ValueError('homepod_private_configuration_required')
    value = json.loads(path.read_text())
    value['host'] = _host(value['host'])
    if not isinstance(value['identifier'], str) or not 1 <= len(value['identifier']) <= 128:
        raise ValueError('homepod_identifier_required')
    credentials = value.get('credentials', {})
    if not isinstance(credentials, dict) or any(k not in {'AirPlay', 'RAOP'} or not isinstance(v, str)
            or not 1 <= len(v) <= 4096 for k,v in credentials.items()):
        raise ValueError('homepod_credentials_invalid')
    return value


def _homepod(config):
    return any((s.properties.get('model') or s.properties.get('am') or '').startswith('AudioAccessory')
               for s in config.services)


def _public(config):
    identifier = next((s.identifier for s in config.services if s.protocol.name == 'AirPlay' and s.identifier),
                      config.identifier)
    return dict(name=config.name, model=str(config.device_info.model.name), homepod=_homepod(config),
                identifier=identifier, addresses=[str(config.address)],
                protocols=[s.protocol.name for s in config.services])


async def discover(timeout=6, *, host=None):
    """Silent protocol-aware discovery; an exact IP can recover lost multicast."""
    from pyatv import scan
    hosts = [_host(host)] if host is not None else None
    devices = await asyncio.wait_for(scan(asyncio.get_running_loop(), hosts=hosts,
                                         timeout=min(15, max(1, int(timeout)))), 20)
    return [_public(config) for config in devices]


async def select(identifier, *, host=None):
    """Persist one actually discovered HomePod as Friday's speech destination."""
    from friday.home import private_json
    devices = await discover(6, host=host)
    selected = [d for d in devices if d['homepod'] and d['identifier'] == identifier]
    if len(selected) != 1 or not selected[0]['addresses']:
        raise Unavailable('homepod_not_discovered')
    device = selected[0]
    target = dict(identifier=identifier, host=_host(device['addresses'][0]), name=device['name'])
    try:
        old = _target()
        if old['identifier'] == identifier:
            target['credentials'] = old.get('credentials', {})
    except (OSError, ValueError, KeyError, TypeError):
        pass
    config = configuration()
    private_json(PRIVATE / 'homepod.json', target)
    private_json(PRIVATE / 'config.json', {**config, 'speech_output':'homepod'})
    return dict(name=device['name'], configured=True, quiet=(PRIVATE / 'voice-paused').exists())


def status():
    reason = None
    try:
        _target()
        configured = True
    except (OSError, ValueError, KeyError, TypeError):
        configured, reason = False, 'homepod_setup_required'
    available = importlib.util.find_spec('pyatv') is not None
    return dict(transport='AirPlay / pyatv', configured=configured, dependency_available=available,
                quiet=(PRIVATE / 'voice-paused').exists(), physical_audio_confirmed=False,
                independent_connection=True, system_audio_redirected=False, reason=reason)


@asynccontextmanager
async def _receiver():
    from pyatv import connect, scan
    from pyatv.const import FeatureName, FeatureState, PairingRequirement, Protocol

    target = _target()
    loop = asyncio.get_running_loop()
    choices = await asyncio.wait_for(scan(loop, hosts=[target['host']], timeout=4), 7)
    selected = [c for c in choices if target['identifier'] in c.all_identifiers]
    if not selected:
        # DHCP may have changed the address. Keep the pinned identity, never
        # substitute another similarly named speaker, TV or Mac.
        choices = await asyncio.wait_for(scan(loop, identifier=target['identifier'], timeout=4), 7)
        selected = [c for c in choices if target['identifier'] in c.all_identifiers]
    if len(selected) != 1:
        raise Unavailable('homepod_not_found' if not selected else 'homepod_identity_ambiguous')
    config = selected[0]
    _host(str(config.address))
    if not _homepod(config):
        raise Unavailable('homepod_identity_mismatch')
    for name, credential in target.get('credentials', {}).items():
        config.set_credentials(getattr(Protocol, name), credential)
    if any(s.protocol in {Protocol.AirPlay, Protocol.RAOP} and s.pairing == PairingRequirement.Mandatory
           and not s.credentials for s in config.services):
        raise Unavailable('homepod_pairing_required')
    device = await asyncio.wait_for(connect(config, loop), 12)
    try:
        if device.features.get_feature(FeatureName.StreamFile).state != FeatureState.Available:
            raise Unavailable('homepod_stream_unavailable')
        yield device
    finally:
        tasks = device.close()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
            except asyncio.TimeoutError:
                pass


async def _availability(device):
    from pyatv.const import DeviceState, Protocol
    # RAOP-only metadata describes our stream, so its default Idle cannot
    # establish whether the HomePod is currently playing someone else's music.
    # The pinned pyatv facade exposes the selected metadata protocol here.
    if getattr(device.metadata, 'main_protocol', None) not in {Protocol.MRP, Protocol.DMAP}:
        raise Unavailable('homepod_playback_state_unavailable')
    playing = await asyncio.wait_for(device.metadata.playing(), 5)
    if playing.device_state in {DeviceState.Idle, DeviceState.Paused, DeviceState.Stopped}:
        return 'ready'
    if playing.device_state in {DeviceState.Playing, DeviceState.Loading, DeviceState.Seeking}:
        return 'busy'
    raise Unavailable('homepod_playback_state_unavailable')


async def probe():
    """Verify identity, connection, stream support and receiver state without audio."""
    current = status()
    if not current['configured'] or not current['dependency_available']:
        return dict(**current, status='unavailable')
    try:
        async with _receiver() as device:
            return dict(**current, status=await _availability(device), connected=True)
    except Exception as error:
        return dict(**{**current, 'reason':str(error) if isinstance(error, Unavailable) else 'homepod_connection_unavailable'},
                    status='unavailable', error_class=type(error).__name__)


async def _stream(device, audio):
    task = asyncio.create_task(device.stream.stream_file(str(audio)))
    deadline = asyncio.get_running_loop().time() + 90
    try:
        while not task.done():
            if (PRIVATE / 'voice-paused').exists():
                return False
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError()
            await asyncio.wait({task}, timeout=.1)
        await task
        return True
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def play(ha, text):
    """Stream only Friday's WAV; preserve other Mac audio and receiver volume."""
    if (PRIVATE / 'voice-paused').exists():
        return dict(status='paused')
    if not isinstance(text, str) or not 1 <= len(text) <= 1200:
        raise ValueError('invalid_reply')
    current = status()
    if not current['configured'] or not current['dependency_available']:
        return dict(status='unavailable', reason='homepod_setup_required')
    if _play_lock.locked():
        return dict(status='busy')
    from .synthesis import synthesize
    audio = None
    async with _play_lock:
        try:
            async with _receiver() as device:
                if await _availability(device) == 'busy':
                    return dict(status='busy')
                audio, _ = await synthesize(text, file_format='wav')
                if (PRIVATE / 'voice-paused').exists():
                    return dict(status='paused')
                # Synthesis takes time; inspect the receiver again before send.
                if await _availability(device) == 'busy':
                    return dict(status='busy')
                completed = await _stream(device, audio)
                return dict(status='accepted' if completed else 'paused', backend_completed=completed,
                            physical_audio_confirmed=False, transport='AirPlay / pyatv',
                            independent_connection=True, system_audio_redirected=False)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return dict(status='unavailable', reason=str(error) if isinstance(error, Unavailable) else 'homepod_connection_unavailable',
                        error_class=type(error).__name__, physical_audio_confirmed=False)
        finally:
            if audio:
                audio.unlink(missing_ok=True)
