from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.common.errors import ValidationError
from app.config import Settings
from app.db.base import Base
from app.db.models.channel import Channel
from app.downloader.schemas import ResolvedSource
from app.storage.media_store import MediaStore
from app.sync.service import SyncService


class PlaylistOnlyDownloader:
    def __init__(self) -> None:
        self.called = False

    def resolve_playlist(self, source_url: str) -> ResolvedSource:
        self.called = True
        return ResolvedSource(
            source_kind="youtube_playlist",
            external_id="PL123",
            source_url=source_url,
            display_name="Demo Playlist",
            cover_url=None,
        )

    def resolve_channel(self, source_input: str) -> ResolvedSource:
        raise AssertionError("resolve_channel should not be called for playlist URLs")


class RejectingDownloader:
    def resolve_playlist(self, source_url: str) -> ResolvedSource:
        raise AssertionError("resolve_playlist should not be called for non-standard playlist URLs")

    def resolve_channel(self, source_input: str) -> ResolvedSource:
        raise AssertionError("resolve_channel should not be called for non-standard playlist URLs")


def build_session_factory(database_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_register_playlist_source_kind(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    downloader = PlaylistOnlyDownloader()
    service = SyncService(
        settings,
        downloader=downloader,
        media_store=MediaStore(settings),
        session_factory=session_factory,
    )

    channel = service.register_channel("https://www.youtube.com/playlist?list=PL123")

    assert downloader.called is True
    with session_factory() as session:
        stored = session.get(Channel, channel.id)
        assert stored is not None
        assert stored.source_kind == "youtube_playlist"
        assert stored.external_channel_id == "PL123"
        assert stored.slug == "demo-playlist"


def test_register_playlist_uses_unicode_slug(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )

    class UnicodePlaylistDownloader:
        def resolve_playlist(self, source_url: str) -> ResolvedSource:
            return ResolvedSource(
                source_kind="youtube_playlist",
                external_id="PLUNICODE",
                source_url=source_url,
                display_name="中文播放列表",
                cover_url=None,
            )

        def resolve_channel(self, source_input: str) -> ResolvedSource:
            raise AssertionError("resolve_channel should not be called for playlist URLs")

    service = SyncService(
        settings,
        downloader=UnicodePlaylistDownloader(),
        media_store=MediaStore(settings),
        session_factory=session_factory,
    )

    channel = service.register_channel("https://www.youtube.com/playlist?list=PLUNICODE")

    with session_factory() as session:
        stored = session.get(Channel, channel.id)
        assert stored is not None
        assert stored.slug == "中文播放列表"


def test_register_playlist_uses_external_id_when_name_has_no_slug_chars(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )

    class SymbolOnlyPlaylistDownloader:
        def resolve_playlist(self, source_url: str) -> ResolvedSource:
            return ResolvedSource(
                source_kind="youtube_playlist",
                external_id="PLFALLBACK123",
                source_url=source_url,
                display_name="!!!",
                cover_url=None,
            )

        def resolve_channel(self, source_input: str) -> ResolvedSource:
            raise AssertionError("resolve_channel should not be called for playlist URLs")

    service = SyncService(
        settings,
        downloader=SymbolOnlyPlaylistDownloader(),
        media_store=MediaStore(settings),
        session_factory=session_factory,
    )

    channel = service.register_channel("https://www.youtube.com/playlist?list=PLFALLBACK123")

    with session_factory() as session:
        stored = session.get(Channel, channel.id)
        assert stored is not None
        assert stored.slug == "plfallback123"


def test_register_rejects_non_standard_playlist_url(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    session_factory = build_session_factory(database_path)
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    service = SyncService(
        settings,
        downloader=RejectingDownloader(),
        media_store=MediaStore(settings),
        session_factory=session_factory,
    )

    with pytest.raises(ValidationError, match="standard playlist URL"):
        service.register_channel("https://www.youtube.com/watch?v=abc&list=PL123")
