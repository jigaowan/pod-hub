from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.time import utc_now
from app.db.models.sync_run import SyncRun


def create_sync_run(session: Session, channel_id: int) -> SyncRun:
    now = utc_now()
    sync_run = SyncRun(
        channel_id=channel_id,
        status="running",
        started_at=now,
        created_at=now,
        discovered_count=0,
        queued_count=0,
        fetched_count=0,
        failed_count=0,
    )
    session.add(sync_run)
    session.flush()
    return sync_run


def finish_sync_run(
    sync_run: SyncRun,
    *,
    status: str,
    discovered_count: int,
    queued_count: int,
    fetched_count: int,
    failed_count: int,
    error_message: str | None = None,
) -> None:
    sync_run.status = status
    sync_run.finished_at = utc_now()
    sync_run.discovered_count = discovered_count
    sync_run.queued_count = queued_count
    sync_run.fetched_count = fetched_count
    sync_run.failed_count = failed_count
    sync_run.error_message = error_message


def create_skipped_sync_run(session: Session, channel_id: int, reason: str) -> SyncRun:
    now = utc_now()
    sync_run = SyncRun(
        channel_id=channel_id,
        status="skipped",
        started_at=now,
        finished_at=now,
        created_at=now,
        error_message=reason,
        discovered_count=0,
        queued_count=0,
        fetched_count=0,
        failed_count=0,
    )
    session.add(sync_run)
    session.flush()
    return sync_run


def get_latest_sync_run(session: Session, channel_id: int) -> SyncRun | None:
    stmt = (
        select(SyncRun)
        .where(SyncRun.channel_id == channel_id)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    )
    return session.scalar(stmt)

