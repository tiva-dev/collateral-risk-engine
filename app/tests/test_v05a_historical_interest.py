from __future__ import annotations

import json, os, tempfile, unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.core.models import Loan
from app.credit.interest import InterestPolicy, accrue_interest, apply_repayment, calculate_day_count_fraction, next_accrual_time
from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.cache import HistoricalDataCache
from app.historical_data.ngnmarket import NGNMarketHistoricalProvider
from app.simulations.data_builder import OfficialDatasetBuilder

class TestV05A(unittest.TestCase):
    def test_env_and_workflow(self):
        self.assertTrue(Path('.env.example').exists())
        gi=Path('.gitignore').read_text(); self.assertIn('.env',gi); self.assertIn('!.env.example',gi)
        wf=Path('.github/workflows/provider-integration.yml').read_text()
        self.assertIn('workflow_dispatch',wf); self.assertNotIn('pull_request:',wf); self.assertNotIn('push:',wf); self.assertIn('${{ secrets.ALPACA_API_KEY }}',wf)
    def test_imports_without_keys(self):
        AlpacaTradingHistoricalProvider(); AlphaVantageHistoricalProvider(); NGNMarketHistoricalProvider()
    def test_alpaca_headers_and_parse(self):
        with patch.dict(os.environ, {'ALPACA_API_KEY':'k','ALPACA_SECRET_KEY':'s'}):
            p=AlpacaTradingHistoricalProvider(); self.assertEqual(p.auth_headers()['APCA-API-KEY-ID'],'k')
        series=p.parse_bars('AAPL', {'bars': {'AAPL':[{'t':'2020-01-02T00:00:00Z','o':1,'h':2,'l':1,'c':2,'v':10}]}}, date(2020,1,1), date(2020,1,3),'1d')
        self.assertEqual(series.bars[0].close,2)
    def test_alpha_parsing(self):
        p=AlphaVantageHistoricalProvider()
        eq=p.parse_daily_adjusted('MSFT', {'Time Series (Daily)': {'2020-01-02': {'1. open':'1','2. high':'2','3. low':'1','4. close':'2','5. adjusted close':'2','6. volume':'5'}}}, date(2020,1,1), date(2020,1,3))
        fx=p.parse_fx_daily('EUR','USD', {'Time Series FX (Daily)': {'2020-01-02': {'4. close':'1.1'}}}, date(2020,1,1), date(2020,1,3))
        self.assertEqual(eq.bars[0].adjusted_close,2); self.assertEqual(fx.rates[0].rate,1.1)
    def test_ngnmarket_parsing_quota(self):
        p=NGNMarketHistoricalProvider(); payload={'success':True,'meta':{'remaining':9},'data':{'prices':[{'date':'2020-01-02','open':1,'high':2,'low':1,'close':2,'volume':3}]}}
        s=p.parse_company_chart('MTNN',payload,date(2020,1,1),date(2020,1,3)); self.assertEqual(s.bars[0].currency,'NGN'); self.assertEqual(p.quota_metadata['remaining'],9)
        fx=p.parse_fx_history('USD','NGN',{'success':True,'data':{'rates':[{'date':'2020-01-02','rate':360}]}},date(2020,1,1),date(2020,1,3)); self.assertEqual(fx.rates[0].rate,360)
    def test_cache(self):
        with tempfile.TemporaryDirectory() as d:
            c=HistoricalDataCache(d); c.write('raw', {'api_key':'secret','value':1}, provider='x')
            got=c.read('raw',provider='x'); self.assertEqual(got['data']['api_key'],'[REDACTED]')
            path=c.key_path('raw',provider='x'); path.write_text('{bad'); self.assertIsNone(c.read('raw',provider='x'))
    def test_cache_first_force_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            c=HistoricalDataCache(d); p=AlphaVantageHistoricalProvider(c); key=dict(provider=p.provider_name,symbol='IBM',start='2020-01-01',end='2020-01-03',interval='1d')
            c.write('normalized', {'Time Series (Daily)': {}}, **key)
            with patch.object(p,'_request_json', side_effect=AssertionError('called')): p.fetch_equity_history('IBM',date(2020,1,1),date(2020,1,3))
            with patch.object(p,'_request_json', return_value={'Time Series (Daily)': {}}) as m: p.fetch_equity_history('IBM',date(2020,1,1),date(2020,1,3),force_refresh=True); self.assertTrue(m.called)
    def test_interest(self):
        s=datetime(2020,1,1,tzinfo=timezone.utc); e=datetime(2020,1,2,tzinfo=timezone.utc)
        self.assertAlmostEqual(calculate_day_count_fraction(s,e,'actual_365'),1/365)
        self.assertAlmostEqual(calculate_day_count_fraction(s,e,'actual_360'),1/360)
        self.assertAlmostEqual(calculate_day_count_fraction(datetime(2020,1,1),datetime(2020,2,1),'thirty_360'),30/360)
        loan,detail=accrue_interest(Loan(1000),InterestPolicy(.365),s,e); self.assertAlmostEqual(loan.accrued_interest,1)
        comp,_=accrue_interest(Loan(1000,10),InterestPolicy(.365,compounding='compound'),s,e); self.assertAlmostEqual(comp.principal,1011.01)
        unchanged,_=accrue_interest(Loan(1000),InterestPolicy(.1,interest_accrual_mode='client_supplied'),s,e); self.assertEqual(unchanged.accrued_interest,0)
        rep,paid=apply_repayment(Loan(100,20,5),30); self.assertEqual((paid['fees_paid'],paid['interest_paid'],paid['principal_paid']),(5,20,5))
        self.assertEqual(next_accrual_time(InterestPolicy(.1,'daily'),s).day,2)
        self.assertEqual(next_accrual_time(InterestPolicy(.1,'monthly'),s).month,2)
        self.assertEqual(next_accrual_time(InterestPolicy(.1,'yearly'),s).year,2021)
    def test_dataset_builder(self):
        b=OfficialDatasetBuilder(output_dir=tempfile.mkdtemp()); self.assertTrue(b.plan_calls())
        m=b.build(dry_run=True); m.missing_symbols.append('MISS')
        path=b.write_manifest(m); self.assertTrue(path.exists()); self.assertIn('checksum', json.loads(path.read_text()))

if __name__ == '__main__': unittest.main()
