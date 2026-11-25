"""Admitted worker -> original HomeQuestions/HomeQueries; no actuator or media.

This in-process owner supplies a distinct short-lived read scope after checking
the current worker capability, active service policy and registered camera source.
It never calls the public Console bootstrap or borrows its identity/history grant.
"""
import hashlib
import json
import math
import time
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request

from homeplane.home_flow_contracts import FlowAbort, FlowRequest, TrustedFlowContext
from homeplane.home_questions import is_home_question
from homeplane.preview_request import prepare_for_viewer
from homeplane.query_contracts import HomeQueryResult, HomeQueryState, ReadScope

from .camera_provenance import camera_source_context
from .intent import address
from .paths import configuration
from .voice_question_contracts import VoiceAnswer, VoiceQuestion


def question_speech(report, now, deadline):
    """Speak individual original readings; never average or use model answers."""
    if report.code == 'question_room_selection_required':
        return '请在问题里加上房间名称再问一次。', deadline
    if report.phase != 'result' or not report.domain or 'query' not in report.domain:
        return report.caption or '这次没有查到，请稍后再问。', deadline
    query = HomeQueryResult.model_validate(report.domain['query'])
    if query.status == 'refused':
        return '这次没有查到，请稍后再问。', deadline
    prefix = '示例数据。' if query.simulated else ''
    if query.kind == 'devices':
        names = [item['name'] for item in query.items[:3]]
        sentence = '、'.join(names) if names else '没有找到对应设备'
        more = '等设备，可以指定房间继续查询' if query.next_cursor else (
            f'等{len(query.items)}台设备' if len(query.items)>3 else '')
        return prefix + sentence + more + '。', deadline
    parts, expiries = [], [deadline]
    for value in query.items[:3]:
        if value['kind'] != 'state':
            continue
        item = HomeQueryState.model_validate(value)
        name = item.device_name or item.name
        label = {'temperature':'温度', 'humidity':'湿度', 'power':'电源'}.get(item.capability, item.capability)
        current = (item.quality == 'healthy' and item.freshness == 'canonical_valid'
                   and item.valid_until is not None and item.valid_until > now and item.value is not None)
        if not current:
            parts.append(f'{name}的{label}暂时没有可用读数')
            continue
        expiries.append(item.valid_until)
        value = item.value
        if item.capability == 'power':
            if type(value) is bool or type(value) is str and value in {'on','off'}:
                parts.append(f'{name}'+('开着' if value is True or value=='on' else '关着'))
            else:
                parts.append(f'{name}的开关状态暂时无法确认')
        elif type(value) in {int, float} and math.isfinite(value):
            number = f'{value:g}'
            unit = '度' if item.unit in {'°C','℃','C','celsius'} else item.unit or ''
            parts.append(f'{name}的{label}'+(f'百分之{number}' if unit == '%' else number+unit))
        else:
            parts.append(f'{name}的{label}为{value}')
    if not parts:
        parts.append('暂时没有可用读数')
    if len(query.items) > 3 or query.next_cursor:
        parts.append('先读出这三项，可以指定房间或设备继续查询')
    # Source names are bounded by the original query contract. Long labels do not
    # become a truncated answer attached to the wrong device.
    reply = prefix + '，'.join(parts) + '。'
    if len(reply) > 1200:
        reply = prefix + '查询结果较多，请在 Friday 查看。'
    return reply, min(expiries)


