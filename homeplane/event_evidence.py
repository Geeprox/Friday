"""Bounded local event review over the existing scoped query and trace contracts.

Reading a timeline never scans recordings, invokes a model or submits an action.
Only operator-reviewed camera/room mappings become recording lookup requests.
"""

import asyncio
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response
from pydantic import AwareDatetime, Field, model_validator

from .action_contracts import CoreRuleEffect, LightExecutionTrace
from .archive_contracts import ArchiveResolveRequest
from .contracts import Contract
from .core_rules import CoreRuleRun
from .ha import HAReadOnlyAdapter
from .query_contracts import HomeQuery, Identifier, ReadScope
from .simulator import Simulator

MAX_PROJECTIONS = 16
PROJECTION_SECONDS = 120
MAX_QUERY_PAGES = 20
DIGEST = r"^[a-f0-9]{64}$"
KINDS = {
    "state-snapshot": "状态记录", "state-transition": "状态变化",
    "source-report": "来源报告", "quality-change": "数据质量变化",
    "action-result": "操作结果", "observation-gap": "观测缺口",
}
STATUSES = {
    "planned": "已生成操作预览", "sent": "已发送", "accepted": "已受理",
    "reported_match": "设备报告与请求一致", "rejected": "未获准",
    "expired": "已过期", "failed": "失败", "unknown_outcome": "结果待核对",
    "running": "正在运行", "waiting": "正在等待", "completed": "运行已结束",
    "cancelled": "已取消", "suppressed": "已暂缓",
}


class EvidenceQuery(Contract):
    expected_context: str = Field(pattern=DIGEST)
    room: str = Field(min_length=1, max_length=128)
    entity_id: Identifier | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    window_seconds: int = Field(default=86400, ge=60, le=86400, strict=True)
    limit: int = Field(default=30, ge=1, le=50, strict=True)

    @model_validator(mode="after")
    def bounded_window(self):
        if bool(self.start) != bool(self.end) or self.start and not (
            0 < (self.end - self.start).total_seconds() <= 86400
        ):
            raise ValueError("bounded_evidence_window_required")
        if any(ord(c) < 32 or ord(c) == 127 for c in self.room):
            raise ValueError("invalid_evidence_room")
        return self


class EvidenceDetail(Contract):
    expected_context: str = Field(pattern=DIGEST)
    projection_id: str = Field(pattern=DIGEST)
    event_id: Identifier


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _label(value, maximum=256):
    return "".join(c for c in str(value)[:maximum] if ord(c) >= 32 and ord(c) != 127)


def _code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", value) else None


