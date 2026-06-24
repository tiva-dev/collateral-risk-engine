from __future__ import annotations
import json, urllib.parse, urllib.request
from datetime import date, datetime, timezone
from app.core.enums import AssetType
from app.market_data.identity import InstrumentIdentity
from .cache import HistoricalDataCache
from .config import load_config
from .models import HistoricalBar, HistoricalFXRate, HistoricalFXSeries, HistoricalSeries
from .providers import HistoricalDataProvider
from .alpaca import ProviderError

class NGNMarketHistoricalProvider(HistoricalDataProvider):
    provider_name="ngnmarket"; provider_capabilities=frozenset({"ngx_companies","ngx_bars","ngx_fx","ngx_indices"})
    def __init__(self, cache:HistoricalDataCache|None=None): super().__init__(); self.config=load_config(); self.cache=cache or HistoricalDataCache(); self.missing_symbols=[]; self.cache_paths=[]; self.raw_response_paths=[]
    def auth_headers(self): return {"Authorization": f"Bearer {self.config.ngnmarket_api_key}"} if self.config.ngnmarket_api_key else {}
    def parse_envelope(self,payload):
        if isinstance(payload,dict) and "meta" in payload: self.quota_metadata.update(payload.get("meta") or {})
        if isinstance(payload,dict) and payload.get("success") is False:
            msg=str(payload.get("error") or payload.get("message") or "NGNMarket error"); self.warnings.append(msg); raise ProviderError(msg)
        return payload.get("data", payload) if isinstance(payload,dict) else payload
    def _request_json(self,path,params=None):
        url=self.config.ngnmarket_base_url.rstrip("/")+path
        if params: url += "?"+urllib.parse.urlencode(params)
        try:
            req=urllib.request.Request(url,headers=self.auth_headers())
            with urllib.request.urlopen(req,timeout=30) as r: return json.loads(r.read().decode())
        except Exception as exc: raise ProviderError(f"NGNMarket request failed: {exc}") from exc
    def _identity(self,symbol, asset_type=AssetType.LISTED_EQUITY): return InstrumentIdentity(f"NGX:{symbol}:NGN",symbol,"NGX","NGN",asset_type,provider_symbol=symbol)
    def parse_company_chart(self,symbol,payload,start_date,end_date):
        data=self.parse_envelope(payload); rows=data.get("prices",data.get("chart",data if isinstance(data,list) else [])) if isinstance(data,(dict,list)) else []
        warnings=list(self.warnings)
        if not rows: warnings.append(f"No NGNMarket chart data for {symbol}"); self.missing_symbols.append(symbol)
        identity=self._identity(symbol); bars=[]
        for b in rows:
            d=b.get("date") or b.get("timestamp") or b.get("time"); dt=date.fromisoformat(str(d)[:10])
            if start_date<=dt<=end_date: bars.append(HistoricalBar(symbol,dt,float(b.get("open",b.get("o",0))),float(b.get("high",b.get("h",0))),float(b.get("low",b.get("l",0))),float(b.get("close",b.get("c",0))),b.get("adjusted_close"),float(b.get("volume",b.get("v",0))),b.get("value_traded"),"NGN",provider_name=self.provider_name,instrument_identity=identity,raw_metadata=b))
        return HistoricalSeries(symbol,bars,self.provider_name,datetime.now(timezone.utc),start_date,end_date,"1d",warnings,{"bar_count":len(bars)},identity)
    def parse_fx_history(self,fc,tc,payload,start_date,end_date):
        data=self.parse_envelope(payload); rows=data.get("rates",data if isinstance(data,list) else []) if isinstance(data,(dict,list)) else []
        warnings=list(self.warnings); rates=[]
        if not rows: warnings.append(f"No NGNMarket FX data for {fc}/{tc}")
        for r in rows:
            d=r.get("date") or r.get("timestamp"); dt=date.fromisoformat(str(d)[:10])
            if start_date<=dt<=end_date: rates.append(HistoricalFXRate(fc,tc,float(r.get("rate",r.get("close",0))),dt,provider_name=self.provider_name,raw_metadata=r))
        return HistoricalFXSeries(fc,tc,rates,self.provider_name,datetime.now(timezone.utc),start_date,end_date,warnings,{"rate_count":len(rates)})
    def fetch_company_list(self,exchange="NGX",force_refresh=False):
        key=dict(provider=self.provider_name,exchange=exchange)
        if not force_refresh and (c:=self.cache.read("raw",**key)): return self.parse_envelope(c["data"])
        p=self._request_json("/companies"); self.raw_response_paths.append(str(self.cache.write("raw",p,**key))); return self.parse_envelope(p)
    def fetch_equity_history(self,instrument,start_date,end_date,interval="1d",force_refresh=False):
        key=dict(provider=self.provider_name,symbol=instrument,start=str(start_date),end=str(end_date),interval=interval)
        if not force_refresh and (c:=self.cache.read("normalized",**key)): return self.parse_company_chart(instrument,c["data"],start_date,end_date)
        p=self._request_json(f"/companies/{instrument}/chart",{"start_date":start_date.isoformat(),"end_date":end_date.isoformat()}); self.raw_response_paths.append(str(self.cache.write("raw",p,**key))); self.cache_paths.append(str(self.cache.write("normalized",p,**key))); return self.parse_company_chart(instrument,p,start_date,end_date)
    def fetch_fx_history(self,from_currency,to_currency,start_date,end_date,force_refresh=False):
        key=dict(provider=self.provider_name,pair=f"{from_currency}{to_currency}",start=str(start_date),end=str(end_date))
        if not force_refresh and (c:=self.cache.read("normalized",**key)): return self.parse_fx_history(from_currency,to_currency,c["data"],start_date,end_date)
        p=self._request_json("/forex/history",{"from":from_currency,"to":to_currency,"start_date":start_date.isoformat(),"end_date":end_date.isoformat()}); self.raw_response_paths.append(str(self.cache.write("raw",p,**key))); self.cache_paths.append(str(self.cache.write("normalized",p,**key))); return self.parse_fx_history(from_currency,to_currency,p,start_date,end_date)
    def fetch_index_history(self,index_symbol,start_date,end_date,force_refresh=False):
        key=dict(provider=self.provider_name,index=index_symbol,start=str(start_date),end=str(end_date))
        if not force_refresh and (c:=self.cache.read("normalized",**key)): return self.parse_company_chart(index_symbol,c["data"],start_date,end_date)
        p=self._request_json(f"/indices/{index_symbol}/chart",{"start_date":start_date.isoformat(),"end_date":end_date.isoformat()}); self.raw_response_paths.append(str(self.cache.write("raw",p,**key))); self.cache_paths.append(str(self.cache.write("normalized",p,**key))); return self.parse_company_chart(index_symbol,p,start_date,end_date)
