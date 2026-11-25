"""Per-factor breakdown of the v0.6.2/v0.7.0 adjustment product (run inside a checkout)."""

from __future__ import annotations

import json
import sys

from app.core.enums import AssetType
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy


def main() -> None:
    engine = CollateralRiskEngine()
    rows = []
    for vol in json.load(sys.stdin):
        market = MarketData(
            asset_id="X", last_price=100.0, bid=99.975, ask=100.025,
            average_daily_volume=10_000_000, average_dollar_volume=1_000_000_000,
            volatility_30d=vol, volatility_90d=vol, data_quality_score=1.0,
        )
        res = engine.evaluate(
            "f", [Holding("X", AssetType.LISTED_EQUITY, 10_000, "USD", "US")],
            Loan(principal=0.0), Policy.default(), {"X": market},
        )
        a = res.asset_results[0]
        rows.append(
            {
                "vol": vol, **{k: round(v, 4) for k, v in a.adjustments.as_dict().items()},
                "product": round(a.adjustments.product, 4),
                "effective_ltv": a.effective_ltv,
                "slv_ratio": round(a.stressed_liquidation_value / a.market_value, 4),
                "horizon_days": a.liquidation_horizon_days,
            }
        )
    json.dump(rows, sys.stdout)


if __name__ == "__main__":
    main()
