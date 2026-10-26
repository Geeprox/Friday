"""Bounded, synchronous focus fences shared by the two legacy consumers and Console."""

import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4

from .interaction_contracts import InteractionBinding, bounded_ref


@dataclass(frozen=True)
class FocusFence:
    slot: tuple
    key: tuple
    epoch: str
    deadline_mono: float


@dataclass(frozen=True)
class FocusRecord:
    target_id: str
    expires_mono: float
    fence: FocusFence


@dataclass(frozen=True)
class FocusResolution:
    target_id: str | None
    fence: FocusFence | None
    reason: str
    expires_mono: float | None = None


class InteractionSessions:
    """No permissions, writes, model, task queue or focus restoration after invalidation."""

    def __init__(self, *, clock=time.monotonic, max_sessions=128, max_ttl_seconds=120):
        if not 1 <= max_sessions <= 128 or not 0 < max_ttl_seconds <= 120:
            raise ValueError("invalid_focus_budget")
        self.clock = clock
        self.max_sessions = max_sessions
        self.max_ttl_seconds = max_ttl_seconds
        self._slots = OrderedDict()
        self._focus = {}

    def _prune(self):
        now = self.clock()
        for slot, fence in list(self._slots.items()):
            if fence.deadline_mono <= now:
                self.invalidate(slot=slot)
        for slot, record in list(self._focus.items()):
            if record.expires_mono <= now:
                self._focus.pop(slot, None)

    def resolve(self, binding: InteractionBinding | None) -> FocusResolution:
        self._prune()
        if binding is None:
            return FocusResolution(None, None, "interaction_unbound")
        slot, now = binding.slot, self.clock()
        if not binding.focus_eligible or (
            binding.deadline_mono is not None and binding.deadline_mono <= now
        ):
            self.invalidate(slot=slot)
            return FocusResolution(None, None, "speaker_unbound_or_expired")
        fence = self._slots.get(slot)
        if fence is None or fence.key != binding.key:
            self.invalidate(slot=slot)
            while len(self._slots) >= self.max_sessions:
                self.invalidate(slot=next(iter(self._slots)))
            # Sessions never outlive their owner's lifecycle or the local budget.
            deadline = min(binding.deadline_mono or now + 300, now + 300)
            fence = FocusFence(slot, binding.key, str(uuid4()), deadline)
            self._slots[slot] = fence
        self._slots.move_to_end(slot)
        record = self._focus.get(slot)
        return FocusResolution(
            record.target_id if record else None, fence, "context" if record else "no_focus",
            record.expires_mono if record else None,
        )

    def current(self, fence: FocusFence | None):
        return bool(
            fence and self._slots.get(fence.slot) == fence
            and self.clock() < fence.deadline_mono
        )

    def commit(self, fence, target_id, *, status, ttl_seconds):
        self._prune()
        bounded_ref(target_id)
        if not 0 < ttl_seconds <= self.max_ttl_seconds:
            raise ValueError("invalid_focus_ttl")
        if not self.current(fence):
            return False
        if status not in {"reported_match", "verified", "simulated", "unchanged"}:
            self._focus.pop(fence.slot, None)
            return False
        self._focus[fence.slot] = FocusRecord(
            target_id, min(self.clock() + ttl_seconds, fence.deadline_mono), fence
        )
        return True

    def invalidate(self, *, slot=None, session_id=None):
        if slot is not None:
            self._slots.pop(slot, None)
            self._focus.pop(slot, None)
            return
        for key in list(self._slots):
            if session_id is None or key[1] == session_id:
                self.invalidate(slot=key)

    @property
    def latest(self):
        self._prune()
        return next(reversed(self._focus.values()), None)
