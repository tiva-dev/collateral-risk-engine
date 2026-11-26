"""Calendar-aware coverage validation for historical series.

A provider can return a series that looks complete but is not: NGNMarket, for
example, silently starts history at the earliest date the plan allows
instead of rejecting a longer request. This module compares each series with
the sessions it should contain and classifies every shortfall.

Shortfalls are classified, never filled:

* **late start** – first observation after the effective start (the later of
  the requested start and the listing date). Beyond the tolerance this is
  reported as suspected silent truncation.
* **stale end** – last observation well before the requested end.
* **internal gap** – a run of expected sessions with no observation, labelled
  ``known_suspension`` when it overlaps a recorded suspension and
  ``unexplained`` otherwise.
* **off-calendar observations** – data on dates the calendar says were closed.

Market-wide closures that no rule calendar knows about (lunar holidays,
ad hoc public holidays) are inferred cross-sectionally before series are
judged; see calendars.infer_market_closures.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from .calendars import SessionCalendar

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


@dataclass(frozen=True)
class CoveragePolicy:
    max_start_lag_sessions: int = 5
    max_end_lag_sessions: int = 5
    max_internal_gap_sessions: int = 10
    min_coverage_ratio: float = 0.95


@dataclass(frozen=True)
class CoverageGap:
    start: date
    end: date
    sessions: int
    classification: str
    note: str = ""


@dataclass
class CoverageReport:
    series: str
    calendar: str
    requested_start: date
    requested_end: date
    effective_start: date
    listing_date: date | None
    listing_verified: bool
    first_observation: date | None
    last_observation: date | None
    expected_sessions: int
    observed_expected_sessions: int
    excused_sessions: int
    off_calendar_observations: int
    coverage_ratio: float
    start_lag_sessions: int
    end_lag_sessions: int
    gaps: list[CoverageGap] = field(default_factory=list)
    status: str = STATUS_PASS
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in (
            "requested_start",
            "requested_end",
            "effective_start",
            "listing_date",
            "first_observation",
            "last_observation",
        ):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        d["gaps"] = [
            {**g, "start": g["start"].isoformat(), "end": g["end"].isoformat()}
            for g in d["gaps"]
        ]
        return d


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def validate_series_coverage(
    series: str,
    observed: Iterable[Any],
    calendar: SessionCalendar,
    requested_start: date,
    requested_end: date,
    *,
    listing_date: date | None = None,
    listing_verified: bool = False,
    suspensions: Iterable[tuple[date, date, str]] = (),
    unconfirmed_closures: Mapping[date, str] | None = None,
    policy: CoveragePolicy | None = None,
) -> CoverageReport:
    """Judge one series against its calendar.

    ``unconfirmed_closures`` are calendar sessions on which (almost) the whole
    cross-section is missing but no exchange notice confirms a closure. They
    could be holidays or a simultaneous provider outage, so they stay in the
    expected sessions (they count as missing), are labelled
    ``unconfirmed_market_wide_absence`` and are not counted towards the
    unexplained-gap failure threshold.
    """
    unconfirmed = dict(unconfirmed_closures or {})
    policy = policy or CoveragePolicy()
    obs = sorted({_as_date(d) for d in observed})
    effective_start = max(requested_start, listing_date) if listing_date else requested_start
    sessions = calendar.sessions(effective_start, requested_end)
    session_set = set(sessions)
    obs_set = set(obs)
    susp = [(a, b, why) for a, b, why in suspensions]

    def suspended(d: date) -> str | None:
        for a, b, why in susp:
            if a <= d <= b:
                return why
        return None

    excused = {d for d in sessions if suspended(d)}
    denominator = max(1, len(sessions) - len(excused))
    observed_expected = len([d for d in obs if d in session_set and d not in excused])
    off_calendar = len(
        [d for d in obs if effective_start <= d <= requested_end and d not in session_set]
    )
    report = CoverageReport(
        series=series,
        calendar=calendar.name,
        requested_start=requested_start,
        requested_end=requested_end,
        effective_start=effective_start,
        listing_date=listing_date,
        listing_verified=listing_verified,
        first_observation=obs[0] if obs else None,
        last_observation=obs[-1] if obs else None,
        expected_sessions=len(sessions),
        observed_expected_sessions=observed_expected,
        excused_sessions=len(excused),
        off_calendar_observations=off_calendar,
        coverage_ratio=round(observed_expected / denominator, 6),
        start_lag_sessions=0,
        end_lag_sessions=0,
    )
    if not obs:
        report.status = STATUS_FAIL
        report.failures.append("no observations in the requested window")
        return report
    if listing_date and not listing_verified:
        report.warnings.append(
            f"late start excused by unverified listing date {listing_date.isoformat()}"
        )
    if listing_date and obs[0] < listing_date:
        report.warnings.append(
            f"observations start {obs[0].isoformat()}, before the recorded listing "
            f"date {listing_date.isoformat()}: check the reference data"
        )

    report.start_lag_sessions = len(
        [d for d in sessions if d < obs[0] and d not in excused]
    )
    report.end_lag_sessions = len(
        [d for d in sessions if d > obs[-1] and d not in excused]
    )
    if report.start_lag_sessions > policy.max_start_lag_sessions:
        report.failures.append(
            "suspected silent truncation: first observation "
            f"{obs[0].isoformat()} is {report.start_lag_sessions} sessions after the "
            f"effective start {effective_start.isoformat()}"
        )
    if report.end_lag_sessions > policy.max_end_lag_sessions:
        report.failures.append(
            f"stale series: last observation {obs[-1].isoformat()} is "
            f"{report.end_lag_sessions} sessions before {requested_end.isoformat()}"
        )

    # Internal gaps between first and last observation. A run of missing
    # sessions is split wherever its explanation changes, so a run that only
    # partly overlaps a suspension cannot excuse its unrelated sessions.
    def label(d: date) -> tuple[str, str]:
        why = suspended(d)
        if why:
            return "known_suspension", why
        if d in unconfirmed:
            return "unconfirmed_market_wide_absence", unconfirmed[d]
        return "unexplained", ""

    segment: list[date] = []
    segment_label: tuple[str, str] | None = None

    def close_segment() -> None:
        if not segment or segment_label is None:
            return
        classification, note = segment_label
        report.gaps.append(
            CoverageGap(segment[0], segment[-1], len(segment), classification, note)
        )
        if (
            classification == "unexplained"
            and len(segment) > policy.max_internal_gap_sessions
        ):
            report.failures.append(
                f"unexplained gap of {len(segment)} sessions "
                f"{segment[0].isoformat()}..{segment[-1].isoformat()}"
            )
        segment.clear()

    previous_missing = False
    for d in sessions:
        if d < obs[0] or d > obs[-1]:
            continue
        if d in obs_set:
            close_segment()
            segment_label = None
            previous_missing = False
            continue
        lab = label(d)
        if previous_missing and lab != segment_label:
            close_segment()
        segment_label = lab
        segment.append(d)
        previous_missing = True
    close_segment()
    # An unexplained gap can be interrupted only by a labelled day; adjacent
    # unexplained segments split around a suspension are still reported
    # separately, each against the threshold on its own.

    if report.coverage_ratio < policy.min_coverage_ratio:
        report.failures.append(
            f"coverage ratio {report.coverage_ratio:.3f} below "
            f"{policy.min_coverage_ratio:.2f}"
        )
    if off_calendar:
        report.warnings.append(
            f"{off_calendar} observation(s) on dates the {calendar.name} calendar "
            "marks closed"
        )
    unexplained_small = [g for g in report.gaps if g.classification == "unexplained"]
    if unexplained_small and not report.failures:
        report.warnings.append(
            f"{len(unexplained_small)} short unexplained gap(s), "
            f"{sum(g.sessions for g in unexplained_small)} session(s) in total"
        )
    market_wide = [
        g for g in report.gaps if g.classification == "unconfirmed_market_wide_absence"
    ]
    if market_wide:
        report.warnings.append(
            f"{sum(g.sessions for g in market_wide)} session(s) missing across the "
            "whole market without a confirmed closure (holiday or provider outage)"
        )
    report.status = (
        STATUS_FAIL if report.failures else STATUS_WARN if report.warnings else STATUS_PASS
    )
    return report


def summarise(reports: Mapping[str, CoverageReport]) -> dict[str, Any]:
    by_status: dict[str, list[str]] = {STATUS_PASS: [], STATUS_WARN: [], STATUS_FAIL: []}
    for name, r in reports.items():
        by_status[r.status].append(name)
    return {
        "status": (
            STATUS_FAIL
            if by_status[STATUS_FAIL]
            else STATUS_WARN
            if by_status[STATUS_WARN]
            else STATUS_PASS
        ),
        "counts": {k: len(v) for k, v in by_status.items()},
        "failing_series": sorted(by_status[STATUS_FAIL]),
        "warning_series": sorted(by_status[STATUS_WARN]),
    }
