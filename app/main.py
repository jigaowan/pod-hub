from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.channels.service import ChannelService
from app.common.logging import configure_logging
from app.config import get_settings
from app.db.session import init_db
from app.feed.service import FeedService
from app.scheduler.jobs import create_scheduler
from app.storage.media_store import MediaStore
from app.sync.coordinator import SyncCoordinator
from app.sync.service import SyncService
from app.transcription.service import TranscriptionService
from app.web.routes import admin, feeds, media, transcripts


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    media_store = MediaStore(settings)
    channel_service = ChannelService(settings, media_store=media_store)
    sync_service = SyncService(settings, media_store=media_store)
    transcription_service = TranscriptionService(settings, media_store=media_store)
    feed_service = FeedService(settings)
    sync_coordinator = SyncCoordinator(sync_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(settings)
        scheduler = create_scheduler(settings, sync_coordinator, transcription_service)
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Pod Hub", lifespan=lifespan)
    app.state.settings = settings
    app.state.media_store = media_store
    app.state.channel_service = channel_service
    app.state.sync_service = sync_service
    app.state.transcription_service = transcription_service
    app.state.feed_service = feed_service
    app.state.sync_coordinator = sync_coordinator

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/admin/channels")

    app.include_router(admin.router)
    app.include_router(feeds.router)
    app.include_router(media.router)
    app.include_router(transcripts.router)
    return app


app = create_app()
