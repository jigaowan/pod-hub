from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.time import utc_now
from app.db.models.episode import Episode
from app.domain.status import EpisodeStatus, TranscriptStatus


def mark_transcript_pending(episode: Episode) -> None:
    episode.transcript_status = TranscriptStatus.PENDING
    episode.transcript_error = None
    episode.updated_at = utc_now()


def list_pending_transcript_episodes(session: Session, limit: int) -> list[Episode]:
    stmt = (
        select(Episode)
        .where(
            Episode.status == EpisodeStatus.AUDIO_READY,
            Episode.audio_path.is_not(None),
            Episode.transcript_status == TranscriptStatus.PENDING,
        )
        .order_by(Episode.audio_fetched_at.asc().nullsfirst(), Episode.id.asc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def mark_transcript_processing(episode: Episode) -> None:
    episode.transcript_status = TranscriptStatus.PROCESSING
    episode.transcript_error = None
    episode.updated_at = utc_now()


def mark_transcript_ready(episode: Episode, transcript_path: Path, transcript_bytes: int) -> None:
    now = utc_now()
    episode.transcript_status = TranscriptStatus.READY
    episode.transcript_path = str(transcript_path)
    episode.transcript_mime_type = "text/vtt"
    episode.transcript_bytes = transcript_bytes
    episode.transcript_generated_at = now
    episode.transcript_error = None
    episode.updated_at = now


def mark_transcript_failed(episode: Episode, error_message: str) -> None:
    episode.transcript_status = TranscriptStatus.FAILED
    episode.transcript_error = error_message
    episode.updated_at = utc_now()
