from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.common.errors import DownloaderError
from app.common.time import utc_now
from app.config import Settings
from app.db.base import Base
from app.db.models.sync_run import SyncRun
from app.db.repositories import channels as channel_repo
from app.downloader.schemas import DiscoveredEpisode, FetchedAudio
from app.storage.media_store import MediaStore
from app.sync.service import SyncService


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def notify_sync_errors(self, *, channel_name: str, errors: list[str]) -> None:
        self.calls.append((channel_name, errors))


def build_session_factory(database_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def build_settings(tmp_path: Path, database_path: Path, *, fetch_batch_size: int = 2) -> Settings:
    return Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
        FETCH_BATCH_SIZE=fetch_batch_size,
    )


def create_channel(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        channel = channel_repo.create_channel(
            session,
            source_kind="youtube_channel",
            external_channel_id="UC123",
            source_input="@demo",
            source_url="https://www.youtube.com/@demo",
            display_name="Demo",
            slug="demo",
        )
        session.commit()
        return channel.id


def make_downloader_error(message: str) -> DownloaderError:
    error = DownloaderError(f"download failed: {message}")
    error.stderr = message
    error.stdout = ""
    error.returncode = 1
    error.command_type = "download_audio"
    return error


class ListFailingDownloader:
    def list_channel_episodes(self, source_url: str, cookie_text: str | None = None) -> list[DiscoveredEpisode]:
        raise make_downloader_error("ERROR: [youtube] UC123: Unable to extract yt initial data; please update yt-dlp")


class MultipleFailingDownloader:
    def list_channel_episodes(self, source_url: str, cookie_text: str | None = None) -> list[DiscoveredEpisode]:
        return [
            DiscoveredEpisode(
                source_video_id="video-1",
                source_position=1,
                webpage_url="https://www.youtube.com/watch?v=video-1",
                title="Video 1",
                description=None,
                upload_date=date(2026, 1, 1),
                published_at=utc_now(),
                duration_seconds=60,
                thumbnail_url=None,
            ),
            DiscoveredEpisode(
                source_video_id="video-2",
                source_position=2,
                webpage_url="https://www.youtube.com/watch?v=video-2",
                title="Video 2",
                description=None,
                upload_date=date(2026, 1, 2),
                published_at=utc_now(),
                duration_seconds=60,
                thumbnail_url=None,
            ),
        ]

    def fetch_episode_audio(
        self,
        webpage_url: str,
        episode_id: int,
        temp_dir: Path,
        cookie_text: str | None = None,
    ) -> FetchedAudio:
        if "video-1" in webpage_url:
            raise make_downloader_error("ERROR: Sign in to confirm you are not a bot")
        raise make_downloader_error("ERROR: [youtube] video-2: Private video. Sign in if you have been granted access")


class ResolvingFailureDownloader:
    def resolve_channel(self, source_input: str, cookie_text: str | None = None):
        raise make_downloader_error("ERROR: Source unavailable")


def test_channel_list_failure_sends_one_notification(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = build_settings(tmp_path, database_path)
    channel_id = create_channel(session_factory)
    notifier = RecordingNotifier()
    service = SyncService(
        settings,
        downloader=ListFailingDownloader(),
        media_store=MediaStore(settings),
        session_factory=session_factory,
        notifier=notifier,
    )

    service.sync_channel(channel_id, notify=True)

    assert notifier.calls == [("Demo", ["Unable to extract yt initial data; please update yt-dlp"])]
    with session_factory() as session:
        sync_run = session.scalar(select(SyncRun))
        assert sync_run is not None
        assert sync_run.status == "failed"


def test_episode_failures_are_summarized_in_one_notification(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = build_settings(tmp_path, database_path)
    channel_id = create_channel(session_factory)
    notifier = RecordingNotifier()
    service = SyncService(
        settings,
        downloader=MultipleFailingDownloader(),
        media_store=MediaStore(settings),
        session_factory=session_factory,
        notifier=notifier,
    )

    service.sync_channel(channel_id, notify=True)

    assert notifier.calls == [
        (
            "Demo",
            [
                "Private video. Sign in if you have been granted access",
                "Sign in to confirm you are not a bot",
            ],
        )
    ]
    with session_factory() as session:
        sync_run = session.scalar(select(SyncRun))
        assert sync_run is not None
        assert sync_run.status == "succeeded"
        assert sync_run.failed_count == 2


def test_register_channel_failure_does_not_notify(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = build_settings(tmp_path, database_path)
    notifier = RecordingNotifier()
    service = SyncService(
        settings,
        downloader=ResolvingFailureDownloader(),
        media_store=MediaStore(settings),
        session_factory=session_factory,
        notifier=notifier,
    )

    with pytest.raises(DownloaderError):
        service.register_channel("@demo")

    assert notifier.calls == []
