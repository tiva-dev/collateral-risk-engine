from __future__ import annotations

import json
from dataclasses import asdict

from app.audit.logger import AuditLogger
from app.core.enums import AssetType, RiskAppetite
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, Policy
from app.market_data.mock_provider import MockMarketDataProvider


def main() -> None:
    provider = MockMarketDataProvider()
    holdings = [
        Holding(asset_id="AAPL", asset_type=AssetType.LISTED_EQUITY, quantity=10),
        Holding(asset_id="NVDA", asset_type=AssetType.HIGH_VOLATILITY_EQUITY, quantity=3),
        Holding(asset_id="SPY", asset_type=AssetType.ETF, quantity=4),
    ]
    market = provider.get_snapshot([h.asset_id for h in holdings])
    policy = Policy.default()
    policy = Policy(
        base_ltv={**policy.base_ltv, AssetType.HIGH_VOLATILITY_EQUITY: 0.45},
        asset_ltv_caps=policy.asset_ltv_caps,
        risk_appetite=RiskAppetite.BALANCED,
        max_participation_rate=0.10,
    )
    loan = Loan(principal=2_100, accrued_interest=15)
    engine = CollateralRiskEngine(audit_logger=AuditLogger("./data/audit/example_audit_log.jsonl"))
    result = engine.evaluate("acct_demo_001", holdings, loan, policy, market)
    print(json.dumps(asdict(result), indent=2, default=str))


if __name__ == "__main__":
    main()
