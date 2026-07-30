from __future__ import annotations

import unittest
from dataclasses import replace

from app.core.enums import AssetType, MarginState, PortfolioActionType, RiskDecision
from app.core.evaluator import CollateralRiskEngine
from app.core.models import (
    Holding,
    Loan,
    MarketData,
    OrderBook,
    OrderBookLevel,
    Policy,
    PortfolioAction,
)
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
        result = self.engine.evaluate(
            "acct_1", holdings, Loan(principal=1_000), self.policy, market
        )
        self.assertGreater(result.approved_credit_limit, 0)
        self.assertIn(result.margin_state, {MarginState.SAFE, MarginState.WATCH})
        self.assertGreater(result.recovery_coverage_ratio or 0, 1.0)

    def test_volatile_concentrated_asset_has_lower_effective_ltv(self) -> None:
        holdings = [Holding("NVDA", AssetType.HIGH_VOLATILITY_EQUITY, 10)]
        market = self.provider.get_snapshot(["NVDA"])
        result = self.engine.evaluate(
            "acct_2", holdings, Loan(principal=500), self.policy, market
        )
        nvda = result.asset_results[0]
        self.assertLessEqual(nvda.effective_ltv, nvda.base_ltv)
        self.assertIn("volatility", nvda.risk_drivers)
        self.assertNotIn("concentration", nvda.risk_drivers)
        self.assertEqual(nvda.adjustments.concentration, 1.0)
        self.assertIsNotNone(nvda.safe_participation_rate)
        self.assertLess(nvda.safe_participation_rate or 1.0, 0.20)

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
        rich = self.engine.evaluate(
            "acct_3a", [holding], Loan(principal=1), self.policy, {"XYZ": rich_book}
        )
        thin = self.engine.evaluate(
            "acct_3b", [holding], Loan(principal=1), self.policy, {"XYZ": thin_book}
        )
        self.assertLess(
            thin.stressed_liquidation_value, rich.stressed_liquidation_value
        )

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
        result = self.engine.evaluate(
            "acct_4", [holding], Loan(principal=100), self.policy, market
        )
        self.assertEqual(result.asset_results[0].lendable_value, 0.0)
        self.assertEqual(result.asset_results[0].stressed_liquidation_value, 0.0)
        self.assertEqual(result.margin_state, MarginState.LIQUIDATION)

    def test_loan_above_dynamic_credit_limit_triggers_margin_call_or_liquidation(
        self,
    ) -> None:
        holdings = [Holding("THIN", AssetType.HIGH_VOLATILITY_EQUITY, 500)]
        market = self.provider.get_snapshot(["THIN"])
        result = self.engine.evaluate(
            "acct_5", holdings, Loan(principal=3_500), self.policy, market
        )
        self.assertIn(
            result.margin_state, {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}
        )
        self.assertGreaterEqual(result.trigger_levels.required_cure_amount, 0.0)

    def test_origination_separates_requested_draw_from_zero_outstanding_balance(
        self,
    ) -> None:
        holdings = [
            Holding("AAPL", AssetType.LISTED_EQUITY, 10),
            Holding("SPY", AssetType.ETF, 10),
        ]
        market = self.provider.get_snapshot([h.asset_id for h in holdings])
        result = self.engine.evaluate(
            "acct_origination",
            holdings,
            Loan(principal=0),
            self.policy,
            market,
            requested_draw_amount=1_000,
        )

        self.assertEqual(result.outstanding_balance, 0.0)
        self.assertEqual(result.requested_draw_amount, 1_000.0)
        self.assertEqual(result.projected_loan_balance, 1_000.0)
        self.assertEqual(result.available_credit, result.approved_credit_limit)
        self.assertEqual(
            result.projected_available_credit,
            max(0.0, result.approved_credit_limit - 1_000.0),
        )
        self.assertGreater(result.dynamic_safety_requirement, 0.0)

    def test_active_monitoring_keeps_outstanding_balance_explicit(self) -> None:
        holdings = [
            Holding("AAPL", AssetType.LISTED_EQUITY, 10),
            Holding("SPY", AssetType.ETF, 10),
        ]
        market = self.provider.get_snapshot([h.asset_id for h in holdings])
        result = self.engine.evaluate(
            "acct_monitor", holdings, Loan(principal=1_000), self.policy, market
        )

        self.assertEqual(result.outstanding_balance, 1_000.0)
        self.assertEqual(result.requested_draw_amount, 0.0)
        self.assertEqual(result.projected_loan_balance, 1_000.0)
        self.assertEqual(result.loan_balance, result.projected_loan_balance)
        self.assertEqual(
            result.available_credit, max(0.0, result.approved_credit_limit - 1_000.0)
        )

    def test_pre_trade_credit_draw_above_available_credit_is_reduced(self) -> None:
        holdings = [Holding("AAPL", AssetType.LISTED_EQUITY, 10)]
        market = self.provider.get_snapshot(["AAPL"])
        baseline = self.engine.evaluate(
            "acct_draw", holdings, Loan(principal=0), self.policy, market
        )
        result = self.engine.pre_trade_check(
            account_ref="acct_draw",
            holdings=holdings,
            loan=Loan(principal=0),
            policy=self.policy,
            market_data=market,
            actions=[
                PortfolioAction(
                    action_type=PortfolioActionType.CREDIT_DRAW,
                    amount=baseline.available_credit + 25.0,
                )
            ],
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.decision, RiskDecision.REDUCE_AVAILABLE_CREDIT)
        self.assertEqual(result.reduced_available_credit, baseline.available_credit)

    def test_pre_trade_withdrawal_that_breaks_safety_triggers_liquidation(self) -> None:
        holdings = [Holding("AAPL", AssetType.LISTED_EQUITY, 10)]
        market = self.provider.get_snapshot(["AAPL"])
        result = self.engine.pre_trade_check(
            account_ref="acct_withdraw",
            holdings=holdings,
            loan=Loan(principal=500),
            policy=self.policy,
            market_data=market,
            actions=[
                PortfolioAction(
                    action_type=PortfolioActionType.WITHDRAWAL,
                    asset_id="AAPL",
                    quantity=9,
                )
            ],
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.decision, RiskDecision.LIQUIDATION)
        self.assertGreater(result.required_repayment_amount, 0.0)

    def test_pre_trade_repayment_can_clear_projected_credit_risk(self) -> None:
        holdings = [Holding("THIN", AssetType.HIGH_VOLATILITY_EQUITY, 500)]
        market = self.provider.get_snapshot(["THIN"])
        result = self.engine.pre_trade_check(
            account_ref="acct_repay",
            holdings=holdings,
            loan=Loan(principal=3_000),
            policy=self.policy,
            market_data=market,
            actions=[
                PortfolioAction(action_type=PortfolioActionType.REPAYMENT, amount=3_000)
            ],
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.decision, RiskDecision.APPROVE)
        self.assertEqual(result.projected_loan_balance, 0.0)

    def test_legacy_pre_trade_buy_without_explicit_funding_is_rejected(self) -> None:
        holdings = [Holding("AAPL", AssetType.LISTED_EQUITY, 10)]
        market = {
            **self.provider.get_snapshot(["AAPL"]),
            "BND": MarketData(
                asset_id="BND",
                last_price=100.0,
                bid=99.0,
                ask=101.0,
                average_daily_volume=1_000_000,
                average_dollar_volume=100_000_000,
                volatility_30d=0.05,
                volatility_90d=0.05,
                data_quality_score=1.0,
            ),
        }
        result = self.engine.pre_trade_check(
            account_ref="acct_legacy_unfunded_buy",
            holdings=holdings,
            loan=Loan(principal=0),
            policy=self.policy,
            market_data=market,
            actions=[
                PortfolioAction(
                    action_type=PortfolioActionType.BUY,
                    asset_id="BND",
                    asset_type=AssetType.BOND,
                    quantity=1.0,
                )
            ],
        )

        self.assertEqual(result.decision, RiskDecision.REJECT)
        self.assertIn("buy action requires", result.reason)

    def test_legacy_pre_trade_buy_with_credit_draw_is_supported(self) -> None:
        holdings = [Holding("AAPL", AssetType.LISTED_EQUITY, 10)]
        market = {
            **self.provider.get_snapshot(["AAPL"]),
            "BND": MarketData(
                asset_id="BND",
                last_price=100.0,
                bid=99.0,
                ask=101.0,
                average_daily_volume=1_000_000,
                average_dollar_volume=100_000_000,
                volatility_30d=0.05,
                volatility_90d=0.05,
                data_quality_score=1.0,
            ),
        }
        result = self.engine.pre_trade_check(
            account_ref="acct_legacy_funded_buy",
            holdings=holdings,
            loan=Loan(principal=0),
            policy=self.policy,
            market_data=market,
            actions=[
                PortfolioAction(
                    action_type=PortfolioActionType.CREDIT_DRAW, amount=200.0
                ),
                PortfolioAction(
                    action_type=PortfolioActionType.BUY,
                    asset_id="BND",
                    asset_type=AssetType.BOND,
                    quantity=1.0,
                ),
            ],
        )

        self.assertNotEqual(result.decision, RiskDecision.REJECT)
        self.assertEqual(result.requested_draw_amount, 200.0)
        self.assertIn(
            "BND", {holding.asset_id for holding in result.projected_holdings}
        )

    def test_legacy_projection_keeps_same_asset_id_different_currency_separate(
        self,
    ) -> None:
        holdings = [
            Holding("CASHX", AssetType.CASH, 100.0, "USD"),
            Holding("CASHX", AssetType.CASH, 200.0, "EUR"),
        ]
        cash_market = MarketData(
            asset_id="CASHX",
            last_price=1.0,
            bid=1.0,
            ask=1.0,
            volatility_30d=0.0,
            volatility_90d=0.0,
            data_quality_score=1.0,
        )
        market = {holding.stable_key: cash_market for holding in holdings}
        result = self.engine.pre_trade_check(
            account_ref="acct_legacy_identity",
            holdings=holdings,
            loan=Loan(principal=0),
            policy=self.policy,
            market_data=market,
            actions=[
                PortfolioAction(action_type=PortfolioActionType.REPAYMENT, amount=1.0)
            ],
        )

        quantities = {
            (holding.asset_id, holding.asset_type, holding.currency): holding.quantity
            for holding in result.projected_holdings
        }
        self.assertEqual(quantities[("CASHX", AssetType.CASH, "USD")], 100.0)
        self.assertEqual(quantities[("CASHX", AssetType.CASH, "EUR")], 200.0)


if __name__ == "__main__":
    unittest.main()
