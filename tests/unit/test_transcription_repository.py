from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models.channel import Channel
from app.db.models.episode import Episode
from app.db.repositories import transcripts as transcript_repo


def build_session_factory(database_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_episode_transcript_status_flow(tmp_path: Path) -> None:
    session_factory = build_session_factory(tmp_path / "podhub.db")
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
        episode = Episode(
            channel_id=channel.id,
            source_video_id="video-ready",
            webpage_url="https://www.youtube.com/watch?v=video-ready",
            title="Ready Episode",
            listed_at=datetime.now(UTC),
            status="audio_ready",
            audio_path="/tmp/1.m4a",
            audio_fetched_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(episode)
        session.flush()

        transcript_repo.mark_transcript_pending(episode)
        pending = transcript_repo.list_pending_transcript_episodes(session, limit=1)
        assert pending == [episode]

        transcript_repo.mark_transcript_processing(episode)
        assert episode.transcript_status == "processing"

        transcript_repo.mark_transcript_ready(episode, tmp_path / "video-ready.vtt", 12)
        assert episode.transcript_status == "ready"
        assert episode.transcript_path == str(tmp_path / "video-ready.vtt")
        assert episode.transcript_mime_type == "text/vtt"
        assert episode.transcript_bytes == 12

        transcript_repo.mark_transcript_failed(episode, "failed")
        assert episode.transcript_status == "failed"
        assert episode.transcript_error == "failed"
