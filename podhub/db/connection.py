from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from podhub.db.migrations import MIGRATIONS
from podhub.domain.models import utc_now


class Database:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "app.db"

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def migrate(self) -> None:
        with self.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise RuntimeError("database schema is newer than this application")
            for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
                statement = ""
                for line in migration.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        conn.execute(statement)
                        statement = ""
                conn.execute(f"PRAGMA user_version={number}")
                set_meta(conn, "schema_version", str(number))

    def backup(self, directory: Path) -> Path:
        target = directory / f"app-{utc_now():%Y%m%d-%H%M%S}.db"
        temporary = directory / f".{uuid4().hex}.db"
        try:
            with self.connect() as conn:
                conn.execute("VACUUM INTO ?", (str(temporary),))
            with temporary.open("rb") as file:
                os.fsync(file.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        for old in sorted(directory.glob("app-*.db"), reverse=True)[7:]:
            old.unlink()
        return target


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
