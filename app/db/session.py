from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.base import Base
from app.db import models  # noqa: F401

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def build_engine(settings: Settings | None = None) -> Engine:
    config = settings or get_settings()
    return create_engine(
        f"sqlite:///{config.database_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )


def get_engine(settings: Settings | None = None) -> Engine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = build_engine(settings)
    return _ENGINE


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(
            bind=get_engine(settings),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
    return _SESSION_FACTORY


def init_db(settings: Settings | None = None) -> None:
    engine = get_engine(settings)
    Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope(settings: Settings | None = None) -> Generator[Session, None, None]:
    session = get_session_factory(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
