"""Console safety controls; public clients cannot inject recognition evidence."""

from typing import Literal

from fastapi import HTTPException, Response
from pydantic import BaseModel, ConfigDict

from .paths import PRIVATE
from .safety import CAPABILITIES, SafetyPolicy, SafetyRuntime, SafetyStore


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review: Literal["acknowledged", "false_alarm", "unreviewed"]


def install(router, service):
    store = SafetyStore(PRIVATE / "safety.sqlite")
    runtime = SafetyRuntime(store)

    @router.get("/api/embodied/safety")
    async def status():
        worker = dict(status="not_running", cameras=[])
        if service.process and service.process.returncode is None:
            try:
                worker = dict(status="running", **await service.request("/api/safety/status"))
            except Exception:
                worker = dict(status="unavailable", cameras=[])
        return dict(
            policy=store.policy(),
            worker=worker,
            capabilities=CAPABILITIES,
            output_scope="selected_device",
            retention_days=30,
            max_events=store.MAX_EVENTS,
            max_evidence_bytes=store.MAX_BYTES,
            evidence_kind="private_keyframes",
        )

    @router.post("/api/embodied/safety/policy")
    async def configure(policy: SafetyPolicy):
        try:
            return store.configure(policy)
        except ValueError:
            raise HTTPException(409, detail="safety_policy_changed") from None

    @router.get("/api/embodied/safety/events")
    async def events():
        return dict(events=store.events())

    @router.get("/api/embodied/safety/events/{event_id}/frames/{ordinal}")
    async def frame(event_id: str, ordinal: int):
        try:
            return Response(
                store.frame(event_id, ordinal),
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        except ValueError:
            raise HTTPException(404, detail="safety_frame_unavailable") from None

    @router.post("/api/embodied/safety/events/{event_id}/review")
    async def review(event_id: str, request: Review):
        try:
            return store.update(event_id, review=request.review)
        except ValueError:
            raise HTTPException(404, detail="safety_event_unavailable") from None

    @router.post("/api/embodied/safety/test")
    async def test():
        if runtime.tasks:
            raise HTTPException(409, detail="safety_output_busy")
        return await runtime.test()

    return runtime
