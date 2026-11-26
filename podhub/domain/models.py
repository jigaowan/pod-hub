from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime | None = None, *, seconds: float = 0) -> str:
    return ((value or utc_now()) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def parse_date(value: Any) -> str | None:
    try:
        if isinstance(value, (int, float)):
            return stamp(datetime.fromtimestamp(value, UTC))
        if isinstance(value, str):
            date = datetime.fromisoformat(value)
            return stamp(date.replace(tzinfo=date.tzinfo or UTC).astimezone(UTC))
    except (ValueError, OverflowError, OSError):
        pass
    return None


@dataclass(frozen=True)
class SourceIdentity:
    kind: str
    external_id: str
    title: str
    normalized_url: str
    cover_url: str | None = None
