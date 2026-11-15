"""播报输出的设置、状态和显式试听入口。"""

import asyncio
import ipaddress
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import airplay, speech_mac
from .paths import PRIVATE, configuration
from .speech_selection import OUTPUTS, OutputKind, output_configuration, save_output


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    output: OutputKind
    identifier: str | None = Field(default=None, min_length=1, max_length=128)
    host: ipaddress.IPv4Address | None = None


class TestOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    output: Literal['homepod', 'mac'] = 'homepod'


def report():
    try:
        config = output_configuration(PRIVATE, configuration)
    except (OSError, ValueError, KeyError, TypeError):
        config = {}
    current = config.get('speech_output', 'panel')
    homepod = airplay.status()
    mac_available = speech_mac.available()
    if homepod['configured']:
        target = airplay._target()
        name = target.get('name')
        homepod['name'] = name[:128] if isinstance(name, str) else 'HomePod'
    configured = {'mac': mac_available, 'homepod': homepod['configured'],
                  'media_player': bool(config.get('speaker_entity')), 'jbl': bool(config.get('speaker_entity')),
                  'panel': bool(config.get('speaker_notify'))}
    return dict(output=current if current in OUTPUTS else 'panel',
                configured=configured[current] if current in OUTPUTS else False, mac_available=mac_available,
                quiet=homepod['quiet'], homepod=homepod)


def install(router):
    selection_lock = asyncio.Lock()

    @router.get('/api/embodied/speech-output')
    async def status():
        return report()

    @router.get('/api/embodied/speech-output/devices')
    async def devices(host: ipaddress.IPv4Address | None = None):
        try:
            found = await airplay.discover(8, host=str(host) if host else None)
            return dict(devices=[d for d in found if d['homepod']], status='complete')
        except (OSError, ValueError, ImportError, TimeoutError):
            return dict(devices=[], status='unavailable', reason='homepod_discovery_unavailable')

    @router.post('/api/embodied/speech-output')
    async def select(request: Selection):
        try:
            async with selection_lock:
                if request.output == 'homepod':
                    if not request.identifier:
                        raise HTTPException(422, detail='homepod_identifier_required')
                    await airplay.select(request.identifier, host=str(request.host) if request.host else None)
                else:
                    if request.identifier or request.host:
                        raise HTTPException(422, detail='unexpected_homepod_target')
                    if request.output == 'mac' and not speech_mac.available():
                        raise HTTPException(409, detail='mac_speech_unavailable')
                    save_output(PRIVATE, request.output)
                return report()
        except (OSError, ValueError, KeyError, TypeError, ImportError, TimeoutError):
            raise HTTPException(409, detail='speech_output_not_saved') from None

    @router.post('/api/embodied/speech-output/probe')
    async def probe():
        return await airplay.probe()

    @router.post('/api/embodied/speech-output/test')
    async def test(request: TestOutput | None = None):
        output = request.output if request is not None else 'homepod'

        def current():
            return output_configuration(PRIVATE, configuration).get('speech_output') == output

        try:
            if not current():
                raise ValueError('speech_output_changed')
        except (OSError, ValueError, TypeError):
            detail = 'homepod_not_selected' if output == 'homepod' else 'speech_output_changed'
            raise HTTPException(409, detail=detail) from None
        if (PRIVATE / 'voice-paused').exists():
            return dict(status='paused')
        from .speech import Speaker
        return await Speaker().say('这是一段播报试听。', current=current)
