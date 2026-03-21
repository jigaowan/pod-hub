from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.episode import Episode
    from app.db.models.sync_run import SyncRun


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (
        Index("ix_channels_status_last_synced_at", "status", "last_synced_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    external_channel_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    source_input: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    episodes: Mapped[list["Episode"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    sync_runs: Mapped[list["SyncRun"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
