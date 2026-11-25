"""Trading-session calendars used to judge historical coverage.

Two layers:

1. **Rule calendars** for holidays fixed by rule (NYSE; Nigerian fixed-date
   public holidays plus Easter).
2. **Cross-sectional inference** for closures that are not fixed by rule: the
   lunar Islamic holidays (Eid al-Fitr, Eid al-Adha, Mawlid) whose dates the
   Federal Government declares each year, and ad hoc closures (state funerals,
   election holidays). A rule-calendar weekday on which almost no instrument
   in the universe has an observation is treated as a market closure.

No third-party calendar package is used, so the rules below are the whole
calendar. Special closures are listed explicitly with their source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta


def easter_sunday(year: int) -> date:
    """Gregorian Easter (Anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _us_observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


# Unscheduled NYSE closures in the period of interest.
NYSE_SPECIAL_CLOSURES: dict[date, str] = {
    date(2018, 12, 5): "National day of mourning, President G.H.W. Bush",
    date(2025, 1, 9): "National day of mourning, President Carter",
}


def nyse_holidays(year: int) -> dict[date, str]:
    h: dict[date, str] = {}
    ny = date(year, 1, 1)
    # NYSE does not close on Friday 31 Dec when 1 Jan falls on a Saturday.
    if ny.weekday() != 5:
        h[_us_observed(ny)] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    h[easter_sunday(year) - timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_us_observed(date(year, 6, 19))] = "Juneteenth"
    h[_us_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    h[_us_observed(date(year, 12, 25))] = "Christmas Day"
    for d, why in NYSE_SPECIAL_CLOSURES.items():
        if d.year == year:
            h[d] = why
    return h


# NGX closures confirmed by a Federal Government declaration (Ministry of
# Interior press release). Lunar holidays are declared each year, so they can
# only enter the calendar once confirmed. Inferred closures that are not in
# this table are treated as unconfirmed market-wide absences (they may be
# provider outages). Each entry: date -> (holiday, source).
_FMI = "Federal Ministry of Interior press release"
CONFIRMED_NGX_CLOSURES: dict[date, tuple[str, str]] = {
    date(2024, 4, 9): ("Eid-el-Fitr", f"{_FMI}, 7 Apr 2024"),
    date(2024, 4, 10): ("Eid-el-Fitr", f"{_FMI}, 7 Apr 2024"),
    date(2024, 4, 11): ("Eid-el-Fitr (additional day)", f"{_FMI}, 9 Apr 2024"),
    date(2025, 6, 6): ("Eid-ul-Adha", f"{_FMI}, 2025"),
    date(2025, 6, 9): ("Eid-ul-Adha", f"{_FMI}, 2025"),
    date(2025, 9, 5): ("Eid-ul-Mawlid", f"{_FMI}, 3 Sep 2025"),
    date(2026, 3, 19): ("Eid-ul-Fitr", f"{_FMI}, 17 Mar 2026"),
    date(2026, 3, 20): ("Eid-ul-Fitr", f"{_FMI}, 17 Mar 2026"),
}


def ngx_rule_holidays(year: int) -> dict[date, str]:
    """Nigerian public holidays fixed by rule.

    Holidays falling on a weekend move to the next working day; Christmas and
    Boxing Day on a weekend move to the following Monday and Tuesday. Lunar
    Islamic holidays are not included: they are found by cross-sectional
    inference (see infer_market_closures).
    """
    h: dict[date, str] = {}

    def add(d: date, name: str) -> None:
        while d.weekday() >= 5 or d in h:
            d += timedelta(days=1)
        h[d] = name

    add(date(year, 1, 1), "New Year's Day")
    easter = easter_sunday(year)
    h[easter - timedelta(days=2)] = "Good Friday"
    h[easter + timedelta(days=1)] = "Easter Monday"
    add(date(year, 5, 1), "Workers' Day")
    # Democracy Day moved from 29 May to 12 June from 2019.
    if year >= 2019:
        add(date(year, 6, 12), "Democracy Day")
    else:
        add(date(year, 5, 29), "Democracy Day")
    add(date(year, 10, 1), "Independence Day")
    add(date(year, 12, 25), "Christmas Day")
    add(date(year, 12, 26), "Boxing Day")
    return h


@dataclass(frozen=True)
class SessionCalendar:
    """Expected sessions for a market over a window."""

    name: str
    holidays: Mapping[date, str] = field(default_factory=dict)
    weekends: bool = False  # True for markets that publish every calendar day

    def is_session(self, d: date) -> bool:
        if not self.weekends and d.weekday() >= 5:
            return False
        return d not in self.holidays

    def sessions(self, start: date, end: date) -> list[date]:
        out = []
        d = start
        while d <= end:
            if self.is_session(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def with_closures(self, closures: Mapping[date, str]) -> SessionCalendar:
        return SessionCalendar(
            self.name, {**self.holidays, **closures}, self.weekends
        )


def _years(start: date, end: date) -> range:
    return range(start.year, end.year + 1)


def nyse_calendar(start: date, end: date) -> SessionCalendar:
    hol: dict[date, str] = {}
    for y in _years(start, end):
        hol.update(nyse_holidays(y))
    return SessionCalendar("XNYS", hol)


def ngx_calendar(start: date, end: date) -> SessionCalendar:
    hol: dict[date, str] = {}
    for y in _years(start, end):
        hol.update(ngx_rule_holidays(y))
    for d, (name, source) in CONFIRMED_NGX_CLOSURES.items():
        if start.year <= d.year <= end.year:
            hol[d] = f"{name} (confirmed: {source})"
    return SessionCalendar("XNSA", hol)


def fx_weekday_calendar(start: date, end: date) -> SessionCalendar:
    """FX fixings: every weekday, holidays not excluded."""
    return SessionCalendar("FX-WEEKDAY", {})


def fx_daily_calendar(start: date, end: date) -> SessionCalendar:
    """Providers that publish a rate for every calendar day (NGNMarket)."""
    return SessionCalendar("FX-DAILY", {}, weekends=True)


def infer_market_closures(
    calendar: SessionCalendar,
    observed_dates_by_instrument: Mapping[str, Iterable[date]],
    start: date,
    end: date,
    max_fraction_trading: float = 0.2,
    min_instruments: int = 5,
) -> dict[date, str]:
    """Candidate closures: calendar sessions on which almost no instrument has data.

    These are candidates only. A simultaneous provider outage looks the same
    as an exchange closure, so callers must not remove these sessions from the
    calendar unless a closure is independently confirmed.

    A date is inferred as a market-wide closure when at most
    ``max_fraction_trading`` of the instruments that were already observed
    on or before it (and observed after it) have an observation on that date.
    The check needs at least ``min_instruments`` live instruments, so it
    cannot excuse gaps in a single series. Each inferred closure should still
    be checked against exchange notices; the returned reason says so.
    """
    obs = {k: set(v) for k, v in observed_dates_by_instrument.items() if v}
    if len(obs) < min_instruments:
        return {}
    spans = {k: (min(v), max(v)) for k, v in obs.items()}
    closures: dict[date, str] = {}
    for d in calendar.sessions(start, end):
        live = [k for k, (a, b) in spans.items() if a <= d <= b]
        if len(live) < min_instruments:
            continue
        trading = sum(1 for k in live if d in obs[k])
        if trading <= max_fraction_trading * len(live):
            closures[d] = (
                f"inferred market closure: {trading}/{len(live)} instruments "
                "observed (verify against exchange notice)"
            )
    return closures
