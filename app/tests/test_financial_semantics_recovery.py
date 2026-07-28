from __future__ import annotations

import unittest
from datetime import UTC, datetime

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
                timestamp=datetime(2025, 1, 2, tzinfo=UTC),
            )
        }
        one = [Holding("ABC", AssetType.LISTED_EQUITY, 1_000, "USD", "XNYS", "ABC")]
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
        for (
            stressed_value,
            loan,
            target,
            expected_repayment,
            expected_injection,
        ) in cases:
            with self.subTest(stressed_value=stressed_value, loan=loan, target=target):
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
        from app.simulations.scenarios.official_portfolios import (
            OfficialPortfolioScenario,
        )

        scenario = OfficialPortfolioScenario(
            "net_proceeds",
            [Holding("ABC", AssetType.LISTED_EQUITY, 10, "USD")],
            "USD",
        )
        bar = HistoricalBar(
            "ABC", date(2025, 1, 2), 10, 10, 10, 10, volume=10_000, currency="USD"
        )
        record = HistoricalReplayEngine(seed=1).replay(scenario, {"ABC": [bar]})[
            "records"
        ][0]
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
        with (
            patch("urllib.request.urlopen", side_effect=responses),
            patch("time.sleep"),
        ):
            provider._request_json({"function": "FX_DAILY"})
        self.assertEqual(provider.last_request_call_count, 2)


