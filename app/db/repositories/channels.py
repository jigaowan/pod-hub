from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.slug import slugify, unique_slug
from app.common.time import utc_now
from app.db.models.channel import Channel


def list_channels(session: Session) -> list[Channel]:
    stmt = select(Channel).order_by(Channel.created_at.desc(), Channel.id.desc())
    return list(session.scalars(stmt))


def list_syncable_channel_ids(session: Session) -> list[int]:
    stmt = (
        select(Channel.id)
        .where(Channel.status.in_(("pending_initial_sync", "active", "error")))
        .order_by(Channel.created_at.asc(), Channel.id.asc())
    )
    return list(session.scalars(stmt))


def get_channel(session: Session, channel_id: int) -> Channel | None:
    return session.get(Channel, channel_id)


def get_channel_by_slug(session: Session, slug: str) -> Channel | None:
    stmt = select(Channel).where(Channel.slug == slug)
    return session.scalar(stmt)


def get_channel_by_external_id(session: Session, external_channel_id: str) -> Channel | None:
    stmt = select(Channel).where(Channel.external_channel_id == external_channel_id)
    return session.scalar(stmt)


def allocate_slug(session: Session, display_name: str) -> str:
    existing = set(session.scalars(select(Channel.slug)))
    return unique_slug(slugify(display_name), existing)


def create_channel(
    session: Session,
    *,
    external_channel_id: str,
    source_input: str,
    source_url: str,
    display_name: str,
    slug: str,
    status: str = "pending_initial_sync",
) -> Channel:
    now = utc_now()
    channel = Channel(
        source_kind="youtube_channel",
        external_channel_id=external_channel_id,
        source_input=source_input,
        source_url=source_url,
        display_name=display_name,
        slug=slug,
        status=status,
        created_at=now,
        updated_at=now,
    )
    session.add(channel)
    session.flush()
    return channel


def mark_sync_started(channel: Channel) -> None:
    channel.status = "syncing"
    channel.updated_at = utc_now()


def mark_sync_succeeded(channel: Channel, finished_at: datetime) -> None:
    channel.status = "active"
    channel.last_synced_at = finished_at
    channel.last_error = None
    channel.updated_at = finished_at


def mark_sync_failed(channel: Channel, error_message: str) -> None:
    channel.status = "error"
    channel.last_error = error_message
    channel.updated_at = utc_now()

