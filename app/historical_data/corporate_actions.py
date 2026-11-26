"""Corporate actions: verified adjustments versus genuine extreme moves.

NGNMarket prices are not adjusted for corporate actions, and its dividend
endpoint needs a Starter plan. Large one-day moves therefore have three
possible explanations, kept apart here:

* ``verified_corporate_action`` - a recorded action (bonus, split, rights,
  cash dividend) whose ex-date and price factor explain the move. Only these
  are used to adjust prices.
* ``disclosure_candidate`` - no verified action, but the company published a
  corporate-action disclosure (dividend, bonus, rights, scheme) in the 30
  days before the move. Flagged for verification; the move is NOT removed.
* ``market_move`` - nothing explains it. It is treated as a genuine market
  movement and stays in the data.

Nothing is excluded on the strength of a threshold alone. The screen
threshold is a review trigger, not a model of exchange price limits.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from .models import HistoricalBar

ACTION_KEYWORDS = re.compile(
    r"(?i)\b(bonus|scrip|split|consolidat|rights issue|rights offer|dividend|"
    r"qualification date|closure of register|scheme of arrangement|"
    r"capital reduction|share reconstruction)\b"
)
DEFAULT_SCREEN = 0.10
DISCLOSURE_LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: date
    kind: str  # bonus | split | consolidation | rights | cash_dividend
    price_factor: float  # expected ex-date price / pre-ex price (e.g. 1-for-5 bonus -> 5/6)
    source: str
    verified: bool = True


# Actions verified against exchange bulletins or company filings. Empty until
# entries are checked; unverified candidates come from the disclosure screen.
VERIFIED_CORPORATE_ACTIONS: dict[str, list[CorporateAction]] = {}


def _day(ts: date | datetime) -> date:
    return ts.date() if isinstance(ts, datetime) else ts


@dataclass(frozen=True)
class ExtremeMove:
    symbol: str
    date: date
    simple_return: float
    classification: str
    explanation: str = ""


def screen_extreme_moves(
    symbol: str,
    bars: list[HistoricalBar],
    *,
    actions: Iterable[CorporateAction] = (),
    disclosures: Iterable[Mapping[str, Any]] = (),
    threshold: float = DEFAULT_SCREEN,
    tolerance: float = 0.02,
) -> list[ExtremeMove]:
    acts = {a.ex_date: a for a in actions if a.verified}
    discl = [
        (date.fromisoformat(str(d.get("disclosed_at"))[:10]), str(d.get("title", "")))
        for d in disclosures
        if d.get("disclosed_at") and ACTION_KEYWORDS.search(str(d.get("title", "")))
    ]
    out = []
    ordered = sorted(bars, key=lambda b: b.timestamp)
    for prev, cur in itertools.pairwise(ordered):
        if prev.close <= 0 or cur.close <= 0:
            continue
        r = cur.close / prev.close - 1
        if abs(r) <= threshold:
            continue
        d = _day(cur.timestamp)
        act = acts.get(d)
        if act and abs((1 + r) - act.price_factor) <= tolerance + threshold:
            out.append(ExtremeMove(symbol, d, r, "verified_corporate_action",
                                   f"{act.kind} factor {act.price_factor:.4f} ({act.source})"))
            continue
        near = [t for dd, t in discl if d - timedelta(days=DISCLOSURE_LOOKBACK_DAYS) <= dd <= d]
        if near:
            out.append(ExtremeMove(symbol, d, r, "disclosure_candidate", near[-1][:140]))
        else:
            out.append(ExtremeMove(symbol, d, r, "market_move"))
    return out


def adjust_for_actions(
    bars: list[HistoricalBar], actions: Iterable[CorporateAction]
) -> list[HistoricalBar]:
    """Back-adjust prices (and volumes) before each verified action's ex-date."""
    verified = sorted((a for a in actions if a.verified), key=lambda a: a.ex_date)
    if not verified:
        return list(bars)
    out = []
    for b in bars:
        d = _day(b.timestamp)
        f = 1.0
        for a in verified:
            if d < a.ex_date:
                f *= a.price_factor
        if f == 1.0:
            out.append(b)
            continue
        out.append(
            replace(
                b,
                open=b.open * f, high=b.high * f, low=b.low * f, close=b.close * f,
                adjusted_close=b.close * f,
                volume=b.volume / f if f else b.volume,
                field_provenance={**b.field_provenance, "adjusted_close": "corporate_action_adjusted"},
            )
        )
    return out
