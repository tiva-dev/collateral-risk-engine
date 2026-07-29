from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app.core.enums import AssetType
from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.cache import HistoricalDataCache
from app.historical_data.models import HistoricalBar
from app.historical_data.ngnmarket import NGNMarketHistoricalProvider
from app.historical_data.providers import ProviderError
from app.simulations.metrics import compute_simulation_metrics
from app.simulations.replay import (
    StressOverlay,
    convert_market_data_currency,
    generate_synthetic_order_book,
    historical_bar_to_market_data,
    rolling_volatility,
)
from app.simulations.reporting import generate_evidence_package
from app.simulations.scenarios.official_portfolios import official_portfolio_scenarios


class V052ProviderFoundationTests(unittest.TestCase):
    def test_provider_error_is_centralized(self):
        import app.historical_data.alpha_vantage as av
        import app.historical_data.ngnmarket as ng

        self.assertIs(av.ProviderError, ProviderError)
        self.assertIs(ng.ProviderError, ProviderError)

    def test_alpaca_pagination_merges_pages_and_records_count(self):
        with tempfile.TemporaryDirectory() as d:
            p = AlpacaTradingHistoricalProvider(HistoricalDataCache(d))
            pages = [
                {
                    "bars": {
                        "AAPL": [
                            {
                                "t": "2024-01-01T00:00:00Z",
                                "o": 1,
                                "h": 2,
                                "l": 1,
                                "c": 2,
                                "v": 10,
                            }
                        ]
                    },
                    "next_page_token": "n",
                },
                {
                    "bars": {
                        "AAPL": [
                            {
                                "t": "2024-01-02T00:00:00Z",
                                "o": 2,
                                "h": 3,
                                "l": 2,
                                "c": 3,
                                "v": 11,
                            }
                        ]
                    }
                },
            ]
            p._request_json = Mock(side_effect=pages)
            s = p.fetch_equity_history("AAPL", date(2024, 1, 1), date(2024, 1, 2))
            self.assertEqual(len(s.bars), 2)
            self.assertEqual(p.provider_coverage_summary["page_count"], 2)
            self.assertTrue(p.cache_paths)
            p._request_json.reset_mock()
            s2 = p.fetch_equity_history("AAPL", date(2024, 1, 1), date(2024, 1, 2))
            self.assertEqual(len(s2.bars), 2)
            p._request_json.assert_not_called()
            self.assertEqual(p.provider_coverage_summary["cached"], 1)

    def test_alpaca_cache_keys_include_query_options(self):
        with tempfile.TemporaryDirectory() as d:
            c = HistoricalDataCache(d)
            a = c.key_path(
                "normalized",
                provider="alpaca",
                symbol="AAPL",
                start="2024-01-01",
                end="2024-01-02",
                interval="1d",
                adjustment="all",
                feed="iex",
                currency="USD",
                limit=10000,
            )
            b = c.key_path(
                "normalized",
                provider="alpaca",
                symbol="AAPL",
                start="2024-01-01",
                end="2024-01-02",
                interval="1d",
                adjustment="raw",
                feed="sip",
                currency="EUR",
                limit=10000,
            )
            self.assertNotEqual(a, b)

    def test_ngnmarket_malformed_rows_are_warnings(self):
        p = NGNMarketHistoricalProvider()
        s = p.parse_company_chart(
            "MTNN",
            {
                "success": True,
                "meta": {"remaining": 1},
                "data": {
                    "chart": [
                        {"date": "2024-01-01"},
                        {"date": "bad", "close": "x"},
                        {"date": "2024-01-02", "close": 10},
                    ]
                },
            },
            date(2024, 1, 1),
            date(2024, 1, 3),
        )
        self.assertEqual(len(s.bars), 1)
        self.assertTrue(s.warnings)
        self.assertEqual(p.quota_metadata["remaining"], 1)

    def test_ngnmarket_null_ohlc_fields_fall_back_to_close(self):
        provider = NGNMarketHistoricalProvider()
        series = provider.parse_company_chart(
            "DANGSUGAR",
            {
                "success": True,
                "data": {
                    "data": [
                        {
                            "date": "2024-01-02",
                            "open": None,
                            "high": None,
                            "low": None,
                            "close": 42.5,
                            "volume": None,
                        }
                    ]
                },
            },
            date(2024, 1, 1),
            date(2024, 1, 3),
        )
        self.assertEqual(len(series.bars), 1)
        bar = series.bars[0]
        self.assertEqual((bar.open, bar.high, bar.low, bar.close), (42.5,) * 4)
        self.assertEqual(bar.volume, 0)
        self.assertEqual(len(series.warnings), 1)
        self.assertIn("filled 3 missing OHLC", series.warnings[0])

    def test_ngnmarket_normalizes_bearer_secret(self):
        with patch.dict(
            "os.environ", {"NGNMARKET_API_KEY": " Bearer ngm_live_fixture "}
        ):
            provider = NGNMarketHistoricalProvider()
        self.assertEqual(
            provider.auth_headers()["Authorization"], "Bearer ngm_live_fixture"
        )
        self.assertEqual(provider.auth_headers()["Accept"], "application/json")
        self.assertEqual(
            provider.auth_headers()["User-Agent"], "collateral-risk-engine/0.6.2"
        )

    def test_ngnmarket_http_error_exposes_safe_status_and_code(self):
        import io
        import json
        import urllib.error

        body = json.dumps(
            {
                "success": False,
                "error": {
                    "code": "INVALID_API_KEY",
                    "message": "API key not found or revoked.",
                },
            }
        ).encode()
        error = urllib.error.HTTPError(
            "https://api.ngnmarket.com/v1/companies",
            401,
            "Unauthorized",
            {},
            io.BytesIO(body),
        )
        with patch.dict("os.environ", {"NGNMARKET_API_KEY": "ngm_live_fixture"}):
            provider = NGNMarketHistoricalProvider()
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(ProviderError) as caught,
        ):
            provider._request_json("/companies")
        self.assertEqual(caught.exception.code, "INVALID_API_KEY")
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("ngm_live_fixture", str(caught.exception))

    def test_ngnmarket_non_json_http_error_preserves_safe_diagnostic(self):
        import io
        import urllib.error

        error = urllib.error.HTTPError(
            "https://api.ngnmarket.com/v1/companies",
            403,
            "Forbidden",
            {"Content-Type": "text/html", "Server": "cloud-edge"},
            io.BytesIO(
                b"<html><body>Access denied for ngm_live_fixture</body></html>"
            ),
        )
        with patch.dict("os.environ", {"NGNMARKET_API_KEY": "ngm_live_fixture"}):
            provider = NGNMarketHistoricalProvider()
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(ProviderError) as caught,
        ):
            provider._request_json("/companies")
        self.assertEqual(caught.exception.code, "http_403")
        self.assertIn("response=Access denied for [redacted]", str(caught.exception))
        self.assertNotIn("ngm_live_fixture", str(caught.exception))
        self.assertEqual(
            caught.exception.metadata["response_headers"]["server"], "cloud-edge"
        )

    def test_ngnmarket_uses_documented_chart_parameters_and_nested_company_list(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            provider = NGNMarketHistoricalProvider(HistoricalDataCache(directory))
            company_payload = {
                "success": True,
                "data": {"data": [{"symbol": "DANGCEM"}], "pagination": {}},
            }
            provider._request_json = Mock(return_value=company_payload)
            self.assertEqual(provider.fetch_company_list(), [{"symbol": "DANGCEM"}])

            chart_payload = {
                "success": True,
                "data": {
                    "data": [
                        {
                            "date": "2024-01-02",
                            "open": 100,
                            "high": 101,
                            "low": 99,
                            "close": 100,
                            "volume": 1_000,
                        }
                    ]
                },
            }
            provider._request_json = Mock(return_value=chart_payload)
            series = provider.fetch_equity_history(
                "DANGCEM",
                date(2024, 1, 1),
                date(2024, 1, 3),
                force_refresh=True,
            )
            self.assertEqual(len(series.bars), 1)
            _, params = provider._request_json.call_args.args
            self.assertEqual(params["format"], "detailed")
            self.assertNotIn("period", params)

    def test_ngnmarket_success_false_raises(self):
        with self.assertRaises(ProviderError):
            NGNMarketHistoricalProvider().parse_company_chart(
                "BAD",
                {"success": False, "error": "nope"},
                date(2024, 1, 1),
                date(2024, 1, 2),
            )


class V052ReplayMetricsReportingTests(unittest.TestCase):
    def test_replay_helpers_metrics_and_reporting(self):
        bar = HistoricalBar(
            "AAPL",
            datetime(2024, 1, 2, tzinfo=UTC),
            100,
            101,
            99,
            100,
            volume=1_000_000,
            provider_name="test",
        )
        md = historical_bar_to_market_data(
            bar,
            [0.01, -0.02, 0.03],
            StressOverlay(spread_widening=2.0, order_book_thinning=0.5),
        )
        self.assertEqual(md.asset_id, "AAPL")
        converted, missing_fx = convert_market_data_currency(
            md, "EUR", {("USD", "EUR"): 0.9}
        )
        self.assertFalse(missing_fx)
        self.assertEqual(converted.metadata["currency"], "EUR")
        self.assertAlmostEqual(converted.last_price, 90.0)
        self.assertAlmostEqual(converted.bid, md.bid * 0.9)
        self.assertIsNone(md.order_book)
        self.assertIsNone(converted.order_book)
        self.assertGreater(rolling_volatility([0.01, -0.02, 0.03], 3), 0)
        ob = generate_synthetic_order_book(100, 1_000_000, 1, thinning=0.5)
        self.assertTrue(ob.bids and ob.asks)
        result = {
            "scenario": "x",
            "records": [
                {
                    "total_obligation": 100,
                    "lifecycle_safe_credit_limit": 90,
                    "credit_limit_breach": 10,
                    "economic_recovery_shortfall": 5,
                    "recovery_coverage_ratio": 0.95,
                    "interest_accrued": 1,
                    "with_interest_balance": 100,
                    "without_interest_balance": 99,
                }
            ],
            "events": [
                {"state": "margin_call", "severity": "warning", "date": "2024-01-02"}
            ],
        }
        m = compute_simulation_metrics(result)
        self.assertEqual(m["worst_credit_limit_breach"], 10)
        lead_result = {
            "scenario": "lead",
            "records": [],
            "events": [
                {"state": "watch", "severity": "warning", "date": "2024-01-01"},
                {"state": "informational", "date": "2024-01-03"},
                {"state": "margin_call", "severity": "critical", "date": "2024-01-10"},
            ],
        }
        lead_metrics = compute_simulation_metrics(lead_result)
        self.assertEqual(lead_metrics["warning_lead_time"], 9)
        self.assertNotIn(None, lead_metrics["event_severity_distribution"])
        self.assertEqual(
            lead_metrics["event_severity_distribution"], {"critical": 1, "warning": 1}
        )
        with tempfile.TemporaryDirectory() as d:
            files = generate_evidence_package([result], [m], d, {"seed": 1})
            self.assertIn("official_validation_metrics.json", files)
            self.assertTrue(Path(files["official_validation_metrics.csv"]).exists())

    def test_official_scenarios_exist(self):
        scenarios = official_portfolio_scenarios()
        self.assertIn("mixed_ngx_us_portfolio_ngn_loan", scenarios)
        self.assertEqual(
            scenarios["us_diversified_etf_portfolio"].holdings[0].asset_type,
            AssetType.ETF,
        )


if __name__ == "__main__":
    unittest.main()
