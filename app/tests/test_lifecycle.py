from __future__ import annotations

import unittest

from app.core.enums import AssetType, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, OrderBook, OrderBookLevel, Policy
from app.lifecycle.service import CreditLifecycleEngine


class CreditLifecycleEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lifecycle = CreditLifecycleEngine(CollateralRiskEngine(audit_logger=None), audit_logger=None)
        self.policy = Policy.default()
        self.holdings = [Holding("SPY", AssetType.ETF, 100)]
        self.market_data = {
            "SPY": MarketData(
                asset_id="SPY",
                last_price=100.0,
                bid=99.90,
                ask=100.10,
                average_daily_volume=1_000_000,
                average_dollar_volume=100_000_000,
                volatility_30d=0.15,
                volatility_90d=0.15,
                data_quality_score=1.0,
                order_book=OrderBook(bids=[OrderBookLevel(price=99.90, quantity=10_000)]),
            )
        }

    def test_zero_loan_origination(self) -> None:
        result = self.lifecycle.originate("acct_origin", self.holdings, self.policy, self.market_data)

        self.assertEqual(result.decision, "approved")
        self.assertEqual(result.current_loan_balance, 0.0)
        self.assertEqual(result.projected_loan_balance, None)
        self.assertGreater(result.approved_credit_limit, 0.0)
        self.assertEqual(result.available_credit, result.approved_credit_limit)
        self.assertEqual(result.margin_state, MarginState.SAFE)
        self.assertEqual(result.required_cure_amount, 0.0)
        self.assertIsNone(result.evaluation.liquidation_plan)

    def test_draw_within_limit_is_approved(self) -> None:
        result = self.lifecycle.check_draw(
            "acct_draw_ok",
            Loan(principal=1_000.0),
            1_000.0,
            self.holdings,
            self.policy,
            self.market_data,
        )

        self.assertEqual(result.decision, "approved")
        self.assertEqual(result.current_loan_balance, 1_000.0)
        self.assertEqual(result.projected_loan_balance, 2_000.0)
        self.assertIsNone(result.max_approved_draw_amount)

    def test_draw_above_limit_is_rejected_when_no_capacity_remains(self) -> None:
        result = self.lifecycle.check_draw(
            "acct_draw_reject",
            Loan(principal=3_881.88),
            100.0,
            self.holdings,
            self.policy,
            self.market_data,
        )

        self.assertEqual(result.decision, "rejected")
        self.assertEqual(result.projected_loan_balance, 3_981.88)
        self.assertEqual(result.max_approved_draw_amount, 0.0)

    def test_partial_draw_approval(self) -> None:
        result = self.lifecycle.check_draw(
            "acct_draw_partial",
            Loan(principal=1_000.0),
            5_000.0,
            self.holdings,
            self.policy,
            self.market_data,
        )

        self.assertEqual(result.decision, "partially_approved")
        self.assertEqual(result.projected_loan_balance, 6_000.0)
        self.assertGreater(result.max_approved_draw_amount or 0.0, 0.0)
        self.assertLess(result.max_approved_draw_amount or 0.0, 5_000.0)

    def test_active_monitoring_safe(self) -> None:
        result = self.lifecycle.monitor("acct_monitor_safe", Loan(principal=1_000.0), self.holdings, self.policy, self.market_data)

        self.assertEqual(result.decision, "safe")
        self.assertEqual(result.margin_state, MarginState.SAFE)
        self.assertEqual(result.current_loan_balance, 1_000.0)
        self.assertEqual(result.required_cure_amount, 0.0)

    def test_active_monitoring_margin_call(self) -> None:
        result = self.lifecycle.monitor(
            "acct_monitor_margin",
            Loan(principal=5_000.0),
            self.holdings,
            self.policy,
            self.market_data,
        )

        self.assertEqual(result.decision, "margin_call")
        self.assertEqual(result.margin_state, MarginState.MARGIN_CALL)
        self.assertIsNotNone(result.evaluation.liquidation_plan)

    def test_active_monitoring_liquidation(self) -> None:
        result = self.lifecycle.monitor(
            "acct_monitor_liquidation",
            Loan(principal=9_000.0),
            self.holdings,
            self.policy,
            self.market_data,
        )

        self.assertEqual(result.decision, "liquidation")
        self.assertEqual(result.margin_state, MarginState.LIQUIDATION)
        self.assertGreater(result.required_cure_amount, 0.0)
        self.assertIsNotNone(result.evaluation.liquidation_plan)


if __name__ == "__main__":
    unittest.main()