class VoiceQuestions:
    def __init__(self, flow, service, infrastructure, preferences, private):
        self.flow, self.service, self.infrastructure = flow, service, infrastructure
        self.preferences, self.private = preferences, private
        self.active = {}

    def _binding(self, request, key, *, require_fresh=True):
        process = getattr(self.service,'process',None)
        if (self.flow.closed or process is None or process.returncode is not None
                or not self.service.capture_enabled or not getattr(self.service,'interaction_enabled',False)
                or (self.private/'voice-input-paused').exists()
                or require_fresh and not 0 <= time.monotonic()-request.captured_mono < 40):
            raise FlowAbort('voice_question_context_changed')
        current_key = (self.private/'control-key').read_text().strip()
        if not current_key or current_key != key:
            raise FlowAbort('voice_question_worker_changed')
        policy = self.infrastructure.policy if self.infrastructure is not None else None
        if policy is not None and not (policy.enabled and policy.recognition and policy.interaction):
            raise FlowAbort('voice_question_input_paused')
        preferences = self.preferences.read()
        if request.voice_revision != preferences.revision:
            raise FlowAbort('voice_preferences_changed')
        selected = [camera for camera in configuration()['cameras'] if camera.get('id') == request.camera]
        if len(selected) != 1 or camera_source_context(selected[0]) != request.camera_source_context:
            raise FlowAbort('voice_question_camera_changed')
        room = selected[0].get('room')
        if not isinstance(room, str) or not 1 <= len(room) <= 128 or any(ord(c)<32 or ord(c)==127 for c in room):
            raise FlowAbort('voice_question_room_unavailable')
        source = hashlib.sha256(json.dumps(selected[0],sort_keys=True).encode()).hexdigest()
        adapter = self.flow.core.adapter
        home_source = (id(adapter),getattr(adapter,'boot_id',None),getattr(adapter,'instance_id',None),
                       self.flow.core.boot_id,self.flow.settings.revision)
        return (home_source, id(process), source, room, preferences,
                policy.model_dump_json() if policy is not None else None)

    async def answer(self, request, incoming, key):
        if not 0 <= time.monotonic()-request.captured_mono <= 10:
            raise HTTPException(409, detail='voice_question_expired')
        if request.camera in self.active or len(self.active) >= 4:
            raise HTTPException(409, detail='voice_question_busy')
        try:
            binding = self._binding(request,key)
            admission = address(request.text, request.attending, wake_word=binding[4].wake_word,
                                after_wake=request.after_wake)
            if not admission['admitted'] or not is_home_question(admission.get('body','')):
                raise FlowAbort('voice_question_not_admitted')
        except (FlowAbort, OSError, ValueError, KeyError, TypeError):
            raise HTTPException(409, detail='voice_question_not_admitted') from None
        stopped = False
        session = 'voice:' + request.request_id
        operation = 'question:' + request.request_id
        deadline = min(time.monotonic()+30, request.captured_mono+40)
        expires = datetime.now(UTC)+timedelta(seconds=deadline-time.monotonic())
        revision = hashlib.sha256(repr((binding,request.request_id,expires)).encode()).hexdigest()
        scope = ReadScope(subject='embodied_voice',revision=revision,expires_at=expires,
                          boundary='qualified_ambient',all_home=True,permitted=True)
        def current():
            try:
                return not stopped and self.active.get(request.camera)==session and self._binding(request,key)==binding
            except (OSError, ValueError, KeyError, TypeError):
                return False
        context = TrustedFlowContext(input_kind='qualified_ambient',session_id=session,
            source_revision=request.camera_source_context,authority_revision=revision,
            boot_id=self.flow.core.boot_id,generation=request.voice_revision,deadline_mono=deadline,
            check_current=lambda _:current(),cancelled=lambda:stopped,
            current_scope=lambda:scope if current() else None,
            room_id=binding[3],camera_id=request.camera,producer_ref='friday.worker.voice_query.v1')
        turn = FlowRequest(session_id=session,turn_id='question',operation_id=operation,
                           idempotency_key=request.request_id,lane='query',text=admission['body'])
        def invalidate():
            nonlocal stopped
            stopped = True
            self.flow.questions.invalidate_operation(session,operation)
        async def run():
            report = await self.flow.questions.answer(turn,context)
            context.check()
            reply, valid_until = question_speech(report,datetime.now(UTC),expires)
            context.check()
            # The voice turn is one-shot. It offers no Console refresh/page token.
            domain = dict(report.domain) if report.domain else None
            if domain is not None:
                domain.pop('refresh_ref',None)
            report = report.model_copy(update={'context_token':None,'choices':[], 'domain':domain})
            return VoiceAnswer(request_id=request.request_id,camera=request.camera,
                camera_source_context=request.camera_source_context,voice_revision=request.voice_revision,
                reply=reply,valid_until=valid_until,report=report)
        try:
            self.active[request.camera] = session
            return await prepare_for_viewer(incoming,run,timeout=max(0,deadline-time.monotonic()),invalidate=invalidate)
        except (FlowAbort, ValueError, TimeoutError):
            raise HTTPException(409, detail='voice_question_context_changed') from None
        finally:
            invalidate()
            self.flow.questions.release_operation(session,operation)
            if self.active.get(request.camera)==session:
                self.active.pop(request.camera,None)


def install(router, service, flow, infrastructure, preferences, private):
    owner = VoiceQuestions(flow,service,infrastructure,preferences,private)
    @router.post('/api/embodied/question', response_model=VoiceAnswer)
    async def question(request: VoiceQuestion, incoming: Request):
        import secrets
        try:
            key = (private/'control-key').read_text().strip()
        except OSError:
            raise HTTPException(403) from None
        if not 32 <= len(key) <= 512 or not secrets.compare_digest(key,incoming.headers.get('x-friday-worker','')):
            raise HTTPException(403)
        return await owner.answer(request,incoming,key)
    return owner
