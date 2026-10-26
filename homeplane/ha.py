"""Explicitly configured, read-only HA ingress. Never imports legacy actuators."""
import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .contracts import (
    Binding,
    CanonicalState,
    Capability,
    Entity,
    Event,
    ExternalRuleRecord,
    Observation,
    Snapshot,
)
from .devices import project_ha
from .quality import resolve

from .runtime_log import observe


class HAReadOnlyAdapter:
    """Read-only snapshots and opt-in continuous events over the same domain contract."""

    def __init__(self, url, token, instance_id, *, client=None, transport=None, ws_connect=connect, token_provider=None):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("HA URL must be http(s), without embedded credentials")
        if parts.query or parts.fragment or not token or not instance_id:
            raise ValueError("Explicit HA URL, token and stable instance_id are required")
        self.url = url.rstrip("/")
        self._token = token
        self._token_provider = token_provider
        self._auth_ready = True
        self.instance_id = instance_id
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(transport=transport, timeout=8, follow_redirects=False, trust_env=False)
        self._ws_connect = ws_connect
        self._ws_url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                                  parts.path.rstrip("/") + "/api/websocket", "", ""))
        self._boot = str(uuid4())
        self._version = 0
        self._evidence_digest = None
        self._events = []
        self._last = None
        self._online = False
        self._gap = False
        self._registry = {}
        self._devices = {}
        self._areas = {}
        self._services = []
        self._registry_ok = False
        self._ha_version = None
        self._light_attributes = {}
        self._control_attributes = {}
        self._lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._stream = None
        self._row_receipts = {}
        self._event_sink = None
        self._journal_ready = True
        self._closed = False

    def capabilities(self):
        return {"snapshot": "supported", "registry_inventory": "supported",
                "execute": "unauthorized", "hardware_refresh": "unsupported",
                "state_changed_subscription": "supported", "external_rule_graph": "unknown",
                "mdns_discovery": "unsupported"}

    def health(self):
        return {"mode": "ha-read-only", "transport": "online" if self._online else ("offline" if self._gap else "unknown"),
                "registry_available": self._registry_ok, "ha_version": self._ha_version,
                "authentication_available": self._auth_ready,
                "continuous_subscription": bool(self._stream and self._stream.ready),
                "subscription": self._stream.report() if self._stream else {"status": "not_started"},
                "observation_gap": self._gap or bool(self._stream and self._stream.gap),
                "diagnostic_journal_available": self._journal_ready,
                "real_write_attempted": False}

    @observe('ha.connect', upstream='ha')
    async def connect(self):
        await self.snapshot()
        return self.health()

    async def discover(self):
        return {"status": "unsupported", "method": "mdns", "reason": "Explicit URL required"}

    @observe('ha.subscribe', upstream='ha')
    async def subscribe(self, *, event_sink=None, timeout=15):
        from .ha_stream import HAStream
        if self._closed:
            raise ValueError("adapter_closed")
        if event_sink is not None:
            self._event_sink = event_sink
        if self._stream is None:
            self._stream = HAStream(self)
            self._stream.start()
        try:
            await asyncio.wait_for(self._stream.first_attempt.wait(), timeout)
        except TimeoutError:
            return {"status": "starting", "read_only": True}
        return {**self._stream.report(), "read_only": True}

    async def inventory(self):
        snap = await self.snapshot()
        return snap.entities

    @observe('ha.close', upstream='ha')
    async def close(self):
        self._closed = True
        if self._stream:
            await self._stream.close()
        self._event_sink = None
        if self._owned_client:
            await self._client.aclose()

    async def execute(self, *_args, **_kwargs):
        return {"status": "unauthorized", "code": "READ_ONLY_ADAPTER", "real_write_attempted": False}

    async def refresh(self, target):
        snap = await self.snapshot()
        known = any(e.id == target or any(b.external_id == target for b in e.bindings) for e in snap.entities)
        return {"target": target, "status": "cached" if known else "unknown",
                "hardware_sample_requested": False, "snapshot_version": snap.version}

    @observe('ha.read', upstream='ha')
    async def _get(self, path):
        async with self._client.stream("GET", self.url + path,
                                       headers={"Authorization": "Bearer " + self._token},
                                       follow_redirects=False, timeout=8) as response:
            if response.status_code in {401, 403}:
                self._auth_ready = False
            response.raise_for_status()
            self._auth_ready = True
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 4 * 1024 * 1024:
                    raise ValueError("HA response exceeds byte budget")
            return _json(content)

    @observe('ha.inventory', upstream='ha')
    async def _inventory(self):
        async with self._ws_connect(self._ws_url, open_timeout=8, close_timeout=2,
                                    max_size=4 * 1024 * 1024, max_queue=16, proxy=None) as ws:
            async with asyncio.timeout(12):
                hello = _object(await ws.recv())
                if hello.get("type") != "auth_required":
                    raise ValueError("Invalid HA handshake")
                await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
                auth = _object(await ws.recv())
                if auth.get("type") != "auth_ok":
                    raise ValueError("HA authentication rejected")
                self._ha_version = auth.get("ha_version")
                results = []
                for ident, kind in enumerate(("entity", "device", "area"), 1):
                    await ws.send(json.dumps({"id": ident, "type": f"config/{kind}_registry/list"}))
                    result = _object(await ws.recv())
                    if result.get("id") != ident or result.get("success") is not True or not isinstance(result.get("result"), list):
                        raise ValueError("Registry inventory unavailable")
                    key = "entity_id" if kind == "entity" else "area_id" if kind == "area" else "id"
                    rows = result["result"]
                    if len(rows) > 10000 or any(not isinstance(r, dict) or not isinstance(r.get(key), str)
                                               or any(r.get(k) is not None and not isinstance(r[k], str)
                                                      for k in ("id", "device_id", "area_id", "unique_id", "platform", "name"))
                                               for r in rows):
                        raise ValueError("Invalid registry rows")
                    if len({r[key] for r in rows}) != len(rows):
                        raise ValueError("Duplicate registry rows")
                    results.append(rows)
                self._registry = {r["entity_id"]: r for r in results[0]}
                self._devices = {r["id"]: r for r in results[1]}
                self._areas = {r["area_id"]: r for r in results[2]}

    def _event(self, kind, payload, now, *, source_seq=None, received_at=None):
        event = Event(source="ha", kind=kind, correlation_id=str(uuid4()), wall_time=now,
                      monotonic_time=time.monotonic(), boot_id=self._boot, payload=payload, source_seq=source_seq,
                      received_at=received_at or datetime.now(timezone.utc),
                      time_basis="upstream_event" if payload.get("upstream_event") == "state_changed" else "local_event")
        self._events.append(event)
        self._events = self._events[-128:]
        if self._event_sink:
            try:
                self._event_sink([event])
                self._journal_ready = True
            except Exception:
                self._journal_ready = False

    @observe('ha.auth', upstream='ha')
    async def _refresh_auth(self):
        if self._token_provider is not None:
            async with self._auth_lock:
                try:
                    token = await self._token_provider()
                    if not isinstance(token, str) or not token:
                        raise ValueError("invalid_token")
                    self._token = token
                    self._auth_ready = True
                except Exception:
                    self._auth_ready = False
                    raise ValueError("authentication_unavailable") from None

    def _limits(self):
        stream = bool(self._stream and self._stream.ready)
        return ["HA last_reported is an integration cache write, not a hardware sample time.",
                "Subscribed changes use a local connection cursor, not a global order; reconciliation never replays transitions." if stream
                else "Continuous events unavailable: read-only polling can miss changes; no event replay on recovery.",
                "External Xiaomi/Geek rules are invisible; no central lease can constrain unknown rules.",
                "This HA ingress never writes or requests fresh hardware samples; separately authorized actions use the Action Gateway."]

    @observe('ha.stream.gap', upstream='ha', failure_code='reason')
    async def _stream_gap(self, reason):
        async with self._lock:
            self._online = False
            self._gap = True
            self._version += 1
            self._event("observation-gap", {"reason": reason, "retry": "bounded_backoff",
                                           "recovery": "snapshot_only_no_replay"}, datetime.now(timezone.utc))

    async def _stream_snapshot(self, stream, results, services):
        async with self._lock:
            stream.published_rows, stream.published_receipts = dict(stream.rows), dict(stream.receipts)
            self._registry = {r["entity_id"]: r for r in results[0]}
            self._devices = {r["id"]: r for r in results[1]}
            self._areas = {r["area_id"]: r for r in results[2]}
            self._services, self._registry_ok, self._online = services, True, True
            self._row_receipts = dict(stream.receipts)
            self._project(list(stream.rows.values()), datetime.now(timezone.utc), self._limits(), emit_snapshot=True,
                          snapshot_origin="subscription_reconcile")

    async def _stream_change(self, stream, change):
        async with self._lock:
            stream.published_rows, stream.published_receipts = dict(stream.rows), dict(stream.receipts)
            external = change["external"]
            entity = next((e for e in self._last.entities if any(b.source == "ha" and b.external_id == external
                          for b in e.bindings)), None) if self._last else None
            old, new = change["old"], change["new"]
            values = [row["state"] for row in (old, new) if row]
            safe = all(len(v) <= 64 and (v in {"on", "off", "unknown", "unavailable"} or _numeric(v)) for v in values)
            stable = entity is not None and self._registry_identity(external) is not None
            eligible = change["eligible"] and stable and safe
            payload = {"entity_id": entity.id if entity else None, "upstream_event": "state_changed",
                       "trigger_eligible": eligible, "hardware_sample_time_known": False,
                       "reason": "live_state_edge" if eligible else "late_report" if change["late"] else "non_trigger_report",
                       "local_cursor": change["cursor"]}
            if safe:
                payload.update({"from": old["state"] if old else None, "to": new["state"] if new else None})
            if entity:
                payload["capability"] = next(c.name for c in entity.capabilities if c.access == "read")
            self._event("state-transition" if eligible else "source-report", payload, change["fired_at"], source_seq=change["cursor"],
                        received_at=change["received_at"])
            self._row_receipts = dict(stream.receipts)
            self._online = True
            # The published raw cache is already current. Project on snapshot
            # demand, not once per source delta: bursts otherwise rebuild every
            # entity for every report and starve HTTP/control deadlines. Event
            # eligibility and durable journaling above still run for every edge.

    @observe('ha.snapshot', upstream='ha')
    async def snapshot(self):
        async with self._lock:
            if self._stream and self._stream.ready:
                self._row_receipts = dict(self._stream.published_receipts)
                return self._project(list(self._stream.published_rows.values()), datetime.now(timezone.utc), self._limits(), emit_snapshot=False)
            return await self._snapshot()

    async def _snapshot(self):
        now = datetime.now(timezone.utc)
        limits = self._limits()
        failure_reason = "snapshot_fetch_failed"
        try:
            await self._refresh_auth()
            rows = await self._get("/api/states")
            validate_states(rows)
        except (httpx.HTTPError, ValueError, TypeError):
            if not self._auth_ready:
                failure_reason = "authentication_unavailable"
            self._online = False
            if not self._gap:
                self._version += 1
            self._gap = True
            self._event("observation-gap", {"reason": failure_reason, "retry": "next_snapshot"}, now)
            limits.append("HA snapshot unavailable; previous observations retained only as stale evidence.")
            return Snapshot(mode="ha-read-only", version=f"ha-{self._boot}-{self._version}", generated_at=now,
                            entities=self._last.entities if self._last else [],
                            devices=self._last.devices if self._last else [],
                            observations=[o.model_copy(update={"transport_health": "offline"})
                                          for o in self._last.observations] if self._last else [],
                            states=[s.model_copy(update={"quality": "stale", "reasons": ["transport_gap"],
                                                         "as_of": now, "valid_until": now, "value": None, "selected_source": None})
                                    for s in self._last.states] if self._last else [],
                            rules=self._rules(), events=list(self._events), limitations=limits)
        self._online = True
        try:
            self._services = await self._get("/api/services")
        except (httpx.HTTPError, ValueError):
            self._services = []
            limits.append("Service inventory unavailable; capability support remains unknown.")
        try:
            await self._inventory()
            self._registry_ok = True
        except (OSError, TimeoutError, ValueError, KeyError, TypeError, WebSocketException):
            self._registry_ok = False
            limits.append("Registry unavailable: previous registry identities retained; unregistered IDs are provisional.")
        self._row_receipts = dict.fromkeys((r["entity_id"] for r in rows), now)
        return self._project(rows, now, limits, emit_snapshot=True, snapshot_origin="read_only_poll")

    def _registry_identity(self, external):
        """Identity qualification comes from registration, never encoded labels."""
        registry = self._registry.get(external, {})
        stable = registry.get("id")
        if not stable and registry.get("unique_id"):
            domain = external.split(".", 1)[0]
            stable = hashlib.sha256(json.dumps(
                [domain, registry.get("platform"), registry["unique_id"]]
            ).encode()).hexdigest()
        return stable or None

    def _identity(self, suffix):
        """Keep compatible IDs; encode accepted labels/long source refs as opaque IDs.

        Instance labels and external HA IDs are not restricted like our public
        references. Never truncate either identity component or export it as a
        path/URL; the upstream binding remains separate and exact.
        """
        candidate = f"ha:{self.instance_id}:{suffix}"
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", candidate):
            return candidate
        raw = json.dumps([self.instance_id, suffix], ensure_ascii=False).encode("utf-8")
        return "ha:ref:" + hashlib.sha256(raw).hexdigest()

    def _project(self, rows, now, limits, *, emit_snapshot, snapshot_origin=None):
        self._light_attributes = {r["entity_id"]: r.get("attributes", {}) for r in rows if r["entity_id"].startswith("light.")}
        self._control_attributes = {r["entity_id"]:r.get("attributes", {}) for r in rows}
        entities, observations, states = [], [], []
        for row in rows:
            external = row["entity_id"]
            domain = external.split(".", 1)[0]
            attrs = row.get("attributes") or {}
            registry = self._registry.get(external, {})
            stable = self._registry_identity(external)
            identity = self._identity(stable or "provisional:" + external)
            # A HA device may be a hub aggregating unrelated sensors. Do not merge evidence by hub ID.
            physical = identity
            bindings = [Binding(source="ha", external_id=external)]
            if registry.get("device_id"):
                bindings.append(Binding(source="ha-device-registry", external_id=registry["device_id"]))
            area = registry.get("area_id") or self._devices.get(registry.get("device_id"), {}).get("area_id")
            capability = "temperature" if attrs.get("device_class") == "temperature" else f"{domain}.state"
            caps = [Capability(name=capability, access="read", support="supported", unit=attrs.get("unit_of_measurement"))]
            # Only qualified live on/off event edges; a cached read is not a trigger.
            if (stable and self._stream and self._stream.ready and not self._stream.gap
                    and domain in {"binary_sensor", "light", "switch", "fan"}
                    and row.get("state") in {"on", "off"}):
                caps.append(Capability(name=capability + ".transition", access="event", support="supported"))
            if domain == "light":
                caps.append(Capability(name="light.set", access="write", support="unsupported"))
            entities.append(Entity(id=identity, name=attrs.get("friendly_name") or external, kind=domain,
                                   room=self._areas.get(area, {}).get("name") or area, physical_source_id=physical,
                                   bindings=bindings, capabilities=caps,
                                   state_version=hashlib.sha256(json.dumps([external, stable, registry.get("device_id"),
                                               row.get("state"), attrs], sort_keys=True).encode()).hexdigest()))
            written = _date(row.get("last_reported") or row.get("last_updated"))
            value = row.get("state")
            unavailable = value in ("unknown", "unavailable", None)
            if capability == "temperature" and not unavailable:
                try:
                    value = float(value)
                    if not (-273.15 <= value <= 1000):
                        unavailable = True
                        value = None
                except (ValueError, TypeError):
                    unavailable = True
                    value = None
            observations.append(Observation(entity_id=identity, capability=capability, value=value,
                                            unit=attrs.get("unit_of_measurement"), physical_source_id=physical,
                                            ingest_source="ha", integration_written_at=written, received_at=self._row_receipts.get(external, now),
                                            freshness_basis="cache_write" if written else "unknown", cached=True,
                                            semantics="periodic_sample" if domain == "sensor" else "latched_state",
                                            transport_health="online"))
            reasons = ["upstream_unavailable"] if unavailable else ["hardware_sample_time_unknown", "ha_cached_state"]
            if not stable:
                reasons.append("provisional_identity_registry_unavailable")
            states.append(CanonicalState(id=f"{identity}:{capability}", value=None if unavailable else value,
                                         unit=attrs.get("unit_of_measurement"), quality="faulty" if unavailable else "unknown",
                                         reasons=reasons, selected_source=None if unavailable else physical, as_of=now))
            # A recent, available on/off read is a reported latch, not a new
            # hardware sample or physical confirmation. Temperature remains unknown.
            if (domain in {"light", "switch", "fan"} and value in {"on", "off"}
                    or domain in {"climate", "cover", "lock", "number", "select"} and not unavailable):
                states[-1] = resolve(observations[-1], now, ttl_seconds=30)
        evidence = {"rows": sorted(rows, key=lambda r: r["entity_id"]), "registry": self._registry,
                    "devices": self._devices, "areas": self._areas, "registry_ok": self._registry_ok,
                    "services": self._services}
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        if digest != self._evidence_digest or self._gap:
            self._version += 1
        self._evidence_digest = digest
        if emit_snapshot:
            self._event("state-snapshot", {"entity_count": len(entities), "reconciled_after_gap": self._gap,
                                          "snapshot_origin": snapshot_origin,
                                          "trigger_eligible": False, "event_replay": False}, now)
        self._gap = False
        self._last = Snapshot(mode="ha-read-only", version=f"ha-{self._boot}-{self._version}", generated_at=now,
                              entities=entities, observations=observations, states=states, rules=self._rules(),
                              events=list(self._events), limitations=limits,
                              devices=project_ha(entities, self._devices, self._areas, self.instance_id))
        return self._last

    def _rules(self):
        return [ExternalRuleRecord(id=self._identity("external-rules"), source="ha",
                                   raw=None, fingerprint=None, coverage_note="External Graph unavailable; no semantics inferred.")]


