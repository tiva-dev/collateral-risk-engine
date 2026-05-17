from __future__ import annotations

import unittest
from dataclasses import asdict
from pathlib import Path

from app.core.enums import AssetType, LifecycleDecisionValue, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, OrderBook, OrderBookLevel, Policy
from app.lifecycle.service import CreditLifecycleEngine, aggregate_holdings, apply_repayment
from app.risk.math_utils import round_money

MIN_COVERAGE_RATIO = 1e-9


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

    def test_zero_loan_origination_uses_outstanding_balance_terms(self) -> None:
        result = self.lifecycle.originate("acct_origin", self.holdings, self.policy, self.market_data)
        payload = asdict(result)

        self.assertEqual(result.decision, LifecycleDecisionValue.APPROVED)
        self.assertEqual(result.current_outstanding_balance, 0.0)
        self.assertEqual(result.projected_outstanding_balance, 0.0)
        self.assertGreater(result.approved_credit_limit, 0.0)
        safe_credit_limit = round_money(
            min(
                result.approved_credit_limit,
                result.evaluation.stressed_liquidation_value
                / max(result.evaluation.trigger_levels.dynamic_warning_coverage, MIN_COVERAGE_RATIO),
            )
        )
        self.assertEqual(result.current_available_credit, safe_credit_limit)
        self.assertEqual(result.projected_available_credit, safe_credit_limit)
        self.assertEqual(result.margin_state, MarginState.SAFE)
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)
        self.assertEqual(result.required_cure_amount, 0.0)
        self.assertEqual(result.minimum_stressed_liquidation_value, 0.0)
        self.assertIsNone(result.liquidation_plan)
        self.assertNotIn("current_loan_balance", payload)
        self.assertNotIn("projected_loan_balance", payload)

    def test_draw_within_limit_is_approved(self) -> None:
        result = self.lifecycle.check_draw(
            "acct_draw_ok",
            Loan(principal=1_000.0),
            1_000.0,
            self.holdings,
            self.policy,
            self.market_data,
        )

        self.assertEqual(result.decision, LifecycleDecisionValue.APPROVED)
        self.assertEqual(result.current_outstanding_balance, 1_000.0)
        self.assertEqual(result.projected_outstanding_balance, 2_000.0)
        projected_safe_credit_limit = round_money(
            min(
                result.approved_credit_limit,
                result.evaluation.stressed_liquidation_value
                / max(result.evaluation.trigger_levels.dynamic_warning_coverage, MIN_COVERAGE_RATIO),
            )
        )
        self.assertEqual(result.projected_available_credit, max(0.0, projected_safe_credit_limit - 2_000.0))
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)
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

        self.assertEqual(result.decision, LifecycleDecisionValue.REJECTED)
        self.assertEqual(result.reason, "requested draw exceeds projected available credit")
        self.assertEqual(result.projected_outstanding_balance, 3_981.88)
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

        self.assertEqual(result.decision, LifecycleDecisionValue.PARTIALLY_APPROVED)
        self.assertEqual(result.reason, "requested draw exceeds projected available credit")
        self.assertEqual(result.projected_outstanding_balance, 6_000.0)
        self.assertGreater(result.max_approved_draw_amount or 0.0, 0.0)
        self.assertLess(result.max_approved_draw_amount or 0.0, 5_000.0)

    def test_repayment_allocation_preserves_remaining_components(self) -> None:
        result = self.lifecycle.check_draw(
            "acct_repay",
            Loan(principal=1_000.0, accrued_interest=50.0, fees=25.0),
            requested_draw_amount=0.0,
            requested_repayment_amount=60.0,
            holdings=self.holdings,
            policy=self.policy,
            market_data=self.market_data,
        )

        self.assertEqual(result.projected_loan.principal, 1_000.0)
        self.assertEqual(result.projected_loan.accrued_interest, 15.0)
        self.assertEqual(result.projected_loan.fees, 0.0)
        self.assertEqual(result.projected_outstanding_balance, 1_015.0)
        self.assertEqual(apply_repayment(Loan(1_000.0, 50.0, 25.0), 1_200.0), Loan(0.0, 0.0, 0.0))

    def test_duplicate_holdings_are_aggregated_by_stable_asset_identity(self) -> None:
        duplicate_holdings = [
            Holding("SPY", AssetType.ETF, 40, "USD"),
            Holding("SPY", AssetType.ETF, 60, "USD"),
            Holding("SPY", AssetType.LISTED_EQUITY, 5, "USD"),
        ]

        aggregated = aggregate_holdings(duplicate_holdings)
        result = self.lifecycle.originate("acct_dupes", duplicate_holdings, self.policy, self.market_data)

        self.assertEqual(len(aggregated), 2)
        self.assertEqual(aggregated[0], Holding("SPY", AssetType.ETF, 100.0, "USD"))
        self.assertEqual(aggregated[1], Holding("SPY", AssetType.LISTED_EQUITY, 5.0, "USD"))
        self.assertEqual(len(result.asset_results), 2)
        self.assertEqual(result.asset_results[0].quantity, 100.0)

    def test_active_monitoring_safe(self) -> None:
        result = self.lifecycle.monitor("acct_monitor_safe", Loan(principal=1_000.0), self.holdings, self.policy, self.market_data)

        self.assertEqual(result.decision, LifecycleDecisionValue.SAFE)
        self.assertEqual(result.margin_state, MarginState.SAFE)
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)
        self.assertEqual(result.current_outstanding_balance, 1_000.0)
        self.assertEqual(result.projected_outstanding_balance, 1_000.0)
        self.assertEqual(result.required_cure_amount, 0.0)

    def test_active_monitoring_margin_call(self) -> None:
        result = self.lifecycle.monitor(
            "acct_monitor_margin",
            Loan(principal=5_000.0),
            self.holdings,
            self.policy,
            self.market_data,
        )

        self.assertEqual(result.decision, LifecycleDecisionValue.MARGIN_CALL)
        self.assertEqual(result.margin_state, MarginState.MARGIN_CALL)
        self.assertIsNotNone(result.liquidation_plan)

    def test_active_monitoring_liquidation(self) -> None:
        result = self.lifecycle.monitor(
            "acct_monitor_liquidation",
            Loan(principal=9_000.0),
            self.holdings,
            self.policy,
            self.market_data,
        )

        self.assertEqual(result.decision, LifecycleDecisionValue.LIQUIDATION)
        self.assertEqual(result.margin_state, MarginState.LIQUIDATION)
        self.assertGreater(result.required_cure_amount, 0.0)
        self.assertGreater(result.minimum_stressed_liquidation_value, result.evaluation.stressed_liquidation_value)
        self.assertIsNotNone(result.liquidation_plan)

    def test_readme_documents_api_contract_terms(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("/risk/evaluate", readme)
        self.assertIn("/portfolio/action/check", readme)
        self.assertIn("preferred endpoint", readme)
        self.assertIn("legacy endpoint", readme)
        self.assertIn("loan_balance", readme)
        self.assertIn("current_outstanding_balance", readme)
        self.assertIn("current_available_credit", readme)
        self.assertIn("outstanding_balance", readme)
        self.assertIn("minimum_stressed_liquidation_value", readme)
        self.assertIn("withdrawal` is an alias for `withdraw_security", readme)
        self.assertIn("rejected", readme)


if __name__ == "__main__":
    unittest.main()
