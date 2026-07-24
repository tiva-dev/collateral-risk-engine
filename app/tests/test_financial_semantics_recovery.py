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


class ReviewRegressionTests(unittest.TestCase):
    def test_replay_does_not_charge_stressed_haircut_as_an_extra_cost(self) -> None:
        from datetime import date
        from app.historical_data.models import HistoricalBar
        from app.simulations.replay import HistoricalReplayEngine
        from app.simulations.scenarios.official_portfolios import OfficialPortfolioScenario

        scenario = OfficialPortfolioScenario(
            "net_proceeds",
            [Holding("ABC", AssetType.LISTED_EQUITY, 10, "USD")],
            "USD",
        )
        bar = HistoricalBar(
            "ABC", date(2025, 1, 2), 10, 10, 10, 10, volume=10_000, currency="USD"
        )
        record = HistoricalReplayEngine(seed=1).replay(
            scenario, {"ABC": [bar]}
        )["records"][0]
        obligation = record["total_obligation"]
        proceeds = record["stressed_liquidation_proceeds"]
        self.assertEqual(record["liquidation_costs"], 0.0)
        self.assertEqual(
            record["economic_recovery_shortfall"], max(0.0, obligation - proceeds)
        )
        self.assertEqual(
            record["recovery_coverage_ratio"], proceeds / max(obligation, 1e-9)
        )

    def test_lifecycle_aggregation_preserves_venue_and_provider_identity(self) -> None:
        from app.lifecycle.service import aggregate_holdings

        holdings = [
            Holding("ABC", AssetType.LISTED_EQUITY, 10, "USD", "XNYS", "p1"),
            Holding("ABC", AssetType.LISTED_EQUITY, 20, "USD", "XNAS", "p2"),
        ]
        aggregated = aggregate_holdings(holdings)
        self.assertEqual(len(aggregated), 2)
        self.assertEqual({holding.exchange for holding in aggregated}, {"XNYS", "XNAS"})
        self.assertEqual({holding.provider_id for holding in aggregated}, {"p1", "p2"})

    def test_metrics_use_breach_fields_and_persisted_liquidation_plans(self) -> None:
        from app.simulations.metrics import compute_simulation_metrics

        record = {
            "date": "2025-01-02",
            "credit_limit_breach": 10.0,
            "margin_state": "margin_call",
            "with_interest_balance": 100.0,
            "without_interest_balance": 95.0,
            "total_obligation": 100.0,
            "lifecycle_safe_credit_limit": 90.0,
            "economic_recovery_shortfall": 10.0,
            "recovery_coverage_ratio": 0.8,
            "liquidation_plan": {
                "plan_complete": False,
                "unrecovered_target_amount": 7.5,
            },
        }
        baseline = {"date": record["date"], "credit_limit_breach": 10.0}
        metrics = compute_simulation_metrics(
            {
                "records": [record],
                "baseline_results": {
                    "dynamic_engine": [baseline],
                    "flat_ltv": [baseline],
                    "static_haircut": [baseline],
                },
            }
        )
        outcome = metrics["dynamic_engine_versus_static_ltv_outcome_table"][0]
        self.assertEqual(outcome["dynamic_credit_limit_breach"], 10.0)
        self.assertEqual(metrics["false_trigger_proxy"], 0.0)
        self.assertEqual(metrics["interest_contribution_to_margin_events"], 5.0)
        self.assertEqual(metrics["liquidation_plan_completeness"], 0.0)
        self.assertEqual(metrics["unrecovered_liquidation_target"], 7.5)

    def test_empty_direct_evaluation_is_not_reported_as_currency_mismatch(self) -> None:
        from fastapi import HTTPException
        from app.api.routes import evaluate_risk
        from app.api.schemas import EvaluateRequest

        request = EvaluateRequest.model_validate(
            {
                "account_ref": "empty",
                "holdings": [],
                "loan": {"principal": 0, "currency": "USD"},
                "market_data": {},
                "policy": {
                    "base_ltv": {"listed_equity": 0.5},
                },
            }
        )
        with self.assertRaises(HTTPException) as caught:
            evaluate_risk(request)
        self.assertNotIn("normalized to loan currency", str(caught.exception.detail))

    def test_alpha_vantage_reports_retry_attempt_count(self) -> None:
        import json
        from unittest.mock import patch
        from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider

        class Response:
            headers = {}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        responses = [
            Response({"Note": "quota"}),
            Response({"Time Series FX (Daily)": {}}),
        ]
        provider = AlphaVantageHistoricalProvider()
        with patch("urllib.request.urlopen", side_effect=responses), patch("time.sleep"):
            provider._request_json({"function": "FX_DAILY"})
        self.assertEqual(provider.last_request_call_count, 2)


if __name__ == "__main__":
    unittest.main()
