from __future__ import annotations
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable
from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.config import load_config
from app.historical_data.manifest import write_manifest
from app.historical_data.models import HistoricalDatasetManifest
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
    def build(self,start_date:date=START_DATE,end_date:date|None=None,force_refresh:bool=False,dry_run:bool=True):
        end=end_date or datetime.now(timezone.utc).date(); missing=[]; cache_paths=[]; quota={}; notes=["Dry-run mode does not call provider APIs."] if dry_run else ["Cache-first provider retrieval used unless force_refresh=true."]
        if not dry_run:
            providers={"alpaca":AlpacaTradingHistoricalProvider(),"ngnmarket":NGNMarketHistoricalProvider(),"alpha_vantage":AlphaVantageHistoricalProvider()}
            for s in (US_UNIVERSE if "alpaca" in self.provider_names else []):
                try: providers["alpaca"].fetch_equity_history(s,start_date,end,force_refresh=force_refresh)
                except Exception: missing.append(s)
            for s in (NGX_UNIVERSE if "ngnmarket" in self.provider_names else []):
                try: providers["ngnmarket"].fetch_equity_history(s,start_date,end,force_refresh=force_refresh)
                except Exception: missing.append(s)
            quota={k:getattr(v,"quota_metadata",{}) for k,v in providers.items() if k in self.provider_names}
        manifest=HistoricalDatasetManifest(dataset_id="official-validation-"+datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"), provider=",".join(sorted(self.provider_names)), universe=official_universe(), instruments=US_UNIVERSE+NGX_UNIVERSE, fx_pairs=FX_PAIRS, start_date=start_date, end_date=end, cache_paths=cache_paths, provider_quota_metadata=quota, missing_symbols=missing, methodology_notes=notes)
        return manifest
    def write_manifest(self, manifest): return write_manifest(manifest,self.output_dir)
