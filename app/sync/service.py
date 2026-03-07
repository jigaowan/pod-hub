from __future__ import annotations

import logging

from sqlalchemy.orm import Session, sessionmaker

from app.common.errors import ValidationError
from app.common.time import utc_now
from app.config import Settings
from app.db.repositories import channels as channel_repo
from app.db.repositories import episodes as episode_repo
from app.db.repositories import sync_runs as sync_run_repo
from app.db.session import get_session_factory
from app.downloader.client import YtDlpClient
from app.storage.media_store import MediaStore
from app.sync.planner import plan_initial_fetches, plan_polling_fetches

MAX_ERROR_LENGTH = 4096


def summarize_error(error: Exception) -> str:
    return " ".join(str(error).split())[:MAX_ERROR_LENGTH]


class SyncService:
    def __init__(
        self,
        settings: Settings,
        *,
        downloader: YtDlpClient | None = None,
        media_store: MediaStore | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings
        self.downloader = downloader or YtDlpClient(settings)
        self.media_store = media_store or MediaStore(settings)
        self.session_factory = session_factory or get_session_factory(settings)
        self.logger = logging.getLogger("podhub.sync")

    def register_channel(self, source_input: str):
        cleaned = source_input.strip()
        if not cleaned:
            raise ValidationError("source_input is required")

        resolved = self.downloader.resolve_channel(cleaned)

        session = self.session_factory()
        try:
            existing = channel_repo.get_channel_by_external_id(session, resolved.external_channel_id)
            if existing is not None:
                raise ValidationError("This channel is already registered")

            slug = channel_repo.allocate_slug(session, resolved.display_name)
            channel = channel_repo.create_channel(
                session,
                external_channel_id=resolved.external_channel_id,
                source_input=cleaned,
                source_url=resolved.source_url,
                display_name=resolved.display_name,
                slug=slug,
            )
            session.commit()
            return channel
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def sync_all_channels(self) -> None:
        session = self.session_factory()
        try:
            channel_ids = channel_repo.list_syncable_channel_ids(session)
        finally:
            session.close()

        for channel_id in channel_ids:
            self.sync_channel(channel_id)

    def record_global_skip(self, reason: str) -> None:
        session = self.session_factory()
        try:
            channel_ids = channel_repo.list_syncable_channel_ids(session)
            for channel_id in channel_ids:
                sync_run_repo.create_skipped_sync_run(session, channel_id, reason)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def sync_channel(self, channel_id: int) -> None:
        session = self.session_factory()
        discovered_count = 0
        queued_count = 0
        fetched_count = 0
        failed_count = 0
        sync_run = None

        try:
            channel = channel_repo.get_channel(session, channel_id)
            if channel is None:
                return

            is_initial_sync = channel.last_synced_at is None
            channel_repo.mark_sync_started(channel)
            sync_run = sync_run_repo.create_sync_run(session, channel.id)
            session.commit()

            discovered_entries = self.downloader.list_channel_episodes(channel.source_url)
            new_episode_ids = episode_repo.upsert_discovered_episodes(
                session,
                channel_id=channel.id,
                discovered_episodes=discovered_entries,
            )
            discovered_count = len(new_episode_ids)

            if is_initial_sync:
                candidates = episode_repo.list_recent_unfetched_episodes(
                    session,
                    channel.id,
                    self.settings.fetch_batch_size,
                )
                selected_episodes = plan_initial_fetches(candidates, self.settings.fetch_batch_size)
            else:
                new_candidates = episode_repo.list_new_unfetched_episodes(
                    session,
                    new_episode_ids,
                    limit=self.settings.fetch_batch_size,
                )
                remaining_slots = max(self.settings.fetch_batch_size - len(new_candidates), 0)
                backlog_candidates = []
                if remaining_slots > 0:
                    backlog_candidates = episode_repo.list_backlog_unfetched_episodes(
                        session,
                        channel_id=channel.id,
                        limit=remaining_slots,
                        exclude_episode_ids=[episode.id for episode in new_candidates],
                    )
                selected_episodes = plan_polling_fetches(
                    new_candidates=new_candidates,
                    backlog_candidates=backlog_candidates,
                    batch_size=self.settings.fetch_batch_size,
                )

            for episode in selected_episodes:
                episode_repo.set_episode_status(episode, "queued")
            queued_count = len(selected_episodes)
            session.commit()

            for episode in selected_episodes:
                temp_dir = self.media_store.temp_dir_for_episode(episode.id)
                try:
                    episode_repo.set_episode_status(episode, "fetching")
                    session.commit()

                    fetched_audio = self.downloader.fetch_episode_audio(episode.webpage_url, episode.id, temp_dir)
                    published_path = self.media_store.publish_audio(fetched_audio.temp_file_path, episode.id)
                    episode_repo.mark_audio_ready(
                        episode,
                        fetched_audio,
                        published_audio_path=str(published_path),
                    )
                    fetched_count += 1
                    session.commit()
                except Exception as exc:
                    failed_count += 1
                    episode_repo.set_episode_status(episode, "failed", last_error=summarize_error(exc))
                    session.commit()
                    self.logger.exception("episode sync failed channel_id=%s episode_id=%s", channel.id, episode.id)
                finally:
                    self.media_store.cleanup_temp_dir(episode.id)

            finished_at = utc_now()
            channel_repo.mark_sync_succeeded(channel, finished_at)
            if sync_run is not None:
                sync_run_repo.finish_sync_run(
                    sync_run,
                    status="succeeded",
                    discovered_count=discovered_count,
                    queued_count=queued_count,
                    fetched_count=fetched_count,
                    failed_count=failed_count,
                )
            session.commit()
        except Exception as exc:
            session.rollback()
            channel = channel_repo.get_channel(session, channel_id)
            if channel is not None:
                channel_repo.mark_sync_failed(channel, summarize_error(exc))
            if sync_run is not None:
                sync_run = session.get(type(sync_run), sync_run.id)
                if sync_run is not None:
                    sync_run_repo.finish_sync_run(
                        sync_run,
                        status="failed",
                        discovered_count=discovered_count,
                        queued_count=queued_count,
                        fetched_count=fetched_count,
                        failed_count=failed_count,
                        error_message=summarize_error(exc),
                    )
            session.commit()
            self.logger.exception("channel sync failed channel_id=%s", channel_id)
        finally:
            session.close()
