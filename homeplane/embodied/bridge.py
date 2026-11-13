"""Worker -> Friday command port; device authority remains in the Console."""

import httpx
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .paths import PRIVATE, camera, configuration


async def question(text, *, camera_id, request_id, captured_mono, voice_revision, attending, after_wake=False):
    """A one-shot admitted read; configured loopback owner only, no retry."""
    from .camera_provenance import camera_source_context
    from .voice_question_contracts import VoiceAnswer, VoiceQuestion
    config = configuration()
    origin = config['main_origin'].rstrip('/')
    address = urlsplit(origin)
    port = address.port if address.port is not None else 80
    if (address.scheme != 'http' or address.hostname != '127.0.0.1' or not 1 <= port <= 65535
            or address.path or address.query or address.fragment or address.username or address.password):
        raise ValueError('question_owner_origin_invalid')
    matches = [c for c in config['cameras'] if c.get('id') == camera_id]
    if len(matches) != 1:
        raise ValueError('question_camera_unavailable')
    selected = matches[0]
    key = (PRIVATE/'control-key').read_text().strip()
    if not 32 <= len(key) <= 512:
        raise ValueError('question_worker_unavailable')
    request = VoiceQuestion(text=text,camera=camera_id,request_id=request_id,captured_mono=captured_mono,
        voice_revision=voice_revision, attending=attending, after_wake=after_wake,
        camera_source_context=camera_source_context(selected))
    async with httpx.AsyncClient(trust_env=False,timeout=32,follow_redirects=False) as client:
        response = await client.post(origin+'/api/embodied/question',
            headers={'Origin':origin,'X-Friday-Worker':key},json=request.model_dump(mode='json'))
        if response.status_code != 200 or len(response.content)>70000:
            raise ValueError('question_read_unavailable')
        answer = VoiceAnswer.model_validate(response.json())
    current = configuration()
    if (current['main_origin'].rstrip('/') != origin
            or [c for c in current['cameras'] if c.get('id')==camera_id] != [selected]
            or (PRIVATE/'control-key').read_text().strip() != key
            or answer.request_id != request_id or answer.camera != camera_id
            or answer.camera_source_context != request.camera_source_context
            or answer.voice_revision != voice_revision or answer.valid_until <= datetime.now(UTC)
            or answer.report.input_kind != 'qualified_ambient'):
        raise ValueError('question_context_changed')
    return answer


async def task(text, *, camera_id, request_id, captured_mono, voice_revision, attending, after_wake=False):
    """Bounded non-executing proposal over the current worker port; no retry."""
    from .camera_provenance import camera_source_context
    from .voice_question_contracts import VoiceQuestion
    from .voice_tasks import VoiceTaskAnswer
    config=configuration()
    origin=config['main_origin'].rstrip('/')
    address=urlsplit(origin)
    port=address.port if address.port is not None else 80
    if (address.scheme!='http' or address.hostname!='127.0.0.1' or not 1<=port<=65535
            or address.path or address.query or address.fragment or address.username or address.password):
        raise ValueError('task_owner_origin_invalid')
    matches=[c for c in config['cameras'] if c.get('id')==camera_id]
    if len(matches)!=1:raise ValueError('task_camera_unavailable')
    selected=matches[0]
    key=(PRIVATE/'control-key').read_text().strip()
    if not 32<=len(key)<=512:raise ValueError('task_worker_unavailable')
    request=VoiceQuestion(text=text,camera=camera_id,request_id=request_id,captured_mono=captured_mono,
        voice_revision=voice_revision, attending=attending, after_wake=after_wake,
        camera_source_context=camera_source_context(selected))
    async with httpx.AsyncClient(trust_env=False,timeout=32,follow_redirects=False) as client:
        response=await client.post(origin+'/api/embodied/task',headers={'Origin':origin,'X-Friday-Worker':key},
                                   json=request.model_dump(mode='json'))
        if response.status_code!=200 or len(response.content)>8192:raise ValueError('task_prepare_unavailable')
        answer=VoiceTaskAnswer.model_validate(response.json())
    current=configuration()
    if (current['main_origin'].rstrip('/')!=origin
            or [c for c in current['cameras'] if c.get('id')==camera_id]!=[selected]
            or (PRIVATE/'control-key').read_text().strip()!=key or answer.request_id!=request_id
            or answer.camera!=camera_id or answer.camera_source_context!=request.camera_source_context
            or answer.voice_revision!=voice_revision or answer.valid_until<=datetime.now(UTC)):
        raise ValueError('task_context_changed')
    return answer


async def discard_task(reference):
    if not isinstance(reference,str) or len(reference)!=64 or any(c not in '0123456789abcdef' for c in reference):
        return
    config=configuration()
    origin=config['main_origin'].rstrip('/')
    address=urlsplit(origin)
    if (address.scheme!='http' or address.hostname!='127.0.0.1' or address.path or address.query
            or address.fragment or address.username or address.password):raise ValueError('task_owner_origin_invalid')
    key=(PRIVATE/'control-key').read_text().strip()
    if not 32<=len(key)<=512:raise ValueError('task_worker_unavailable')
    async with httpx.AsyncClient(trust_env=False,timeout=3,follow_redirects=False) as client:
        await client.post(origin+'/api/embodied/task/'+reference+'/discard',
                          headers={'Origin':origin,'X-Friday-Worker':key},json={})


async def command(device, action, event_id, target=None, camera_id=None, gesture=None):
    config = configuration()
    async with httpx.AsyncClient(trust_env=False, timeout=35, follow_redirects=False) as client:
        response = await client.post(
            config["main_origin"] + "/api/embodied/command",
            headers={
                "Origin": config["main_origin"],
                "X-Friday-Worker": (PRIVATE / "control-key").read_text().strip(),
            },
            json=dict(device=device, action=action, event_id=event_id, room=camera(camera_id)["room"], target=target, gesture=gesture),
        )
        if response.status_code != 200:
            return dict(status="unavailable", acknowledged=False, reached_target=False)
        return response.json()
