from __future__ import annotations
import argparse, json, re
from datetime import date, timedelta
from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.ngnmarket import NGNMarketHistoricalProvider

REDACTED='***redacted***'

def _summary(name, series):
    n=len(getattr(series,'bars',[]) or getattr(series,'rates',[]) or [])
    warnings=[w for w in (getattr(series,'warnings',[]) or []) if 'key' not in str(w).lower() and 'secret' not in str(w).lower()]
    return {'provider':name,'status':'ok','rows':n,'warnings':[_sanitize(w) for w in warnings[:5]],'quota_metadata':_sanitize(getattr(series,'data_quality_summary',{}).get('quota_metadata',{}))}

def _sanitize(value):
    if isinstance(value,dict): return {k:(REDACTED if any(x in k.lower() for x in ('key','secret','token','authorization')) else _sanitize(v)) for k,v in value.items()}
    if isinstance(value,list): return [_sanitize(v) for v in value]
    value=re.sub(r'(?i)(bearer\s+)[^\s,;]+',r'\1'+REDACTED,str(value))
    return re.sub(r'(?i)((?:api[_-]?key|apikey|token|secret|authorization)\s*[=:]\s*)[^\s,;&]+',r'\1'+REDACTED,value)

def _failure(name, exc): return {'provider':name,'status':'error','rows':0,'warnings':[_sanitize(exc)],'quota_metadata':{}}

def main(argv=None):
    p=argparse.ArgumentParser(description='v0.6 real provider validation smoke test')
    p.add_argument('--confirm-real-provider-calls',action='store_true')
    p.add_argument('--start-date',default=(date.today()-timedelta(days=10)).isoformat())
    p.add_argument('--end-date',default=date.today().isoformat())
    a=p.parse_args(argv)
    if not a.confirm_real_provider_calls:
        print(json.dumps({'refused':True,'reason':'Refusing real provider calls without --confirm-real-provider-calls','providers_called':[]},indent=2,sort_keys=True))
        return 0
    start=date.fromisoformat(a.start_date); end=date.fromisoformat(a.end_date); results=[]
    try: results.append(_summary('alpaca',AlpacaTradingHistoricalProvider().fetch_equity_history('SPY',start,end,force_refresh=True)))
    except Exception as exc: results.append(_failure('alpaca',exc))
    try: results.append(_summary('alpha_vantage',AlphaVantageHistoricalProvider().fetch_fx_history('EUR','USD',start,end,force_refresh=True)))
    except Exception as exc: results.append(_failure('alpha_vantage',exc))
    try:
        ngn=NGNMarketHistoricalProvider()
        try: companies=ngn.fetch_company_list('NGX',force_refresh=True); results.append({'provider':'ngnmarket','status':'ok','rows':len(companies) if isinstance(companies,list) else 0,'warnings':[],'quota_metadata':_sanitize(dict(ngn.quota_metadata) if isinstance(getattr(ngn,'quota_metadata',{}), dict) else {})})
        except Exception: results.append(_summary('ngnmarket',ngn.fetch_equity_history('MTNN',start,end,force_refresh=True)))
    except Exception as exc: results.append(_failure('ngnmarket',exc))
    print(json.dumps({'refused':False,'summary':results},indent=2,sort_keys=True))
    return 0
if __name__=='__main__': raise SystemExit(main())
