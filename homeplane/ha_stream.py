"""Bounded read-only HA event ingress; snapshot recovery never replays event edges."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

from .ha import _date, _object, validate_registry, validate_states

from .runtime_log import observe

EVENT_TYPES = ("state_changed", "entity_registry_updated", "device_registry_updated", "area_registry_updated")
READ_COMMANDS = {"subscribe_events", "get_states", "get_services",
                 "config/entity_registry/list", "config/device_registry/list", "config/area_registry/list"}


def signature(row):
    if row is None:
        return None
    return hashlib.sha256(json.dumps([row["entity_id"], row["state"], row.get("attributes", {}),
                                      row.get("last_updated")], sort_keys=True).encode()).hexdigest()


class HAStream:
    def __init__(self, adapter, *, refresh_seconds=20, retry_seconds=.5):
        self.adapter = adapter
        self.refresh_seconds, self.retry_seconds = refresh_seconds, retry_seconds
        self.task = None
        self.first_attempt = asyncio.Event()
        self.ready = False
        self.gap = False
        self.rows = {}
        self.row_bytes = {}
        self.total_bytes = 0
        self.receipts = {}
        self.published_rows = {}
        self.published_receipts = {}
        self.buffer = []
        self.buffer_bytes = 0
        self.sequence = 0
        self.connection_id = None
        self.ident = 0
        self.subscriptions = {}
        self.counts = {"subscription_acks": 0, "events_received": 0, "reconciles": 0,
                       "duplicates": 0, "reconciled_deltas": 0, "connections": 0}

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="ha-read-only-events")

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        self.ready = False

    def report(self):
        return {"status": "closed" if self.task is None else "connected" if self.ready else "reconnecting", "observation_gap": self.gap,
                "local_cursor": f"{self.connection_id}:{self.sequence}" if self.connection_id else None,
                "global_order": "unavailable", "counts": dict(self.counts)}

    def event(self, frame):
        event = frame.get("event")
        expected = self.subscriptions.get(frame.get("id"))
        if (frame.get("type") != "event" or type(frame.get("id")) is not int or expected is None or not isinstance(event, dict)
                or event.get("event_type") != expected or not isinstance(event.get("data"), dict)
                or _date(event.get("time_fired")) is None):
            raise ValueError("invalid_event_frame")
        self.counts["events_received"] += 1
        self.sequence += 1
        return event

    @observe('ha.read', upstream='ha')
    async def command(self, ws, kind, **params):
        if kind not in READ_COMMANDS:
            raise ValueError("read_only_command_required")
        self.ident += 1
        ident = self.ident
        if kind == "subscribe_events":
            if params.get("event_type") not in EVENT_TYPES:
                raise ValueError("unsupported_event_type")
            self.subscriptions[ident] = params["event_type"]
        await ws.send(json.dumps({"id": ident, "type": kind, **params}))
        async with asyncio.timeout(8):
            while True:
                frame = _object(await ws.recv())
                if frame.get("type") == "event":
                    event = self.event(frame)
                    size = len(json.dumps(event).encode())
                    if len(self.buffer) >= 256 or self.buffer_bytes + size > 512 * 1024:
                        raise ValueError("event_buffer_overflow")
                    self.buffer.append(event)
                    self.buffer_bytes += size
                    continue
                if frame.get("type") != "result" or type(frame.get("id")) is not int or frame.get("id") != ident or frame.get("success") is not True:
                    raise ValueError("read_command_rejected")
                if kind == "subscribe_events":
                    self.counts["subscription_acks"] += 1
                return frame.get("result")

    def delta(self, event, *, initial=False):
        data = event["data"]
        external = data.get("entity_id")
        old, new = data.get("old_state"), data.get("new_state")
        if not isinstance(external, str) or len(external) > 256:
            raise ValueError("invalid_state_event")
        for row in (old, new):
            if row is not None:
                validate_states([row])
                if row["entity_id"] != external:
                    raise ValueError("event_identity_mismatch")
        if old is None and new is None:
            raise ValueError("empty_state_event")
        current = self.rows.get(external)
        if signature(current) == signature(new):
            self.counts["duplicates"] += 1
            return None
        new_time = _date(new.get("last_updated")) if new else None
        current_time = _date(current.get("last_updated")) if current else None
        if new_time and current_time and new_time < current_time:
            self.counts["duplicates"] += 1
            return None
        if signature(current) != signature(old):
            raise ValueError("state_delta_discontinuity")
        received = datetime.now(UTC)
        if new is None:
            self.rows.pop(external, None)
            self.receipts.pop(external, None)
            self.total_bytes -= self.row_bytes.pop(external, 0)
        else:
            if external not in self.rows and len(self.rows) >= 10000:
                raise ValueError("state_capacity_exceeded")
            size = len(json.dumps(new).encode())
            total = self.total_bytes - self.row_bytes.get(external, 0) + size
            if total > 8 * 1024 * 1024:
                raise ValueError("state_cache_overflow")
            self.total_bytes, self.row_bytes[external] = total, size
            self.rows[external] = new
            self.receipts[external] = received
        fired = _date(event["time_fired"])
        age = (received - fired).total_seconds()
        eligible = (not initial and old is not None and new is not None and old["state"] != new["state"]
                    and old["state"] not in {"unknown", "unavailable"}
                    and new["state"] not in {"unknown", "unavailable"}
                    and new_time is not None and current_time is not None and new_time > current_time
                    and abs((new_time - fired).total_seconds()) <= 5
                    and 0 <= age <= 5)
        return {"external": external, "old": old, "new": new, "received_at": received,
                "fired_at": fired, "eligible": eligible, "late": not 0 <= age <= 5,
                "cursor": f"{self.connection_id}:{self.sequence}"}

    async def reconcile(self, ws):
        # Every command drains interleaved events. Re-read on ambiguous state/registry
        # changes; finite attempts admit the consistency boundary on a busy source.
        for _attempt in range(3):
            async with asyncio.timeout(12):
                results = []
                for kind in ("entity", "device", "area"):
                    result = await self.command(ws, f"config/{kind}_registry/list")
                    validate_registry(result, kind)
                    results.append(result)
                services = await self.command(ws, "get_services")
                if not isinstance(services, dict) or len(services) > 256 or any(
                    not isinstance(k, str) or not isinstance(v, dict) for k, v in services.items()
                ):
                    raise ValueError("invalid_services")
                rows = await self.command(ws, "get_states")
                validate_states(rows)
            now = datetime.now(UTC)
            self.rows = {r["entity_id"]: r for r in rows}
            self.row_bytes = {k: len(json.dumps(r).encode()) for k, r in self.rows.items()}
            self.total_bytes = sum(self.row_bytes.values())
            if self.total_bytes > 8 * 1024 * 1024:
                raise ValueError("state_cache_overflow")
            self.receipts = dict.fromkeys(self.rows, now)
            events, self.buffer = self.buffer, []
            self.buffer_bytes = 0
            ambiguous = any(e["event_type"] != "state_changed" for e in events)
            for event in events:
                if event["event_type"] != "state_changed":
                    continue
                try:
                    self.delta(event, initial=True)
                except ValueError as exc:
                    if str(exc) != "state_delta_discontinuity":
                        raise
                    ambiguous = True
            self.counts["reconciled_deltas"] += len(events)
            if ambiguous:
                continue
            await self.adapter._stream_snapshot(self, results, [{"domain": k, "services": v} for k, v in services.items()])
            self.counts["reconciles"] += 1
            self.gap = False
            self.ready = True
            return
        raise ValueError("reconcile_not_converged")

    @observe('ha.stream', upstream='ha')
    async def run(self):
        failures = 0
        while True:
            try:
                async with asyncio.timeout(12):
                    await self.adapter._refresh_auth()
                async with self.adapter._ws_connect(self.adapter._ws_url, open_timeout=8, close_timeout=2,
                                                     max_size=4 * 1024 * 1024, max_queue=16,
                                                     ping_interval=20, ping_timeout=10, proxy=None) as ws:
                    self.connection_id, self.sequence, self.ident = str(uuid4()), 0, 0
                    self.subscriptions, self.buffer, self.buffer_bytes = {}, [], 0
                    async with asyncio.timeout(12):
                        hello = _object(await ws.recv())
                        if hello.get("type") != "auth_required":
                            raise ValueError("invalid_handshake")
                        await ws.send(json.dumps({"type": "auth", "access_token": self.adapter._token}))
                        auth = _object(await ws.recv())
                        if auth.get("type") != "auth_ok":
                            self.adapter._auth_ready = False
                            raise ValueError("authentication_rejected")
                        self.adapter._auth_ready = True
                        self.adapter._ha_version = auth.get("ha_version")
                        for event_type in EVENT_TYPES:
                            await self.command(ws, "subscribe_events", event_type=event_type)
                    self.counts["connections"] += 1
                    await self.reconcile(ws)
                    failures = 0
                    self.first_attempt.set()
                    deadline = time.monotonic() + self.refresh_seconds
                    while True:
                        try:
                            async with asyncio.timeout(max(.001, deadline - time.monotonic())):
                                frame = _object(await ws.recv())
                        except TimeoutError:
                            await self.reconcile(ws)
                            deadline = time.monotonic() + self.refresh_seconds
                            continue
                        event = self.event(frame)
                        if event["event_type"] != "state_changed":
                            await self.reconcile(ws)
                            deadline = time.monotonic() + self.refresh_seconds
                        else:
                            change = self.delta(event)
                            if change:
                                await self.adapter._stream_change(self, change)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ready = False
                self.gap = True
                # Fixed reason only; upstream exception/frames can contain secrets.
                reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                    "event_buffer_overflow", "state_delta_discontinuity", "authentication_rejected",
                    "reconcile_not_converged", "event_identity_mismatch"} else "subscription_disconnected"
                await self.adapter._stream_gap(reason)
                self.first_attempt.set()
                failures += 1
                await asyncio.sleep(min(30, self.retry_seconds * 2 ** min(failures - 1, 6)))
