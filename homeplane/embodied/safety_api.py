"""Console safety controls; public clients cannot inject recognition evidence."""

import asyncio
import sqlite3
from typing import Literal

from fastapi import HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .paths import PRIVATE
from .safety import CAPABILITIES, SafetyPolicy, SafetyRuntime, SafetyStore
from .safety_storage import SafetyStorageBusy


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review: Literal["acknowledged", "false_alarm", "unreviewed"]


class Retry(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    expected_revision: int = Field(ge=0)


def install(router, service, *, archive=None, infrastructure=None):
    store = SafetyStore(PRIVATE / "safety.sqlite")
    runtime = SafetyRuntime(store)
    from .safety_archive import SafetyArchive
    recordings = SafetyArchive(store, archive)

    async def protected(operation):
        try:
            return await operation
        except (OSError, sqlite3.Error, SafetyStorageBusy):
            raise HTTPException(503, detail='safety_storage_unavailable') from None

    async def stored(operation, *args, **kwargs):
        return await protected(runtime.io.call(operation, *args, **kwargs))

    @router.get("/api/embodied/safety")
    async def status():
        await protected(runtime.sync_policy())
        worker = dict(status="not_running", cameras=[])
        if service.process and service.process.returncode is None:
            try:
                worker = dict(status="running", **await asyncio.wait_for(service.request("/api/safety/status"), 2))
            except Exception:
                worker = dict(status="unavailable", cameras=[])
        sound_review = worker.get('sound_review') or runtime.sound.status()
        crying_available = (sound_review.get('available') is True and 'infant_cry_supported' in sound_review
                            and sound_review['infant_cry_supported'] is not False)
        capabilities = [dict(row, status='unavailable') if row['id'] == 'crying' and not crying_available else row
                        for row in CAPABILITIES]
        return dict(
            policy=runtime.policy_cache,
            worker=worker,
            supervision=infrastructure.report() if infrastructure is not None else None,
            capabilities=capabilities,
            output_scope="selected_device",
            retention_days=30,
            max_events=store.MAX_EVENTS,
            max_evidence_bytes=store.MAX_BYTES,
            evidence_kind="private_keyframes",
            visual_review=worker.get("visual_review") or runtime.visual.status(),
            sound_review=sound_review,
        )

    @router.post('/api/embodied/safety/retry')
    async def retry(request: Retry):
        if infrastructure is None:
            raise HTTPException(409,detail='recovery_not_available')
        try:
            return await infrastructure.retry(expected_revision=request.expected_revision)
        except ValueError as error:
            raise HTTPException(409,detail='source_conflict' if str(error)=='source_conflict' else 'recovery_not_available') from None
        except TimeoutError:
            raise HTTPException(503,detail='recovery_busy') from None

    @router.post("/api/embodied/safety/policy")
    async def configure(policy: SafetyPolicy):
        try:
            saved = await stored(store.configure, policy)
        except ValueError:
            raise HTTPException(409, detail="safety_policy_changed") from None
        try:
            await runtime.sync_policy()
        except (OSError, sqlite3.Error, SafetyStorageBusy):
            pass
        if service.process and service.process.returncode is None:
            try:
                await asyncio.wait_for(service.request('/api/safety/policy', {}), 2)
            except Exception:
                pass  # Worker revision watcher independently observes the durable save.
        return saved

    @router.get("/api/embodied/safety/events")
    async def events():
        return dict(events=await stored(store.events))

    @router.get('/api/embodied/safety/events/{event_id}/recordings')
    async def event_recordings(event_id: str):
        try:
            return await stored(recordings.references, event_id)
        except ValueError:
            raise HTTPException(404, detail='safety_event_unavailable') from None

    @router.get("/api/embodied/safety/events/{event_id}/frames/{ordinal}")
    async def frame(event_id: str, ordinal: int):
        try:
            return Response(
                await stored(store.frame, event_id, ordinal),
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        except ValueError:
            raise HTTPException(404, detail="safety_frame_unavailable") from None

    @router.post("/api/embodied/safety/events/{event_id}/review")
    async def review(event_id: str, request: Review):
        try:
            return await stored(store.update, event_id, review=request.review)
        except ValueError:
            raise HTTPException(404, detail="safety_event_unavailable") from None

    @router.post("/api/embodied/safety/test")
    async def test():
        if runtime.tasks:
            raise HTTPException(409, detail="safety_output_busy")
        return await protected(runtime.test())

    return runtime