def _date(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (AttributeError, ValueError, TypeError):
        return None


def _numeric(value):
    import math
    try:
        return math.isfinite(float(value))
    except (ValueError, TypeError):
        return False


def validate_states(rows):
    if not isinstance(rows, list) or len(rows) > 10000 or any(
        not isinstance(r, dict) or not isinstance(r.get("entity_id"), str) or len(r["entity_id"]) > 256
        or not isinstance(r.get("state"), str) or len(r["state"]) > 1024
        or not isinstance(r.get("attributes", {}), dict)
        or any(r.get("attributes", {}).get(k) is not None and not isinstance(r["attributes"][k], str)
               for k in ("friendly_name", "unit_of_measurement", "device_class")) for r in rows
    ) or len({r["entity_id"] for r in rows}) != len(rows):
        raise ValueError("invalid_states")


def validate_registry(rows, kind):
    key = "entity_id" if kind == "entity" else "area_id" if kind == "area" else "id"
    if not isinstance(rows, list) or len(rows) > 10000 or any(
        not isinstance(r, dict) or not isinstance(r.get(key), str)
        or any(r.get(k) is not None and not isinstance(r[k], str)
               for k in ("id", "device_id", "area_id", "unique_id", "platform", "name")) for r in rows
    ) or len({r[key] for r in rows}) != len(rows):
        raise ValueError("invalid_registry")


def _json(raw):
    def reject_constant(_value):
        raise ValueError("Non-finite JSON constant")
    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Invalid HA JSON") from exc


def _object(raw):
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("HA message exceeds byte budget")
    value = _json(raw)
    if not isinstance(value, dict):
        raise ValueError("HA message must be an object")
    return value
