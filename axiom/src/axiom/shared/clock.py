from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> str:
    return format_utc(datetime.now(timezone.utc))


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("UTC timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)
