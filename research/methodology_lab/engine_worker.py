"""Run the operational CRI engine of one checkout on a batch of requests.

Invoked as a subprocess with the checkout on PYTHONPATH, so v0.6.2 and v0.7.0
can be compared on identical inputs without importing both ``app`` packages
into one interpreter. Reads a JSON list of requests from stdin and writes a
JSON list of results to stdout. Uses ``Policy.default()`` of that checkout.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from app.core.enums import AssetType
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy
from app.lifecycle.service import CreditLifecycleEngine


def _principal_capacity(obligation_capacity: float, term_days: int, rate: float) -> float | None:
    try:
        from app.credit.interest import (
            InterestPolicy,
            principal_capacity_from_obligation,
        )
    except ImportError:
        return None
    try:
        terms = InterestPolicy(
            annual_interest_rate=rate, payment_frequency="at_maturity", term_days=term_days
        )
    except TypeError:
        return None
    return principal_capacity_from_obligation(
        obligation_capacity, terms, datetime(2026, 1, 1, tzinfo=UTC)
    )


def main() -> None:
    requests = json.load(sys.stdin)
    engine = CollateralRiskEngine()
    lifecycle = CreditLifecycleEngine(risk_engine=engine)
    policy = Policy.default()
    out = []
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for req in requests:
        holdings, market = [], {}
        for a in req["assets"]:
            asset_id = a["id"]
            holdings.append(
                Holding(asset_id, AssetType(a["type"]), a["quantity"], "USD", "US")
            )
            fields = {
                "asset_id": asset_id,
                "last_price": a["price"],
                "bid": a["price"] * (1 - a["spread"] / 2),
                "ask": a["price"] * (1 + a["spread"] / 2),
                "average_daily_volume": a["adv_value"] / a["price"],
                "average_dollar_volume": a["adv_value"],
                "volatility_30d": a["vol30"],
                "volatility_90d": a["vol90"],
                "timestamp": now,
                "data_quality_score": 1.0,
            }
            optional = {
                "volatility_252d": a.get("vol252"),
                "max_drawdown_252d": a.get("mdd252"),
                "max_gap_252d": a.get("gap252"),
            }
            names = MarketData.__dataclass_fields__
            fields.update({k: v for k, v in optional.items() if k in names})
            market[asset_id] = MarketData(**fields)
        result = engine.evaluate("lab", holdings, Loan(principal=0.0), policy, market)
        obligation_capacity = lifecycle._safe_credit_limit(result)
        principal = _principal_capacity(obligation_capacity, req["term_days"], req["rate"])
        out.append(
            {
                "id": req["id"],
                "mv": result.portfolio_market_value,
                "approved": result.approved_credit_limit,
                "safe_obligation": obligation_capacity,
                "principal": obligation_capacity if principal is None else principal,
                "risk_score": result.portfolio_risk_score,
            }
        )
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
