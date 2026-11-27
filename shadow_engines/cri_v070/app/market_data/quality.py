from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def age_minutes(timestamp: datetime, now: datetime | None = None) -> float:
    current = now or utc_now()
    return max(
        0.0, (ensure_aware(current) - ensure_aware(timestamp)).total_seconds() / 60.0
    )


def clamp_score(score: float) -> float:
    return max(0.0, min(1.0, score))
