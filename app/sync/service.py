from __future__ import annotations

import logging

from sqlalchemy.orm import Session, sessionmaker

from app.common.time import utc_now
from app.config import Settings
from app.db.repositories import channels as channel_repo
from app.db.repositories import episodes as episode_repo
from app.db.repositories import sync_runs as sync_run_repo
from app.db.repositories import transcripts as transcript_repo
from app.db.session import get_session_factory
from app.domain.status import EpisodeStatus, SyncRunStatus
from app.downloader.client import YtDlpClient
from app.notifications.client import AlarmNotifier
from app.runtime_settings.service import RuntimeSettingsService
from app.storage.media_store import MediaStore
from app.sync.error_messages import collect_unique_errors, extract_ytdlp_error_message
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
        notifier: AlarmNotifier | None = None,
        runtime_settings_service: RuntimeSettingsService | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_settings_service = runtime_settings_service or RuntimeSettingsService(
            settings,
            session_factory=session_factory,
        )
        self.downloader = downloader or YtDlpClient(settings, runtime_settings_service=self.runtime_settings_service)
        self.media_store = media_store or MediaStore(settings)
        self.session_factory = session_factory or get_session_factory(settings)
        self.notifier = notifier or AlarmNotifier(settings, runtime_settings_service=self.runtime_settings_service)
        self.logger = logging.getLogger("podhub.sync")

    def sync_all_channels(self, *, notify: bool = False) -> None:
        session = self.session_factory()
        try:
            channel_ids = channel_repo.list_syncable_channel_ids(session)
        finally:
            session.close()

        for channel_id in channel_ids:
            self.sync_channel(channel_id, notify=notify)

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

    def sync_channel(self, channel_id: int, *, notify: bool = False) -> None:
        session = self.session_factory()
        discovered_count = 0
        queued_count = 0
        fetched_count = 0
        failed_count = 0
        sync_run = None
        notification_errors: list[str] = []
        channel_name = f"channel_id={channel_id}"

        try:
            runtime_settings = self.runtime_settings_service.get_effective_settings()
            channel = channel_repo.get_channel(session, channel_id)
            if channel is None:
                return

            channel_name = channel.display_name
            is_initial_sync = channel.last_synced_at is None
            channel_repo.mark_sync_started(channel)
            sync_run = sync_run_repo.create_sync_run(session, channel.id)
            session.commit()

            cookie_text = channel.cookie_text
            discovered_entries = self._list_source_episodes(
                channel.source_kind,
                channel.source_url,
                cookie_text,
            )
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
                    runtime_settings.fetch_batch_size,
                )
                selected_episodes = plan_initial_fetches(candidates, runtime_settings.fetch_batch_size)
            else:
                new_candidates = episode_repo.list_new_unfetched_episodes(
                    session,
                    new_episode_ids,
                    limit=runtime_settings.fetch_batch_size,
                )
                remaining_slots = max(runtime_settings.fetch_batch_size - len(new_candidates), 0)
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
                    batch_size=runtime_settings.fetch_batch_size,
                )

            for episode in selected_episodes:
                episode_repo.set_episode_status(episode, EpisodeStatus.QUEUED)
            queued_count = len(selected_episodes)
            session.commit()

            for episode in selected_episodes:
                temp_dir = self.media_store.temp_dir_for_episode(episode.id)
                try:
                    episode_repo.set_episode_status(episode, EpisodeStatus.FETCHING)
                    session.commit()

                    fetched_audio = self.downloader.fetch_episode_audio(
                        episode.webpage_url,
                        episode.id,
                        temp_dir,
                        cookie_text=cookie_text,
                    )
                    published_path = self.media_store.publish_audio(
                        fetched_audio.temp_file_path,
                        channel.slug,
                    )
                    episode_repo.mark_audio_ready(
                        episode,
                        fetched_audio,
                        published_audio_path=str(published_path),
                    )
                    if channel.transcription_enabled and runtime_settings.transcription_configured:
                        transcript_repo.mark_transcript_pending(episode)
                    fetched_count += 1
                    session.commit()
                except Exception as exc:
                    failed_count += 1
                    notification_errors.append(extract_ytdlp_error_message(exc))
                    episode_repo.set_episode_status(episode, EpisodeStatus.FAILED, last_error=summarize_error(exc))
                    session.commit()
                    self.logger.exception("episode sync failed channel_id=%s episode_id=%s", channel.id, episode.id)
                finally:
                    self.media_store.cleanup_temp_dir(episode.id)

            finished_at = utc_now()
            unique_notification_errors = collect_unique_errors(notification_errors)
            sync_error_message = self._build_sync_error_message(unique_notification_errors)
            if failed_count:
                channel_repo.mark_sync_failed(channel, sync_error_message)
            else:
                channel_repo.mark_sync_succeeded(channel, finished_at)
            if sync_run is not None:
                sync_run_repo.finish_sync_run(
                    sync_run,
                    status=SyncRunStatus.FAILED if failed_count else SyncRunStatus.SUCCEEDED,
                    discovered_count=discovered_count,
                    queued_count=queued_count,
                    fetched_count=fetched_count,
                    failed_count=failed_count,
                    error_message=sync_error_message if failed_count else None,
                )
            session.commit()
            if notify:
                self._notify_sync_errors(channel_name, unique_notification_errors)
        except Exception as exc:
            session.rollback()
            notification_errors.append(extract_ytdlp_error_message(exc))
            channel = channel_repo.get_channel(session, channel_id)
            if channel is not None:
                channel_name = channel.display_name
                channel_repo.mark_sync_failed(channel, summarize_error(exc))
            if sync_run is not None:
                sync_run = session.get(type(sync_run), sync_run.id)
                if sync_run is not None:
                    sync_run_repo.finish_sync_run(
                        sync_run,
                        status=SyncRunStatus.FAILED,
                        discovered_count=discovered_count,
                        queued_count=queued_count,
                        fetched_count=fetched_count,
                        failed_count=failed_count,
                        error_message=summarize_error(exc),
                    )
            session.commit()
            self.logger.exception("channel sync failed channel_id=%s", channel_id)
            if notify:
                self._notify_sync_errors(channel_name, notification_errors)
        finally:
            session.close()

    def _notify_sync_errors(self, channel_name: str, errors: list[str]) -> None:
        if errors:
            self.notifier.notify_sync_errors(channel_name=channel_name, errors=errors)

    def _build_sync_error_message(self, errors: list[str]) -> str:
        if not errors:
            return ""
        return summarize_error(Exception("; ".join(errors)))

    def _list_source_episodes(self, source_kind: str, source_url: str, cookie_text: str | None = None):
        if source_kind == "youtube_playlist":
            return self.downloader.list_playlist_episodes(source_url, cookie_text=cookie_text)
        return self.downloader.list_channel_episodes(source_url, cookie_text=cookie_text)
