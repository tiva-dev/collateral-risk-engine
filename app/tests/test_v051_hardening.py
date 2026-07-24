from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from app.core.models import Loan
from app.credit.interest import InterestPolicy, accrue_scheduled_periods
from app.historical_data.alpaca import AlpacaTradingHistoricalProvider, ProviderError
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.cache import HistoricalDataCache, content_hash
from app.historical_data.ngnmarket import NGNMarketHistoricalProvider
from app.historical_data.providers import HistoricalDataProvider
from app.simulations.config.official_validation_universe import (
    FX_PAIRS,
    LOAN_CURRENCIES,
    NGX_UNIVERSE,
    START_DATE,
    US_UNIVERSE,
    official_universe,
)
from app.simulations.data_builder import OfficialDatasetBuilder


class TestV051Hardening(unittest.TestCase):
    def test_builder_methods_and_dry_run_cli(self):
        self.assertTrue(callable(OfficialDatasetBuilder().build))
        self.assertTrue(callable(OfficialDatasetBuilder().write_manifest))
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.simulations.build_official_dataset",
                    "--dry-run",
                    "--output-dir",
                    d,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("fetch_equity_history", r.stdout)
            self.assertTrue(list(Path(d).glob("*.manifest.json")))

    def test_builder_non_dry_run_records_missing_reason(self):
        with patch(
            "app.simulations.data_builder.AlpacaTradingHistoricalProvider"
        ) as cls:
            cls.return_value.fetch_equity_history.side_effect = ProviderError("no data")
            m = OfficialDatasetBuilder(["alpaca"]).build(
                date(2020, 1, 1), date(2020, 1, 2), dry_run=False
            )
            self.assertIn("SPY", m.missing_symbol_reasons)
            self.assertTrue(m.provider_coverage_summary["alpaca"]["missing"])

    def test_identity_and_provider_metadata(self):
        a = AlpacaTradingHistoricalProvider()
        s = a.parse_bars(
            "AAPL",
            {
                "bars": {
                    "AAPL": [
                        {"t": "2020-01-02T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1}
                    ]
                }
            },
            date(2020, 1, 1),
            date(2020, 1, 3),
            "1d",
        )
        self.assertEqual(s.instrument_identity.exchange, "US")
        self.assertEqual(s.bars[0].instrument_identity.symbol, "AAPL")
        n = NGNMarketHistoricalProvider()
        ns = n.parse_company_chart(
            "MTNN",
            {
                "success": True,
                "data": {
                    "prices": [
                        {
                            "date": "2020-01-02",
                            "open": 1,
                            "high": 1,
                            "low": 1,
                            "close": 1,
                        }
                    ]
                },
            },
            date(2020, 1, 1),
            date(2020, 1, 3),
        )
        self.assertEqual(ns.instrument_identity.currency, "NGN")
        p1 = AlphaVantageHistoricalProvider()
        p2 = AlphaVantageHistoricalProvider()
        p1.quota_metadata["x"] = 1
        self.assertNotIn("x", p2.quota_metadata)
        self.assertIsInstance(HistoricalDataProvider.provider_capabilities, frozenset)

    def test_provider_parsing_and_cache_paths(self):
        with tempfile.TemporaryDirectory() as d:
            c = HistoricalDataCache(d)
            a = AlpacaTradingHistoricalProvider(c)
            payload = {
                "bars": {
                    "AAPL": [
                        {
                            "t": "2020-01-02T00:00:00Z",
                            "o": 1,
                            "h": 2,
                            "l": 1,
                            "c": 2,
                            "v": 1,
                        }
                    ],
                    "MSFT": [],
                }
            }
            with patch.object(a, "_request_json", return_value=payload) as req:
                self.assertEqual(
                    a.fetch_equity_history("AAPL", date(2020, 1, 1), date(2020, 1, 3))
                    .bars[0]
                    .close,
                    2,
                )
                self.assertTrue(a.cache_paths and a.raw_response_paths)
                a.fetch_equity_history("AAPL", date(2020, 1, 1), date(2020, 1, 3))
                self.assertEqual(req.call_count, 1)
                a.fetch_equity_history(
                    "AAPL", date(2020, 1, 1), date(2020, 1, 3), force_refresh=True
                )
                self.assertEqual(req.call_count, 2)
            self.assertTrue(
                a.parse_bars(
                    "MSFT", payload, date(2020, 1, 1), date(2020, 1, 3), "1d"
                ).warnings
            )

    def test_alpha_and_ngn_payloads(self):
        av = AlphaVantageHistoricalProvider()
        self.assertTrue(
            av.parse_daily_adjusted(
                "IBM", {"Note": "rate"}, date(2020, 1, 1), date(2020, 1, 2)
            ).warnings
        )
        self.assertTrue(
            av.parse_fx_daily(
                "EUR", "USD", {}, date(2020, 1, 1), date(2020, 1, 2)
            ).warnings
        )
        ng = NGNMarketHistoricalProvider()
        self.assertEqual(
            ng.parse_envelope(
                {
                    "success": True,
                    "meta": {"remaining": 1},
                    "data": [{"symbol": "MTNN"}],
                }
            ),
            [{"symbol": "MTNN"}],
        )
        self.assertEqual(ng.quota_metadata["remaining"], 1)
        self.assertEqual(
            ng.parse_fx_history(
                "USD",
                "NGN",
                {
                    "success": True,
                    "data": {"rates": [{"date": "2020-01-02", "rate": 1}]},
                },
                date(2020, 1, 1),
                date(2020, 1, 3),
            )
            .rates[0]
            .rate,
            1,
        )
        self.assertEqual(
            ng.parse_company_chart(
                "NGXASI",
                {
                    "success": True,
                    "data": {
                        "chart": [
                            {
                                "date": "2020-01-02",
                                "open": 1,
                                "high": 1,
                                "low": 1,
                                "close": 1,
                            }
                        ]
                    },
                },
                date(2020, 1, 1),
                date(2020, 1, 3),
            )
            .bars[0]
            .close,
            1,
        )

    def test_cache_corruption_manifest_checksum_universe_interest_validation(self):
        with tempfile.TemporaryDirectory() as d:
            c = HistoricalDataCache(d)
            p = c.write("manifest", {"a": 1}, id="x")
            self.assertTrue(str(p).endswith(".json"))
            self.assertEqual(
                c.read("manifest", id="x")["checksum"], content_hash({"a": 1})
            )
            p.write_text(json.dumps({"checksum": "bad", "data": {"a": 1}}))
            self.assertIsNone(c.read("manifest", id="x"))
        m = OfficialDatasetBuilder(["alpaca"]).build(dry_run=True)
        self.assertIn("alpaca", m.provider_coverage_summary)
        self.assertEqual(m.start_date, START_DATE)
        self.assertEqual(set(LOAN_CURRENCIES), {"NGN", "USD", "EUR"})
        self.assertEqual(len(US_UNIVERSE), len(set(US_UNIVERSE)))
        self.assertEqual(len(NGX_UNIVERSE), len(set(NGX_UNIVERSE)))
        self.assertTrue(all(len(p.split("/")) == 2 for p in FX_PAIRS))
        self.assertEqual(official_universe()["start_date"], date(2018, 1, 1))
        for kwargs in [
            {"annual_interest_rate": -1},
            {"annual_interest_rate": 0.1, "accrual_frequency": "bad"},
            {"annual_interest_rate": 0.1, "compounding": "bad"},
            {"annual_interest_rate": 0.1, "day_count_convention": "bad"},
            {"annual_interest_rate": 0.1, "interest_accrual_mode": "bad"},
        ]:
            with self.assertRaises(ValueError):
                InterestPolicy(**kwargs)
        loan, res = accrue_scheduled_periods(
            Loan(1000),
            InterestPolicy(0.12, "monthly"),
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 3, 1, tzinfo=UTC),
        )
        self.assertEqual(res.periods, 2)
        self.assertGreater(loan.accrued_interest, 0)


if __name__ == "__main__":
    unittest.main()
