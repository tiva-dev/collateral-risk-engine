import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.core.enums import AssetType, RiskAppetite
from app.core.models import Holding
from app.credit.interest import InterestPolicy
from app.historical_data.models import HistoricalBar, HistoricalFXRate
from app.simulations.metrics import compute_simulation_metrics
from app.simulations.replay import HistoricalReplayEngine, StressOverlay, convert_market_data_currency, historical_bar_to_market_data
from app.simulations.run_official_validation import _load_replay_inputs, _synthetic_thin_bars
from app.simulations.scenarios.official_portfolios import OfficialPortfolioScenario


class V053ReadinessHardeningTests(unittest.TestCase):
    def test_missing_fx_zeroes_market_data_and_creates_shortfall_metric(self):
        bar = HistoricalBar("MTNN", date(2024, 1, 1), 100, 100, 100, 100, volume=1000, currency="NGN")
        md, missing = convert_market_data_currency(historical_bar_to_market_data(bar), "USD", {}, as_of=date(2024, 1, 1))
        self.assertTrue(missing)
        self.assertEqual(md.last_price, 0)
        self.assertIsNone(md.bid)
        self.assertIsNone(md.ask)
        self.assertEqual(md.average_dollar_volume, 0)
        self.assertIsNone(md.order_book)
        self.assertTrue(md.metadata["missing_required_fx"])

        scenario = OfficialPortfolioScenario("fx_gap", [Holding("MTNN", AssetType.LISTED_EQUITY, 10, "NGN")], "USD")
        result = HistoricalReplayEngine(seed=1).replay(scenario, {"MTNN": [bar]}, fx_rates={})
        self.assertGreater(result["records"][0]["fx_missing"], 0)
        self.assertGreater(result["records"][0]["shortfall"], 0)
        self.assertEqual(compute_simulation_metrics(result)["fx_missing_events"], 1)

    def test_time_indexed_fx_inverse_and_stale_flags(self):
        bars = [HistoricalBar("MTNN", date(2024, 1, 1), 100, 100, 100, 100, volume=1000, currency="NGN"), HistoricalBar("MTNN", date(2024, 1, 3), 100, 100, 100, 100, volume=1000, currency="NGN")]
        fx = {("USD", "NGN"): [HistoricalFXRate("USD", "NGN", 1000, date(2024, 1, 1))]}
        scenario = OfficialPortfolioScenario("fx_curve", [Holding("MTNN", AssetType.LISTED_EQUITY, 1, "NGN")], "USD")
        result = HistoricalReplayEngine(seed=1).replay(scenario, {"MTNN": bars}, fx_rates=fx)
        self.assertAlmostEqual(result["records"][0]["collateral_value"], 0.1)
        self.assertFalse(result["records"][0]["fx_missing"])

        old_fx = {("NGN", "USD"): [HistoricalFXRate("NGN", "USD", 0.001, date(2023, 12, 1))]}
        stale = HistoricalReplayEngine(seed=1).replay(scenario, {"MTNN": bars}, fx_rates=old_fx)
        self.assertTrue(stale["records"][0]["fx_stale"])

    def test_normalized_cache_loader_and_provider_native_warning_tolerance(self):
        with tempfile.TemporaryDirectory() as td:
            bar_path = Path(td) / "bar.json"; bar_path.write_text(json.dumps({"data":{"instrument":"AAPL","provider_name":"mock","retrieved_at":"2024-01-02T00:00:00+00:00","start_date":"2024-01-01","end_date":"2024-01-01","warnings":[],"data_quality_summary":{},"bars":[{"timestamp":"2024-01-01","open":10,"high":10,"low":10,"close":10,"volume":100,"currency":"USD"}]}}))
            fx_path = Path(td) / "fx.json"; fx_path.write_text(json.dumps({"data":{"from_currency":"EUR","to_currency":"USD","provider_name":"mock","retrieved_at":"2024-01-02T00:00:00+00:00","start_date":"2024-01-01","end_date":"2024-01-01","warnings":[],"data_quality_summary":{},"rates":[{"timestamp":"2024-01-01","rate":1.1}]}}))
            native_path = Path(td) / "native.json"; native_path.write_text(json.dumps({"data":{"Time Series (Daily)":{"2024-01-01":{"4. close":"10"}}}}))
            bars, fx = _load_replay_inputs({"cache_paths":[str(bar_path), str(fx_path), str(native_path)]})
            self.assertIn("AAPL", bars)
            self.assertIn(("EUR", "USD"), fx)

    def test_baselines_metrics_policy_interest_and_synthetic_thin(self):
        bars = {"AAPL":[HistoricalBar("AAPL", date(2024, 1, 1), 100,100,100,100, volume=1000), HistoricalBar("AAPL", date(2024, 2, 1), 80,80,80,80, volume=1000)]}
        conservative = OfficialPortfolioScenario("p", [Holding("AAPL", AssetType.LISTED_EQUITY, 10)], "USD", base_ltv_policy=0.5, risk_appetite=RiskAppetite.CONSERVATIVE, loan_terms=InterestPolicy(0.36))
        aggressive = OfficialPortfolioScenario("p", [Holding("AAPL", AssetType.LISTED_EQUITY, 10)], "USD", base_ltv_policy=0.8, risk_appetite=RiskAppetite.AGGRESSIVE, loan_terms=InterestPolicy(0.0))
        r1 = HistoricalReplayEngine(seed=1).replay(conservative, bars)
        r2 = HistoricalReplayEngine(seed=1).replay(aggressive, bars)
        self.assertIn("flat_ltv", r1["baseline_results"])
        self.assertIn("static_haircut", r1["baseline_results"])
        self.assertIn("dynamic_engine", r1["baseline_results"])
        self.assertAlmostEqual(r1["baseline_results"]["flat_ltv"][0]["credit_limit"], 500.0)
        configured_flat = HistoricalReplayEngine(seed=1).replay(conservative, bars, flat_ltv=0.65)
        self.assertAlmostEqual(configured_flat["baseline_results"]["flat_ltv"][0]["credit_limit"], 650.0)
        self.assertNotEqual(r1["records"][0]["credit_limit"], r2["records"][0]["credit_limit"])
        self.assertGreater(r1["records"][-1]["loan_balance"], r1["records"][-1]["principal"])
        metrics = compute_simulation_metrics(r1)
        self.assertIn("dynamic_engine_versus_static_ltv_outcome_table", metrics)
        self.assertIsInstance(metrics["shortfall_reduction_versus_flat_ltv"], float)
        self.assertEqual(_synthetic_thin_bars(date(2024,1,1), date(2024,1,3), 7), _synthetic_thin_bars(date(2024,1,1), date(2024,1,3), 7))

    def test_stress_overlay_identity_surfaces_in_metrics(self):
        result = {
            "scenario": "retail_stress::price_gap",
            "base_scenario": "retail_stress",
            "stress_name": "price_gap",
            "records": [],
            "events": [],
            "baseline_results": {},
        }
        metrics = compute_simulation_metrics(result)
        self.assertEqual(metrics["scenario"], "retail_stress::price_gap")
        self.assertEqual(metrics["base_scenario"], "retail_stress")
        self.assertEqual(metrics["stress_name"], "price_gap")


if __name__ == "__main__":
    unittest.main()
