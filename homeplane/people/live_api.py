"""Metadata-only registration/control, behind the Console's existing guards."""

import asyncio
import sqlite3

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


def create_router(catalog, service):
    router = APIRouter(prefix="/api/people/live")
    gate = asyncio.Lock()

    def store():
        from homeplane.embodied.paths import PRIVATE
        return IdentityStore(PRIVATE / "identities.sqlite")

    def metadata():
        db = store()
        try:
            profiles, presence = db.registered(), db.presence()
            portraits={p for p in profiles if db.portrait(p) is not None}
            candidates=db.candidates()
        finally:
            db.close()
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
                    observations=presence, candidates=candidates, cameras=service.cameras(), policy=POLICY,
                    experimental=True, security_authentication=False, action_authorized=False)

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
                db.name_candidate(catalog,request.candidate,request.name,request.expected_revision,request.person_id)
                return JSONResponse(metadata(),headers={'Cache-Control':'no-store'})
            except PeopleError as error:
                raise HTTPException(409,detail={'code':error.code}) from None
            except (ValueError,sqlite3.Error):
                raise HTTPException(409,detail={'code':'identity_candidate_unavailable'}) from None
            finally:db.close()

    @router.get("")
    async def status():
        result = metadata()
        try:
            result["recognition"] = await service.identity_status()
        except (ValueError, OSError):
            result["recognition"] = dict(state="stopped", tracks=[])
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

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
                return JSONResponse(dict(metadata(), enrollment=result), headers={"Cache-Control": "no-store"})
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
        return JSONResponse(metadata(), headers={"Cache-Control": "no-store"})

    return router
