from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session, sessionmaker

from app.common.cookies import normalize_cookie_text
from app.common.errors import ValidationError
from app.config import Settings
from app.db.repositories import channels as channel_repo
from app.db.session import get_session_factory
from app.downloader.client import YtDlpClient
from app.storage.media_store import MediaStore


class ChannelService:
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
        self.logger = logging.getLogger("podhub.channels")

    def register_channel(
        self,
        source_input: str,
        cookie_text: str | None = None,
        *,
        transcription_enabled: bool = False,
    ):
        cleaned = source_input.strip()
        if not cleaned:
            raise ValidationError("source_input is required")
        normalized_cookie_text = normalize_cookie_text(cookie_text)

        resolved = self._resolve_source(cleaned, normalized_cookie_text)

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
                cookie_text=normalized_cookie_text,
                transcription_enabled=transcription_enabled,
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

    def update_channel_cookie(self, channel_slug: str, cookie_text: str | None) -> bool:
        normalized_cookie_text = normalize_cookie_text(cookie_text)

        session = self.session_factory()
        try:
            channel = channel_repo.get_channel_by_slug(session, channel_slug)
            if channel is None:
                return False

            channel_repo.update_cookie_text(channel, normalized_cookie_text)
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_channel_cookie(self, channel_slug: str) -> bool:
        return self.update_channel_cookie(channel_slug, None)

    def update_channel_transcription(self, channel_slug: str, transcription_enabled: bool) -> bool:
        session = self.session_factory()
        try:
            channel = channel_repo.get_channel_by_slug(session, channel_slug)
            if channel is None:
                return False

            channel_repo.update_transcription_enabled(channel, transcription_enabled)
            session.commit()
            return True
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

    def _resolve_source(self, source_input: str, cookie_text: str | None = None):
        if self._is_standard_playlist_url(source_input):
            return self.downloader.resolve_playlist(source_input, cookie_text=cookie_text)
        if self._has_playlist_param(source_input):
            raise ValidationError(
                "Detected a playlist parameter. Please use a standard playlist URL like "
                "https://www.youtube.com/playlist?list=PL..."
            )
        return self.downloader.resolve_channel(source_input, cookie_text=cookie_text)

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
