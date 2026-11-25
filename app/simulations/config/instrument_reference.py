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
        verified=False,
        source="listing by introduction, May 2019 (to verify against NGX bulletin)",
    ),
    "AIRTELAFRI": InstrumentReference(
        "AIRTELAFRI",
        listing_date=date(2019, 7, 4),
        verified=False,
        source="NGX listing following June 2019 LSE IPO (to verify against NGX bulletin)",
    ),
    "BUACEMENT": InstrumentReference(
        "BUACEMENT",
        listing_date=date(2020, 1, 9),
        verified=False,
        source="listing after CCNN merger, January 2020 (to verify against NGX bulletin)",
        predecessor="CCNN",
    ),
    "FIRSTHOLDCO": InstrumentReference(
        "FIRSTHOLDCO",
        listing_date=None,
        verified=False,
        source=(
            "FBN Holdings renamed First HoldCo; ticker changed from FBNH. Whether "
            "the provider carries pre-rename history under FIRSTHOLDCO is unverified"
        ),
        predecessor="FBNH",
    ),
}


def reference_for(symbol: str) -> InstrumentReference:
    return INSTRUMENT_REFERENCE.get(symbol, InstrumentReference(symbol))
