from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.common.time import utc_now
from app.config import Settings
from app.db.base import Base
from app.db.models.channel import Channel
from app.db.models.episode import Episode
from app.db.models.sync_run import SyncRun
from app.db.repositories import channels as channel_repo
from app.storage.media_store import MediaStore
from app.sync.service import SyncService


def build_session_factory(database_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_delete_channel_removes_project_records_but_keeps_media_files(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    media_store = MediaStore(settings)
    service = SyncService(
        settings,
        media_store=media_store,
        session_factory=session_factory,
    )

    media_file = settings.media_dir / "demo-channel" / "episode.mp3"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"demo audio")
    cover_file = settings.media_dir / "demo-channel" / "cover.jpg"
    cover_file.write_bytes(b"cover")

    now = utc_now()
    with session_factory() as session:
        channel = channel_repo.create_channel(
            session,
            source_kind="youtube_channel",
            external_channel_id="UC123",
            source_input="@demo",
            source_url="https://www.youtube.com/@demo",
            display_name="Demo Channel",
            slug="demo-channel",
            status="active",
        )
        session.add(
            Episode(
                channel_id=channel.id,
                source_video_id="video-1",
                webpage_url="https://www.youtube.com/watch?v=video-1",
                title="Episode 1",
                description=None,
                upload_date=None,
                published_at=now,
                duration_seconds=120,
                thumbnail_url=None,
                audio_path=str(media_file),
                audio_mime_type="audio/mpeg",
                audio_bytes=123,
                audio_fetched_at=now,
                listed_at=now,
                status="audio_ready",
                last_error=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SyncRun(
                channel_id=channel.id,
                status="succeeded",
                started_at=now,
                finished_at=now,
                discovered_count=1,
                queued_count=1,
                fetched_count=1,
                failed_count=0,
                error_message=None,
                created_at=now,
            )
        )
        session.commit()

    deleted = service.delete_channel("demo-channel")

    assert deleted is True
    with session_factory() as session:
        assert session.scalar(select(Channel).where(Channel.slug == "demo-channel")) is None
        assert session.scalar(select(func.count()).select_from(Episode)) == 0
        assert session.scalar(select(func.count()).select_from(SyncRun)) == 0
    assert media_file.exists()
    assert cover_file.exists()


def test_delete_channel_returns_false_when_slug_is_missing(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    service = SyncService(
        settings,
        media_store=MediaStore(settings),
        session_factory=session_factory,
    )

    assert service.delete_channel("missing-channel") is False
