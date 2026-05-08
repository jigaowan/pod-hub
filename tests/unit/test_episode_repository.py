from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models.channel import Channel
from app.db.models.episode import Episode
from app.db.repositories import episodes as episode_repo
from app.downloader.schemas import DiscoveredEpisode


def build_session_factory(database_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def discovered(video_id: str, source_position: int) -> DiscoveredEpisode:
    return DiscoveredEpisode(
        source_video_id=video_id,
        source_position=source_position,
        webpage_url=f"https://www.youtube.com/watch?v={video_id}",
        title=video_id,
        description=None,
        upload_date=None,
        published_at=None,
        duration_seconds=None,
        thumbnail_url=None,
    )


def test_upsert_discovered_episodes_updates_youtube_positions_without_duplicates(tmp_path: Path) -> None:
    session_factory = build_session_factory(tmp_path / "podhub.db")

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

        first_ids = episode_repo.upsert_discovered_episodes(
            session,
            channel_id=channel.id,
            discovered_episodes=[
                discovered("video-a", 1),
                discovered("video-b", 2),
            ],
        )
        session.commit()

        second_ids = episode_repo.upsert_discovered_episodes(
            session,
            channel_id=channel.id,
            discovered_episodes=[
                discovered("video-b", 1),
                discovered("video-a", 2),
            ],
        )
        session.commit()

        episodes = list(session.scalars(select(Episode).order_by(Episode.source_video_id)))

    assert len(first_ids) == 2
    assert second_ids == []
    assert [(episode.source_video_id, episode.source_position) for episode in episodes] == [
        ("video-a", 2),
        ("video-b", 1),
    ]
