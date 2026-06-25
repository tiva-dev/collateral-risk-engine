from __future__ import annotations
import argparse, json
from datetime import date, datetime, timezone
from pathlib import Path
from app.historical_data.cache import content_hash
from app.simulations.metrics import compute_simulation_metrics
from app.simulations.reporting import SIMULATION_CONFIG_VERSION, generate_evidence_package
from app.simulations.replay import HistoricalReplayEngine
from app.historical_data.models import HistoricalBar
from app.simulations.scenarios.official_portfolios import official_portfolio_scenarios

def parse_date(v): return date.fromisoformat(v)

def _parse_timestamp(value):
    if isinstance(value, (date, datetime)):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Unsupported timestamp value: {value!r}")

def _bar_from_payload(payload: dict) -> HistoricalBar:
    return HistoricalBar(
        instrument=payload.get("instrument") or payload.get("symbol") or payload.get("asset_id"),
        timestamp=_parse_timestamp(payload.get("timestamp") or payload.get("date") or payload.get("t")),
        open=float(payload.get("open", payload.get("o", payload.get("close", payload.get("c", 0))))),
        high=float(payload.get("high", payload.get("h", payload.get("close", payload.get("c", 0))))),
        low=float(payload.get("low", payload.get("l", payload.get("close", payload.get("c", 0))))),
        close=float(payload.get("close", payload.get("c", 0))),
        adjusted_close=payload.get("adjusted_close"),
        volume=float(payload.get("volume", payload.get("v", 0)) or 0),
        value_traded=payload.get("value_traded"),
        currency=payload.get("currency", "USD"),
        source=payload.get("source", "historical_provider"),
        provider_name=payload.get("provider_name", payload.get("provider", "cache")),
        data_quality_score=float(payload.get("data_quality_score", 1.0)),
        warnings=list(payload.get("warnings", [])),
        raw_metadata=dict(payload.get("raw_metadata", {})),
    )

def _load_replay_inputs(manifest: dict) -> tuple[dict[str, list[HistoricalBar]], dict[tuple[str, str], float]]:
    bars_by_symbol: dict[str, list[HistoricalBar]] = {}
    fx_rates: dict[tuple[str, str], float] = {}
    for cache_path in manifest.get("cache_paths", []):
        path = Path(cache_path)
        if not path.exists():
            continue
        payload = json.loads(path.read_text()).get("data")
        series_items = payload if isinstance(payload, list) else [payload]
        for item in series_items:
            if not isinstance(item, dict):
                continue
            if "rates" in item:
                frm = item.get("from_currency")
                to = item.get("to_currency")
                rates = item.get("rates") or []
                if frm and to and rates:
                    latest = rates[-1]
                    fx_rates[(frm, to)] = float(latest.get("rate", 0) or 0)
                continue
            bars = item.get("bars")
            if isinstance(bars, dict):
                iterable = [(sym, rows) for sym, rows in bars.items()]
            elif isinstance(bars, list):
                iterable = [(item.get("instrument"), bars)]
            else:
                iterable = []
            for symbol, rows in iterable:
                if not symbol or not isinstance(rows, list):
                    continue
                parsed=[]
                for row in rows:
                    if isinstance(row, dict):
                        row = {**row, "instrument": row.get("instrument") or symbol}
                        parsed.append(_bar_from_payload(row))
                bars_by_symbol.setdefault(symbol, []).extend(parsed)
    for symbol in list(bars_by_symbol):
        bars_by_symbol[symbol].sort(key=lambda b: b.timestamp)
    return bars_by_symbol, {k: v for k, v in fx_rates.items() if v > 0}
def main():
    p=argparse.ArgumentParser(description="Run v0.5B official validation replay")
    p.add_argument("--dataset-manifest"); p.add_argument("--start-date",type=parse_date); p.add_argument("--end-date",type=parse_date); p.add_argument("--scenario",default="all"); p.add_argument("--output-dir"); p.add_argument("--seed",type=int,default=42); p.add_argument("--flat-ltv",type=float,default=0.70); p.add_argument("--static-haircut-profile",default="standard"); p.add_argument("--dry-run",action="store_true")
    a=p.parse_args(); scenarios=official_portfolio_scenarios(); selected=list(scenarios) if a.scenario=="all" else [a.scenario]
    missing=[s for s in selected if s not in scenarios]
    if missing: raise SystemExit(f"Unknown scenario(s): {', '.join(missing)}")
    manifest={}
    if a.dataset_manifest:
        manifest=json.loads(Path(a.dataset_manifest).read_text())
    out=Path(a.output_dir or "simulation_outputs"); out.mkdir(parents=True, exist_ok=True)
    config={"seed":a.seed,"flat_ltv":a.flat_ltv,"static_haircut_profile":a.static_haircut_profile,"scenario":a.scenario,"start_date":str(a.start_date) if a.start_date else None,"end_date":str(a.end_date) if a.end_date else None,"manifest_checksum":content_hash(manifest) if manifest else None,"simulation_config_version":SIMULATION_CONFIG_VERSION,"run_timestamp":datetime.now(timezone.utc).isoformat()}
    if a.dry_run:
        print(json.dumps({"dry_run":True,"scenarios":selected,"manifest_loaded":bool(manifest),"output_dir":str(out),"config":config},indent=2,sort_keys=True)); return
    bars_by_symbol, fx_rates = _load_replay_inputs(manifest)
    if not bars_by_symbol:
        raise SystemExit("No cached historical bars found in dataset manifest cache_paths; run the dataset builder first or pass a manifest with normalized caches.")
    engine=HistoricalReplayEngine(manifest,a.seed)
    results=[]
    for s in selected:
        scenario=scenarios[s]
        scenario_bars={h.asset_id: bars_by_symbol[h.asset_id] for h in scenario.holdings if h.asset_id in bars_by_symbol}
        missing=[h.asset_id for h in scenario.holdings if h.asset_id not in scenario_bars]
        if missing:
            raise SystemExit(f"Missing cached bars for scenario {s}: {', '.join(missing)}")
        results.append(engine.replay(scenario, scenario_bars, fx_rates=fx_rates, start_date=a.start_date, end_date=a.end_date))
    metrics=[compute_simulation_metrics(r,a.flat_ltv,manifest=manifest) for r in results]
    files=generate_evidence_package(results,metrics,str(out),config); print(json.dumps(files,indent=2,sort_keys=True))
if __name__=="__main__": main()
