from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import Settings
from app.sync.coordinator import SyncCoordinator
from app.transcription.service import TranscriptionService


def create_scheduler(
    settings: Settings,
    coordinator: SyncCoordinator,
    transcription_service: TranscriptionService | None = None,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        coordinator.run_scheduled_sync,
        trigger="interval",
        minutes=settings.sync_interval,
        id="channel-sync",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    if transcription_service is not None:
        scheduler.add_job(
            transcription_service.process_pending,
            trigger="interval",
            minutes=1,
            id="transcription-worker",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    scheduler.start()
    return scheduler
