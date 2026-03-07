from __future__ import annotations

from datetime import UTC, date, datetime, time


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_upload_date(value: str | None) -> date | None:
    if not value or len(value) != 8:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def published_at_from_upload_date(value: str | None) -> datetime | None:
    parsed = parse_upload_date(value)
    if parsed is None:
        return None
    return datetime.combine(parsed, time.min, tzinfo=UTC)

