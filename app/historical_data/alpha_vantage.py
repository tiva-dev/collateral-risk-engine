from __future__ import annotations
import csv, io, json, urllib.parse, urllib.request
from datetime import date, datetime, timezone
from typing import Any
from .cache import HistoricalDataCache
from .config import load_config
from .models import HistoricalBar, HistoricalFXRate, HistoricalFXSeries, HistoricalSeries
from .providers import HistoricalDataProvider
from .alpaca import ProviderError

class AlphaVantageHistoricalProvider(HistoricalDataProvider):
    provider_name="alpha_vantage"; provider_capabilities={"equity_daily_adjusted","fx_daily"}
    def __init__(self, cache: HistoricalDataCache|None=None): self.config=load_config(); self.cache=cache or HistoricalDataCache(); self.quota_metadata={}
    def _request_json(self, params):
        q=dict(params); 
        if self.config.alpha_vantage_api_key: q["apikey"]=self.config.alpha_vantage_api_key
        url=self.config.alpha_vantage_base_url+"?"+urllib.parse.urlencode(q)
        try:
            with urllib.request.urlopen(url, timeout=30) as r: return json.loads(r.read().decode())
        except Exception as exc: raise ProviderError(f"Alpha Vantage request failed: {exc}") from exc
    def parse_daily_adjusted(self, instrument, payload, start_date, end_date):
        warnings=[]
        if "Note" in payload or "Information" in payload: warnings.append(payload.get("Note") or payload.get("Information"))
        series=payload.get("Time Series (Daily)", {})
        bars=[]
        for d,b in sorted(series.items()):
            dt=date.fromisoformat(d)
            if start_date<=dt<=end_date: bars.append(HistoricalBar(instrument, dt, float(b.get("1. open",0)), float(b.get("2. high",0)), float(b.get("3. low",0)), float(b.get("4. close",0)), float(b.get("5. adjusted close", b.get("4. close",0))), float(b.get("6. volume",0)), provider_name=self.provider_name, raw_metadata=b))
        return HistoricalSeries(instrument,bars,self.provider_name,datetime.now(timezone.utc),start_date,end_date,"1d",warnings,{"bar_count":len(bars)})
    def parse_fx_daily(self, fc, tc, payload, start_date, end_date):
        series=payload.get("Time Series FX (Daily)", {}); rates=[]
        for d,b in sorted(series.items()):
            dt=date.fromisoformat(d)
            if start_date<=dt<=end_date: rates.append(HistoricalFXRate(fc,tc,float(b.get("4. close",0)),dt,provider_name=self.provider_name,raw_metadata=b))
        return HistoricalFXSeries(fc,tc,rates,self.provider_name,datetime.now(timezone.utc),start_date,end_date,data_quality_summary={"rate_count":len(rates)})
    def parse_csv(self, text): return list(csv.DictReader(io.StringIO(text)))
    def fetch_equity_history(self,instrument,start_date,end_date,interval="1d",force_refresh=False):
        key=dict(provider=self.provider_name,symbol=instrument,start=str(start_date),end=str(end_date),interval=interval)
        if not force_refresh and (c:=self.cache.read("normalized",**key)): return self.parse_daily_adjusted(instrument,c["data"],start_date,end_date)
        p=self._request_json({"function":"TIME_SERIES_DAILY_ADJUSTED","symbol":instrument,"outputsize":"full"}); self.cache.write("normalized",p,**key); return self.parse_daily_adjusted(instrument,p,start_date,end_date)
    def fetch_fx_history(self,from_currency,to_currency,start_date,end_date,force_refresh=False):
        key=dict(provider=self.provider_name,pair=f"{from_currency}{to_currency}",start=str(start_date),end=str(end_date))
        if not force_refresh and (c:=self.cache.read("normalized",**key)): return self.parse_fx_daily(from_currency,to_currency,c["data"],start_date,end_date)
        p=self._request_json({"function":"FX_DAILY","from_symbol":from_currency,"to_symbol":to_currency,"outputsize":"full"}); self.cache.write("normalized",p,**key); return self.parse_fx_daily(from_currency,to_currency,p,start_date,end_date)
