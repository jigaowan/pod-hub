from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.repositories import episodes as episode_repo
from app.db.repositories import transcripts as transcript_repo
from app.db.session import get_session_factory
from app.storage.media_store import MediaStore
from app.sync.service import summarize_error
from app.transcription.client import CloudflareTranscriptionClient


class TranscriptionService:
    def __init__(
        self,
        settings: Settings,
        *,
        client: CloudflareTranscriptionClient | None = None,
        media_store: MediaStore | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or CloudflareTranscriptionClient(settings)
        self.media_store = media_store or MediaStore(settings)
        self.session_factory = session_factory or get_session_factory(settings)
        self.logger = logging.getLogger("podhub.transcription")

    def process_pending(self, *, limit: int = 1) -> int:
        if not self.settings.transcription_configured:
            return 0

        processed = 0
        for _ in range(limit):
            if not self.process_next_pending():
                break
            processed += 1
        return processed

    def process_next_pending(self) -> bool:
        session = self.session_factory()
        try:
            episodes = transcript_repo.list_pending_transcript_episodes(session, limit=1)
            if not episodes:
                return False

            episode = episodes[0]
            transcript_repo.mark_transcript_processing(episode)
            session.commit()
            episode_id = episode.id
            audio_path = Path(episode.audio_path or "")
            channel_slug = episode.channel.slug
            source_video_id = episode.source_video_id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        try:
            vtt = self.client.transcribe_to_vtt(audio_path)
            transcript_path = self.media_store.publish_transcript(vtt, channel_slug, source_video_id)
            transcript_bytes = transcript_path.stat().st_size
        except Exception as exc:
            self._mark_failed(episode_id, summarize_error(exc))
            self.logger.exception("episode transcription failed episode_id=%s", episode_id)
            return True

        session = self.session_factory()
        try:
            episode = episode_repo.get_episode(session, episode_id)
            if episode is not None:
                transcript_repo.mark_transcript_ready(episode, transcript_path, transcript_bytes)
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _mark_failed(self, episode_id: int, error_message: str) -> None:
        session = self.session_factory()
        try:
            episode = episode_repo.get_episode(session, episode_id)
            if episode is not None:
                transcript_repo.mark_transcript_failed(episode, error_message)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