class RecoveryEndToEndTests(unittest.TestCase):
    def test_ineligible_asset_has_zero_borrowing_and_recovery(self) -> None:
        market = {
            "ABC": MarketData(
                "ABC",
                100,
                bid=99,
                ask=101,
                average_daily_volume=10_000,
                data_quality_score=0.10,
                timestamp=datetime(2025, 1, 2, tzinfo=UTC),
            )
        }
        result = CollateralRiskEngine().evaluate(
            "ineligible",
            [Holding("ABC", AssetType.LISTED_EQUITY, 10)],
            Loan(100),
            Policy.default(),
            market,
        )
        self.assertFalse(result.asset_results[0].eligible)
        self.assertEqual(result.approved_credit_limit, 0)
        self.assertEqual(result.stressed_liquidation_value, 0)

    def test_ngn_devaluation_is_directionally_consistent(self) -> None:
        from datetime import date

        from app.simulations.replay import (
            StressOverlay,
            build_fx_curves,
            lookup_fx_rate,
        )

        curves = build_fx_curves({("USD", "NGN"): 1_000})
        stress = StressOverlay(fx_devaluation=0.25)
        usd_ngn, _ = lookup_fx_rate(
            "USD", "NGN", curves, date(2025, 1, 2), stress=stress
        )
        ngn_usd, _ = lookup_fx_rate(
            "NGN", "USD", curves, date(2025, 1, 2), stress=stress
        )
        self.assertAlmostEqual(usd_ngn, 1_000 / 0.75)
        self.assertAlmostEqual(ngn_usd, 0.001 * 0.75)

    def test_fx_lookup_selects_latest_historical_rate(self) -> None:
        from datetime import date

        from app.simulations.replay import build_fx_curves, lookup_fx_rate

        curves = build_fx_curves(
            {
                ("USD", "NGN"): [
                    {"date": "2025-01-01", "rate": 1_500},
                    {"date": "2025-01-03", "rate": 1_600},
                ]
            }
        )
        rate, metadata = lookup_fx_rate(
            "USD", "NGN", curves, date(2025, 1, 2), stale_after_days=5
        )
        self.assertEqual(rate, 1_500)
        self.assertEqual(metadata["fx_rate_date"], "2025-01-01")

    def test_comparison_regimes_use_distinct_origination_paths(self) -> None:
        from datetime import date

        from app.historical_data.models import HistoricalBar
        from app.simulations.replay import (
            COMMON_EXPOSURE,
            POLICY_ORIGINATION,
            HistoricalReplayEngine,
        )
        from app.simulations.scenarios.official_portfolios import (
            OfficialPortfolioScenario,
        )

        scenario = OfficialPortfolioScenario(
            "regimes",
            [Holding("ABC", AssetType.LISTED_EQUITY, 10)],
            "USD",
        )
        bars = {
            "ABC": [
                HistoricalBar(
                    "ABC",
                    date(2025, 1, 2),
                    100,
                    101,
                    99,
                    100,
                    adjusted_close=100,
                    volume=1_000_000,
                )
            ]
        }
        common = HistoricalReplayEngine(seed=1).replay(
            scenario, bars, comparison_regime=COMMON_EXPOSURE
        )
        originated = HistoricalReplayEngine(seed=1).replay(
            scenario, bars, comparison_regime=POLICY_ORIGINATION
        )
        common_balances = {
            common["records"][0]["total_obligation"],
            common["baseline_results"]["flat_ltv"][0]["total_obligation"],
            common["baseline_results"]["static_haircut"][0]["total_obligation"],
        }
        originated_balances = {
            originated["records"][0]["total_obligation"],
            originated["baseline_results"]["flat_ltv"][0]["total_obligation"],
            originated["baseline_results"]["static_haircut"][0]["total_obligation"],
        }
        self.assertEqual(len(common_balances), 1)
        self.assertGreater(len(originated_balances), 1)

    def test_dataset_loader_fails_when_manifest_cache_is_missing(self) -> None:
        import tempfile
        from pathlib import Path

        from app.simulations.run_official_validation import _load_replay_inputs

        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing-normalized-cache.json"
            with self.assertRaises(FileNotFoundError):
                _load_replay_inputs({"cache_paths": [str(missing)]})

    def test_mocked_provider_to_evidence_qa_and_calibration(self) -> None:
        import json
        import tempfile
        from datetime import date
        from pathlib import Path
        from unittest.mock import patch

        from app.historical_data.alpha_vantage import (
            AlphaVantageHistoricalProvider,
        )
        from app.historical_data.cache import HistoricalDataCache, content_hash
        from app.historical_data.manifest import write_manifest
        from app.historical_data.models import HistoricalDatasetManifest
        from app.simulations.calibration import generate_calibration_diagnostics
        from app.simulations.evidence_quality import validate_evidence_package
        from app.simulations.metrics import compute_simulation_metrics
        from app.simulations.replay import (
            COMMON_EXPOSURE,
            POLICY_ORIGINATION,
            HistoricalReplayEngine,
        )
        from app.simulations.reporting import generate_evidence_package
        from app.simulations.run_official_validation import _load_replay_inputs
        from app.simulations.scenarios.official_portfolios import (
            OfficialPortfolioScenario,
        )

        response = {
            "Time Series (Daily)": {
                "2025-01-02": {
                    "1. open": "100",
                    "2. high": "101",
                    "3. low": "99",
                    "4. close": "100",
                    "5. adjusted close": "100",
                    "6. volume": "1000000",
                },
                "2025-01-03": {
                    "1. open": "90",
                    "2. high": "91",
                    "3. low": "89",
                    "4. close": "90",
                    "5. adjusted close": "90",
                    "6. volume": "900000",
                },
            }
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            provider = AlphaVantageHistoricalProvider(
                HistoricalDataCache(temporary_directory)
            )
            with patch.object(provider, "_request_json", return_value=response):
                provider.fetch_equity_history(
                    "AAPL",
                    date(2025, 1, 2),
                    date(2025, 1, 3),
                    force_refresh=True,
                )
            dataset = HistoricalDatasetManifest(
                dataset_id="fixture-dataset",
                provider="alpha_vantage",
                universe={"fixture": True},
                instruments=["AAPL"],
                fx_pairs=[],
                start_date=date(2025, 1, 2),
                end_date=date(2025, 1, 3),
                cache_paths=provider.cache_paths,
                provider_coverage_summary={
                    "alpha_vantage": {"requested": 1, "available": 1}
                },
                earliest_available_date_by_symbol={"AAPL": date(2025, 1, 2)},
            )
            manifest_path = write_manifest(dataset, temporary_directory)
            manifest = json.loads(manifest_path.read_text())
            bars, fx = _load_replay_inputs(manifest)
            self.assertFalse(fx)
            scenario = OfficialPortfolioScenario(
                "fixture",
                [Holding("AAPL", AssetType.LISTED_EQUITY, 10)],
                "USD",
            )
            results = []
            for regime in (COMMON_EXPOSURE, POLICY_ORIGINATION):
                replay = HistoricalReplayEngine(manifest, seed=7).replay(
                    scenario, bars, comparison_regime=regime
                )
                replay.update(
                    {
                        "base_scenario": "fixture",
                        "stress_name": "baseline",
                        "synthetic_data_used": False,
                    }
                )
                results.append(replay)
            metrics = [
                compute_simulation_metrics(result, manifest=manifest)
                for result in results
            ]
            files = generate_evidence_package(
                results,
                metrics,
                temporary_directory,
                {
                    "dataset_manifest": manifest,
                    "dataset_manifest_identity": manifest["checksum"],
                    "seed": 7,
                },
            )
            qa = validate_evidence_package(files)
            self.assertTrue(qa["passed"], qa["blocking_errors"])
            saved_records = json.loads(
                Path(files["official_validation_records.json"]).read_text()
            )
            recomputed = [
                compute_simulation_metrics(result, manifest=manifest)
                for result in saved_records
            ]
            self.assertEqual(content_hash(recomputed), content_hash(metrics))
            calibration = generate_calibration_diagnostics(metrics, temporary_directory)
            self.assertTrue(calibration["scenarios"])
            saved_records[0]["missing_fx_dates"] = ["2025-01-02"]
            Path(files["official_validation_records.json"]).write_text(
                json.dumps(saved_records), encoding="utf-8"
            )
            manifest_file = Path(files["official_validation_manifest.json"])
            evidence_manifest = json.loads(manifest_file.read_text())
            evidence_manifest["results_checksum"] = content_hash(saved_records)
            evidence_manifest["artifact_checksums"][
                "official_validation_records.json"
            ] = content_hash(saved_records)
            manifest_file.write_text(
                json.dumps(evidence_manifest), encoding="utf-8"
            )
            missing_fx_qa = validate_evidence_package(files)
            self.assertFalse(missing_fx_qa["passed"])
            self.assertTrue(
                any(
                    "provider-backed baseline has missing FX observations" in error
                    for error in missing_fx_qa["blocking_errors"]
                )
            )
            saved_records[0]["missing_fx_dates"] = []
            saved_records[0]["required_instruments"] = ["AAPL", "MISSING"]
            Path(files["official_validation_records.json"]).write_text(
                json.dumps(saved_records), encoding="utf-8"
            )
            evidence_manifest["results_checksum"] = content_hash(saved_records)
            evidence_manifest["artifact_checksums"][
                "official_validation_records.json"
            ] = content_hash(saved_records)
            manifest_file.write_text(
                json.dumps(evidence_manifest), encoding="utf-8"
            )
            partial_portfolio_qa = validate_evidence_package(files)
            self.assertFalse(partial_portfolio_qa["passed"])
            self.assertTrue(
                any(
                    "provider-backed baseline contains a partial portfolio" in error
                    for error in partial_portfolio_qa["blocking_errors"]
                )
            )
            Path(files["official_validation_metrics.json"]).write_text(
                "[]", encoding="utf-8"
            )
            tampered_qa = validate_evidence_package(files)
            self.assertFalse(tampered_qa["passed"])
            self.assertTrue(tampered_qa["blocking_errors"])


if __name__ == "__main__":
    unittest.main()
