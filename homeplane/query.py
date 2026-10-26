"""Bounded read projection over the existing Core and local journal.

No extra device/config/model/media client. Stable short-lived pages hold only the
requested projection; cursor scope/source/registry changes and expiry fail closed.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from .contracts import Event
from .query_contracts import (
    ITEM_MODELS,
    HomeQuery,
    HomeQueryResult,
    ReadScope,
    arl_query_descriptor,
    decode_arl_query,
)


class _ReadAbort(Exception):
    def __init__(self, code):
        self.code = code


class HomeQueries:
    def __init__(self, core, *, wall_now=None, monotonic=None, family_media=None):
        self.core = core
        self.family_media = family_media
        self.now = wall_now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.gate = asyncio.Lock()
        self.key = secrets.token_bytes(32)
        self.pages = {}
        self.closed = False

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _permission(self, scope, request):
        now = self.now()
        if (
            not scope.permitted
            or scope.boundary == "unverified"
            or not scope.subject
            or scope.expires_at.tzinfo is None
            or scope.expires_at <= now
            or scope.boundary == "loopback_console"
            and scope.subject != "console"
        ):
            return "read_permission_required"
        if request.source == "family_media":
            return None if request.source_ref in scope.family_media_sources else "family_media_permission_required"
        if request.kind in {"history", "evidence"} and not scope.history:
            return "history_permission_required"
        if not scope.all_home:
            if set(request.rooms) - scope.rooms or (
                not scope.rooms and set(request.entities) - scope.entities
            ):
                return "outside_read_scope"
        return None

    def _scope(self, scope):
        return {
            "subject": scope.subject,
            "boundary": scope.boundary,
            "revision": scope.revision,
            "rooms": sorted(scope.rooms),
            "entities": sorted(scope.entities),
            "all_home": scope.all_home,
            "history": scope.history,
            "permitted": scope.permitted,
            "family_media_sources": sorted(scope.family_media_sources),
            # Console reconstructs its request-local 5s I/O scope on every POST;
            # registered peers instead have an actual host-approved grant lease.
            "expires_at": scope.expires_at.isoformat() if scope.boundary == "registered_peer" else None,
        }

    def _response(self, request, scope, *, status="refused", code, items=None, page=None, cursor=None):
        page = page or {}
        return HomeQueryResult(
            status=status,
            code=code,
            kind=request.kind,
            source=request.source,
            observed_at=self.now(),
            snapshot_at=page.get("at"),
            snapshot_ref=page.get("snapshot_ref"),
            items=items or [],
            next_cursor=cursor,
            coverage="bounded_local_family_catalog_not_physical_copy_inventory"
            if request.source == "family_media" else "bounded_local_journal_not_complete_household_history"
            if request.kind in {"history", "evidence"}
            else "selected_snapshot_only_not_physical_confirmation",
            limitations=page.get("limitations", []),
            permission_scope={
                "subject": scope.subject,
                "boundary": scope.boundary,
                "revision": scope.revision,
                "requested_rooms": request.rooms,
                "requested_entities": request.entities,
                "requested_source": request.source,
                "requested_source_ref": request.source_ref,
            },
            simulated=page.get("simulated"),
        )

    def _prune(self):
        now, mono = self.now(), self.monotonic()
        self.pages = {
            key: page
            for key, page in self.pages.items()
            if now >= page["created"]
            and mono >= page["mono"]
            and mono - page["mono"] < 30
            and (now - page["created"]).total_seconds() < 30
            and (page["grant_expires"] is None or now < page["grant_expires"])
        }

    def close(self):
        self.pages.clear()
        self.closed = True

    def revoke(self, subject):
        """Invalidate projections on host revocation; current_scope gates in-flight I/O.

        This does not create, register, or change any grant. A trusted host must also
        update its current_scope provider before calling this cache cleanup hook.
        """
        self.pages = {key: page for key, page in self.pages.items() if page["subject"] != subject}

    def _source(self):
        adapter = self.core.adapter
        return (
            id(adapter),
            getattr(adapter, "boot_id", getattr(adapter, "_boot", None)),
            getattr(adapter, "instance_id", None),
        )

    @staticmethod
    def _choose(snapshot, request, scope):
        return {
            e.id: e
            for e in [*snapshot.entities, *snapshot.derived_entities]
            if (not request.rooms or e.room in request.rooms)
            and (not request.entities or e.id in request.entities)
            and (scope.all_home or e.id in scope.entities or e.room in scope.rooms)
        }

    def _bindings(self, chosen):
        return self._digest(
            [
                entity.model_dump(mode="json", exclude={"state_version"})
                for entity in sorted(chosen.values(), key=lambda value: value.id)
            ]
        )

    @staticmethod
    def _scalar(value):
        if value is None or type(value) is bool:
            return value
        if type(value) is int and -1e12 <= value <= 1e12:
            return value
        if type(value) is float and math.isfinite(value) and -1e12 <= value <= 1e12:
            return value
        if isinstance(value, str) and len(value) <= 256 and not any(ord(c) < 32 for c in value) and not (
            "://" in value or value.startswith(("/", "~/", "\\\\")) or re.match(r"^[A-Za-z]:[\\\\/]", value)
        ):
            return value
        return None

    @staticmethod
    def _opaque(value, maximum=128):
        # Identifiers are references, never paths, URLs, filenames, or raw payloads.
        return value if isinstance(value, str) and re.fullmatch(
            rf"[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{maximum - 1}}}", value
        ) else None

    @staticmethod
    def _label(value, maximum=256):
        if value is None:
            return None
        return "".join(c for c in str(value)[:maximum] if ord(c) >= 32 and ord(c) != 127)

    def _token(self, cache_id, offset):
        body = json.dumps({"id": cache_id, "offset": offset}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(body + hmac.digest(self.key, body, "sha256")).decode()

    def _decode(self, token):
        try:
            raw = base64.b64decode(token, altchars=b"-_", validate=True)
            body, digest = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.digest(self.key, body, "sha256"), digest):
                raise ValueError()
            value = json.loads(body)
            if (
                set(value) != {"id", "offset"}
                or not isinstance(value["id"], str)
                or type(value["offset"]) is not int
                or value["offset"] < 0
            ):
                raise ValueError()
            return value["id"], value["offset"]
        except (ValueError, TypeError, KeyError):
            raise ValueError("invalid_query_cursor") from None

    async def read(self, request, scope, *, deadline=None, current_scope=None, cancelled=None):
        """Read a bounded projection; host callbacks are synchronous and nonblocking.

        deadline is an absolute monotonic time. Registered peer composition must
        supply current_scope to recheck real host revocation before any delivery.
        No callback is accepted from request JSON. Cancellation never retries I/O.
        """
        request = HomeQuery.model_validate(request.model_dump())
        if self.closed:
            return self._response(request, scope, code="query_closed")
        refused = self._permission(scope, request)
        if refused:
            self.revoke(scope.subject)
            return self._response(request, scope, code=refused)
        started, wall_started = self.monotonic(), self.now()
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            return self._response(request, scope, code="invalid_query_deadline")
        deadline = min(started + request.timeout_ms / 1000, deadline) if deadline is not None else (
            started + request.timeout_ms / 1000
        )
        grant = self._digest(self._scope(scope))

        def check():
            if self.closed:
                raise _ReadAbort("query_closed")
            if self.monotonic() < started or self.now() < wall_started:
                raise _ReadAbort("query_clock_changed")
            if self.monotonic() >= deadline:
                raise _ReadAbort("query_deadline_exceeded")
            try:
                if cancelled is not None and cancelled():
                    raise _ReadAbort("query_cancelled")
                live_scope = current_scope() if current_scope is not None else scope
                if not isinstance(live_scope, ReadScope) or self._permission(live_scope, request) or (
                    self._digest(self._scope(live_scope)) != grant
                ):
                    raise _ReadAbort("query_context_changed")
            except _ReadAbort:
                raise
            except Exception:
                raise _ReadAbort("query_context_changed") from None

        try:
            check()
            if self.gate.locked():
                return self._response(request, scope, code="query_busy")
            async with self.gate:
                async with asyncio.timeout(max(0, deadline - self.monotonic())):
                    return await self._read(request, scope, grant, check)
        except TimeoutError:
            return self._response(request, scope, code="query_deadline_exceeded")
        except _ReadAbort as exc:
            # Drop this principal's projections after cancellation/revocation/fault.
            self.revoke(scope.subject)
            return self._response(request, scope, code=exc.code)

    async def _read(self, request, scope, grant, check):
        self._prune()
        if request.source == "family_media":
            return await self._read_family(request, scope, grant, check)
        if request.kind in {"history", "evidence"} and request.end > self.now():
            return self._response(request, scope, code="future_history_window")
        fingerprint = self._digest(request.model_dump(mode="json", exclude={"cursor"}))
        identifier = None
        if request.cursor:
            try:
                identifier, offset = self._decode(request.cursor)
            except ValueError:
                return self._response(request, scope, code="invalid_query_cursor")
            page = self.pages.get(identifier)
            if page is None:
                return self._response(request, scope, code="query_cursor_expired")
            if page["request"] != fingerprint or page["grant"] != grant or page["source"] != self._source():
                return self._response(request, scope, code="query_context_changed")
            try:
                current = await self.core.snapshot()
            except TimeoutError:
                raise
            except Exception:
                return self._response(request, scope, code="query_source_unavailable")
            self._prune()
            if identifier not in self.pages:
                return self._response(request, scope, code="query_cursor_expired")
            check()
            if self._source() != page["source"] or self._bindings(
                self._choose(current, request, scope)
            ) != page["bindings"]:
                return self._response(request, scope, code="query_context_changed")
        else:
            source = self._source()
            try:
                snapshot = await self.core.snapshot()
            except TimeoutError:
                raise
            except Exception:
                return self._response(request, scope, code="query_source_unavailable")
            check()
            if self._source() != source:
                return self._response(request, scope, code="query_context_changed")
            chosen = self._choose(snapshot, request, scope)
            if len(chosen) > 256:
                return self._response(request, scope, code="query_projection_too_broad")
            missing = bool(set(request.entities) - chosen.keys()) or bool(
                set(request.rooms) - {e.room for e in chosen.values()}
            )
            limitations = [
                "confidence_not_statistically_calibrated",
                "physical_effect_not_confirmed",
                "external_rules_may_be_invisible_and_are_not_intercepted",
            ]
            if missing:
                limitations.append("some_requested_resources_unknown_or_outside_selection")
            try:
                if request.kind == "state":
                    items = self._states(snapshot, chosen, request, check)
                elif request.kind == "devices":
                    items = self._devices(snapshot, chosen)
                else:
                    if request.end > self.now():
                        return self._response(request, scope, code="future_history_window")
                    items = self._history(snapshot, chosen, request, check)
                    limitations += [
                        "local_journal_is_bounded_and_incomplete",
                        "unscoped_sensitive_and_unrecognized_events_omitted",
                        "temporal_proximity_does_not_establish_causation",
                    ]
                    if request.kind == "evidence" and set(request.evidence_refs) - {row["id"] for row in items}:
                        missing = True
                        limitations.append("some_evidence_refs_missing_expired_or_outside_selection")
                check()
                encoded = json.dumps(items, ensure_ascii=False, allow_nan=False).encode()
            except (ValueError, sqlite3.Error, TypeError):
                return self._response(request, scope, code="query_projection_unavailable")
            if len(encoded) > 262144:
                return self._response(request, scope, code="query_projection_too_broad")
            try:
                for item in items:
                    check()
                    ITEM_MODELS[item["kind"]].model_validate(item)
            except (ValueError, KeyError, TypeError):
                return self._response(request, scope, code="query_projection_unavailable")
            if len(items) > request.limit and len(self.pages) >= 16:
                return self._response(request, scope, code="query_capacity_exceeded")
            identifier, offset = str(uuid4()), 0
            page = {
                "items": items,
                "request": fingerprint,
                "grant": grant,
                "subject": scope.subject,
                "grant_expires": scope.expires_at if scope.boundary == "registered_peer" else None,
                "source": source,
                "created": self.now(),
                "mono": self.monotonic(),
                "at": snapshot.generated_at,
                "snapshot_ref": snapshot.version,
                "simulated": snapshot.mode == "simulator",
                "limitations": limitations,
                "missing": missing,
                "bindings": self._bindings(chosen),
            }
        result = [dict(item) for item in page["items"][offset : offset + request.limit]]
        if request.kind == "state":
            for item in result:
                if item.get("quality") == "healthy" and (
                    item["valid_until"] is None or datetime.fromisoformat(item["valid_until"]) <= self.now()
                ):
                    item.update(
                        quality="stale", freshness="expired", value=None, value_interval=None,
                        reasons=[*item["reasons"], "query_page_value_expired"],
                    )
        remaining = offset + len(result)
        cursor = self._token(identifier, remaining) if remaining < len(page["items"]) else None
        check()
        try:
            answer = self._response(
                request, scope,
                status="empty" if not result else "partial" if request.kind in {"history", "evidence"} or (
                    page["missing"]
                ) else "ok",
                code="no_matching_evidence" if not result else "selected_evidence",
                items=result, page=page, cursor=cursor,
            )
            if len(json.dumps(answer.model_dump(mode="json"), ensure_ascii=False, allow_nan=False).encode()) > 65536:
                return self._response(request, scope, code="query_response_budget")
        except (ValueError, TypeError):
            return self._response(request, scope, code="query_projection_unavailable")
        check()
        # Admit only complete responses. Failed/oversized projections never consume page capacity.
        if cursor:
            self.pages[identifier] = page
        return answer

    async def _read_family(self, request, scope, grant, check):
        """Read cached catalog evidence only; never scan, stat or resolve originals.

        The household room grant is intentionally insufficient. Each source-ref
        needs its own host-owned scope; counts exclude other catalog sources.
        Cached metadata is recomputed on every page so age cannot preserve ready.
        """
        from .family_media import FamilyMediaError
        from .family_media.contracts import FamilyMediaAssetRequest

        if self.family_media is None:
            return self._response(request, scope, code="family_media_not_composed")
        fingerprint = self._digest(request.model_dump(mode="json", exclude={"cursor"}))
        identifier, offset = str(uuid4()), 0
        if request.cursor:
            try:
                identifier, offset = self._decode(request.cursor)
            except ValueError:
                return self._response(request, scope, code="invalid_query_cursor")
            saved = self.pages.get(identifier)
            if saved is None:
                return self._response(request, scope, code="query_cursor_expired")
            if saved["request"] != fingerprint or saved["grant"] != grant:
                raise _ReadAbort("query_context_changed")
        try:
            context = await self.family_media.context()
            check()
            if context.fingerprint != request.expected_context or context.source_ref != request.source_ref:
                raise _ReadAbort("query_context_changed")
            items, missing = [], False
            for reference in request.evidence_refs:
                check()
                try:
                    evidence = await self.family_media.asset_evidence(
                        FamilyMediaAssetRequest(expected_context=context.fingerprint, asset_id=reference.split(":", 1)[1]),
                        source_ref=request.source_ref,
                    )
                except FamilyMediaError as exc:
                    if exc.code == "family_media_asset_not_found":
                        missing = True
                        continue
                    raise
                check()
                row = {
                    "kind": "family_media_evidence", "id": reference,
                    "asset_id": evidence.asset_id, "original_hash": evidence.original_hash,
                    "source_ref": request.source_ref, "context_fingerprint": evidence.context_fingerprint,
                    "config_generation": context.config_generation, "connection_epoch": context.connection_epoch,
                    "status": evidence.status, "original_count": evidence.original_count,
                    "ready_original_count": evidence.ready_original_count, "other_original_count": evidence.other_original_count,
                    "original_count_basis": "observed_source_bindings_including_historical_epochs",
                    "originals": [{"item_id": item.item_id, "media_kind": item.media_kind, "status": item.status,
                                   "last_seen_at": item.last_seen_at,
                                   "original_ref": item.original_ref.model_dump(mode="json") if item.original_ref else None}
                                  for item in evidence.originals[:8]],
                    "originals_truncated": evidence.partial or len(evidence.originals) > 8,
                    "query_observed_at": self.now(), "simulated": evidence.simulated,
                }
                items.append(ITEM_MODELS["family_media_evidence"].model_validate(row).model_dump(mode="json"))
            latest = await self.family_media.context()
            check()
            if latest.fingerprint != context.fingerprint:
                raise _ReadAbort("query_context_changed")
        except FamilyMediaError as exc:
            if exc.code in {"family_media_context_changed", "family_media_closed"}:
                raise _ReadAbort("query_context_changed") from None
            return self._response(request, scope, code="query_projection_unavailable")
        except (ValueError, TypeError, sqlite3.Error):
            return self._response(request, scope, code="query_projection_unavailable")
        if len(json.dumps(items, ensure_ascii=False, allow_nan=False).encode()) > 262144:
            return self._response(request, scope, code="query_projection_too_broad")
        result = items[offset:offset + request.limit]
        remaining = offset + len(result)
        cursor = self._token(identifier, remaining) if remaining < len(items) else None
        if cursor and identifier not in self.pages and len(self.pages) >= 16:
            return self._response(request, scope, code="query_capacity_exceeded")
        page = {
            "subject": scope.subject, "request": fingerprint, "grant": grant,
            "grant_expires": scope.expires_at if scope.boundary == "registered_peer" else None,
            "created": self.now(), "mono": self.monotonic(), "at": None,
            "snapshot_ref": context.fingerprint, "simulated": context.simulated,
            "limitations": ["cached_metadata_rechecked_each_page", "capture_time_unknown",
                            "physical_copy_count_unknown", "metadata_revision_not_immutable_content",
                            "temporal_proximity_does_not_establish_causation"],
        }
        if missing:
            page["limitations"].append("some_evidence_refs_missing_or_outside_selection")
        answer = self._response(request, scope, status="partial" if result else "empty",
                                code="selected_evidence" if result else "no_matching_evidence",
                                items=result, page=page, cursor=cursor)
        if len(json.dumps(answer.model_dump(mode="json"), ensure_ascii=False, allow_nan=False).encode()) > 65536:
            return self._response(request, scope, code="query_response_budget")
        check()
        # Keep the original cursor lease; later pages cannot extend its lifetime.
        if cursor and identifier not in self.pages:
            self.pages[identifier] = page
        return answer

    def _states(self, snapshot, chosen, request, check=lambda: None):
        rows = []
        sources = {e.physical_source_id for e in chosen.values()}
        for state in sorted(snapshot.states, key=lambda value: value.id):
            check()
            entity = next((e for e in sorted(chosen.values(), key=lambda e: -len(e.id))
                           if state.id.startswith(e.id + ":")), None)
            if entity is None:
                continue
            capability = state.id[len(entity.id) + 1 :]
            if request.capabilities and capability not in request.capabilities:
                continue
            healthy = (
                state.quality == "healthy"
                and state.valid_until is not None
                and self.now() < state.valid_until
                and state.as_of <= self.now()
            )
            value = self._scalar(state.value) if healthy else None
            device = next((device for device in snapshot.devices if entity.id in device.function_ids), None)
            quality = state.quality
            reasons = [self._label(reason) for reason in state.reasons[:16]]
            if quality == "healthy" and not healthy:
                quality = "stale" if state.valid_until and state.valid_until <= self.now() else "unknown"
                reasons.append("query_state_expired_or_clock_unknown")
            elif healthy and value is None:
                quality = "unknown"
                reasons.append("query_value_shape_unsupported_or_missing")
            rows.append(
                {
                    "kind": "state",
                    "id": state.id,
                    "entity_id": entity.id,
                    "device_id": device.id if device else None,
                    "device_name": self._label(device.name) if device else None,
                    "name": self._label(entity.name),
                    "room": entity.room,
                    "capability": capability,
                    "value": value,
                    "unit": self._label(state.unit, 64),
                    "quality": quality,
                    "freshness": "canonical_valid" if quality == "healthy" else "expired" if quality == "stale" else "unknown",
                    "reasons": reasons,
                    "selected_source": self._opaque(state.selected_source)
                    if state.selected_source in sources else None,
                    "excluded_sources": {
                        key: self._label(reason)
                        for key, reason in list(state.excluded_sources.items())[:8]
                        if key in sources and self._opaque(key)
                    },
                    "as_of": state.as_of.isoformat(),
                    "valid_until": state.valid_until.isoformat() if state.valid_until else None,
                    "as_of_basis": "canonical_resolution_not_hardware_sample",
                    "value_interval": state.value_interval if quality == "healthy" else None,
                    "diagnostic_interval": state.value_interval if quality != "healthy" else None,
                    "calibration_version": self._label(state.calibration_version, 128),
                    "resolver_version": self._label(state.resolver_version, 128),
                    "inferred": state.inferred,
                    "simulated": state.simulated,
                    "confidence": None,
                    "evidence_refs": [ref for ref in state.evidence_refs if self._opaque(ref)][:32],
                    "source_times": [
                        {
                            "physical_source_id": o.physical_source_id,
                            "ingest_source": o.ingest_source,
                            "measured_at": o.measured_at.isoformat() if o.measured_at else None,
                            "source_reported_at": o.source_reported_at.isoformat()
                            if o.source_reported_at
                            else None,
                            "integration_written_at": o.integration_written_at.isoformat()
                            if o.integration_written_at
                            else None,
                            "received_at": o.received_at.isoformat(),
                            "freshness_basis": o.freshness_basis,
                            "cached": o.cached,
                            "clock_uncertainty_seconds": o.clock_uncertainty_seconds,
                        }
                        for o in snapshot.observations
                        if o.entity_id in chosen
                        and (o.entity_id == entity.id or o.physical_source_id == state.selected_source)
                        and o.capability == capability
                    ][:8],
                }
            )
        return rows

    def _devices(self, snapshot, chosen):
        return [
            {
                "kind": "device",
                "id": device.id,
                "name": self._label(device.name),
                "room": device.room if device.room in {
                    chosen[key].room for key in device.function_ids if key in chosen
                } else None,
                "category": device.category,
                "identity_basis": device.identity_basis,
                "function_ids": [key for key in device.function_ids if key in chosen],
                "simulated": device.simulated,
                "execution_authority": False,
            }
            for device in sorted(snapshot.devices, key=lambda item: item.id)
            if any(key in chosen for key in device.function_ids)
        ]

    def _history(self, snapshot, chosen, request, check=lambda: None):
        rows = []
        for rowid, body in self.core.store.connection.execute(
            "SELECT rowid,CASE WHEN length(CAST(body AS BLOB)) <= 16384 THEN body END "
            "FROM events ORDER BY rowid DESC LIMIT 1000"
        ).fetchall():
            check()
            if body is None:
                continue
            try:
                event = Event.model_validate_json(body)
            except ValueError:
                continue
            if (
                event.simulated != (snapshot.mode == "simulator")
                or event.sensitivity != "household"
                or not request.start <= event.wall_time < request.end
                or not self._opaque(event.event_id) or not self._opaque(event.source)
                or request.kind == "evidence" and event.event_id not in request.evidence_refs
            ):
                continue
            refs = self._event_entities(event, chosen)
            if not refs:
                continue
            payload = {}
            for key in ("from", "to", "quality", "status", "code", "reason"):
                if key not in event.payload:
                    continue
                value = event.payload[key]
                # Free-text reasons/status fields can carry credentials, paths, or
                # transcripts. Export only code-shaped metadata, not raw messages.
                payload[key] = self._scalar(value) if key in {"from", "to"} else (
                    value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", value)
                    else None
                )
            causation = self._opaque(event.causation_id)
            rows.append(
                {
                    "kind": "event",
                    "id": event.event_id,
                    "source": event.source,
                    "event_kind": event.kind,
                    "entity_refs": sorted(refs),
                    "event_time": event.wall_time.isoformat(),
                    "source_reported_at": event.wall_time.isoformat()
                    if event.time_basis == "upstream_event"
                    else None,
                    "event_time_basis": event.time_basis,
                    "observed_at": event.received_at.isoformat() if event.received_at else None,
                    "query_observed_at": snapshot.generated_at.isoformat(),
                    "hardware_sample_time": None,
                    "causation_ref": causation,
                    "causation_status": "recorded_by_source_not_independently_verified"
                    if causation
                    else "unknown",
                    "correlation_ref": self._opaque(event.correlation_id),
                    "summary": payload,
                    "simulated": event.simulated,
                    "confidence": None,
                    "_row": rowid,
                }
            )
        rows.sort(key=lambda item: (datetime.fromisoformat(item["event_time"]), item["_row"]))
        return [{key: value for key, value in row.items() if key != "_row"} for row in rows]

    def _event_entities(self, event, chosen):
        entity_id = event.payload.get("entity_id")
        if isinstance(entity_id, str):
            return {key for key in chosen if entity_id == key or entity_id.startswith(key + ":")}
        if event.kind != "action-result":
            return set()
        action = event.payload.get("action_id")
        if self._opaque(action):
            for table in ("light_actions", "traces"):
                row = self.core.store.connection.execute(
                    f"SELECT CASE WHEN length(CAST(body AS BLOB)) <= 16384 THEN body END "
                    f"FROM {table} WHERE id=?", (action,)
                ).fetchone()
                if row and row[0]:
                    body = json.loads(row[0])
                    request = body.get("request") if isinstance(body, dict) else None
                    target = request.get("target") if isinstance(request, dict) else None
                    return {target} if isinstance(target, str) and target in chosen else set()
        run = event.payload.get("rule_run_id")
        if self._opaque(run):
            # A runtime table is optional in standalone CLI/domain use.
            exists = self.core.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='rule_runs'"
            ).fetchone()
            if exists:
                row = self.core.store.connection.execute(
                    "SELECT CASE WHEN length(CAST(body AS BLOB)) <= 16384 THEN body END "
                    "FROM rule_runs WHERE id=?", (run,)
                ).fetchone()
                if row and row[0]:
                    body = json.loads(row[0])
                    effects = body.get("effects") if isinstance(body, dict) else None
                    if not isinstance(effects, list) or len(effects) > 64:
                        return set()
                    return {
                        effect.get("target")
                        for effect in effects
                        if isinstance(effect, dict) and isinstance(effect.get("target"), str)
                        and effect["target"] in chosen
                    }
        return set()


class HomeQueryConsumer:
    """Source-only ARL consumer adapter, without a transport or tool registration.

    Reuses the actual installed SDK validator/encoder. The integration owner must
    compose a verified peer and a nonblocking host-owned current_scope provider.
    This adapter never launches ARL, submits a job, or requests a model/grant.
    """

    def __init__(self, queries, *, current_scope):
        if not callable(current_scope):
            raise ValueError("host_scope_provider_required")
        self.queries, self.current_scope = queries, current_scope

    async def read(self, arguments, *, deadline, cancelled=lambda: False):
        from arl.contracts import Fault, encode, validate

        validate(arguments, arl_query_descriptor()["inputSchema"])
        try:
            request = decode_arl_query(arguments)
        except (ValueError, TypeError):
            raise Fault("invalid_arguments") from None
        try:
            scope = self.current_scope()
        except Exception:
            raise Fault("read_permission_required") from None
        if not isinstance(scope, ReadScope) or scope.boundary != "registered_peer":
            raise Fault("read_permission_required")
        answer = await self.queries.read(
            request, scope, deadline=deadline, current_scope=self.current_scope, cancelled=cancelled,
        )
        result = answer.model_dump(mode="json")
        if len(encode(result)) > 65536:
            raise Fault("query_response_budget")
        return result


@dataclass(frozen=True)
class HomeQueryAdoptionConfig:
    """Host-owned existing peer binding and read-scope provider, never caller JSON.

    No process registration, AppPolicy update, or grant creation occurs here.
    This deliberately supports process-local transient delivery only. Publishing
    it as an ARL job/tool or forwarding its output needs separate retention/egress
    policy and is outside this ordinary source composition.
    """

    peer_app: str
    peer_pid: int = field(repr=False)
    peer_verifier: object = field(repr=False)
    current_scope: object = field(repr=False)

    def __post_init__(self):
        from arl.identity import RegisteredPeerVerifier

        if (
            self.peer_app not in {"mio", "friday"}
            or type(self.peer_pid) is not int or self.peer_pid <= 0
            or not isinstance(self.peer_verifier, RegisteredPeerVerifier)
            or self.peer_app not in self.peer_verifier.app_ids
            or not callable(self.current_scope)
        ):
            raise ValueError("invalid_query_adoption_config")

    def bind(self, queries):
        return HomeQueryAdoption(queries, self)


class HomeQueryAdoption:
    """Event-loop-owned consumer over Friday's existing Core and actual SDK verifier.

    Source wiring for a trusted owner, without network/HTTP identity claims. The
    caller must already have a verifier binding and an approved household scope.
    There is no actor factory, registration, runtime/client launch, job submission,
    persistent result store, or new grant. None is inferred from the local Console.
    """

    def __init__(self, queries, config):
        if not isinstance(config, HomeQueryAdoptionConfig):
            raise ValueError("invalid_query_adoption_config")
        self.queries, self.config = queries, config
        self.consumer = HomeQueryConsumer(queries, current_scope=self._peer_scope)
        self.owner_loop = None
        self.closed = False

    @staticmethod
    def unconfigured_status():
        return {
            "configured": False, "mode": "local_transient_only",
            "peer_verified": None, "household_scope_verified": None,
            "tool_registered": False, "grant_created": False,
            "friday_result_persistence": "none",
            "projection_cache_ttl_seconds": 30,
            "arl_job_results_used": False, "cloud_egress": False,
            "mio_result_retention": "not_established",
            "downstream_model_policy": "not_established",
            "external_transport": "not_composed",
        }

    def status(self):
        # Metadata is configuration only; it never probes identity/scope/source.
        return {**self.unconfigured_status(), "configured": True,
                "peer_app": self.config.peer_app,
                "lifecycle": "closed" if self.closed else "bound" if self.owner_loop else "not_bound"}

    def bind_loop(self):
        loop = asyncio.get_running_loop()
        if self.closed or self.owner_loop is not None and self.owner_loop is not loop:
            raise ValueError("query_consumer_lifecycle_conflict")
        self.owner_loop = loop

    def close(self):
        self.closed = True
        self.queries.revoke(self.config.peer_app)

    def _peer_scope(self):
        from arl.contracts import Fault

        if self.closed or not self.config.peer_verifier.is_registered(
            self.config.peer_app, self.config.peer_pid
        ):
            raise Fault("read_permission_required")
        scope = self.config.current_scope()
        if not isinstance(scope, ReadScope) or scope.subject != self.config.peer_app or (
            scope.boundary != "registered_peer"
        ):
            raise Fault("read_permission_required")
        return scope

    async def read(self, arguments, *, deadline, cancelled=lambda: False):
        from arl.contracts import Fault

        if self.closed:
            raise Fault("query_closed")
        if self.owner_loop is None:
            raise Fault("query_consumer_not_bound")
        if self.owner_loop is not asyncio.get_running_loop():
            raise Fault("query_consumer_wrong_loop")
        # Every scope recheck invokes the supplied SDK's current process-identity
        # guard. The pinned app/PID never comes from arguments, headers, or JSON.
        return await self.consumer.read(arguments, deadline=deadline, cancelled=cancelled)
