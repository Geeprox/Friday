"""Short, source-bound party holds; no capture, motor or durable intent authority."""

from collections import OrderedDict
import math
import secrets

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["renew", "end"]
    context: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_scope: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: int = Field(ge=1, le=2**53 - 1)
    not_after: float = Field(allow_inf_nan=False)
    camera: str | None = Field(default=None, pattern=r"^cam_[a-f0-9]{10}$")
    session: str | None = Field(default=None, min_length=1, max_length=128)
    frame_mono: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class PartyCamera:
    LEASE_SECONDS = 3

    def __init__(self):
        self.scope = secrets.token_hex(16)
        self.revision = 0
        self.current = None
        self.requests = OrderedDict()

    def _clear(self):
        if self.current is not None:
            self.current = None
            self.revision += 1

    def _prune(self, now):
        if self.current and now >= self.current["until"]:
            self._clear()
        self.requests = OrderedDict(
            (key, value)
            for key, value in self.requests.items()
            if now - value["at"] < 60 or self.current and self.current["context"] == key
        )

    def status(self, *, camera, session, frame_mono, now):
        self._prune(now)
        if self.current and (camera, session) != (self.current["camera"], self.current["session"]):
            self._clear()
        return dict(
            protocol=1,
            scope=self.scope,
            revision=self.revision,
            active=self.current is not None,
            frame_mono=frame_mono,
        )

    def window(self, now):
        self._prune(now)
        return self.scope, self.revision, self.current is not None

    def command(self, payload, sample, now, *, motor_busy=False, motor_last_sent=-100.0, game_active=False):
        try:
            request = Command.model_validate(payload)
        except ValueError:
            raise ValueError("party_camera_request_invalid") from None
        self._prune(now)
        if request.expected_scope != self.scope:
            raise ValueError("party_camera_source_changed")
        if not 0 < request.not_after - now <= 2:
            raise ValueError("party_camera_request_expired")
        previous = self.requests.get(request.context)
        if previous and (
            request.sequence < previous["sequence"]
            or request.sequence == previous["sequence"]
            and not (request.action == "end" and previous["closed"])
            or request.action == "renew"
            and previous["closed"]
        ):
            raise ValueError("party_camera_request_retired")
        if previous is None and len(self.requests) >= 64:
            raise ValueError("party_camera_capacity")
        if request.action == "renew":
            if game_active:
                raise ValueError("motion_game_camera_busy")
            stamp = sample.get("_local_received")
            if (
                request.camera is None
                or request.session is None
                or request.camera != sample.get("camera_id")
                or request.session != sample.get("session_id")
                or sample.get("status") != "live"
                or sample.get("tracking_current") is not True
                or sample.get("scene_visible") is not True
                or type(stamp) not in {int, float}
                or not math.isfinite(stamp)
                or request.frame_mono is None
                or not 0 <= now - request.frame_mono < 1
                or not request.frame_mono <= stamp <= now
                or stamp <= motor_last_sent
                or motor_busy
                or now - motor_last_sent < 2
            ):
                raise ValueError("party_camera_observation_unavailable")
            if self.current and self.current["context"] != request.context:
                raise ValueError("party_camera_busy")
            self.current = dict(
                context=request.context,
                camera=request.camera,
                session=request.session,
                until=now + self.LEASE_SECONDS,
            )
            self.revision += 1
        elif self.current and self.current["context"] == request.context:
            self._clear()
        self.requests[request.context] = dict(
            sequence=request.sequence, closed=request.action == "end", at=now
        )
        return dict(
            scope=self.scope,
            context=request.context,
            sequence=request.sequence,
            active=bool(self.current and self.current["context"] == request.context),
        )
