from __future__ import annotations

import json
from dataclasses import asdict

from app.core.enums import AssetType
from app.core.models import Holding, Loan, Policy
from app.market_data.mock_provider import MockMarketDataProvider
from app.simulations.backtester import compare_flat_ltv_to_dynamic_engine


def main() -> None:
    holdings = [
        Holding(
            asset_id="NVDA", asset_type=AssetType.HIGH_VOLATILITY_EQUITY, quantity=6
        ),
        Holding(
            asset_id="THIN", asset_type=AssetType.HIGH_VOLATILITY_EQUITY, quantity=800
        ),
    ]
    market = MockMarketDataProvider().get_snapshot([h.asset_id for h in holdings])
    loan = Loan(principal=5_000, accrued_interest=40)
    results = compare_flat_ltv_to_dynamic_engine(
        account_ref="acct_stress_001",
        holdings=holdings,
        loan=loan,
        policy=Policy.default(),
        market_data=market,
        flat_ltv=0.70,
    )
    print(json.dumps([asdict(r) for r in results], indent=2, default=str))


if __name__ == "__main__":
    main()
