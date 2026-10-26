"""Loopback-only synthetic fixture app. Never imports Friday's live composition."""

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import create_router
from .catalog import PeopleCatalog
from .fixture import demo_factory, seed


def create_demo():
    app = FastAPI(title="Friday People synthetic fixture")
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    catalog = PeopleCatalog(connection, _cluster_factory=demo_factory())
    seed(catalog)
    app.state.people = catalog
    app.state.connection = connection
    app.include_router(create_router(catalog))
    static = Path(__file__).with_name("static")
    app.mount("/people-static", StaticFiles(directory=static), name="people-static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-store"})

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_demo(), host="127.0.0.1", port=18764, access_log=False)
