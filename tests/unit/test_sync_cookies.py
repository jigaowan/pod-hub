from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.common.media import AUDIO_FILE_EXTENSION, episode_storage_filename
from app.common.time import utc_now
from app.config import Settings
from app.db.base import Base
from app.db.repositories import channels as channel_repo
from app.downloader.schemas import DiscoveredEpisode, FetchedAudio
from app.storage.media_store import MediaStore
from app.sync.service import SyncService


class CookieTrackingDownloader:
    def __init__(self) -> None:
        self.list_cookie_text = None
        self.fetch_cookie_text = None

    def list_channel_episodes(self, source_url: str, cookie_text: str | None = None) -> list[DiscoveredEpisode]:
        self.list_cookie_text = cookie_text
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
            )
        ]

    def fetch_episode_audio(
        self,
        webpage_url: str,
        episode_id: int,
        temp_dir: Path,
        cookie_text: str | None = None,
    ) -> FetchedAudio:
        self.fetch_cookie_text = cookie_text
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file_path = temp_dir / episode_storage_filename(webpage_url)
        temp_file_path.write_bytes(b"x" * 2048)
        return FetchedAudio(
            title="Video 1",
            description=None,
            upload_date=date(2026, 1, 1),
            published_at=utc_now(),
            duration_seconds=60,
            thumbnail_url=None,
            audio_mime_type=f"audio/{AUDIO_FILE_EXTENSION}",
            audio_bytes=temp_file_path.stat().st_size,
            temp_file_path=temp_file_path,
        )


def build_session_factory(database_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_sync_channel_passes_cookie_text_to_discovery_and_fetch(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
        FETCH_BATCH_SIZE=1,
    )
    cookie_text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"
    downloader = CookieTrackingDownloader()
    service = SyncService(
        settings,
        downloader=downloader,
        media_store=MediaStore(settings),
        session_factory=session_factory,
    )

    with session_factory() as session:
        channel = channel_repo.create_channel(
            session,
            source_kind="youtube_channel",
            external_channel_id="UC123",
            source_input="@demo",
            source_url="https://www.youtube.com/@demo",
            display_name="Demo",
            slug="demo",
            cookie_text=cookie_text,
        )
        session.commit()
        channel_id = channel.id

    service.sync_channel(channel_id)

    assert downloader.list_cookie_text == cookie_text
    assert downloader.fetch_cookie_text == cookie_text
