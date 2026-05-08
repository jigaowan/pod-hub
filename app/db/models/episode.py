from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.channel import Channel


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint("channel_id", "source_video_id", name="uq_episodes_channel_video"),
        Index("ix_episodes_channel_published_at", "channel_id", "published_at"),
        Index("ix_episodes_channel_source_position", "channel_id", "source_position"),
        Index("ix_episodes_channel_status_listed_at", "channel_id", "status", "listed_at"),
        Index("ix_episodes_status_audio_fetched_at", "status", "audio_fetched_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    source_video_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_position: Mapped[int | None] = mapped_column(Integer)
    webpage_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    upload_date: Mapped[date | None] = mapped_column(Date)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    audio_path: Mapped[str | None] = mapped_column(Text)
    audio_mime_type: Mapped[str | None] = mapped_column(String(64))
    audio_bytes: Mapped[int | None] = mapped_column(Integer)
    audio_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    channel: Mapped["Channel"] = relationship(back_populates="episodes")
