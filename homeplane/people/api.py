"""Opt-in owner-mounted review API. No inference, enrollment or upload endpoints."""

from fastapi import APIRouter, HTTPException, Response

from .catalog import PeopleError
from .contracts import Correct, Edit, Merge, Name, PeoplePage, ReviewVoice, Split


def create_router(catalog):
    """Mount under Friday's existing authentication, origin and request guards."""
    router = APIRouter(prefix="/api/people", tags=["people"])

    def call(method, request=None):
        try:
            return method(request) if request is not None else method()
        except PeopleError as error:
            raise HTTPException(409, detail={"code": error.code}) from None

    @router.get("", response_model=PeoplePage)
    def page(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(catalog.page)

    @router.post("/name", response_model=PeoplePage)
    def name(request: Name, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(catalog.name, request)

    @router.post("/merge", response_model=PeoplePage)
    def merge(request: Merge, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(catalog.merge, request)

    @router.post("/split", response_model=PeoplePage)
    def split(request: Split, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(catalog.split, request)

    @router.post("/correct", response_model=PeoplePage)
    def correct(request: Correct, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(catalog.correct, request)

    @router.post("/undo", response_model=PeoplePage)
    def undo(request: Edit, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(catalog.undo, request)

    @router.post("/voice-review", response_model=PeoplePage)
    def voice_review(request: ReviewVoice, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(catalog.review_voice, request)

    return router
