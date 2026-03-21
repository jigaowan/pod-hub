from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

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

        resolved = self._resolve_source(cleaned)

        session = self.session_factory()
        try:
            existing = channel_repo.get_channel_by_external_id(session, resolved.external_id)
            if existing is not None:
                raise ValidationError("This source is already registered")

            slug = channel_repo.allocate_slug(
                session,
                resolved.display_name,
                fallback=resolved.external_id,
            )
            channel = channel_repo.create_channel(
                session,
                source_kind=resolved.source_kind,
                external_channel_id=resolved.external_id,
                source_input=cleaned,
                source_url=resolved.source_url,
                display_name=resolved.display_name,
                slug=slug,
            )
            session.commit()
            if resolved.cover_url:
                try:
                    self.media_store.save_channel_cover(resolved.cover_url, channel.slug)
                except Exception:
                    self.logger.exception(
                        "channel cover download failed channel_id=%s source_url=%s",
                        channel.id,
                        resolved.cover_url,
                    )
            return channel
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_channel(self, channel_slug: str) -> bool:
        session = self.session_factory()
        try:
            channel = channel_repo.get_channel_by_slug(session, channel_slug)
            if channel is None:
                return False

            channel_repo.delete_channel(session, channel)
            session.commit()
            return True
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

            discovered_entries = self._list_source_episodes(channel.source_kind, channel.source_url)
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
                    published_path = self.media_store.publish_audio(
                        fetched_audio.temp_file_path,
                        channel.slug,
                    )
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

    def _resolve_source(self, source_input: str):
        if self._is_standard_playlist_url(source_input):
            return self.downloader.resolve_playlist(source_input)
        if self._has_playlist_param(source_input):
            raise ValidationError(
                "Detected a playlist parameter. Please use a standard playlist URL like "
                "https://www.youtube.com/playlist?list=PL..."
            )
        return self.downloader.resolve_channel(source_input)

    def _list_source_episodes(self, source_kind: str, source_url: str):
        if source_kind == "youtube_playlist":
            return self.downloader.list_playlist_episodes(source_url)
        return self.downloader.list_channel_episodes(source_url)

    def _is_standard_playlist_url(self, source_input: str) -> bool:
        parsed = urlparse(source_input)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if not parsed.path.rstrip("/").endswith("/playlist"):
            return False
        query = parse_qs(parsed.query)
        list_ids = query.get("list") or []
        return bool(list_ids and list_ids[0])

    def _has_playlist_param(self, source_input: str) -> bool:
        parsed = urlparse(source_input)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if parsed.path.rstrip("/").endswith("/playlist"):
            return False
        query = parse_qs(parsed.query)
        list_ids = query.get("list") or []
        return bool(list_ids and list_ids[0])
