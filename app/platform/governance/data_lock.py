"""Locked-period boundary (docs/governance/LOCKED_PERIOD_PROTOCOL.md).

All provider data dated on or before EXAMINED_THROUGH has been examined during
development. Data dated from LOCKED_FROM is reserved for the one-time formal
evaluation of the frozen release candidate. Backtests and lifecycle validation
never read locked data unless the run is the formal evaluation AND the process
is started with CRI_LOCKED_EVALUATION_AUTHORISED=1. Live shadow (prospective
evidence) is not affected: it decides on current data by design.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

EXAMINED_THROUGH = date(2026, 11, 25)
LOCKED_FROM = date(2026, 11, 27)


def locked_evaluation_authorised(params: dict[str, Any]) -> bool:
    return bool(params.get("locked_evaluation")) and os.environ.get("CRI_LOCKED_EVALUATION_AUTHORISED") == "1"


def cap_bars(bars: dict[str, list[dict[str, Any]]], params: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Bars visible to a backtest: nothing after EXAMINED_THROUGH unless authorised."""
    if locked_evaluation_authorised(params):
        return bars
    cut = EXAMINED_THROUGH.isoformat()
    return {k: [b for b in v if str(b["bar_date"])[:10] <= cut] for k, v in bars.items()}
