"""Privately authorized registered devices, durable send claim, bounded readback.

Read-only ingress stays read-only. No native rules, raw service tools, toggle or
background outbox sender. Every live action requires an unexpired explicit plan.
"""

import asyncio
import fcntl
import hashlib
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import TypeAdapter

from .action_contracts import CoreRuleEffect, HomeDeviceAuthority, LightAuthority, LightExecutionTrace
from .contracts import Capability, Event
from .ha import HAReadOnlyAdapter
from .ha_control import (
    OPERATIONS,
    HADeviceWriter,
    admitted_capabilities,
    bindings,
    parameter_error,
    reported_matches,
)

from .runtime_log import observe
from .event_provenance import entity_source_context

AUTHORITY = TypeAdapter(LightAuthority | HomeDeviceAuthority)


class ActionController:
    def __init__(self, core, authority_path):
        self.core = core
        self.authority_path = Path(authority_path)
        self.boot_id = str(uuid4())
        self._deadlines = {}
        self._lock = asyncio.Lock()
        self.authority_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(p.is_symlink() for p in self.authority_path.parents):
            raise ValueError("authority_symlink_rejected")
        fd = os.open(
            self.authority_path.with_suffix(".execution.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        self._owner = os.fdopen(fd, "a")
        self._owner_owned = False
        try:
            fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._owner_owned = True
        except BlockingIOError:
            pass
        # Sent work from a previous process is never resumed or sent again.
        for trace in self.core.store.unresolved_light_actions() if self._owner_owned else []:
            self._stage(trace, "unknown_outcome", "interrupted_send_requires_readback")

    def close(self):
        self._owner.close()
        self._owner_owned = False

    def pause(self):
        authority, revision = self.authority(include_inactive=True)
        if authority is None:
            return {"status": "paused", "action_targets": []}
        self._set_enabled(authority, revision, False)
        return {"status": "paused", "action_targets": [], "external_rules_changed": False}

    def _set_enabled(self, authority, revision, enabled):
        if hashlib.sha256(self.authority_path.read_bytes()).hexdigest() != revision:
            raise ValueError("source_conflict")
        fd, name = tempfile.mkstemp(prefix=".authority-", dir=self.authority_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(authority.model_copy(update={"enabled": enabled}).model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.authority_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def resume(self, expected_revision):
        authority, revision = self.authority(include_inactive=True)
        adapter = self.core.adapter
        if (
            not authority
            or revision != expected_revision
            or not isinstance(adapter, HAReadOnlyAdapter)
            or authority.ha_url.rstrip("/") != adapter.url
            or authority.ha_instance != adapter.instance_id
        ):
            raise ValueError("source_conflict")
        self._set_enabled(authority, revision, True)
        # Restoring the existing grant never renews an old action plan.
        self._deadlines.clear()

    def policy_report(self):
        authority, revision = self.authority(include_inactive=True)
        return {
            "configured": authority is not None,
            "enabled": bool(authority and authority.enabled),
            "revision": revision,
            "scope": authority.scope
            if isinstance(authority, HomeDeviceAuthority)
            else "single_light"
            if authority
            else None,
            "operations": authority.operations
            if isinstance(authority, HomeDeviceAuthority)
            else ["light.set"]
            if authority
            else [],
            "expires_at": authority.expires_at if authority else None,
            "device_write_attempted": False,
        }

    def authority(self, *, include_inactive=False):
        if not self._owner_owned:
            return None, None
        path = self.authority_path
        try:
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                return None, None
            metadata = path.stat()
            if metadata.st_size > 16384 or metadata.st_mode & 0o077 or not path.is_file():
                return None, None
            body = path.read_bytes()
            authority = AUTHORITY.validate_json(body)
            if (not include_inactive and not authority.enabled) or (
                authority.expires_at is not None and authority.expires_at <= datetime.now(UTC)
            ):
                return None, None
            return authority, hashlib.sha256(body).hexdigest()
        except (OSError, ValueError):
            return None, None

    def targets(self, snapshot):
        authority, _ = self.authority()
        adapter = self.core.adapter
        if snapshot.mode != "ha-read-only" or not isinstance(adapter, HAReadOnlyAdapter) or authority is None:
            return []
        if authority.ha_url.rstrip("/") != adapter.url or authority.ha_instance != adapter.instance_id:
            return []
        if isinstance(authority, HomeDeviceAuthority):
            if not adapter.health().get("registry_available"):
                return []
            return [
                e.id
                for e in snapshot.entities
                if bindings(e)
                and (authority.external_entity_ids is None
                     or bindings(e)[0] in authority.external_entity_ids)
                and any(
                    d.category == "device"
                    and not d.simulated
                    and e.id in d.function_ids
                    and any(
                        b.source == "ha-device-registry" and b.external_id == bindings(e)[1]
                        for b in d.bindings
                    )
                    for d in snapshot.devices
                )
                and admitted_capabilities(adapter, e, authority)
            ]
        entity = next((e for e in snapshot.entities if e.id == authority.target), None)
        entity_bindings = {(b.source, b.external_id) for b in entity.bindings} if entity else set()
        if (
            not entity
            or entity.kind != "light"
            or ":provisional:" in entity.id
            or ("ha", authority.external_entity_id) not in entity_bindings
            or ("ha-device-registry", authority.registry_device_id) not in entity_bindings
        ):
            return []
        return [entity.id]

    def decorate(self, snapshot):
        targets = self.targets(snapshot)
        entities = []
        authority, _ = self.authority()
        for entity in snapshot.entities:
            if entity.id in targets:
                caps = [c for c in entity.capabilities if c.name not in OPERATIONS]
                # 1 is the API's numeric bound, NOT a verified hardware minimum.
                if isinstance(authority, HomeDeviceAuthority):
                    caps += admitted_capabilities(self.core.adapter, entity, authority)
                else:
                    caps.append(
                        Capability(
                            name="light.set",
                            access="write",
                            support="supported",
                            unit="ha_brightness",
                            minimum=1 if authority.allow_brightness else None,
                            maximum=255 if authority.allow_brightness else None,
                        )
                    )
                entity = entity.model_copy(update={"capabilities": caps})
            entities.append(entity)
        devices = []
        for device in snapshot.devices:
            caps = {
                (c.name, c.access): c for e in entities if e.id in device.function_ids for c in e.capabilities
            }
            devices.append(device.model_copy(update={"capabilities": list(caps.values())}))
        return snapshot.model_copy(
            update={"entities": entities, "devices": devices, "action_targets": targets}
        )

    def _policy(self, request, snapshot, authority):
        now = datetime.now(UTC)
        if request.expires_at <= now:
            return "expired_request"
        if (request.expires_at - now).total_seconds() > 60:
            return "unsupported_ttl"
        if request.target not in self.targets(snapshot) or not authority:
            return "unauthorized"
        if not self.core.adapter.health().get("registry_available"):
            return "identity_unavailable"
        if self.core.adapter.health().get("transport") != "online":
            return "offline_dependency"
        entity = next(e for e in snapshot.entities if e.id == request.target)
        if entity.state_version != request.expected_state_version:
            return "source_conflict"
        if not any(
            s.id.startswith(entity.id + ":")
            and s.quality == "healthy"
            and s.valid_until
            and now < s.valid_until
            for s in snapshot.states
        ):
            return "stale_input"
        operation = getattr(request, "operation", "light.set")
        if isinstance(authority, HomeDeviceAuthority):
            cap = next(
                (
                    c
                    for c in admitted_capabilities(self.core.adapter, entity, authority)
                    if c.name == operation
                ),
                None,
            )
            return parameter_error(cap, operation, request.desired) if cap else "unsupported"
        if operation != "light.set":
            return "unauthorized"
        services = self.core.adapter._services
        required = "turn_off" if request.desired["power"] == "off" else "turn_on"
        if not isinstance(services, list) or not any(
            isinstance(s, dict)
            and s.get("domain") == "light"
            and isinstance(s.get("services"), dict)
            and required in s["services"]
            for s in services
        ):
            return "unsupported"
        if "brightness" in request.desired:
            if not authority.allow_brightness:
                return "unauthorized"
            raw = getattr(self.core.adapter, "_light_attributes", {}).get(authority.external_entity_id, {})
            modes = raw.get("supported_color_modes", [])
            if not isinstance(modes, list) or not any(
                isinstance(m, str)
                and m in {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}
                for m in modes
            ):
                return "unsupported"
        return None

    @observe('action.transition', upstream='local')
    def _stage(self, trace, status, code, *, reported=None):
        event = Event(
            source="action-gateway",
            source_context=trace.events[0].source_context if trace.events else None,
            kind="action-result",
            correlation_id=trace.correlation_id,
            causation_id=trace.events[0].event_id if trace.events else None,
            wall_time=datetime.now(UTC),
            monotonic_time=time.monotonic(),
            boot_id=self.boot_id,
            payload={
                "action_id": trace.action_id,
                "status": status,
                "code": code,
                "write_attempted": trace.real_write_attempted or status == "sent",
                "physical_verification": "NOT_RUN",
            },
        )
        updated = trace.model_copy(
            update={
                "status": status,
                "code": code,
                "events": [*trace.events, event],
                "real_write_attempted": trace.real_write_attempted or status == "sent",
                "reported": reported if reported is not None else trace.reported,
            }
        )
        self.core.store.save_light_action(updated, expected_status=trace.status)
        return updated

    @observe('action.plan', upstream='local', key='request.idempotency_key')
    async def plan(self, request, *, managed_guard=None):
        async with self._lock:
            if isinstance(request, CoreRuleEffect) and managed_guard is None:
                raise ValueError("managed_guard_required")
            self._deadlines = {
                key: deadline for key, deadline in self._deadlines.items() if deadline > time.monotonic()
            }
            existing = self.core.store.light_action(key=request.idempotency_key)
            if existing:
                if existing.request != request:
                    raise ValueError("source_conflict")
                return existing
            snapshot = await self.core.snapshot()
            authority, revision = self.authority()
            code = self._policy(request, snapshot, authority)
            if isinstance(request, CoreRuleEffect):
                code = managed_guard(snapshot) or code
            trace = LightExecutionTrace(
                action_id=str(uuid4()),
                correlation_id=request.run_id if isinstance(request, CoreRuleEffect) else str(uuid4()),
                request=request,
                created_at=datetime.now(UTC),
                boot_id=self.boot_id,
                authority_revision=revision,
                status="expired" if code == "expired_request" else "rejected" if code else "planned",
                code=code or "awaiting_explicit_execution",
                reasons=[
                    "Only the local operator's current device and operation authority applies.",
                    "HA readback is reported state, not independent physical confirmation.",
                    "Device mechanics/brightness minimum/no-flash and offline paths require separate qualification.",
                    "External rules remain outside this action authority.",
                ],
            )
            entity = next((e for e in snapshot.entities if e.id == request.target), None)
            if entity is not None and isinstance(self.core.adapter, HAReadOnlyAdapter):
                trace.target_binding_fingerprint = self.binding_fingerprint(entity)
            if getattr(request, "operation", "light.set") != "light.set":
                trace.route = "ha-scoped-device"
            event = Event(
                source="action-gateway",
                source_context=entity_source_context(self.core.adapter, entity) if entity is not None else None,
                kind="action-result",
                correlation_id=trace.correlation_id,
                causation_id=request.source_event_id if isinstance(request, CoreRuleEffect) else None,
                wall_time=trace.created_at,
                monotonic_time=time.monotonic(),
                boot_id=self.boot_id,
                payload={
                    "action_id": trace.action_id,
                    "status": trace.status,
                    "code": trace.code,
                    "write_attempted": False,
                },
            )
            trace.events.append(event)
            self.core.store.save_light_action(trace)
            if trace.status == "planned":
                self._deadlines[trace.action_id] = (
                    time.monotonic() + (request.expires_at - trace.created_at).total_seconds()
                )
            return trace

    @observe('action.approve', upstream='ha', key='action_id')
    async def execute(self, action_id, *, managed_guard=None, dispatch_guard=None):
        async with self._lock:
            trace = self.core.store.light_action(action_id)
            if trace is None:
                raise ValueError("not_found")
            if isinstance(trace.request, CoreRuleEffect) and managed_guard is None:
                raise ValueError("managed_guard_required")
            if trace.status != "planned":
                return trace
            if trace.boot_id != self.boot_id or action_id not in self._deadlines:
                return self._stage(trace, "expired", "restart_requires_new_plan")
            if time.monotonic() >= self._deadlines[action_id]:
                return self._stage(trace, "expired", "expired_request")
            # A healthy event stream can outlive its HTTP access token. Refresh
            # the existing grant BEFORE the final policy/gesture/deadline gate,
            # never retry a device POST after an uncertain outcome.
            if isinstance(self.core.adapter, HAReadOnlyAdapter):
                try:
                    async with asyncio.timeout(max(0, self._deadlines[action_id]-time.monotonic())):
                        await self.core.adapter._refresh_auth()
                except TimeoutError:
                    return self._stage(trace, "expired", "expired_request")
                except (httpx.HTTPError, ValueError, OSError):
                    return self._stage(trace, "rejected", "authentication_unavailable")
            snapshot = await self.core.snapshot()
            authority, revision = self.authority()
            code = self._policy(trace.request, snapshot, authority)
            if revision != trace.authority_revision:
                code = "authority_changed"
            if time.monotonic() >= self._deadlines[action_id]:
                code = "expired_request"
            if isinstance(trace.request, CoreRuleEffect):
                code = managed_guard(snapshot) or code
            if code:
                return self._stage(trace, "expired" if code == "expired_request" else "rejected", code)
            if dispatch_guard is not None and dispatch_guard() is not True:
                raise ValueError("dispatch_guard_rejected")
            # Commit the send claim before touching the network. DB/CAS failure
            # prevents the write
            # Another Core cannot claim this plan twice.
            trace = self._stage(trace, "sent", "send_claim_recorded")
            try:
                entity = next(e for e in snapshot.entities if e.id == trace.request.target)
                writer = HADeviceWriter(
                    self.core.adapter, entity, getattr(trace.request, "operation", "light.set")
                )
                async with asyncio.timeout(max(0, self._deadlines[action_id] - time.monotonic())):
                    outcome = await writer.send(trace.request.desired)
                if outcome != "accepted":
                    return self._stage(
                        trace, "failed" if outcome == "rejected_by_ha" else "unknown_outcome", outcome
                    )
                trace = self._stage(trace, "accepted", "ha_service_accepted")
                reported = await self._settle(writer, trace.request.desired)
                matches = reported_matches(trace.request.desired, reported)
                return self._stage(
                    trace,
                    "reported_match" if matches else "unknown_outcome",
                    "ha_reported_match" if matches else "reported_state_mismatch",
                    reported=reported,
                )
            except asyncio.CancelledError:
                self._stage(trace, "unknown_outcome", "request_cancelled_after_send")
                raise
            except (httpx.HTTPError, ValueError, TimeoutError, OSError):
                return self._stage(trace, "unknown_outcome", "readback_or_transport_unavailable")

    async def _settle(self, writer, desired):
        """Wait for asynchronous HA reports, with one send and a bounded read budget.

        Retry delays never apply to sends. A failed/ambiguous send goes directly
        to unknown_outcome and cannot enter this path.
        """
        reported = None
        for delay in (0, .25, .5, 1, 2):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(2):
                    reported = await writer.readback()
                if reported_matches(desired, reported):
                    return reported
            except (httpx.HTTPError, ValueError, TimeoutError, OSError):
                continue
        if reported is None:
            raise ValueError("unavailable_readback")
        return reported

    @observe('action.reconcile', upstream='ha', key='action_id')
    async def reconcile(self, action_id):
        """Explicit read-only reconciliation. Never sends or re-enqueues an action."""
        async with self._lock:
            trace = self.core.store.light_action(action_id)
            if trace is None:
                raise ValueError("not_found")
            if trace.status != "unknown_outcome" or not self._owner_owned:
                return trace
            snapshot = await self.core.snapshot()
            adapter = self.core.adapter
            entity = next((e for e in snapshot.entities if e.id == trace.request.target), None)
            if not isinstance(adapter, HAReadOnlyAdapter) or not entity:
                return trace
            binding = next((b.external_id for b in entity.bindings if b.source == "ha"), None)
            if not binding or trace.target_binding_fingerprint != self.binding_fingerprint(entity):
                return trace
            try:
                reported = await HADeviceWriter(
                    adapter, entity, getattr(trace.request, "operation", "light.set")
                ).readback()
            except (httpx.HTTPError, ValueError, TimeoutError, OSError):
                return trace
            matches = reported_matches(trace.request.desired, reported)
            reported["causality"] = "unknown; readback cannot prove the original request caused this state"
            recent = datetime.now(UTC) < trace.request.expires_at + timedelta(seconds=60)
            return self._stage(
                trace,
                "reported_match" if matches and recent else "unknown_outcome",
                "readback_match_causality_unconfirmed"
                if matches and recent
                else "late_or_mismatched_readback",
                reported=reported,
            )

    def binding_fingerprint(self, entity):
        import json

        return hashlib.sha256(
            json.dumps(
                [
                    self.core.adapter.url,
                    self.core.adapter.instance_id,
                    entity.id,
                    sorted((b.source, b.external_id) for b in entity.bindings),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()


class HAScopedLightWriter:
    """Private implementation; no client-controlled HA URL/domain/service/target."""

    def __init__(self, adapter, authority):
        if not isinstance(adapter, HAReadOnlyAdapter) or not authority.enabled:
            raise ValueError("unauthorized")
        self.adapter, self.authority = adapter, authority

    async def send(self, desired):
        service = "turn_off" if desired["power"] == "off" else "turn_on"
        body = {"entity_id": self.authority.external_entity_id}
        if "brightness" in desired:
            body["brightness"] = desired["brightness"]
        # Absolute, single combined request. This does not certify no physical flash.
        async with self.adapter._client.stream(
            "POST",
            self.adapter.url + "/api/services/light/" + service,
            json=body,
            headers={"Authorization": "Bearer " + self.adapter._token},
            follow_redirects=False,
            timeout=4,
        ) as response:
            if response.status_code in {400, 401, 403, 404, 405, 422}:
                return "rejected_by_ha"
            if response.status_code != 200:
                return "service_outcome_unknown"
            # Success denotes HA acceptance
            # Ignore unrelated changed-state list.
            return "accepted"

    async def readback(self):
        return await read_light_state(self.adapter, self.authority.external_entity_id)


async def read_light_state(adapter, external):
    import re

    if not re.fullmatch(r"light\.[a-z0-9_]+", external):
        raise ValueError("invalid_readback_binding")
    row = await adapter._get("/api/states/" + external)
    if not isinstance(row, dict) or row.get("entity_id") != external:
        raise ValueError("invalid_readback")
    power, attributes = row.get("state"), row.get("attributes", {})
    if not isinstance(power, str) or power not in {"on", "off"} or not isinstance(attributes, dict):
        raise ValueError("unavailable_readback")
    brightness = attributes.get("brightness")
    if brightness is not None and (type(brightness) is not int or not 0 <= brightness <= 255):
        raise ValueError("invalid_readback")
    return {
        "power": power,
        "brightness": brightness,
        "received_at": datetime.now(UTC).isoformat(),
        "evidence": "HA reported latch; not independently physically confirmed",
    }
