from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.db.models.channel import Channel
from app.db.models.episode import Episode
from app.db.models.sync_run import SyncRun


@dataclass(slots=True)
class ChannelListItemView:
    id: int
    display_name: str
    slug: str
    status: str
    last_synced_at: datetime | None
    last_error: str | None


@dataclass(slots=True)
class EpisodeItemView:
    id: int
    title: str
    status: str
    published_at: datetime | None
    has_audio: bool
    last_error: str | None


@dataclass(slots=True)
class SyncRunSummaryView:
    status: str
    started_at: datetime
    finished_at: datetime | None
    discovered_count: int
    queued_count: int
    fetched_count: int
    failed_count: int
    error_message: str | None


def to_channel_list_items(channels: list[Channel]) -> list[ChannelListItemView]:
    return [
        ChannelListItemView(
            id=channel.id,
            display_name=channel.display_name,
            slug=channel.slug,
            status=channel.status,
            last_synced_at=channel.last_synced_at,
            last_error=channel.last_error,
        )
        for channel in channels
    ]


def to_episode_items(episodes: list[Episode]) -> list[EpisodeItemView]:
    return [
        EpisodeItemView(
            id=episode.id,
            title=episode.title,
            status=episode.status,
            published_at=episode.published_at,
            has_audio=episode.status == "audio_ready" and bool(episode.audio_path),
            last_error=episode.last_error,
        )
        for episode in episodes
    ]


def to_sync_run_summary(sync_run: SyncRun | None) -> SyncRunSummaryView | None:
    if sync_run is None:
        return None

    return SyncRunSummaryView(
        status=sync_run.status,
        started_at=sync_run.started_at,
        finished_at=sync_run.finished_at,
        discovered_count=sync_run.discovered_count,
        queued_count=sync_run.queued_count,
        fetched_count=sync_run.fetched_count,
        failed_count=sync_run.failed_count,
        error_message=sync_run.error_message,
    )

