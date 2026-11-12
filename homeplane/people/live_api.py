"""Metadata-only registration/control, behind the Console's existing guards."""

import asyncio
import sqlite3
import math
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .catalog import PeopleError
from .live import POLICY, IdentityStore


class Register(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    camera: str = Field(pattern=r"^cam_[a-f0-9]{10}$")
    session: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    track: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=40)
    person_id: str | None = Field(default=None, pattern=r"^person_[a-f0-9]{24}$")


class Forget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    person_id: str = Field(pattern=r"^person_[a-f0-9]{24}$")


class NameCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    expected_revision: int = Field(ge=0)
    candidate: str = Field(pattern=r'^candidate_[a-f0-9]{24}$')
    name: str = Field(min_length=1,max_length=40)
    person_id: str | None = Field(default=None,pattern=r'^person_[a-f0-9]{24}$')
    members: list[str] | None = Field(default=None,min_length=1,max_length=8)
    group_revision: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')


class CandidateGroup(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    members:list[str]=Field(min_length=2,max_length=8)
    separate:bool=False


def qualify_presence(rows, recognition, *, now=None):
    """Recent history is current presence only with a fresh live source row."""
    now = time.monotonic() if now is None else now
    views = recognition.get('sessions', [recognition]) if isinstance(recognition, dict) else []
    active, duplicate = {}, set()
    if not isinstance(views, list):views = []
    for view in views:
        if not isinstance(view, dict) or view.get('state') != 'live':continue
        camera, session, tracks = view.get('camera'), view.get('session'), view.get('tracks')
        if not isinstance(camera, str) or not isinstance(session, str) or not isinstance(tracks, list):continue
        for row in tracks:
            if not isinstance(row, dict) or row.get('state') not in {'matched', 'tracked'}:continue
            observed = row.get('observed_monotonic')
            if (type(observed) not in (int, float) or not math.isfinite(observed)
                    or not 0 <= now-observed <= 1 or not isinstance(row.get('track'), str)
                    or not isinstance(row.get('person_id'), str)):
                continue
            key = (camera, session, row['track'])
            if key in active:duplicate.add(key)
            active[key] = row['person_id']
    for row in rows:
        key = (row['camera'], row['session'], row['track'])
        if not row['present'] or key not in active or key in duplicate or active.get(key) != row['person_id']:
            row.update(present=False, facing=None)
    return rows


def create_router(catalog, service):
    router = APIRouter(prefix="/api/people/live")
    gate = asyncio.Lock()

    def store():
        from homeplane.embodied.paths import PRIVATE
        return IdentityStore(PRIVATE / "identities.sqlite")

    def stored_metadata():
        db = store()
        try:
            profiles, presence = db.registered(), db.presence()
            portraits={p for p in profiles if db.portrait(p) is not None}
            candidates=db.candidates()
            candidate_groups=db.candidate_groups()
            group_undo=db.group_undo_available()
        finally:
            db.close()
        return profiles, presence, portraits, candidates, candidate_groups, group_undo

    def metadata(stored=None):
        profiles, presence, portraits, candidates, candidate_groups, group_undo = stored_metadata() if stored is None else stored
        page = catalog.page()
        people = {p.person_id: p for p in page.people}
        for row in presence:
            pid = row["person_id"]
            visited = set()
            while pid in people and people[pid].merged_into and pid not in visited:
                visited.add(pid)
                pid = people[pid].merged_into
            row.update(person_id=pid if pid in people else None,
                       name=people[pid].name if pid in people else None)
            if not row["name"] or row["person_id"] not in profiles and not visited:
                row.update(present=False, state="unknown", facing=None)
        return dict(revision=page.revision, profiles=[dict(person_id=p.person_id, name=p.name,
                    enrolled=p.person_id in profiles, updated=profiles.get(p.person_id),
                    portrait=p.person_id in portraits)
                    for p in page.people if p.name and not p.merged_into],
                    observations=presence, candidates=candidates,candidate_groups=candidate_groups,group_undo_available=group_undo,cameras=service.cameras(), policy=POLICY,
                    experimental=True, security_authentication=False, action_authorized=False)

    async def live_metadata():
        result = metadata(await asyncio.to_thread(stored_metadata))
        try:
            source = getattr(service, 'identity_status', None)
            if source is None:raise ValueError('identity_not_running')
            result['recognition'] = await asyncio.wait_for(source(), 1)
        except (ValueError, OSError, TimeoutError):
            result['recognition'] = dict(state='stopped', tracks=[])
        result['observations'] = qualify_presence(result['observations'], result['recognition'])
        return result

    @router.get('/{person_id}/portrait')
    async def portrait(person_id: str):
        if not any(p.person_id==person_id and p.name and not p.merged_into for p in catalog.page().people):
            raise HTTPException(404)
        db=store()
        try:photo=db.portrait(person_id)
        finally:db.close()
        if photo is None:raise HTTPException(404)
        return Response(photo,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

    @router.get('/candidates/{candidate}/portrait')
    async def candidate_portrait(candidate: str):
        db=store()
        try:
            photo=db.candidate_photo(candidate) if any(c['id']==candidate for c in db.candidates()) else None
        finally:db.close()
        if photo is None:raise HTTPException(404)
        return Response(photo,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

    @router.post('/name-candidate')
    async def name_candidate(request: NameCandidate):
        async with gate:
            db=store()
            try:
                db.name_candidate(catalog,request.candidate,request.name,request.expected_revision,request.person_id,
                                  members=request.members,group_revision=request.group_revision)
                return JSONResponse(await live_metadata(),headers={'Cache-Control':'no-store'})
            except PeopleError as error:
                raise HTTPException(409,detail={'code':error.code}) from None
            except (ValueError,sqlite3.Error):
                raise HTTPException(409,detail={'code':'identity_candidate_unavailable'}) from None
            finally:db.close()

    @router.post('/candidate-group')
    async def candidate_group(request:CandidateGroup):
        async with gate:
            db=store()
            try:
                db.edit_candidate_group(request.members,separate=request.separate)
                return JSONResponse(await live_metadata(),headers={'Cache-Control':'no-store'})
            except (ValueError,sqlite3.Error):
                raise HTTPException(409,detail={'code':'identity_candidate_unavailable'}) from None
            finally:db.close()

    @router.post('/candidate-group/undo')
    async def candidate_group_undo():
        async with gate:
            db=store()
            try:
                db.undo_candidate_group()
                return JSONResponse(await live_metadata(),headers={'Cache-Control':'no-store'})
            except (ValueError,sqlite3.Error):
                raise HTTPException(409,detail={'code':'identity_candidate_undo_unavailable'}) from None
            finally:db.close()

    @router.get("")
    async def status():
        return JSONResponse(await live_metadata(), headers={"Cache-Control": "no-store"})

    @router.post("/register")
    async def register(request: Register):
        async with gate:
            try:
                ready = await service.identity_status()
                ready = next((s for s in ready.get('sessions',[ready]) if s.get('camera')==request.camera),{})
                if (ready.get("camera") != request.camera or ready.get("session") != request.session
                        or not any(t["track"] == request.track and t.get("enroll_ready")
                                   for t in ready.get("tracks", []))):
                    raise ValueError("identity_samples_required")
                pid = catalog.register_profile(request.name, request.expected_revision, request.person_id)
                result = await service.identity_enroll(request.camera, request.session, request.track, pid)
                return JSONResponse(dict(await live_metadata(), enrollment=result), headers={"Cache-Control": "no-store"})
            except (PeopleError, ValueError, OSError) as error:
                code = error.code if isinstance(error, PeopleError) else str(error)
                if code not in {"identity_samples_required", "identity_session_changed", "identity_samples_inconsistent"}:
                    code = error.code if isinstance(error, PeopleError) else "identity_enrollment_unavailable"
                raise HTTPException(409, detail={"code": code}) from None

    @router.post("/forget")
    async def forget(request: Forget):
        db = store()
        try:
            db.forget(request.person_id)
        finally:
            db.close()
        return JSONResponse(await live_metadata(), headers={"Cache-Control": "no-store"})

    return router
