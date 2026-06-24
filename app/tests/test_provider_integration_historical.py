from __future__ import annotations
import os, unittest
from datetime import date, timedelta

from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.ngnmarket import NGNMarketHistoricalProvider

RUN = os.getenv("RUN_PROVIDER_INTEGRATION_TESTS", "").lower() == "true"

@unittest.skipUnless(RUN, "RUN_PROVIDER_INTEGRATION_TESTS=true is required for real provider calls")
class TestProviderIntegrationHistorical(unittest.TestCase):
    def test_alpaca_one_symbol_historical_bars(self):
        if not (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY")): self.skipTest("Alpaca secrets missing")
        p=AlpacaTradingHistoricalProvider(); end=date.today()-timedelta(days=2); s=p.fetch_equity_history("AAPL",end-timedelta(days=7),end)
        self.assertTrue(s.bars or s.warnings)
    def test_alpha_vantage_one_fx_daily_call(self):
        if not os.getenv("ALPHA_VANTAGE_API_KEY"): self.skipTest("Alpha Vantage secret missing")
        p=AlphaVantageHistoricalProvider(); end=date.today()-timedelta(days=2); s=p.fetch_fx_history("EUR","USD",end-timedelta(days=7),end)
        self.assertTrue(s.rates or s.warnings)
    def test_ngnmarket_company_list_or_chart(self):
        if not os.getenv("NGNMARKET_API_KEY"): self.skipTest("NGNMarket secret missing")
        p=NGNMarketHistoricalProvider(); data=p.fetch_company_list()
        self.assertTrue(data or p.warnings)

if __name__ == "__main__": unittest.main()
