"""Bounded Why conversation over the existing scoped Query and Archive services.

This owns transient display references and cancellation, never history SQL,
device/rule plans, NAS sessions, scanners, players or a background scheduler.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .archive_contracts import (
    ArchivePlaybackRequest,
    ArchiveResolution,
    ArchiveResolveRequest,
    ArchiveTimeRange,
)
from .home_flow_contracts import (
    MAX_ACTIVE_OPERATIONS,
    MAX_HISTORY_BYTES,
    MAX_SESSIONS,
    MAX_TURNS,
    SESSION_TTL_SECONDS,
    FlowAbort,
    FlowChoice,
    FlowRequest,
    FlowResponse,
    TrustedFlowContext,
)
from .home_flow_ports import ArchivePorts, QueryPorts
from .home_query_intent import parse_home_query_intent
from .query_contracts import HomeQuery, HomeQueryResult


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _grant(scope):
    return _digest([scope.subject, scope.boundary, scope.revision, scope.expires_at,
                    sorted(scope.rooms), sorted(scope.entities), scope.all_home, scope.history, scope.permitted])


@dataclass
class _Session:
    binding: tuple
    grant: str
    deadline: float
    created_at: datetime
    expires_at: datetime
    operation_id: str
    token: str = field(default_factory=lambda: secrets.token_hex(32))
    text: str = ""
    room: str | None = None
    entity: str | None = None
    window: ArchiveTimeRange | None = None
    query: HomeQuery | None = None
    next_cursor: str | None = None
    candidates: dict = field(default_factory=dict)
    evidence_refs: tuple = ()
    archive_request: ArchiveResolveRequest | None = None
    segments: dict = field(default_factory=dict)
    playbacks: dict = field(default_factory=dict)
    seen: set = field(default_factory=set)
    operations: set = field(default_factory=set)
    turns: int = 0
    invalidated: bool = False
    lifecycle_invalidated: bool = False
    task: object = None


class HomeExplanation:
    def __init__(self, queries: QueryPorts, archive: ArchivePorts | None = None, *, room_aliases=None):
        if not isinstance(queries, QueryPorts) or archive is not None and not isinstance(archive, ArchivePorts):
            raise ValueError("original_query_archive_ports_required")
        self.queries, self.archive = queries, archive
        self.room_aliases = dict(room_aliases or {})
        if (len(self.room_aliases) > 64 or any(type(k) is not str or type(v) is not str
                or not 0 < len(k) <= 128 or not 0 < len(v) <= 128
                for k, v in self.room_aliases.items())):
            raise ValueError("bounded_private_room_aliases_required")
        self.sessions = {}
        self.retired = {}
        self.closed = False

    def _response(self, request, context, phase, code, *, state=None, caption="", domain=None,
                  choices=(), nas_lease="closed", expires_at=None):
        return FlowResponse(
            session_id=request.session_id, turn_id=request.turn_id, operation_id=request.operation_id,
            phase=phase, code=code, input_kind=context.input_kind,
            context_token=state.token if state else None,
            expires_at=expires_at or (state.expires_at if state else None), caption=caption,
            domain=domain, choices=list(choices), nas_lease=nas_lease,
            limitations=["bounded_text_why_only", "complete_episode_unavailable",
                         "live_nas_and_media_clock_not_qualified", "full_action_trace_enrichment_held"],
        )

    def _revoke(self, state):
        if not self.archive:
            return
        for plan_id, (context, _, _) in list(state.playbacks.items()):
            # Original revoke is unconditional cleanup, even after grant loss.
            self.archive.player.revoke(plan_id)
            if self.archive.reader_guard:
                try:
                    self.archive.reader_guard.revoke(plan_id, context)
                except Exception:
                    pass  # Never disclose a URL after failed route registration/cleanup.
        state.playbacks.clear()

    def _states(self, session_id):
        current = self.sessions.get(session_id)
        return ([current] if current else []) + self.retired.get(session_id, [])

    def _collect(self):
        for key, states in list(self.retired.items()):
            pending = [state for state in states if state.task and not state.task.done()]
            if pending:
                self.retired[key] = pending
            else:
                del self.retired[key]

    def _stop_operation(self, state):
        state.invalidated = True
        state.segments.clear()
        state.evidence_refs = ()
        state.candidates.clear()
        state.next_cursor = None
        self._revoke(state)
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if state.task and not state.task.done() and state.task is not current_task:
            state.task.cancel()

    def invalidate(self, session_id):
        """Whole-session lifecycle fence; the app calls before source rebinding."""
        for state in self._states(session_id):
            state.lifecycle_invalidated = True
            self._stop_operation(state)

    def _check(self, state, context, deadline=None, *, allow_cancelled_operation=False):
        # A retired operation can never invalidate or clean a newer operation.
        if state.invalidated and not allow_cancelled_operation:
            raise FlowAbort("flow_cancelled")
        try:
            context.check()
            if self.closed or state.lifecycle_invalidated:
                raise FlowAbort("flow_cancelled")
            if (context.focus_key, context.household_timezone) != state.binding:
                raise FlowAbort("flow_context_changed")
            if (context.monotonic() >= state.deadline or context.wall_now() < state.created_at
                    or context.wall_now() >= state.expires_at):
                raise FlowAbort("flow_expired")
            if deadline is not None and context.monotonic() >= deadline:
                raise FlowAbort("flow_operation_deadline")
            scope = context.read_scope()
            if _grant(scope) != state.grant:
                raise FlowAbort("flow_read_scope_changed")
        except FlowAbort:
            if self.sessions.get(context.session_id) is state:
                self.invalidate(context.session_id)
            else:
                self._stop_operation(state)
            raise
        for plan_id, (bound, expires, wall_expires) in list(state.playbacks.items()):
            if context.monotonic() >= expires or context.wall_now() >= wall_expires:
                self.archive.player.revoke(plan_id)
                if self.archive.reader_guard:
                    try:
                        self.archive.reader_guard.revoke(plan_id, bound)
                    except Exception:
                        pass
                del state.playbacks[plan_id]
        return scope

    def _start(self, request, context, *, existing=False):
        request = FlowRequest.model_validate(request.model_dump())
        if self.closed or request.session_id != context.session_id:
            raise FlowAbort("flow_session_unavailable")
        if request.lane not in {"auto", "why"}:
            raise FlowAbort("unsupported_why_lane")
        try:
            context.check()
            scope = context.read_scope()
        except FlowAbort:
            self.invalidate(context.session_id)
            raise
        previous = self.sessions.get(request.session_id)
        if previous and context.monotonic() >= previous.deadline:
            self.invalidate(request.session_id)
            raise FlowAbort("flow_expired")
        for key, old in list(self.sessions.items()):
            if context.monotonic() >= old.deadline:
                self.invalidate(key)
                if not old.task or old.task.done():
                    del self.sessions[key]
        self._collect()
        state = self.sessions.get(request.session_id)
        identifiers = {_digest([kind, value]) for kind, value in (
            ("turn", request.turn_id), ("key", request.idempotency_key))}
        if state:
            self._check(state, context, allow_cancelled_operation=True)
            if identifiers & state.seen:
                raise FlowAbort("flow_duplicate_turn")
            if state.turns >= MAX_TURNS:
                raise FlowAbort("flow_turn_budget")
            if request.operation_id != state.operation_id:
                if (existing or request.kind != "turn" or not request.text or request.expected_context
                        or request.candidate_id or request.evidence_ref):
                    raise FlowAbort("flow_operation_reference_mismatch")
                if _digest(request.operation_id) in state.operations:
                    raise FlowAbort("flow_operation_finished")
                active = sum(bool(old.task and not old.task.done()) for old in self._states(request.session_id))
                if active >= MAX_ACTIVE_OPERATIONS:
                    raise FlowAbort("flow_busy")
                self._stop_operation(state)
                if state.task and not state.task.done():
                    self.retired.setdefault(request.session_id, []).append(state)
                state = _Session(state.binding, state.grant, state.deadline, state.created_at, state.expires_at,
                                 request.operation_id, seen=state.seen, operations=state.operations,
                                 turns=state.turns)
                self.sessions[request.session_id] = state
            elif request.expected_context != state.token:
                raise FlowAbort("flow_reference_context_changed" if request.expected_context
                                else "exact_why_reference_required")
        if state is None:
            if existing or request.expected_context is not None:
                raise FlowAbort("flow_reference_expired")
            if len(set(self.sessions) | set(self.retired)) >= MAX_SESSIONS:
                raise FlowAbort("flow_session_capacity")
            now = context.wall_now()
            deadline = min(context.deadline_mono, context.monotonic() + SESSION_TTL_SECONDS)
            state = _Session((context.focus_key, context.household_timezone), _grant(scope), deadline,
                             now, now + timedelta(seconds=deadline - context.monotonic()), request.operation_id)
            self.sessions[request.session_id] = state
        self._check(state, context)
        if request.expected_context is not None and request.expected_context != state.token:
            raise FlowAbort("flow_reference_context_changed")
        if existing and request.expected_context != state.token:
            raise FlowAbort("exact_why_reference_required")
        if state.task and not state.task.done():
            raise FlowAbort("flow_busy")
        if identifiers & state.seen:
            raise FlowAbort("flow_duplicate_turn")
        if state.turns >= MAX_TURNS:
            raise FlowAbort("flow_turn_budget")
        state.seen.update(identifiers)
        state.operations.add(_digest(request.operation_id))
        state.turns += 1
        state.task = asyncio.current_task()
        return request, state, scope, min(state.deadline, context.monotonic() + 3)

    def _commit(self, state, context, **updates):
        self._check(state, context)
        projection = {
            "text": updates.get("text", state.text), "room": updates.get("room", state.room),
            "entity": updates.get("entity", state.entity), "seen": sorted(state.seen),
            "operations": sorted(state.operations),
            "candidates": updates.get("candidates", state.candidates),
            "evidence_refs": updates.get("evidence_refs", state.evidence_refs),
            "segments": {k: v.model_dump(mode="json") for k, v in updates.get("segments", state.segments).items()},
            "query": updates.get("query", state.query),
        }
        if len(json.dumps(projection, ensure_ascii=False, default=str).encode()) > MAX_HISTORY_BYTES:
            raise FlowAbort("flow_history_budget")
        for key, value in updates.items():
            setattr(state, key, value)
        # A displayed reference belongs to this exact question/selection/page.
        state.token = secrets.token_hex(32)

    def _finish(self, response, state, context, **updates):
        self._commit(state, context, **updates)
        return response.model_copy(update={"context_token": state.token})

    def _refused_query(self, request, context, state, report, code):
        response = self._response(request, context, "refused", code, state=state, domain={
            "query": report.model_dump(mode="json"),
            "explanation": {"level": "external_or_unknown", "statements": [],
                            "complete_household_history": False},
            "archive": None,
            "archive_status": {"status": "unavailable", "reason": "home_query_refused", "choices": []},
            "trace_status": {"status": "held", "reason": "scoped_action_trace_unavailable"},
        })
        self._revoke(state)
        return self._finish(response, state, context, text="", candidates={}, query=None, segments={},
                            archive_request=None, next_cursor=None, evidence_refs=(), window=None,
                            room=None, entity=None)

    async def _read(self, query, state, context, deadline):
        scope = self._check(state, context, deadline)
        result = await self.queries.queries.read(
            query, scope, deadline=deadline,
            current_scope=lambda: self._check(state, context, deadline),
            cancelled=lambda: state.invalidated or context.cancelled(),
        )
        self._check(state, context, deadline)
        if not isinstance(result, HomeQueryResult):
            raise FlowAbort("query_contract_unavailable")
        wire = result.permission_scope
        if (wire["subject"] != scope.subject or wire["revision"] != scope.revision
                or wire["boundary"] != scope.boundary or wire["requested_rooms"] != query.rooms
                or wire["requested_entities"] != query.entities):
            raise FlowAbort("query_scope_mismatch")
        if query.entities and any(set(item.get("entity_refs", ())) - set(query.entities) for item in result.items):
            raise FlowAbort("query_evidence_outside_selection")
        return result

    def _room_target(self, target, requested, private):
        mentioned = None
        for label in sorted(self.room_aliases, key=len, reverse=True):
            if target.startswith(label):
                mentioned = self.room_aliases[label]
                target = target[len(label):].removeprefix("的").strip()
                break
        if mentioned and requested and mentioned != requested:
            raise FlowAbort("question_room_selection_mismatch")
        return requested or mentioned or private, target

    @staticmethod
    def _explanation(query):
        rows = query.items
        statements = []
        for row in rows:
            if row["event_kind"] == "observation-gap":
                level, caption = "history_gap", "存在观测缺口，这段历史不能完整还原。"
            elif row["causation_ref"] and row["causation_status"] == "recorded_by_source_not_independently_verified":
                level, caption = "source_recorded", "来源记录了触发关联，尚未独立核验；引用不能证明完整触发规则或物理效果。"
            elif row["summary"].get("reason") in {"external_rule_opaque", "external_rules_invisible", "cause_unknown"}:
                level, caption = "external_or_unknown", "原记录的原因未知或外部规则不可见，无法确定触发原因。"
            elif len(rows) > 1:
                level, caption = "temporal_only", "记录位于同一时间窗，只能说明时间相关，不能断言因果。"
            else:
                level, caption = "external_or_unknown", "有事件记录，但没有可核验的因果链；外部规则可能不可见。"
            statements.append({"level": level, "caption": caption, "evidence_refs": [row["id"]]})
        levels = {item["level"] for item in statements}
        level = next((kind for kind in ("history_gap", "source_recorded", "temporal_only", "external_or_unknown")
                      if kind in levels), "no_matching_records")
        return {
            "level": level, "statements": statements, "complete_household_history": False,
        }

    def _metadata_allowed(self, context, request):
        context.check()
        allowed = self.archive.verify_metadata(context, request) is True
        context.check()
        return allowed

    async def _archive(self, state, context, deadline, *, room, window, selected_camera):
        def unavailable(reason):
            return None, {"status": "unavailable", "reason": reason, "choices": []}, {}, None, []
        ports = self.archive
        if ports is None or ports.verify_metadata is None:
            return unavailable("archive_metadata_permission_unavailable")
        if (window.end - window.start).total_seconds() > 1800:
            return unavailable("archive_narrower_window_required")
        if room is None:
            return unavailable("archive_room_selection_required")
        try:
            self._check(state, context)
            mapping = ports.cameras_for_room(context, room) if ports.cameras_for_room else ()
            if type(mapping) is not tuple or len(mapping) > 16 or any(type(v) is not str for v in mapping):
                return unavailable("archive_camera_mapping_unavailable")
            self._check(state, context)
            if selected_camera and mapping and selected_camera not in mapping:
                return unavailable("archive_camera_outside_room_mapping")
            camera = selected_camera or (mapping[0] if len(mapping) == 1 else None)
            current = ports.index.context()
            self._check(state, context)
            available = {c["id"]: c for c in current["cameras"] if c.get("source_binding_current") is not False}
            if camera is None:
                choices, candidates = [], {}
                for camera_id in sorted(set(mapping) if mapping else available)[:16]:
                    if camera_id not in available:
                        continue
                    request = ArchiveResolveRequest(expected_context=current["fingerprint"], camera_id=camera_id,
                                                    time_range=window)
                    if self._metadata_allowed(context, request):
                        choice_id = camera_id
                        choices.append(FlowChoice(id=choice_id, label=available[camera_id]["name"][:256]))
                        candidates[choice_id] = ("camera", camera_id)
                return (None, {"status": "unavailable", "reason": "archive_camera_selection_required",
                               "choices": [choice.model_dump(mode="json") for choice in choices]},
                        candidates, None, choices)
            if camera not in available:
                return unavailable("archive_camera_mapping_unavailable")
            request = ArchiveResolveRequest(expected_context=current["fingerprint"], camera_id=camera, time_range=window)
            if not self._metadata_allowed(context, request):
                return unavailable("archive_metadata_permission_required")
            archive_deadline = min(state.deadline, context.deadline_mono, context.monotonic() + 3)
            async with asyncio.timeout(max(0, archive_deadline - context.monotonic())):
                result = await ports.index.resolve(request)
            self._check(state, context)
            if context.monotonic() >= archive_deadline:
                return unavailable("archive_metadata_deadline")
            if not self._metadata_allowed(context, request):
                return unavailable("archive_metadata_permission_revoked")
            if (not isinstance(result, ArchiveResolution) or result.context_fingerprint != request.expected_context
                    or result.camera_id != camera or result.time_range != window
                    or ports.index.context()["fingerprint"] != request.expected_context):
                return unavailable("archive_context_changed")
            return result, {"status": result.status, "reason": result.reason, "choices": []}, {}, request, []
        except FlowAbort:
            raise
        except TimeoutError:
            self._check(state, context)
            return unavailable("archive_metadata_deadline")
        except Exception:
            self._check(state, context)
            return unavailable("archive_metadata_unavailable")

    async def answer(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse:
        state = None
        try:
            if (request.kind != "turn" or request.plan_id or request.approval_digest
                    or request.ttl_seconds is not None):
                raise FlowAbort("unsupported_why_operation")
            request, state, scope, deadline = self._start(request, context)
            if request.candidate_id and request.expected_context != state.token:
                raise FlowAbort("exact_why_reference_required")
            choice = state.candidates.get(request.candidate_id) if request.candidate_id else None
            if request.candidate_id and choice is None:
                raise FlowAbort("why_candidate_unavailable")
            if request.text and request.candidate_id:
                raise FlowAbort("why_selection_with_new_question")
            text = request.text or state.text
            span = state.window if not request.text else None
            parsed = parse_home_query_intent(
                text, household_timezone=context.household_timezone, now=context.wall_now(),
                start=request.start or (span.start if span else None),
                end=request.end or (span.end if span else None),
            )
            if parsed.phase != "read":
                response = self._response(request, context, parsed.phase, parsed.code, state=state,
                                          caption="请选择明确的家庭时区与起止时间。" if parsed.phase == "clarify"
                                          else "这句话超出有限灯亮原因查询语法，请拆分问题。")
                self._revoke(state)
                return self._finish(response, state, context, text=text if parsed.phase == "clarify" else "",
                                    candidates={}, query=None, segments={}, archive_request=None,
                                    next_cursor=None, evidence_refs=(), window=None, room=None, entity=None)
            room, target = self._room_target(parsed.target_text, request.room_id,
                                             state.room if not request.text else context.room_id)
            entity = request.device_id or (state.entity if not request.text else None)
            camera = request.camera_id
            if choice:
                if choice[0] == "room":
                    room = choice[1]
                elif choice[0] == "device":
                    entity = choice[1]
                elif choice[0] == "camera":
                    camera = choice[1]
            if not room and not entity:
                candidates, choices = {}, []
                for room_id in sorted(scope.rooms)[:16]:
                    key = "room:" + secrets.token_hex(16)
                    label = next((name for name, value in self.room_aliases.items() if value == room_id), room_id)
                    candidates[key] = ("room", room_id)
                    choices.append(FlowChoice(id=key, label=label))
                response = self._response(request, context, "clarify", "why_room_selection_required", state=state,
                                          caption="请选择这次查询的房间。", choices=choices)
                self._revoke(state)
                return self._finish(response, state, context, text=text, window=parsed.time_range,
                                    candidates=candidates, query=None, segments={}, archive_request=None,
                                    next_cursor=None, evidence_refs=(), room=None, entity=None)
            is_page = choice and choice[0] == "page"
            if is_page:
                if (state.query is None or choice[1] != state.next_cursor or request.text or request.start
                        or request.evidence_ref or room != state.room or entity != state.entity):
                    raise FlowAbort("why_cursor_unavailable")
                query = state.query.model_copy(update={"cursor": choice[1]})
                room, entity = state.room, state.entity
            else:
                devices = await self._read(HomeQuery(kind="devices", rooms=[room] if room else [],
                                                    entities=[entity] if entity and not room else [], limit=50),
                                           state, context, deadline)
                if devices.status == "refused":
                    return self._refused_query(request, context, state, devices, devices.code)
                matches = []
                for item in devices.items:
                    lamps = [key for key in item["function_ids"] if key.startswith("light.")]
                    if item["category"] != "device" or not lamps:
                        continue
                    selected = [key for key in lamps if entity in {key, item["id"]}] if entity else lamps
                    if target in {"灯", item["name"], item["id"], *lamps}:
                        matches.extend((key, item["name"], item["room"]) for key in selected)
                if len(matches) != 1 or devices.next_cursor and not entity:
                    candidates, choices = {}, []
                    for key, label, _ in matches[:16]:
                        candidate = "device:" + secrets.token_hex(16)
                        candidates[candidate] = ("device", key)
                        choices.append(FlowChoice(id=candidate, label=label[:256]))
                    response = self._response(request, context, "clarify", "why_device_selection_required", state=state,
                                              caption="请选择要查询的灯；设备名或候选仍有歧义。", choices=choices)
                    self._revoke(state)
                    return self._finish(response, state, context, text=text, room=room,
                                        window=parsed.time_range, candidates=candidates, query=None, segments={},
                                        archive_request=None, next_cursor=None, evidence_refs=(), entity=None)
                entity, _, actual_room = matches[0]
                room = room or actual_room
                query = HomeQuery(kind="evidence" if request.evidence_ref else "history",
                                  rooms=[room] if room and (scope.all_home or room in scope.rooms) else [],
                                  entities=[entity], start=parsed.time_range.start, end=parsed.time_range.end,
                                  evidence_refs=[request.evidence_ref] if request.evidence_ref else [], limit=20)
            report = await self._read(query, state, context, deadline)
            if report.status == "refused" or request.evidence_ref and not report.items:
                return self._refused_query(request, context, state, report, report.code
                                           if report.status == "refused" else "why_evidence_ref_unavailable")
            archive, archive_status, candidates, archive_request, choices = await self._archive(
                state, context, deadline, room=room, window=parsed.time_range, selected_camera=camera,
            )
            if report.next_cursor and len(choices) < 16:
                key = "page:" + secrets.token_hex(16)
                candidates[key] = ("page", report.next_cursor)
                choices.append(FlowChoice(id=key, label="读取下一页依据"))
            domain = {"query": report.model_dump(mode="json"), "explanation": self._explanation(report),
                      "archive": archive.model_dump(mode="json") if archive else None,
                      "archive_status": archive_status,
                      "trace_status": {"status": "held", "reason": "scoped_action_trace_unavailable"}}
            response = self._response(request, context, "result", "why_bounded_evidence", state=state,
                                      caption="以下依据来自所选设备和时间窗；本地日志有界，完整原因仍可能未知。"
                                      if report.items else "本次有界日志未查到匹配记录，不能据此判断没有发生。",
                                      domain=domain, choices=choices,
                                      nas_lease="metadata_only" if archive else "closed")
            self._check(state, context)
            self._revoke(state)
            return self._finish(response, state, context, text=text, room=room, entity=entity,
                                window=parsed.time_range, query=query, next_cursor=report.next_cursor,
                                candidates=candidates, evidence_refs=tuple(item["id"] for item in report.items),
                                archive_request=archive_request,
                                segments={s.media_ref: s for s in archive.segments if s.status == "ready"}
                                if archive else {})
        except asyncio.CancelledError:
            return self._response(request, context, "cancelled", "flow_cancelled", state=state)
        except (FlowAbort, ValueError) as exc:
            code = str(exc) if isinstance(exc, FlowAbort) else "flow_response_or_request_budget"
            if state and code not in {"flow_busy", "flow_duplicate_turn", "exact_why_reference_required"}:
                self._revoke(state)
            return self._response(request, context, "cancelled" if code == "flow_cancelled" else "refused",
                                  code, state=state)
        finally:
            if state and state.task is asyncio.current_task():
                state.task = None
            self._collect()

    async def prepare_playback(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse:
        state, plan, delivered = None, None, False
        try:
            if (request.kind != "playback" or request.text or request.plan_id or request.approval_digest
                    or request.ttl_seconds is not None or request.candidate_id):
                raise FlowAbort("explicit_archive_click_required")
            request, state, _, deadline = self._start(request, context, existing=True)
            segment = state.segments.get(request.evidence_ref)
            original = state.archive_request
            if segment is None or original is None:
                raise FlowAbort("archive_reference_unavailable")
            if (request.camera_id and request.camera_id != original.camera_id
                    or request.room_id and request.room_id != state.room
                    or request.device_id and request.device_id != state.entity
                    or request.start or request.end):
                raise FlowAbort("archive_reference_selection_changed")
            ports = self.archive
            if (ports is None or ports.verify_metadata is None or ports.verify_content is None
                    or ports.content_deadline is None or ports.reader_guard is None):
                raise FlowAbort("archive_content_reader_guard_unavailable")
            window = ArchiveTimeRange(start=max(original.time_range.start, segment.time_range.start),
                                      end=min(original.time_range.end, segment.time_range.end))
            play = ArchivePlaybackRequest(expected_context=original.expected_context, camera_id=original.camera_id,
                                          time_range=window, media_ref=segment.media_ref)

            def permitted():
                self._check(state, context, deadline)
                if (not self._metadata_allowed(context, play) or ports.verify_content(context, play) is not True):
                    raise FlowAbort("archive_content_permission_required")
                expires = ports.content_deadline(context, play)
                self._check(state, context, deadline)
                if type(expires) not in {float, int} or not math.isfinite(expires) or expires <= context.monotonic():
                    raise FlowAbort("archive_content_lease_unavailable")
                return expires

            grant_deadline = permitted()
            async with asyncio.timeout(max(0, deadline - context.monotonic())):
                plan = await ports.player.prepare(play)
            grant_deadline = min(grant_deadline, permitted())
            effective = min(state.deadline, context.deadline_mono, grant_deadline,
                            context.monotonic() + (plan.expires_at - context.wall_now()).total_seconds())
            if effective <= context.monotonic():
                raise FlowAbort("archive_playback_expired")
            if ports.reader_guard.bind(plan, play, context, deadline_mono=effective) is not True:
                raise FlowAbort("archive_content_reader_guard_unavailable")
            permitted()
            response = self._response(request, context, "result", "archive_playback_prepared", state=state,
                                      caption="原播放器已准备有时限的片段引用；解码、时钟与真实 NAS 仍未核验。",
                                      domain={"playback": plan.model_dump(mode="json"),
                                              "archive_status": {"status": "ready", "reason": "explicit_click_prepared"}},
                                      expires_at=min(state.expires_at, plan.expires_at,
                                                     context.wall_now() + timedelta(seconds=effective - context.monotonic())),
                                      nas_lease="metadata_only" if plan.simulated else "content_qualified")
            state.playbacks[plan.plan_id] = (context, effective, response.expires_at)
            self._check(state, context, deadline)
            delivered = True
            return response
        except asyncio.CancelledError:
            return self._response(request, context, "cancelled", "flow_cancelled", state=state)
        except Exception as exc:
            code = str(exc) if isinstance(exc, FlowAbort) else "archive_playback_unavailable"
            return self._response(request, context, "cancelled" if code == "flow_cancelled" else "unavailable",
                                  code, state=state)
        finally:
            if plan is not None and not delivered:
                if state:
                    state.playbacks.pop(plan.plan_id, None)
                self.archive.player.revoke(plan.plan_id)
                if self.archive.reader_guard:
                    try:
                        self.archive.reader_guard.revoke(plan.plan_id, context)
                    except Exception:
                        pass
            if state and state.task is asyncio.current_task():
                state.task = None
            self._collect()

    async def cancel(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse:
        if request.kind != "cancel" or request.session_id != context.session_id:
            return self._response(request, context, "refused", "flow_session_unavailable")
        current = self.sessions.get(request.session_id)
        if current and (context.focus_key, context.household_timezone) != current.binding:
            return self._response(request, context, "refused", "flow_context_changed")
        state = next((old for old in self._states(request.session_id)
                      if old.operation_id == request.operation_id), None)
        task = state.task if state else None
        if state:
            self._stop_operation(state)
        if task and task is not asyncio.current_task() and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        return self._response(request, context, "cancelled", "flow_cancelled", state=state)

    async def close(self):
        self.closed = True
        tasks = []
        for session_id in set(self.sessions) | set(self.retired):
            tasks.extend(state.task for state in self._states(session_id)
                         if state.task and state.task is not asyncio.current_task())
            self.invalidate(session_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.sessions.clear()
        self.retired.clear()
