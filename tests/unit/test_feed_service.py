from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models.channel import Channel
from app.db.models.episode import Episode
from app.db.repositories import settings as settings_repo
from app.feed.service import FeedService


def item_titles(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    return [item.findtext("title") or "" for item in root.findall("./channel/item")]


def test_feed_service_only_renders_audio_ready_items(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    with session_factory() as session:
        channel = Channel(
            source_kind="youtube_channel",
            external_channel_id="UC123",
            source_input="@demo",
            source_url="https://www.youtube.com/@demo",
            display_name="Demo Channel",
            slug="demo-channel",
            status="active",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(channel)
        session.flush()
        session.add_all(
            [
                Episode(
                    channel_id=channel.id,
                    source_video_id="video-ready",
                    webpage_url="https://www.youtube.com/watch?v=video-ready",
                    title="Ready Episode",
                    description="Ready",
                    published_at=datetime(2025, 1, 1, tzinfo=UTC),
                    listed_at=datetime.now(UTC),
                    status="audio_ready",
                    audio_path="/tmp/1.m4a",
                    audio_mime_type="audio/mp4",
                    audio_bytes=123,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ),
                Episode(
                    channel_id=channel.id,
                    source_video_id="video-discovered",
                    webpage_url="https://www.youtube.com/watch?v=video-discovered",
                    title="Discovered Episode",
                    description="Hidden",
                    listed_at=datetime.now(UTC),
                    status="discovered",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ),
            ]
        )
        session.commit()

    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    service = FeedService(settings, session_factory=session_factory)
    xml = service.render_channel_feed("demo-channel")

    assert xml is not None
    assert "Ready Episode" in xml
    assert "Discovered Episode" not in xml
    assert "https://podhub.example.com/media/1.m4a" in xml
    assert 'type="audio/mp4"' in xml
    assert '<guid isPermaLink="false">podhub:youtube:UC123:video-ready</guid>' in xml


def test_feed_service_uses_runtime_base_url_override(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    with session_factory() as session:
        channel = Channel(
            source_kind="youtube_channel",
            external_channel_id="UC123",
            source_input="@demo",
            source_url="https://www.youtube.com/@demo",
            display_name="Demo Channel",
            slug="demo-channel",
            status="active",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(channel)
        session.flush()
        session.add(
            Episode(
                channel_id=channel.id,
                source_video_id="video-ready",
                webpage_url="https://www.youtube.com/watch?v=video-ready",
                title="Ready Episode",
                listed_at=datetime.now(UTC),
                status="audio_ready",
                audio_path="/tmp/1.m4a",
                audio_mime_type="audio/mp4",
                audio_bytes=123,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        settings_repo.set_setting(session, "BASE_URL", "https://db.example.com")
        session.commit()

    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    service = FeedService(settings, session_factory=session_factory)

    xml = service.render_channel_feed("demo-channel")

    assert xml is not None
    assert "https://db.example.com/media/1.m4a" in xml


def test_feed_service_renders_podcast_transcript_for_ready_vtt(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    with session_factory() as session:
        channel = Channel(
            source_kind="youtube_channel",
            external_channel_id="UC123",
            source_input="@demo",
            source_url="https://www.youtube.com/@demo",
            display_name="Demo Channel",
            slug="demo-channel",
            status="active",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(channel)
        session.flush()
        session.add(
            Episode(
                channel_id=channel.id,
                source_video_id="video-ready",
                webpage_url="https://www.youtube.com/watch?v=video-ready",
                title="Ready Episode",
                description="Ready",
                published_at=datetime(2025, 1, 1, tzinfo=UTC),
                listed_at=datetime.now(UTC),
                status="audio_ready",
                audio_path="/tmp/1.m4a",
                audio_mime_type="audio/mp4",
                audio_bytes=123,
                transcript_status="ready",
                transcript_path="/tmp/1.vtt",
                transcript_mime_type="text/vtt",
                transcript_bytes=45,
                transcript_generated_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()

    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    service = FeedService(settings, session_factory=session_factory)
    xml = service.render_channel_feed("demo-channel")

    assert xml is not None
    assert 'xmlns:podcast="https://podcastindex.org/namespace/1.0"' in xml
    assert '<podcast:transcript url="https://podhub.example.com/transcripts/1.vtt" type="text/vtt" rel="captions" />' in xml


def test_feed_service_orders_playlist_items_by_youtube_position(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    with session_factory() as session:
        channel = Channel(
            source_kind="youtube_playlist",
            external_channel_id="PL123",
            source_input="https://www.youtube.com/playlist?list=PL123",
            source_url="https://www.youtube.com/playlist?list=PL123",
            display_name="Demo Playlist",
            slug="demo-playlist",
            status="active",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(channel)
        session.flush()
        session.add_all(
            [
                Episode(
                    channel_id=channel.id,
                    source_video_id="video-third",
                    source_position=3,
                    webpage_url="https://www.youtube.com/watch?v=video-third",
                    title="Third By Playlist",
                    description=None,
                    published_at=datetime(2026, 1, 3, tzinfo=UTC),
                    listed_at=datetime.now(UTC),
                    status="audio_ready",
                    audio_path="/tmp/3.m4a",
                    audio_mime_type="audio/mp4",
                    audio_bytes=123,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ),
                Episode(
                    channel_id=channel.id,
                    source_video_id="video-first",
                    source_position=1,
                    webpage_url="https://www.youtube.com/watch?v=video-first",
                    title="First By Playlist",
                    description=None,
                    published_at=datetime(2026, 1, 1, tzinfo=UTC),
                    listed_at=datetime.now(UTC),
                    status="audio_ready",
                    audio_path="/tmp/1.m4a",
                    audio_mime_type="audio/mp4",
                    audio_bytes=123,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ),
                Episode(
                    channel_id=channel.id,
                    source_video_id="video-second",
                    source_position=2,
                    webpage_url="https://www.youtube.com/watch?v=video-second",
                    title="Second By Playlist",
                    description=None,
                    published_at=datetime(2026, 1, 2, tzinfo=UTC),
                    listed_at=datetime.now(UTC),
                    status="audio_ready",
                    audio_path="/tmp/2.m4a",
                    audio_mime_type="audio/mp4",
                    audio_bytes=123,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ),
            ]
        )
        session.commit()

    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    service = FeedService(settings, session_factory=session_factory)
    xml = service.render_channel_feed("demo-playlist")

    assert xml is not None
    assert item_titles(xml) == [
        "First By Playlist",
        "Second By Playlist",
        "Third By Playlist",
    ]


def test_feed_service_keeps_channel_items_ordered_by_publish_date(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    with session_factory() as session:
        channel = Channel(
            source_kind="youtube_channel",
            external_channel_id="UC123",
            source_input="@demo",
            source_url="https://www.youtube.com/@demo",
            display_name="Demo Channel",
            slug="demo-channel",
            status="active",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(channel)
        session.flush()
        session.add_all(
            [
                Episode(
                    channel_id=channel.id,
                    source_video_id="video-first-position",
                    source_position=1,
                    webpage_url="https://www.youtube.com/watch?v=video-first-position",
                    title="Older But First Position",
                    description=None,
                    published_at=datetime(2026, 1, 1, tzinfo=UTC),
                    listed_at=datetime.now(UTC),
                    status="audio_ready",
                    audio_path="/tmp/1.m4a",
                    audio_mime_type="audio/mp4",
                    audio_bytes=123,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ),
                Episode(
                    channel_id=channel.id,
                    source_video_id="video-second-position",
                    source_position=2,
                    webpage_url="https://www.youtube.com/watch?v=video-second-position",
                    title="Newer But Second Position",
                    description=None,
                    published_at=datetime(2026, 1, 2, tzinfo=UTC),
                    listed_at=datetime.now(UTC),
                    status="audio_ready",
                    audio_path="/tmp/2.m4a",
                    audio_mime_type="audio/mp4",
                    audio_bytes=123,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ),
            ]
        )
        session.commit()

    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    service = FeedService(settings, session_factory=session_factory)
    xml = service.render_channel_feed("demo-channel")

    assert xml is not None
    assert item_titles(xml) == [
        "Newer But Second Position",
        "Older But First Position",
    ]
