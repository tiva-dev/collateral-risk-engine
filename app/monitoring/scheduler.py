from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar, Protocol

from app.core.enums import MarginState


class MonitoringScheduler(Protocol):
    def next_check_after(
        self, margin_state: MarginState | None, now: datetime | None = None
    ) -> datetime: ...


class SimpleMonitoringScheduler:
    """Manual-tick scheduling policy; no background worker is started in v0.4."""

    intervals: ClassVar[dict[MarginState, timedelta]] = {
        MarginState.SAFE: timedelta(minutes=15),
        MarginState.WATCH: timedelta(minutes=5),
        MarginState.RESTRICT_NEW_BORROWING: timedelta(minutes=1),
        MarginState.MARGIN_CALL: timedelta(seconds=30),
        MarginState.LIQUIDATION: timedelta(seconds=0),
    }

    def next_check_after(
        self, margin_state: MarginState | None, now: datetime | None = None
    ) -> datetime:
        base = now or datetime.now(UTC)
        return base + self.intervals.get(
            margin_state or MarginState.SAFE, self.intervals[MarginState.SAFE]
        )
