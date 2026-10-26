import time
from datetime import UTC, datetime
from uuid import uuid4

from .contracts import ActionRequest, ActionTrace, Event
from .store import TraceStore


class HomeCore:
    def __init__(self, adapter, store: TraceStore):
        self.adapter, self.store = adapter, store
        self.boot_id = str(uuid4())
        self.actions = None
        self.measurements = None
        self._diagnostic_snapshot = None

    async def snapshot(self):
        snapshot = await self.adapter.snapshot()
        if self.measurements:
            snapshot = self.measurements.apply(self.adapter, snapshot)
        self.store.append_events(snapshot.events)
        events = self.store.recent_events(simulated=snapshot.mode == "simulator")
        snapshot = snapshot.model_copy(update={"events": events})
        snapshot = self.actions.decorate(snapshot) if self.actions else snapshot
        self._diagnostic_snapshot = (self.adapter, snapshot)
        return snapshot

    async def dry_run(self, request: ActionRequest) -> ActionTrace:
        existing = self.store.by_key(request.idempotency_key)
        if existing:
            if existing.request != request:
                raise ValueError("source_conflict")
            return existing
        snapshot = await self.snapshot()
        now = datetime.now(UTC)
        code, reasons, status = "dry_run_only", ["No device write; preview is not authorization or execution."], "planned"
        entity = next((e for e in snapshot.entities if e.id == request.target), None)
        if request.expires_at <= now:
            code, status = "expired_request", "expired"
        elif (request.expires_at - now).total_seconds() > 60:
            code, status = "unsupported_ttl", "rejected"
        elif request.expected_version != snapshot.version:
            code, status = "source_conflict", "rejected"
        elif entity is None:
            code, status = "ambiguous_target", "rejected"
        else:
            cap = next((c for c in entity.capabilities if c.name == request.capability and c.access == "write"), None)
            desired = request.desired
            if cap is None or cap.support != "supported":
                code, status = "unsupported", "rejected"
            elif not desired or set(desired) - {"power", "brightness"}:
                code, status = "unsupported_parameters", "rejected"
            elif "power" in desired and desired["power"] not in ("on", "off"):
                code, status = "unsupported_parameters", "rejected"
            elif "brightness" in desired and (
                isinstance(desired["brightness"], bool) or not isinstance(desired["brightness"], (float, int))
                or cap.minimum is None or cap.maximum is None
                or not cap.minimum <= desired["brightness"] <= cap.maximum
            ):
                code, status = "unsupported_parameters", "rejected"
            elif not any(s.id in {entity.id + ":power", entity.id + ":light.state"} and s.quality == "healthy"
                         and s.valid_until is not None and now < s.valid_until for s in snapshot.states):
                code, status = "stale_input", "rejected"
        if status != "planned":
            reasons = [code, "Rejected before any actuator; external rules remain outside this policy."]
        action_id, correlation = str(uuid4()), str(uuid4())
        event = Event(
            source="home-core", kind="action-result", correlation_id=correlation,
            wall_time=now, monotonic_time=time.monotonic(), boot_id=self.boot_id,
            payload={"action_id": action_id, "status": status, "code": code, "write_attempted": False},
            simulated=snapshot.mode == "simulator",
        )
        trace = ActionTrace(
            action_id=action_id, correlation_id=correlation, request=request,
            status=status, code=code, reasons=reasons, events=[event], simulated=snapshot.mode == "simulator",
        )
        self.store.save(trace)
        self.store.append_events(trace.events)
        return trace
