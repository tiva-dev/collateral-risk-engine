"""Probe: is 'executing the plan restores the account' monotonic in sale size?

Grid over two- and three-asset portfolios that mix liquid/illiquid and
low/high volatility holdings, across principals that put the account in
MARGIN_CALL or LIQUIDATION. For each case it records the planner's grid
verdict changes per ordering, the ordering chosen, and whether executing the
advisory day by day (execution module, interest, settlement) restores the
account.

    python -m research.audit_probes.planner_monotonicity
"""

from __future__ import annotations

import json
from collections import Counter

from app.core.enums import AssetType, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, Policy
from app.tests.test_unified_liquidation_planning import (
    EXEC,
    TERMS,
    _execute_to_settlement,
    _md,
)

PROFILES = {
    "liquid+illiquid": [
        ("A", AssetType.ETF, 600.0, {"vol": 0.15, "adv_shares": 5e6}),
        ("B", AssetType.HIGH_VOLATILITY_EQUITY, 400.0, {"vol": 0.9, "adv_shares": 4e3, "spread": 0.02}),
    ],
    "two_similar": [
        ("A", AssetType.LISTED_EQUITY, 500.0, {"vol": 0.3, "adv_shares": 2e5}),
        ("B", AssetType.LISTED_EQUITY, 500.0, {"vol": 0.35, "adv_shares": 1.5e5}),
    ],
    "concentrated_big+small": [
        ("A", AssetType.LISTED_EQUITY, 900.0, {"vol": 0.25, "adv_shares": 1e6}),
        ("B", AssetType.HIGH_VOLATILITY_EQUITY, 100.0, {"vol": 0.8, "adv_shares": 2e3, "spread": 0.03}),
    ],
    "three_mixed": [
        ("A", AssetType.ETF, 300.0, {"vol": 0.12, "adv_shares": 5e6}),
        ("B", AssetType.LISTED_EQUITY, 400.0, {"vol": 0.35, "adv_shares": 5e4}),
        ("C", AssetType.HIGH_VOLATILITY_EQUITY, 300.0, {"vol": 0.85, "adv_shares": 3e3, "spread": 0.02}),
    ],
}


def main() -> None:
    engine = CollateralRiskEngine()
    summary = {}
    for name, spec in PROFILES.items():
        holdings = [Holding(a, t, q, "USD", "US") for a, t, q, _ in spec]
        market = {a: _md(a, **kw) for a, _, _, kw in spec}
        stats: Counter = Counter()
        for principal in range(5_000, 100_000, 500):
            ev = engine.evaluate("p", holdings, Loan(float(principal)), Policy.default(), market)
            if ev.margin_state not in {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}:
                continue
            plan, final, _ = _execute_to_settlement(
                engine, holdings, Loan(float(principal)), market, EXEC, TERMS, None
            )
            stats["cases"] += 1
            for ordering, info in plan.search.get("orderings", {}).items():
                if not info["monotonic_on_grid"]:
                    stats[f"non_monotone_{ordering}"] += 1
            stats[f"chosen_{plan.ordering}"] += 1
            stats["plan_restores"] += plan.restores_safety_standard
            restored = final is not None and final.margin_state not in {
                MarginState.MARGIN_CALL, MarginState.LIQUIDATION}
            stats["executed_restores"] += restored
            stats["planned_but_execution_failed"] += plan.restores_safety_standard and not restored
        summary[name] = dict(stats)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
