from __future__ import annotations
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Iterable
from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.config import load_config
from app.historical_data.manifest import write_manifest
from app.historical_data.models import HistoricalDatasetManifest, HistoricalFXSeries, HistoricalSeries
from app.historical_data.ngnmarket import NGNMarketHistoricalProvider
from app.simulations.config.official_validation_universe import FX_PAIRS, NGX_UNIVERSE, START_DATE, US_UNIVERSE, official_universe

class OfficialDatasetBuilder:
    def __init__(self, providers: Iterable[str] | None=None, output_dir: str | None=None):
        self.provider_names=set(providers or ["alpaca","ngnmarket","alpha_vantage"]); self.config=load_config(); self.output_dir=output_dir or self.config.simulation_output_dir
    def plan_calls(self):
        calls=[]
        if "alpaca" in self.provider_names: calls += [{"provider":"alpaca","operation":"fetch_equity_history","symbol":s} for s in US_UNIVERSE]
        if "ngnmarket" in self.provider_names: calls += [{"provider":"ngnmarket","operation":"fetch_equity_history","symbol":s} for s in NGX_UNIVERSE]
        for pair in FX_PAIRS:
            if "ngnmarket" in self.provider_names: calls.append({"provider":"ngnmarket","operation":"fetch_fx_history","pair":pair})
            if "alpha_vantage" in self.provider_names: calls.append({"provider":"alpha_vantage","operation":"fetch_fx_history","pair":pair})
        return calls
    def build(self,start_date: date = START_DATE,end_date: date | None = None,force_refresh: bool = False,dry_run: bool = True) -> HistoricalDatasetManifest:
        end = end_date or datetime.now(timezone.utc).date(); missing=[]; reasons={}; warnings=[]; cache_paths=[]; raw_paths=[]; quota={}; earliest={}; identities={}; coverage={}
        notes = ["Dry-run mode does not call provider APIs."] if dry_run else ["Cache-first provider retrieval used unless force_refresh=true."]
        providers = {}
        if not dry_run:
            providers={"alpaca":AlpacaTradingHistoricalProvider(),"ngnmarket":NGNMarketHistoricalProvider(),"alpha_vantage":AlphaVantageHistoricalProvider()}
            def record(name: str, symbol: str, fn):
                try:
                    series=fn(); count=len(series.bars) if isinstance(series,HistoricalSeries) else len(series.rates) if isinstance(series,HistoricalFXSeries) else 0
                    coverage.setdefault(name,{"requested":0,"available":0,"missing":0}); coverage[name]["requested"]+=1
                    if count: coverage[name]["available"]+=1
                    else:
                        coverage[name]["missing"]+=1; missing.append(symbol); reasons[symbol]="provider returned no data"
                    warnings.extend(getattr(series,"warnings",[]) or [])
                    if getattr(series,"instrument_identity",None): identities[symbol]=asdict(series.instrument_identity)
                    if isinstance(series,HistoricalSeries) and series.bars: earliest[symbol]=min(b.timestamp for b in series.bars)
                    if isinstance(series,HistoricalFXSeries) and series.rates: earliest[symbol]=min(r.timestamp for r in series.rates)
                except Exception as exc:
                    coverage.setdefault(name,{"requested":0,"available":0,"missing":0}); coverage[name]["requested"]+=1; coverage[name]["missing"]+=1
                    missing.append(symbol); reasons[symbol]=str(exc); warnings.append(f"{name} failed for {symbol}: {exc}")
            for s in (US_UNIVERSE if "alpaca" in self.provider_names else []): record("alpaca",s,lambda s=s: providers["alpaca"].fetch_equity_history(s,start_date,end,force_refresh=force_refresh))
            for s in (NGX_UNIVERSE if "ngnmarket" in self.provider_names else []): record("ngnmarket",s,lambda s=s: providers["ngnmarket"].fetch_equity_history(s,start_date,end,force_refresh=force_refresh))
            for pair in FX_PAIRS:
                fc,tc=pair.split("/")
                if "ngnmarket" in self.provider_names: record("ngnmarket",pair,lambda fc=fc,tc=tc: providers["ngnmarket"].fetch_fx_history(fc,tc,start_date,end,force_refresh=force_refresh))
                if "alpha_vantage" in self.provider_names: record("alpha_vantage",pair,lambda fc=fc,tc=tc: providers["alpha_vantage"].fetch_fx_history(fc,tc,start_date,end,force_refresh=force_refresh))
            for k,v in providers.items():
                if k in self.provider_names:
                    quota[k]=getattr(v,"quota_metadata",{}); cache_paths += getattr(v,"cache_paths",[]); raw_paths += getattr(v,"raw_response_paths",[])
        else:
            coverage={p:{"planned_calls":sum(1 for c in self.plan_calls() if c["provider"]==p)} for p in self.provider_names}
        return HistoricalDatasetManifest(dataset_id="official-validation-"+datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),provider=",".join(sorted(self.provider_names)),universe=official_universe(),instruments=US_UNIVERSE+NGX_UNIVERSE,fx_pairs=FX_PAIRS,start_date=start_date,end_date=end,cache_paths=cache_paths,raw_response_paths=raw_paths,provider_quota_metadata=quota,warnings=warnings,missing_symbols=missing,earliest_available_date_by_symbol=earliest,methodology_notes=notes,missing_symbol_reasons=reasons,provider_coverage_summary=coverage,instrument_identities=identities)
    def write_manifest(self, manifest): return write_manifest(manifest,self.output_dir)
