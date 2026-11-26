from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles

from podhub.config import Config, load_config
from podhub.core.scheduler import Scheduler
from podhub.core.services import NotFound, Services
from podhub.web.routes import admin, public


def create_app(
    config: Config | None = None, *, services: Services | None = None, run_scheduler: bool = True,
) -> FastAPI:
    services = services if services is not None else Services(config or load_config())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services.initialize()
        if not run_scheduler:
            yield
            return
        with services.service_lock():
            scheduler = Scheduler(services)
            scheduler.recover()
            task = asyncio.create_task(scheduler.run(), name="podhub:scheduler")
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services
    app.include_router(public.router)
    app.include_router(admin.router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
    for error in (ValueError, NotFound, sqlite3.IntegrityError):
        app.add_exception_handler(error, admin.user_error)

    @app.middleware("http")
    async def public_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/feeds/", "/media/", "/covers/", "/artifacts/")):
            response.headers["X-Robots-Tag"] = "noindex"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    def index() -> Response:
        return RedirectResponse("/admin")

    @app.get("/healthz")
    def health() -> Response:
        try:
            with services.db.connect() as conn:
                migrated = conn.execute("SELECT 1 FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.Error:
            migrated = None
        if migrated:
            return Response("ok\n", media_type="text/plain")
        return Response("unavailable\n", status_code=503, media_type="text/plain")

    return app
