from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(slots=True)
class ResolvedChannel:
    external_channel_id: str
    source_url: str
    display_name: str


@dataclass(slots=True)
class DiscoveredEpisode:
    source_video_id: str
    webpage_url: str
    title: str
    description: str | None
    upload_date: date | None
    published_at: datetime | None
    duration_seconds: int | None
    thumbnail_url: str | None


@dataclass(slots=True)
class FetchedAudio:
    title: str
    description: str | None
    upload_date: date | None
    published_at: datetime | None
    duration_seconds: int | None
    thumbnail_url: str | None
    audio_mime_type: str
    audio_bytes: int
    temp_file_path: Path