class EventEvidence:
    def __init__(self, core, queries, archive, *, core_rules=None, context_revision=lambda: None,
                 wall_now=None, monotonic=time.monotonic):
        self.core, self.queries, self.archive = core, queries, archive
        self.core_rules, self.context_revision = core_rules, context_revision
        self.now = wall_now or (lambda: datetime.now(UTC))
        self.monotonic, self.projections = monotonic, {}
        self.registry = {}

    def _prune(self):
        now, mono = self.now(), self.monotonic()
        self.projections = {key: value for key, value in self.projections.items()
                            if value["created"] <= now < value["expires_at"]
                            and value["mono"] <= mono < value["deadline"]}

    def _binding(self, snapshot):
        adapter = self.core.adapter
        actions = getattr(self.core, "actions", None)
        registry = None
        if isinstance(adapter, HAReadOnlyAdapter):
            registry = [adapter._registry_ok,
                        [(key, *[value.get(field) for field in ("id", "unique_id", "platform", "device_id", "area_id")])
                         for key, value in sorted(adapter._registry.items())],
                        [(key, value.get("area_id")) for key, value in sorted(adapter._devices.items())]]
        elif isinstance(adapter, Simulator):
            registry = adapter.source_fixture_identity
        return _digest([
            id(adapter), getattr(adapter, "boot_id", getattr(adapter, "_boot", None)),
            getattr(adapter, "instance_id", None), getattr(adapter, "url", None), self.core.boot_id, snapshot.mode,
            self.context_revision(), actions.policy_report()["revision"] if actions else None,
            self.archive.context()["fingerprint"] if self.archive else None,
            registry,
            [(record.id, record.fingerprint) for record in self.core_rules.records()] if self.core_rules else None,
            [entity.model_dump(mode="json", exclude={"state_version"})
             for entity in sorted([*snapshot.entities, *snapshot.derived_entities], key=lambda e: e.id)],
        ])

    def _quick_binding(self):
        """Cheap in-loop revocation token; full registry fences surround awaits.

        HomeQueries already validates the selected registry projection per page.
        Its per-event check must not serialize the entire household each time.
        """
        adapter = self.core.adapter
        return (id(adapter), getattr(adapter, "boot_id", getattr(adapter, "_boot", None)),
                getattr(adapter, "instance_id", None), getattr(adapter, "url", None), self.core.boot_id, self.context_revision(),
                getattr(self.archive, "revision", None), getattr(self.archive, "closed", False),
                getattr(self.core_rules, "generation", None))

    async def _current(self, expected=None):
        snapshot = await self.core.snapshot()
        binding = self._binding(snapshot)
        if expected is not None and binding != expected:
            self.projections.clear()
            raise ValueError("evidence_context_changed")
        self.registry = {entity.id: entity for entity in [*snapshot.entities, *snapshot.derived_entities]}
        return snapshot, binding

    async def context(self):
        snapshot, binding = await self._current()
        groups = {}
        for entity in [*snapshot.entities, *snapshot.derived_entities]:
            room = entity.room
            if not isinstance(room, str) or not 0 < len(room) <= 128:
                continue
            if any(ord(c) < 32 or ord(c) == 127 for c in room):
                continue
            groups.setdefault(room, []).append(entity)
        rooms = []
        for room in sorted(groups)[:64]:
            entities = sorted(groups[room], key=lambda item: (item.name, item.id))
            rooms.append({"id": room, "name": room, "entity_count": len(entities),
                          "requires_entity": len(entities) > 256,
                          "entities": [{"id": e.id, "name": _label(e.name)} for e in entities[:16]],
                          "entities_partial": len(entities) > 16})
        return {"schema_version": 1, "fingerprint": binding, "rooms": rooms,
                "rooms_partial": len(groups) > 64, "simulated": snapshot.mode == "simulator",
                "observed_at": self.now(), "window_seconds": 86400,
                "coverage": "bounded_local_journal_not_complete_household_history",
                "device_write_attempted": False, "model_inference_attempted": False}

    async def targets(self, expected_context, room, search=""):
        snapshot, binding = await self._current(expected_context)
        entities = [e for e in [*snapshot.entities, *snapshot.derived_entities] if e.room == room]
        if not entities:
            raise ValueError("evidence_selection_unavailable")
        if search:
            entities = [e for e in entities if search.casefold() in e.name.casefold()]
        entities.sort(key=lambda item: (item.name, item.id))
        return {"context_fingerprint": binding, "room": room,
                "entities": [{"id": e.id, "name": _label(e.name)} for e in entities[:128]],
                "partial": len(entities) > 128, "total": len(entities), "device_write_attempted": False}

    def _scope(self):
        # This router is composed only inside the same guarded local Console as
        # /api/query. No peer identity or grant can be supplied by request JSON.
        return ReadScope(subject="console", revision="local-console-event-evidence-v1",
                         expires_at=self.now() + timedelta(seconds=5),
                         boundary="loopback_console", all_home=True, history=True, permitted=True)

    def _fence(self, expected, snapshot):
        if self._binding(snapshot) != expected:
            raise ValueError("evidence_context_changed")

    async def query(self, request):
        self._prune()
        snapshot, binding = await self._current(request.expected_context)
        entities = {e.id: e for e in [*snapshot.entities, *snapshot.derived_entities]
                    if e.room == request.room}
        if not entities or request.entity_id is not None and request.entity_id not in entities:
            raise ValueError("evidence_selection_unavailable")
        if request.entity_id is not None:
            entities = {request.entity_id: entities[request.entity_id]}
        if len(entities) > 256:
            raise ValueError("evidence_choose_device")
        end = request.end or self.now()
        start = request.start or end - timedelta(seconds=request.window_seconds)
        if end > self.now():
            raise ValueError("evidence_future_window")
        query = HomeQuery(kind="history", rooms=[request.room],
                          entities=[request.entity_id] if request.entity_id else [],
                          start=start, end=end, limit=50)
        scope, rows, report = self._scope(), [], None
        quick_binding = self._quick_binding()
        deadline = self.monotonic() + 3
        for _ in range(MAX_QUERY_PAGES):
            report = await self.queries.read(
                query, scope, deadline=deadline,
                current_scope=lambda: scope if self._quick_binding() == quick_binding else None,
            )
            if report.status == "refused":
                raise ValueError("evidence_query_unavailable")
            rows.extend(report.items)
            if not report.next_cursor:
                break
            query = query.model_copy(update={"cursor": report.next_cursor})
        else:
            raise ValueError("evidence_query_budget")
        current, _ = await self._current(binding)
        self._fence(binding, current)
        rows.sort(key=lambda row: (datetime.fromisoformat(row["event_time"]), row["id"]), reverse=True)
        selected = rows[:request.limit]
        if len(self.projections) >= MAX_PROJECTIONS:
            # Completed UI refreshes may release the oldest metadata-only view.
            self.projections.pop(next(iter(self.projections)))
        identifier = secrets.token_hex(32)
        self.projections[identifier] = {
            "binding": binding, "created": self.now(), "expires_at": self.now() + timedelta(seconds=PROJECTION_SECONDS),
            "mono": self.monotonic(), "deadline": self.monotonic() + PROJECTION_SECONDS,
            "selected": {row["id"] for row in selected}, "rows": {row["id"]: row for row in rows},
            "entities": entities, "room": request.room, "start": start, "end": end,
        }
        return {"schema_version": 1, "context_fingerprint": binding, "projection_id": identifier,
                "expires_in": PROJECTION_SECONDS, "status": "partial" if selected else "empty",
                "room": request.room, "start": start, "end": end,
                "items": [self._item(row, entities) for row in selected],
                "coverage_gaps": self._coverage_gaps(start, end),
                "older_items_omitted": len(rows) > len(selected),
                "simulated": snapshot.mode == "simulator", "complete_household_history": False,
                "limitations": report.limitations if report else [],
                "device_write_attempted": False, "model_inference_attempted": False}

    def _coverage_gaps(self, start, end):
        """Source-wide connection gaps do not imply an event in a selected room.

        Legacy source-wide gaps have no entity binding. Only the current exact
        producer boot can qualify this limited coverage notice, never a causal,
        current-device or recording association.
        """
        adapter, result = self.core.adapter, []
        if isinstance(adapter, HAReadOnlyAdapter):
            source, boot, simulated = "ha", adapter._boot, False
        elif isinstance(adapter, Simulator):
            source, boot, simulated = "simulator", adapter.boot_id, True
        else:
            return result
        from .contracts import Event
        for raw in self.core.store.connection.execute(
            "SELECT CASE WHEN length(CAST(body AS BLOB)) <= 16384 THEN body END FROM events ORDER BY rowid DESC LIMIT 1000"
        ).fetchall():
            if not raw[0]:
                continue
            try:
                event = Event.model_validate_json(raw[0])
            except ValueError:
                continue
            if (event.kind != "observation-gap" or event.source != source or event.boot_id != boot
                    or event.simulated != simulated or event.sensitivity != "household"
                    or event.payload.get("entity_id") is not None or not start <= event.wall_time < end):
                continue
            result.append({"event_time": event.wall_time.isoformat(),
                           "code": _code(event.payload.get("reason")), "scope": "current_connection_not_room_event",
                           "caption": "家庭连接记录过观测缺口，所选范围的历史可能不完整。"})
        result.sort(key=lambda item: datetime.fromisoformat(item["event_time"]), reverse=True)
        return result[:8]

    def _item(self, row, entities):
        names = [_label(entities[ref].name) for ref in row["entity_refs"] if ref in entities][:8]
        event = self._event(row["id"])
        _action, trace = self._action(event, entities) if event else (None, None)
        provenance = self._provenance(event, trace)
        if not provenance["verified"]:
            names = []
        return {**row, "title": ("历史" if not provenance["verified"] else "") + KINDS[row["event_kind"]], "entity_names": names,
                "status_label": STATUSES.get(row["summary"].get("status")),
                "source_binding": provenance["state"], "source_binding_caption": provenance["caption"],
                "physically_confirmed": False}

    def _provenance(self, event, trace=None):
        adapter = self.core.adapter
        if event is not None and getattr(event, "source_context", None) and (
            isinstance(adapter, HAReadOnlyAdapter) and event.source == "ha" and not event.simulated
            or isinstance(adapter, Simulator) and event.source in {
                "simulator", "simulator-quality", "simulator:explicit-contact"
            } and event.simulated
            or trace is not None
        ):
            from .event_provenance import entity_source_context
            reference = trace.request.target if trace else event.payload.get("entity_id")
            entity = next((item for key, item in sorted(self.registry.items(), key=lambda value: -len(value[0]))
                           if isinstance(reference, str) and (reference == key or reference.startswith(key + ":"))), None)
            if entity is not None and entity_source_context(adapter, entity) == event.source_context:
                return {"state": "persistent_source_context", "verified": True, "recording_verified": True,
                        "caption": "来源与当前家庭、设备及房间一致。"}
        if trace is not None and getattr(event, "source_context", None) is None:
            return {"state": "persistent_action_binding", "verified": True, "recording_verified": False,
                    "caption": "原操作记录已核对，历史房间尚未确认；暂不能关联录像。"}
        return {"state": "unverified", "verified": False, "recording_verified": False,
                "caption": "历史来源尚未确认，暂不能关联当前设备、规则或录像。"}

    @staticmethod
    def _same_source(left, right):
        return left.source == right.source and left.simulated == right.simulated and (
            left.boot_id == right.boot_id or getattr(left, "source_context", None) is not None
            and left.source_context == getattr(right, "source_context", None)
        )

    def _event(self, identifier):
        row = self.core.store.connection.execute(
            "SELECT CASE WHEN length(CAST(body AS BLOB)) <= 16384 THEN body END FROM events WHERE id=?",
            (identifier,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        from .contracts import Event
        try:
            return Event.model_validate_json(row[0])
        except ValueError:
            return None

    def _action(self, event, entities):
        identifier = event.payload.get("action_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier):
            return None, None
        row = self.core.store.connection.execute(
            "SELECT CASE WHEN length(CAST(body AS BLOB)) <= 32768 THEN body END FROM light_actions WHERE id=?",
            (identifier,),
        ).fetchone()
        if not row or not row[0]:
            return None, None
        try:
            trace = LightExecutionTrace.model_validate_json(row[0])
        except ValueError:
            return None, None
        actions = getattr(self.core, "actions", None)
        if (trace.request.target not in entities or not actions
                or event.source != "action-gateway" or event.boot_id != trace.boot_id
                or event.simulated != trace.simulated
                or not any(e == event for e in trace.events)
                or trace.target_binding_fingerprint is None
                or trace.target_binding_fingerprint != actions.binding_fingerprint(entities[trace.request.target])):
            return None, None
        return {"id": trace.action_id, "target": trace.request.target,
                "target_name": _label(entities[trace.request.target].name),
                "actor": trace.request.actor,
                "actor_label": {"console": "Friday 控制界面", "cli": "本机命令行",
                                "core_rule": "Friday 自动化", "test": "合成测试"}.get(trace.request.actor, "来源待核对"),
                "operation": getattr(trace.request, "operation", "light.set"),
                "requested": {key: self.queries._scalar(value) for key, value in trace.request.desired.items()},
                "status": trace.status, "status_label": STATUSES.get(trace.status), "code": _code(trace.code),
                "reported_match": trace.status == "reported_match", "real_write_attempted": trace.real_write_attempted,
                "physically_confirmed": False,
                "readback_caption": "设备报告与请求一致；尚未独立确认物理效果。" if trace.status == "reported_match"
                else "只展示原操作记录；结果未知时不会自动重发。"}, trace

    def _rules(self, event, trace, entities, snapshot):
        if self.core_rules is None:
            return []
        exists = self.core.store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='core_rule_runs'"
        ).fetchone()
        if not exists:
            return []
        run_id = getattr(trace.request, "run_id", None) if trace else None
        runs = self.core.store.connection.execute(
            "SELECT CASE WHEN length(CAST(body AS BLOB)) <= 16384 THEN body END FROM core_rule_runs "
            "WHERE event_id=? OR id=? ORDER BY rowid DESC LIMIT 8", (event.event_id, run_id or ""),
        ).fetchall()
        records = {record.id: record for record in self.core_rules.records()}
        result = []
        for raw in runs:
            if not raw[0]:
                continue
            try:
                run = CoreRuleRun.model_validate_json(raw[0])
            except ValueError:
                continue
            record = records.get(run.record_id)
            if record is None or record.rule.id != run.rule_id or record.fingerprint != run.fingerprint:
                continue
            try:
                current_scope = self.core_rules.scope(record.rule, snapshot)
            except (ValueError, KeyError, TypeError):
                continue
            if record.source_scope != current_scope or event.simulated != run.simulated:
                continue
            if trace is not None and not (
                isinstance(trace.request, CoreRuleEffect)
                and trace.request.run_id == run.id and trace.request.rule_id == run.rule_id
                and trace.request.rule_fingerprint == run.fingerprint
                and trace.request.source_event_id == run.event_id
            ):
                continue
            trigger_event = self._event(run.event_id)
            if trigger_event is None or trigger_event.sensitivity != "household" or not self._provenance(trigger_event)["verified"]:
                continue
            trigger = getattr(record.rule.trigger, "entity", None)
            targets = {effect.get("target") for effect in run.effects if isinstance(effect, dict)}
            if trigger not in entities and not targets.intersection(entities):
                continue
            result.append({"record_id": record.id, "rule_id": run.rule_id,
                           "name": _label(record.rule.intent, 256), "enabled": record.enabled,
                           "run_id": run.id, "status": run.status, "status_label": STATUSES.get(run.status),
                           "source_event_id": run.event_id,
                           "code": _code(run.code), "relation": "recorded_trigger_or_action_lineage",
                           "navigation": "Automations", "direct_write_available": False,
                           "caption": "运行记录明确引用了本次事件或操作；查看规则后另行预览修改。"})
        return result

    def _recordings(self, row, entities):
        if self.archive is None:
            return [], "archive_unavailable"
        context = self.archive.context()
        if context.get("closed") or not context.get("configured"):
            return [], "archive_not_configured"
        rooms = {entities[ref].room for ref in row["entity_refs"] if ref in entities and entities[ref].room}
        if len(rooms) != 1:
            return [], "recording_room_unknown"
        areas = getattr(self.core.adapter, "_areas", {})
        if isinstance(areas, dict) and sum(isinstance(area, dict) and area.get("name") in rooms
                                           for area in areas.values()) > 1:
            return [], "recording_room_ambiguous"
        event_time = datetime.fromisoformat(row["event_time"])
        if row["event_time_basis"] == "unknown":
            return [], "event_clock_unknown"
        result, room_binding_unreviewed = [], False
        for camera in context.get("cameras", [])[:16]:
            if camera.get("room") not in rooms or camera.get("simulated") is not row["simulated"]:
                continue
            if camera.get("room_binding_current") is not True:
                room_binding_unreviewed = True
                continue
            if camera.get("source_binding_current") is not True:
                continue
            uncertainty = camera.get("clock_uncertainty_seconds")
            qualified = row["simulated"] or (
                camera.get("source_binding_current") is True
                and camera.get("timing_reviewed") is True
                and camera.get("completion_convention") == "closed_files_operator_reviewed"
                and type(uncertainty) in (float, int) and math.isfinite(uncertainty)
                and 0 <= uncertainty <= 60
            )
            if not qualified:
                continue
            try:
                request = ArchiveResolveRequest(
                    expected_context=context["fingerprint"], camera_id=camera["id"],
                    time_range={"start": event_time - timedelta(seconds=15),
                                "end": event_time + timedelta(seconds=15)},
                ).model_dump(mode="json")
            except (KeyError, ValueError):
                continue
            result.append({"event_id": row["id"], "event_time": row["event_time"],
                           "time_basis": row["event_time_basis"], "camera_name": _label(camera.get("name", "录像")),
                           "request": request,
                           "causal_relation": "unknown", "playback_verified": False})
        return result, "configured_camera_time_lookup" if result else "camera_room_binding_unreviewed" if (
            room_binding_unreviewed
        ) else "recording_mapping_or_clock_unreviewed"

    async def detail(self, request):
        self._prune()
        saved = self.projections.get(request.projection_id)
        if saved is None:
            raise ValueError("evidence_projection_expired")
        if saved["binding"] != request.expected_context or request.event_id not in saved["selected"]:
            raise ValueError("evidence_reference_unavailable")
        snapshot, _ = await self._current(request.expected_context)
        # Re-admit the exact original event through the same scoped query. A
        # cached UI row cannot outlive journal removal, source change or expiry.
        original = saved["rows"][request.event_id]
        references = [request.event_id]
        if original["causation_ref"] in saved["rows"]:
            references.append(original["causation_ref"])
        original_event = self._event(request.event_id)
        _original_action, original_trace = self._action(original_event, saved["entities"]) if original_event else (None, None)
        trigger_ref = getattr(original_trace.request, "source_event_id", None) if original_trace else None
        if trigger_ref in saved["rows"] and trigger_ref not in references:
            references.append(trigger_ref)
        references.extend(item["id"] for item in saved["rows"].values()
                          if item["id"] not in references and original["correlation_ref"]
                          and item["correlation_ref"] == original["correlation_ref"])
        references = references[:10]
        query = HomeQuery(kind="evidence", rooms=[saved["room"]],
                          entities=list(saved["entities"]) if len(saved["entities"]) == 1 else [],
                          evidence_refs=references, start=saved["start"], end=saved["end"], limit=10)
        report = await self.queries.read(query, self._scope())
        admitted = {item["id"]: item for item in report.items}
        if report.status == "refused" or request.event_id not in admitted:
            raise ValueError("evidence_reference_unavailable")
        row = admitted[request.event_id]
        if row != saved["rows"][request.event_id]:
            # query_observed_at intentionally advances; compare durable facts.
            old = {k: v for k, v in saved["rows"][request.event_id].items() if k != "query_observed_at"}
            if {k: v for k, v in row.items() if k != "query_observed_at"} != old:
                raise ValueError("evidence_reference_changed")
        event = self._event(request.event_id)
        if event is None:
            raise ValueError("evidence_reference_unavailable")
        action, trace = self._action(event, saved["entities"])
        provenance = self._provenance(event, trace)
        if not provenance["verified"]:
            action = None
        rules = self._rules(event, trace, saved["entities"], snapshot) if provenance["verified"] else []
        cause = admitted.get(row["causation_ref"])
        cause_event = self._event(cause["id"]) if cause else None
        if cause_event:
            _cause_action, cause_trace = self._action(cause_event, saved["entities"])
            cause_verified = self._provenance(cause_event, cause_trace)["verified"]
            explicit_cross_source = bool(trace and rules
                                         and getattr(trace.request, "source_event_id", None) == cause_event.event_id)
            if not provenance["verified"] or not cause_verified or not (
                self._same_source(event, cause_event) or explicit_cross_source
            ):
                cause = None
        if not provenance["verified"]:
            reason = {"level": "source_unverified", "caption": provenance["caption"]}
        elif row["event_kind"] == "observation-gap":
            reason = {"level": "history_gap", "caption": "这段时间存在观测缺口，不能完整还原经过。"}
        elif cause is not None:
            reason = {"level": "source_recorded", "caption": "来源记录了关联引用；尚未独立核验触发原因。"}
        elif rules:
            reason = {"level": "recorded_rule_run", "caption": "规则运行记录引用了本次事件；不能据此确认物理效果。"}
        else:
            reason = {"level": "unknown", "caption": "现有记录不能确定触发原因；外部规则或手动操作可能不可见。"}
        correlated = []
        if provenance["verified"]:
            for item in admitted.values():
                if item["id"] == row["id"] or not row["correlation_ref"] or item["correlation_ref"] != row["correlation_ref"]:
                    continue
                linked_event = self._event(item["id"])
                if linked_event is None or not self._same_source(event, linked_event):
                    continue
                linked_item = self._item(item, saved["entities"])
                if linked_item["source_binding"] != "unverified":
                    correlated.append(linked_item)
        recordings, recording_code = self._recordings(row, saved["entities"]) if provenance["recording_verified"] else (
            [], "event_source_unverified"
        )
        trigger = admitted.get(trigger_ref) if rules else None
        if trigger is not None and self._item(trigger, saved["entities"])["source_binding"] == "unverified":
            trigger = None
        latest, _ = await self._current(request.expected_context)
        self._fence(request.expected_context, latest)
        if self.projections.get(request.projection_id) is not saved or self.monotonic() >= saved["deadline"]:
            raise ValueError("evidence_projection_expired")
        return {"schema_version": 1, "context_fingerprint": request.expected_context,
                "event": self._item(row, saved["entities"]), "explanation": reason,
                "source_binding": provenance["state"], "source_binding_caption": provenance["caption"],
                "recorded_cause": self._item(cause, saved["entities"]) if cause else None,
                "recorded_trigger": self._item(trigger, saved["entities"]) if trigger else None,
                "cause_reference_available": cause is not None,
                "correlated_events": correlated, "correlation_caption": "同一记录组只说明来源关联，时间接近不证明因果。",
                "action": action, "rules": rules, "recordings": recordings, "recording_code": recording_code,
                "recording_caption": "按已核对的房间与录像时钟查找附近片段；画面不是事件因果证明。",
                "complete_household_history": False, "physically_confirmed": False,
                "external_rules_intercepted": False, "device_write_attempted": False,
                "model_inference_attempted": False}


def create_router(core, lock, queries, archive, *, core_rules=None, context_revision=lambda: None):
    router = APIRouter(prefix="/api/home/evidence")
    owner = EventEvidence(core, queries, archive, core_rules=core_rules, context_revision=context_revision)
    router.evidence_owner = owner

    async def run(operation, response):
        response.headers["Cache-Control"] = "no-store"
        try:
            async with asyncio.timeout(5):
                async with lock:
                    return await operation()
        except TimeoutError:
            raise HTTPException(503, detail="evidence_deadline_exceeded", headers={"Cache-Control": "no-store"}) from None
        except (ValueError, OSError, sqlite3.Error) as exc:
            code = str(exc) if str(exc) in {
                "evidence_context_changed", "evidence_selection_unavailable", "evidence_choose_device",
                "evidence_future_window", "evidence_query_unavailable", "evidence_query_budget",
                "evidence_projection_expired", "evidence_reference_unavailable", "evidence_reference_changed",
            } else "evidence_unavailable"
            raise HTTPException(409, detail=code, headers={"Cache-Control": "no-store"}) from None

    @router.get("/context")
    async def context(response: Response):
        return await run(owner.context, response)

    @router.post("/query")
    async def query(request: EvidenceQuery, response: Response):
        return await run(lambda: owner.query(request), response)

    @router.get("/targets")
    async def targets(response: Response, expected_context: str, room: str, search: str = ""):
        if not re.fullmatch(DIGEST, expected_context) or not 0 < len(room) <= 128 or len(search) > 128:
            raise HTTPException(422, detail="invalid_evidence_selection", headers={"Cache-Control": "no-store"})
        return await run(lambda: owner.targets(expected_context, room, search), response)

    @router.post("/detail")
    async def detail(request: EvidenceDetail, response: Response):
        return await run(lambda: owner.detail(request), response)

    return router
