"""Validated, bounded recognizer events and declarative action bindings."""

from __future__ import annotations

import time
from collections import deque
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from .catalog import RECOGNIZERS


class RecognitionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recognizer: str = Field(max_length=64)
    camera: str = Field(pattern=r"^cam_[a-f0-9]{10}$")
    session: str = Field(min_length=1, max_length=64)
    track: int = Field(ge=1, le=2**31)
    sequence: int = Field(ge=0)
    observed_mono: float
    confidence: float = Field(ge=0, le=1)
    value: float | None = Field(default=None, ge=-1000, le=1000)


class RecognizerRegistry:
    def __init__(self):
        self.specs = dict(RECOGNIZERS)
        self.implementations: dict[str, Callable] = {}
        self.seen = deque(maxlen=512)
        self.sequence = 0
        self.previous = {}

    def register(self, name, implementation, *, kind, actions):
        if name in self.specs or kind not in {"hold", "ray", "transition", "continuous", "impulse", "cycle"}:
            raise ValueError("recognizer_conflict")
        if not name.startswith(("hand.", "body.")) or not callable(implementation):
            raise ValueError("invalid_recognizer")
        allowed = {a for spec in self.specs.values() for a in spec["actions"]}
        if not actions or set(actions) - allowed:
            raise ValueError("action_not_allowed")
        self.specs[name] = dict(name=name, kind=kind, actions=list(actions))
        self.implementations[name] = implementation

    def admit(self, event: RecognitionEvent, *, camera, session, bound_track, now=None):
        now = time.monotonic() if now is None else now
        identity = (event.camera, event.session, event.track, event.recognizer, event.sequence)
        if (
            event.recognizer not in self.specs
            or event.camera != camera
            or event.session != session
            or event.track != bound_track
            or not 0 <= now - event.observed_mono <= 1.5
            or event.confidence < 0.6
            or identity in self.seen
        ):
            return False
        self.seen.append(identity)
        return True

    def observe(self, *, camera, session, people, hands, game, now):
        """Plugins receive bounded geometry, never audio, images or credentials.

        Return `(track, confidence, value)` per event. Existing built-ins retain
        their mature debounce/game state machines; registered additions share
        the same camera/session/owner fence before a fixed action is dispatched.
        """
        events = []
        context = dict(people=people, hands=hands, game=game, observed_mono=now)
        for name, implementation in self.implementations.items():
            try:
                outputs = list(implementation(context))[:16]
                for track, confidence, value in outputs:
                    self.sequence += 1
                    events.append(
                        RecognitionEvent(
                            recognizer=name,
                            camera=camera,
                            session=session,
                            track=track,
                            sequence=self.sequence,
                            observed_mono=now,
                            confidence=confidence,
                            value=value,
                        )
                    )
            except (ValueError, TypeError, KeyError):
                continue
        return events


class ActionBindings:
    """Server-owned names bind geometry to known actions; no eval or service API."""

    def __init__(self, registry):
        self.registry = registry
        self.bindings = {}
        self.last_fired = {}

    def bind(self, recognizer, action, callback, *, cooldown=1.0):
        if (
            recognizer not in self.registry.specs
            or action not in self.registry.specs[recognizer]["actions"]
            or not callable(callback)
            or not 0.5 <= cooldown <= 60
        ):
            raise ValueError("action_not_allowed")
        self.bindings[recognizer] = (action, callback, cooldown)

    async def dispatch(self, event, *, camera, session, bound_track, now, current=None):
        if current is not None and current() is not True:
            return False
        binding = self.bindings.get(event.recognizer)
        if not binding or not self.registry.admit(
            event, camera=camera, session=session, bound_track=bound_track, now=now
        ):
            return False
        key = (camera, session, bound_track, event.recognizer)
        if now - self.last_fired.get(key, -100) < binding[2]:
            return False
        self.last_fired[key] = now
        await binding[1](event)
        return True
