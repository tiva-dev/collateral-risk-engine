from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.enums import AssetType, DataMode
from app.core.models import MarketData
from app.api.routes import normalize_market_data
from app.api.schemas import MarketDataNormalizeRequest
from app.market_data.aggregator import MarketDataAggregator, ProviderRouter
from app.market_data.identity import InstrumentIdentity
from app.market_data.policy import FXPolicy, MarketDataPolicy
from app.market_data.providers import (
    FXRate,
    MarketStatus,
    MockEquityProvider,
    MockFXProvider,
    RawQuote,
)


class MarketDataAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.aapl = InstrumentIdentity("AAPL", "AAPL", "NASDAQ", "USD", AssetType.LISTED_EQUITY)
        self.mtnn = InstrumentIdentity("MTNN", "MTNN", "NGX", "NGN", AssetType.LISTED_EQUITY)

    def test_asset_currency_same_as_loan_currency_no_fx_needed(self) -> None:
        result = MarketDataAggregator().normalize([self.aapl], loan_currency="USD", data_mode=DataMode.PROVIDED_BY_US)
        normalized = result.normalized_market_data["AAPL"]
        self.assertEqual(normalized.local_price, normalized.converted_price)
        self.assertIsNone(normalized.fx_rate_used)
        self.assertNotIn("missing_required_fx", normalized.warnings)

    def test_ngn_loan_and_ngn_asset_no_fx_needed(self) -> None:
        result = MarketDataAggregator().normalize([self.mtnn], loan_currency="NGN", data_mode=DataMode.PROVIDED_BY_US)
        normalized = result.normalized_market_data["MTNN"]
        self.assertEqual(normalized.local_currency, "NGN")
        self.assertEqual(normalized.loan_currency, "NGN")
        self.assertEqual(normalized.local_price, normalized.converted_price)
        self.assertIsNone(normalized.fx_rate_used)

    def test_usd_loan_and_ngn_asset_requires_fx(self) -> None:
        result = MarketDataAggregator().normalize([self.mtnn], loan_currency="USD", data_mode=DataMode.PROVIDED_BY_US)
        normalized = result.normalized_market_data["MTNN"]
        self.assertEqual(normalized.fx_rate_used, 0.00067)
        self.assertAlmostEqual(normalized.converted_price, 275.0 * 0.00067)
        self.assertEqual(normalized.fx_source, "provided_by_us")

    def test_client_supplied_daily_fx_accepted_when_fresh_enough(self) -> None:
        result = MarketDataAggregator().normalize(
            [self.mtnn],
            loan_currency="USD",
            data_mode=DataMode.HYBRID,
            client_supplied_fx_rates={("NGN", "USD"): FXRate("NGN", "USD", 0.0007, self.now, quality_score=0.99)},
        )
        normalized = result.normalized_market_data["MTNN"]
        self.assertEqual(normalized.fx_source, "client_supplied")
        self.assertEqual(normalized.fx_rate_used, 0.0007)
        self.assertGreater(normalized.data_quality_score, 0.9)

    def test_client_supplied_stale_fx_receives_quality_haircut(self) -> None:
        stale = self.now - timedelta(days=3)
        policy = MarketDataPolicy(fx=FXPolicy(allow_fallback_provider=False, max_fx_age_minutes=60, stale_fx_haircut=0.5))
        quote = RawQuote(self.mtnn, 275.0, timestamp=self.now, source="client_supplied", provider_name="client", data_quality_score=1.0)
        result = MarketDataAggregator().normalize(
            [self.mtnn],
            loan_currency="USD",
            data_mode=DataMode.CLIENT_SUPPLIED,
            market_data_policy=policy,
            client_supplied_quotes={"MTNN": quote},
            client_supplied_fx_rates={("NGN", "USD"): FXRate("NGN", "USD", 0.0007, stale, quality_score=1.0)},
        )
        normalized = result.normalized_market_data["MTNN"]
        self.assertIn("stale_fx", normalized.warnings)
        self.assertLess(normalized.data_quality_score, 0.6)

    def test_fallback_provider_fx_used_when_client_fx_stale_and_fallback_allowed(self) -> None:
        stale = self.now - timedelta(days=3)
        policy = MarketDataPolicy(fx=FXPolicy(max_fx_age_minutes=60, allow_fallback_provider=True))
        result = MarketDataAggregator().normalize(
            [self.mtnn],
            loan_currency="USD",
            data_mode=DataMode.HYBRID,
            market_data_policy=policy,
            client_supplied_fx_rates={("NGN", "USD"): FXRate("NGN", "USD", 0.0008, stale, quality_score=1.0)},
        )
        normalized = result.normalized_market_data["MTNN"]
        self.assertEqual(normalized.fx_source, "provided_by_us")
        self.assertEqual(normalized.fx_rate_used, 0.00067)

    def test_conservative_fx_selection_chooses_lower_collateral_value(self) -> None:
        policy = MarketDataPolicy(fx=FXPolicy(use_conservative_rate_when_sources_disagree=True))
        result = MarketDataAggregator().normalize(
            [self.mtnn],
            loan_currency="USD",
            data_mode=DataMode.HYBRID,
            market_data_policy=policy,
            client_supplied_fx_rates={("NGN", "USD"): FXRate("NGN", "USD", 0.0009, self.now, quality_score=1.0)},
        )
        normalized = result.normalized_market_data["MTNN"]
        self.assertEqual(normalized.fx_rate_used, 0.00067)
        self.assertIn("conservative_fx_rate_selected", normalized.warnings)

    def test_missing_fx_causes_low_data_quality_and_warning(self) -> None:
        quote = RawQuote(self.mtnn, 275.0, timestamp=self.now, source="client_supplied", provider_name="client")
        result = MarketDataAggregator().normalize(
            [self.mtnn],
            loan_currency="USD",
            data_mode=DataMode.CLIENT_SUPPLIED,
            client_supplied_quotes={"MTNN": quote},
        )
        normalized = result.normalized_market_data["MTNN"]
        self.assertIn("missing_required_fx", normalized.warnings)
        self.assertLessEqual(normalized.data_quality_score, 0.05)

    def test_stale_quote_reduces_data_quality(self) -> None:
        stale_quote = RawQuote(self.aapl, 190.0, timestamp=self.now - timedelta(hours=3), source="client_supplied", provider_name="client", data_quality_score=1.0)
        policy = MarketDataPolicy(max_quote_age_minutes_by_exchange={"NASDAQ": 10}, stale_quote_haircut=0.5)
        result = MarketDataAggregator().normalize(
            [self.aapl],
            loan_currency="USD",
            data_mode=DataMode.CLIENT_SUPPLIED,
            market_data_policy=policy,
            client_supplied_quotes={"AAPL": stale_quote},
        )
        normalized = result.normalized_market_data["AAPL"]
        self.assertIn("stale_quote", normalized.warnings)
        self.assertLess(normalized.data_quality_score, 0.6)

    def test_market_closed_adds_warning_but_does_not_automatically_reject(self) -> None:
        air = InstrumentIdentity("AIR", "AIR", "XPAR", "EUR", AssetType.LISTED_EQUITY)
        result = MarketDataAggregator().normalize([air], loan_currency="USD", data_mode=DataMode.PROVIDED_BY_US)
        normalized = result.normalized_market_data["AIR"]
        self.assertIn("market_closed", normalized.warnings)
        self.assertGreater(normalized.data_quality_score, 0.5)

    def test_halted_market_maps_to_halted_data(self) -> None:
        equity = MockEquityProvider(market_statuses={"NASDAQ": MarketStatus.HALTED})
        result = MarketDataAggregator(equity_provider=equity).normalize([self.aapl], loan_currency="USD", data_mode=DataMode.PROVIDED_BY_US)
        normalized = result.normalized_market_data["AAPL"]
        self.assertIn("halted", normalized.warnings)
        self.assertTrue(normalized.to_market_data().halted)

    def test_hybrid_mode_prefers_valid_client_supplied_data(self) -> None:
        quote = RawQuote(self.aapl, 195.0, timestamp=self.now, source="client_supplied", provider_name="client", data_quality_score=0.99)
        result = MarketDataAggregator().normalize(
            [self.aapl],
            loan_currency="USD",
            data_mode=DataMode.HYBRID,
            client_supplied_quotes={"AAPL": quote},
        )
        normalized = result.normalized_market_data["AAPL"]
        self.assertEqual(normalized.source, "client_supplied")
        self.assertEqual(normalized.converted_price, 195.0)

    def test_provided_by_us_mode_uses_mock_provider(self) -> None:
        result = MarketDataAggregator().normalize([self.aapl], loan_currency="USD", data_mode=DataMode.PROVIDED_BY_US)
        normalized = result.normalized_market_data["AAPL"]
        self.assertEqual(normalized.provider_name, "mock_equity_provider")
        self.assertEqual(normalized.converted_price, 190.0)

    def test_client_supplied_mode_downgrades_missing_client_data(self) -> None:
        result = MarketDataAggregator().normalize([self.aapl], loan_currency="USD", data_mode=DataMode.CLIENT_SUPPLIED)
        self.assertIn("AAPL", result.missing_data)
        self.assertEqual(result.quality_report["AAPL"], 0.0)
        self.assertIn("missing_client_supplied_quote", result.warnings_by_instrument["AAPL"])

    def test_normalized_output_converts_into_existing_market_data(self) -> None:
        result = MarketDataAggregator().normalize([self.mtnn], loan_currency="USD", data_mode=DataMode.PROVIDED_BY_US)
        market_data = result.normalized_market_data["MTNN"].to_market_data()
        self.assertIsInstance(market_data, MarketData)
        self.assertEqual(market_data.asset_id, "MTNN")
        self.assertAlmostEqual(market_data.last_price, 275.0 * 0.00067)
        self.assertEqual(market_data.metadata["instrument"]["stable_key"], "NGX:MTNN:NGN")

    def test_market_data_normalize_endpoint_contract(self) -> None:
        request = MarketDataNormalizeRequest.model_validate(
            {
                "instruments": [
                    {
                        "asset_id": "MTNN",
                        "symbol": "MTNN",
                        "exchange": "NGX",
                        "currency": "NGN",
                        "asset_type": "listed_equity",
                    }
                ],
                "loan_currency": "USD",
                "data_mode": "provided_by_us",
            }
        )
        payload = normalize_market_data(request).model_dump(mode="json")
        self.assertIn("normalized_market_data", payload)
        self.assertIn("warnings", payload)
        self.assertIn("quality_scores", payload)
        self.assertIn("fx_decisions", payload)
        self.assertIn("missing_data", payload)
        self.assertEqual(payload["normalized_market_data"]["MTNN"]["loan_currency"], "USD")
        self.assertEqual(payload["fx_decisions"]["MTNN"]["fx_rate_used"], 0.00067)


if __name__ == "__main__":
    unittest.main()
