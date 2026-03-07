from __future__ import annotations

from datetime import UTC, date, datetime, time


def utc_now() -> datetime:
    return datetime.now(UTC)


def assume_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def browser_datetime_iso(value: datetime | None) -> str | None:
    normalized = assume_utc(value)
    if normalized is None:
        return None
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def browser_datetime_fallback(value: datetime | None) -> str:
    normalized = assume_utc(value)
    if normalized is None:
        return "-"
    return normalized.strftime("%Y-%m-%d %H:%M:%S UTC")


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
