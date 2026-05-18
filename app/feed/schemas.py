from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class FeedEpisode:
    id: int
    source_video_id: str
    webpage_url: str
    title: str
    description: str | None
    published_at: datetime | None
    audio_mime_type: str | None
    audio_bytes: int | None
    transcript_ready: bool


@dataclass(slots=True)
class FeedChannel:
    display_name: str
    source_url: str
    external_channel_id: str
    episodes: list[FeedEpisode]
