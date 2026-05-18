from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.common.media import episode_storage_filename
from app.config import Settings
from app.db.base import Base
from app.db.models.episode import Episode
from app.db.repositories import channels as channel_repo
from app.downloader.schemas import DiscoveredEpisode, FetchedAudio
from app.storage.media_store import MediaStore
from app.sync.service import SyncService


def build_session_factory(database_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


class SuccessfulDownloader:
    def list_channel_episodes(self, source_url: str, cookie_text: str | None = None) -> list[DiscoveredEpisode]:
        return [
            DiscoveredEpisode(
                source_video_id="video-ready",
                source_position=1,
                webpage_url="https://www.youtube.com/watch?v=video-ready",
                title="Video Ready",
                description=None,
                upload_date=date(2026, 1, 1),
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
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
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file_path = temp_dir / episode_storage_filename(webpage_url)
        temp_file_path.write_bytes(b"audio")
        return FetchedAudio(
            title="Video Ready",
            description=None,
            upload_date=date(2026, 1, 1),
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            duration_seconds=60,
            thumbnail_url=None,
            audio_mime_type="audio/mp4",
            audio_bytes=5,
            temp_file_path=temp_file_path,
        )


def test_sync_queues_transcription_without_waiting_for_cloudflare(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
        FETCH_BATCH_SIZE=1,
        CLOUDFLARE_AI_GATEWAY_URL="https://gateway.ai.cloudflare.com/v1/account/default/compat/chat/completions",
        CLOUDFLARE_API_TOKEN="secret",
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
            transcription_enabled=True,
        )
        session.commit()
        channel_id = channel.id

    service = SyncService(
        settings,
        downloader=SuccessfulDownloader(),
        media_store=MediaStore(settings),
        session_factory=session_factory,
    )

    service.sync_channel(channel_id)

    with session_factory() as session:
        episode = session.scalar(select(Episode))
        assert episode is not None
        assert episode.status == "audio_ready"
        assert episode.transcript_status == "pending"
        assert episode.audio_path is not None
