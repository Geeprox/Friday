"""Product speech-output settings; importing or inspecting status is silent."""

import ipaddress
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import airplay
from .paths import PRIVATE, configuration


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    output: Literal['panel', 'media_player', 'jbl', 'homepod']
    identifier: str | None = Field(default=None, min_length=1, max_length=128)
    host: ipaddress.IPv4Address | None = None


def report():
    try:
        config = configuration()
    except (OSError, ValueError, KeyError, TypeError):
        config = {}
    current = config.get('speech_output', 'panel')
    homepod = airplay.status()
    if homepod['configured']:
        target = airplay._target()
        name = target.get('name')
        homepod['name'] = name[:128] if isinstance(name, str) else 'HomePod'
    return dict(output=current if current in {'panel','media_player','jbl','homepod'} else 'panel',
                configured=bool(homepod['configured'] if current == 'homepod' else
                                config.get('speaker_entity') if current in {'media_player', 'jbl'} else
                                config.get('speaker_notify')),
                quiet=homepod['quiet'], homepod=homepod)


def install(router):
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
            if request.output == 'homepod':
                if not request.identifier:
                    raise HTTPException(422, detail='homepod_identifier_required')
                await airplay.select(request.identifier, host=str(request.host) if request.host else None)
            else:
                if request.identifier or request.host:
                    raise HTTPException(422, detail='unexpected_homepod_target')
                from friday.home import private_json
                private_json(PRIVATE / 'config.json', {**configuration(), 'speech_output':request.output})
            return report()
        except (OSError, ValueError, KeyError, TypeError, ImportError, TimeoutError):
            raise HTTPException(409, detail='speech_output_not_saved') from None

    @router.post('/api/embodied/speech-output/probe')
    async def probe():
        return await airplay.probe()

    @router.post('/api/embodied/speech-output/test')
    async def test():
        if configuration().get('speech_output') != 'homepod':
            raise HTTPException(409, detail='homepod_not_selected')
        return await airplay.play(None, '我是 Friday，这是 HomePod 的语音播报测试。')
