from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db.session import ensure_schema_compatibility


def test_schema_compatibility_adds_cookie_text_to_existing_channels_table(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY,
                    source_kind TEXT NOT NULL,
                    external_channel_id TEXT NOT NULL,
                    source_input TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_synced_at DATETIME,
                    last_error TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )

    ensure_schema_compatibility(engine)
    ensure_schema_compatibility(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("channels")}
    assert "cookie_text" in columns


def test_schema_compatibility_adds_source_position_to_existing_episodes_table(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY,
                    source_kind TEXT NOT NULL,
                    external_channel_id TEXT NOT NULL,
                    source_input TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_synced_at DATETIME,
                    last_error TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE episodes (
                    id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    source_video_id TEXT NOT NULL,
                    webpage_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    upload_date DATE,
                    published_at DATETIME,
                    duration_seconds INTEGER,
                    thumbnail_url TEXT,
                    audio_path TEXT,
                    audio_mime_type TEXT,
                    audio_bytes INTEGER,
                    audio_fetched_at DATETIME,
                    listed_at DATETIME NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )

    ensure_schema_compatibility(engine)
    ensure_schema_compatibility(engine)

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("episodes")}
    indexes = {index["name"] for index in inspector.get_indexes("episodes")}
    assert "source_position" in columns
    assert "ix_episodes_channel_source_position" in indexes
