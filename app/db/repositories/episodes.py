from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.common.time import utc_now
from app.db.models.episode import Episode
from app.downloader.schemas import DiscoveredEpisode, FetchedAudio


def list_episodes_for_channel(session: Session, channel_id: int) -> list[Episode]:
    stmt = (
        select(Episode)
        .where(Episode.channel_id == channel_id)
        .order_by(Episode.published_at.desc().nullslast(), Episode.id.desc())
    )
    return list(session.scalars(stmt))


def get_episode(session: Session, episode_id: int) -> Episode | None:
    return session.get(Episode, episode_id)


def list_feed_ready_episodes(session: Session, channel_id: int) -> list[Episode]:
    stmt = (
        select(Episode)
        .where(Episode.channel_id == channel_id, Episode.status == "audio_ready")
        .order_by(Episode.published_at.desc().nullslast(), Episode.id.desc())
    )
    return list(session.scalars(stmt))


def upsert_discovered_episodes(
    session: Session,
    *,
    channel_id: int,
    discovered_episodes: Iterable[DiscoveredEpisode],
) -> list[int]:
    entries = list(discovered_episodes)
    if not entries:
        return []

    video_ids = [entry.source_video_id for entry in entries]
    existing_stmt = select(Episode).where(
        Episode.channel_id == channel_id,
        Episode.source_video_id.in_(video_ids),
    )
    existing = {
        episode.source_video_id: episode
        for episode in session.scalars(existing_stmt)
    }

    now = utc_now()
    new_episode_ids: list[int] = []

    for entry in entries:
        episode = existing.get(entry.source_video_id)
        if episode is None:
            episode = Episode(
                channel_id=channel_id,
                source_video_id=entry.source_video_id,
                webpage_url=entry.webpage_url,
                title=entry.title,
                description=entry.description,
                upload_date=entry.upload_date,
                published_at=entry.published_at,
                duration_seconds=entry.duration_seconds,
                thumbnail_url=entry.thumbnail_url,
                listed_at=now,
                status="discovered",
                created_at=now,
                updated_at=now,
            )
            session.add(episode)
            session.flush()
            new_episode_ids.append(episode.id)
            continue

        episode.webpage_url = entry.webpage_url
        episode.title = entry.title
        episode.description = entry.description or episode.description
        episode.upload_date = entry.upload_date or episode.upload_date
        episode.published_at = entry.published_at or episode.published_at
        episode.duration_seconds = entry.duration_seconds or episode.duration_seconds
        episode.thumbnail_url = entry.thumbnail_url or episode.thumbnail_url
        episode.updated_at = now

    return new_episode_ids


def list_recent_unfetched_episodes(session: Session, channel_id: int, limit: int) -> list[Episode]:
    stmt = (
        select(Episode)
        .where(
            Episode.channel_id == channel_id,
            Episode.status.in_(("discovered", "failed")),
            Episode.audio_path.is_(None),
        )
        .order_by(Episode.published_at.desc().nullslast(), Episode.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def list_new_unfetched_episodes(session: Session, episode_ids: Iterable[int], limit: int | None = None) -> list[Episode]:
    ids = list(episode_ids)
    if not ids:
        return []

    order_case = case({episode_id: position for position, episode_id in enumerate(ids)}, value=Episode.id)
    stmt = (
        select(Episode)
        .where(
            Episode.id.in_(ids),
            Episode.status.in_(("discovered", "failed")),
            Episode.audio_path.is_(None),
        )
        .order_by(order_case)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def list_backlog_unfetched_episodes(
    session: Session,
    *,
    channel_id: int,
    limit: int,
    exclude_episode_ids: Iterable[int] | None = None,
) -> list[Episode]:
    excluded = tuple(exclude_episode_ids or ())
    stmt = (
        select(Episode)
        .where(
            Episode.channel_id == channel_id,
            Episode.status.in_(("discovered", "failed")),
            Episode.audio_path.is_(None),
        )
        .order_by(Episode.published_at.asc().nullsfirst(), Episode.listed_at.asc(), Episode.id.asc())
        .limit(limit)
    )
    if excluded:
        stmt = stmt.where(Episode.id.not_in(excluded))
    return list(session.scalars(stmt))


def set_episode_status(episode: Episode, status: str, *, last_error: str | None = None) -> None:
    episode.status = status
    episode.last_error = last_error
    episode.updated_at = utc_now()


def mark_audio_ready(
    episode: Episode,
    fetched_audio: FetchedAudio,
    *,
    published_audio_path: str,
) -> None:
    now = utc_now()
    episode.title = fetched_audio.title
    episode.description = fetched_audio.description
    episode.upload_date = fetched_audio.upload_date
    episode.published_at = fetched_audio.published_at
    episode.duration_seconds = fetched_audio.duration_seconds
    episode.thumbnail_url = fetched_audio.thumbnail_url
    episode.audio_path = published_audio_path
    episode.audio_mime_type = fetched_audio.audio_mime_type
    episode.audio_bytes = fetched_audio.audio_bytes
    episode.audio_fetched_at = now
    episode.status = "audio_ready"
    episode.last_error = None
    episode.updated_at = now
