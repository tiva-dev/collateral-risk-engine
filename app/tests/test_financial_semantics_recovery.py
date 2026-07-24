from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.core.enums import AssetType
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy
from app.liquidation.plan import collateral_injection_only_cure, repayment_only_cure


class FinancialSemanticsRecoveryTests(unittest.TestCase):
    def test_position_row_splitting_is_financially_invariant(self) -> None:
        engine = CollateralRiskEngine()
        market = {
            "ABC": MarketData(
                "ABC",
                20,
                bid=19.9,
                ask=20.1,
                average_daily_volume=100_000,
                average_dollar_volume=2_000_000,
                volatility_30d=0.25,
                timestamp=datetime(2025, 1, 2, tzinfo=timezone.utc),
            )
        }
        one = [
            Holding(
                "ABC", AssetType.LISTED_EQUITY, 1_000, "USD", "XNYS", "ABC"
            )
        ]
        split = [
            Holding("ABC", AssetType.LISTED_EQUITY, 100, "USD", "XNYS", "ABC")
            for _ in range(10)
        ]

        single_result = engine.evaluate(
            "one", one, Loan(5_000), Policy.default(), market
        )
        split_result = engine.evaluate(
            "split", split, Loan(5_000), Policy.default(), market
        )

        self.assertEqual(len(single_result.asset_results), 1)
        self.assertEqual(len(split_result.asset_results), 1)
        self.assertEqual(
            (
                single_result.portfolio_market_value,
                single_result.asset_results[0].adjustments.concentration,
                single_result.risk_adjusted_collateral_value,
                single_result.stressed_liquidation_value,
                single_result.approved_credit_limit,
                single_result.portfolio_risk_score,
                single_result.margin_state,
            ),
            (
                split_result.portfolio_market_value,
                split_result.asset_results[0].adjustments.concentration,
                split_result.risk_adjusted_collateral_value,
                split_result.stressed_liquidation_value,
                split_result.approved_credit_limit,
                split_result.portfolio_risk_score,
                split_result.margin_state,
            ),
        )
        single_hhi = sum(
            (asset.market_value / single_result.portfolio_market_value) ** 2
            for asset in single_result.asset_results
        )
        split_hhi = sum(
            (asset.market_value / split_result.portfolio_market_value) ** 2
            for asset in split_result.asset_results
        )
        self.assertEqual(single_hhi, 1)
        self.assertEqual(split_hhi, 1)

    def test_distinct_cure_formulas(self) -> None:
        cases = (
            (0, 0, 1.25, 0, 0),
            (125, 100, 1.25, 0, 0),
            (100, 100, 1.25, 20, 25),
            (0, 100, 2, 100, 200),
        )
        for stressed_value, loan, target, expected_repayment, expected_injection in cases:
            with self.subTest(
                stressed_value=stressed_value, loan=loan, target=target
            ):
                self.assertEqual(
                    repayment_only_cure(stressed_value, loan, target),
                    expected_repayment,
                )
                self.assertEqual(
                    collateral_injection_only_cure(stressed_value, loan, target),
                    expected_injection,
                )

    def test_cure_target_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            repayment_only_cure(1, 1, 0)
        with self.assertRaises(ValueError):
            collateral_injection_only_cure(1, 1, 0)


if __name__ == "__main__":
    unittest.main()
