"""Listing, predecessor and suspension reference data for the official universe.

Used by coverage validation to tell a legitimate late start (the instrument did
not trade yet) from a provider truncation. ``verified`` is True only where the
date was checked against an NGX market bulletin or contemporaneous report; the
source is recorded. Unverified entries still excuse a late start, but the
coverage report flags them so they are checked before evidence is relied on.

Predecessors matter for history depth: GTCO's price history before 24 June
2021 trades as GUARANTY (Guaranty Trust Bank), and ACCESSCORP's before 28
March 2022 as ACCESS (Access Bank). The holding-company exchanges were 1:1, but
splicing a predecessor series into the successor is a methodology decision and
is not done here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Suspension:
    start: date
    end: date  # last suspended session, inclusive
    reason: str
    source: str


@dataclass(frozen=True)
class InstrumentReference:
    symbol: str
    listing_date: date | None = None
    verified: bool = False
    source: str = ""
    predecessor: str | None = None
    predecessor_last_session: date | None = None
    suspensions: tuple[Suspension, ...] = field(default_factory=tuple)


_NGX_BULLETINS = "doclib.ngxgroup.com market bulletins"

INSTRUMENT_REFERENCE: dict[str, InstrumentReference] = {
    "GTCO": InstrumentReference(
        "GTCO",
        listing_date=date(2021, 6, 24),
        verified=True,
        source=f"{_NGX_BULLETINS} NGXREG/LRD/MB17/21/06/24 (24 June 2021)",
        predecessor="GUARANTY",
        predecessor_last_session=date(2021, 6, 17),
    ),
    "ACCESSCORP": InstrumentReference(
        "ACCESSCORP",
        listing_date=date(2022, 3, 28),
        verified=True,
        source=(
            f"{_NGX_BULLETINS} NGXREG/LRD/MB16/22/03/24 (suspension 24 March 2022); "
            "listing reported 28 March 2022"
        ),
        predecessor="ACCESS",
        predecessor_last_session=date(2022, 3, 23),
    ),
    "BUAFOODS": InstrumentReference(
        "BUAFOODS",
        listing_date=date(2022, 1, 5),
        verified=True,
        source=f"{_NGX_BULLETINS} NGXREG/LRD/MB01/22/01/05 (5 January 2022)",
    ),
    "MTNN": InstrumentReference(
        "MTNN",
        listing_date=date(2019, 5, 16),
        verified=True,
        source="NSE listing by introduction on the Premium Board, 16 May 2019 (NSE/MTN announcements)",
    ),
    "AIRTELAFRI": InstrumentReference(
        "AIRTELAFRI",
        # Previously recorded as 4 July 2019 (unverified). The NSE postponed
        # the 5 July listing; shares listed on Tuesday 9 July 2019.
        listing_date=date(2019, 7, 9),
        verified=True,
        source="NSE cross-border secondary listing, 9 July 2019 (NSE announcement)",
    ),
    "BUACEMENT": InstrumentReference(
        "BUACEMENT",
        listing_date=date(2020, 1, 9),
        verified=True,
        source="CCNN delisted and BUA Cement (formerly Obu Cement) listed, 9 January 2020",
        predecessor="CCNN",
        predecessor_last_session=date(2020, 1, 8),
    ),
    "FIRSTHOLDCO": InstrumentReference(
        "FIRSTHOLDCO",
        # Listed before 2018 as FBN Holdings; a name and ticker change only,
        # so the series is continuous across the change.
        listing_date=None,
        verified=True,
        source=(
            "NGX Regulation market bulletin NGXREG/IRD/MB15/25/03/5, 5 March 2025: "
            "FBN Holdings renamed First HoldCo, symbol FBNH -> FirstHoldCo"
        ),
        predecessor="FBNH",
    ),
}


def reference_for(symbol: str) -> InstrumentReference:
    return INSTRUMENT_REFERENCE.get(symbol, InstrumentReference(symbol))
