from __future__ import annotations

import unittest
from dataclasses import replace

from app.core.enums import AssetType, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, OrderBook, OrderBookLevel, Policy
from app.market_data.mock_provider import MockMarketDataProvider


class CollateralRiskEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = CollateralRiskEngine(audit_logger=None)
        self.policy = Policy.default()
        self.provider = MockMarketDataProvider()

    def test_diversified_liquid_portfolio_is_safe(self) -> None:
        holdings = [
            Holding("AAPL", AssetType.LISTED_EQUITY, 10),
            Holding("SPY", AssetType.ETF, 10),
        ]
        market = self.provider.get_snapshot([h.asset_id for h in holdings])
        result = self.engine.evaluate("acct_1", holdings, Loan(principal=1_000), self.policy, market)
        self.assertGreater(result.approved_credit_limit, 0)
        self.assertIn(result.margin_state, {MarginState.SAFE, MarginState.WATCH})
        self.assertGreater(result.recovery_coverage_ratio or 0, 1.0)

    def test_volatile_concentrated_asset_has_lower_effective_ltv(self) -> None:
        holdings = [Holding("NVDA", AssetType.HIGH_VOLATILITY_EQUITY, 10)]
        market = self.provider.get_snapshot(["NVDA"])
        result = self.engine.evaluate("acct_2", holdings, Loan(principal=500), self.policy, market)
        nvda = result.asset_results[0]
        self.assertLess(nvda.effective_ltv, nvda.base_ltv)
        self.assertIn("volatility", nvda.risk_drivers)
        self.assertIn("concentration", nvda.risk_drivers)

    def test_order_book_thinning_reduces_stressed_recovery(self) -> None:
        rich_book = MarketData(
            asset_id="XYZ",
            last_price=10.0,
            bid=9.99,
            ask=10.01,
            average_daily_volume=1_000_000,
            average_dollar_volume=10_000_000,
            volatility_30d=0.40,
            volatility_90d=0.40,
            data_quality_score=1.0,
            order_book=OrderBook(bids=[OrderBookLevel(price=9.99, quantity=20_000)]),
        )
        thin_book = replace(
            rich_book,
            order_book=OrderBook(bids=[OrderBookLevel(price=9.50, quantity=100)]),
        )
        holding = Holding("XYZ", AssetType.LISTED_EQUITY, 1_000)
        rich = self.engine.evaluate("acct_3a", [holding], Loan(principal=1), self.policy, {"XYZ": rich_book})
        thin = self.engine.evaluate("acct_3b", [holding], Loan(principal=1), self.policy, {"XYZ": thin_book})
        self.assertLess(thin.stressed_liquidation_value, rich.stressed_liquidation_value)

    def test_halted_asset_zero_lendable_value(self) -> None:
        holding = Holding("HALT", AssetType.LISTED_EQUITY, 100)
        market = {
            "HALT": MarketData(
                asset_id="HALT",
                last_price=10.0,
                bid=9.5,
                ask=10.5,
                volatility_30d=0.50,
                average_dollar_volume=1_000_000,
                halted=True,
            )
        }
        result = self.engine.evaluate("acct_4", [holding], Loan(principal=100), self.policy, market)
        self.assertEqual(result.asset_results[0].lendable_value, 0.0)
        self.assertEqual(result.asset_results[0].stressed_liquidation_value, 0.0)
        self.assertEqual(result.margin_state, MarginState.LIQUIDATION)

    def test_loan_above_dynamic_credit_limit_triggers_margin_call_or_liquidation(self) -> None:
        holdings = [Holding("THIN", AssetType.HIGH_VOLATILITY_EQUITY, 500)]
        market = self.provider.get_snapshot(["THIN"])
        result = self.engine.evaluate("acct_5", holdings, Loan(principal=3_000), self.policy, market)
        self.assertIn(result.margin_state, {MarginState.MARGIN_CALL, MarginState.LIQUIDATION})
        self.assertGreaterEqual(result.trigger_levels.required_cure_amount, 0.0)


if __name__ == "__main__":
    unittest.main()
