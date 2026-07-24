from datetime import datetime, timezone

import pytest

from app.core.enums import AssetType
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy
from app.liquidation.plan import collateral_injection_only_cure, repayment_only_cure


def test_position_row_splitting_is_financially_invariant() -> None:
    engine = CollateralRiskEngine()
    market = {"ABC": MarketData("ABC", 20, bid=19.9, ask=20.1,
        average_daily_volume=100_000, average_dollar_volume=2_000_000,
        volatility_30d=.25, timestamp=datetime(2025, 1, 2, tzinfo=timezone.utc))}
    one = [Holding("ABC", AssetType.LISTED_EQUITY, 1_000, "USD", "XNYS", "ABC")]
    split = [Holding("ABC", AssetType.LISTED_EQUITY, 100, "USD", "XNYS", "ABC") for _ in range(10)]
    a = engine.evaluate("one", one, Loan(5_000), Policy.default(), market)
    b = engine.evaluate("split", split, Loan(5_000), Policy.default(), market)
    assert len(a.asset_results) == len(b.asset_results) == 1
    assert (a.portfolio_market_value, a.asset_results[0].adjustments.concentration,
            a.risk_adjusted_collateral_value, a.stressed_liquidation_value,
            a.approved_credit_limit, a.portfolio_risk_score, a.margin_state) == (
            b.portfolio_market_value, b.asset_results[0].adjustments.concentration,
            b.risk_adjusted_collateral_value, b.stressed_liquidation_value,
            b.approved_credit_limit, b.portfolio_risk_score, b.margin_state)
    # A single-name portfolio's HHI is one in either representation.
    assert sum((x.market_value / a.portfolio_market_value) ** 2 for x in a.asset_results) == 1
    assert sum((x.market_value / b.portfolio_market_value) ** 2 for x in b.asset_results) == 1


@pytest.mark.parametrize("slv,loan,target,repay,inject", [
    (0, 0, 1.25, 0, 0), (125, 100, 1.25, 0, 0),
    (100, 100, 1.25, 20, 25), (0, 100, 2, 100, 200),
])
def test_distinct_cure_formulas(slv, loan, target, repay, inject) -> None:
    assert repayment_only_cure(slv, loan, target) == repay
    assert collateral_injection_only_cure(slv, loan, target) == inject


def test_cure_target_must_be_positive() -> None:
    with pytest.raises(ValueError):
        repayment_only_cure(1, 1, 0)
    with pytest.raises(ValueError):
        collateral_injection_only_cure(1, 1, 0)
