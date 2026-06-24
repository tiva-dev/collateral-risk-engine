from __future__ import annotations
import argparse, json
from datetime import date, datetime, timezone
from pathlib import Path
from app.historical_data.cache import content_hash
from app.simulations.metrics import compute_simulation_metrics
from app.simulations.reporting import generate_evidence_package
from app.simulations.replay import HistoricalReplayEngine
from app.simulations.scenarios.official_portfolios import official_portfolio_scenarios

def parse_date(v): return date.fromisoformat(v)
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
    config={"seed":a.seed,"flat_ltv":a.flat_ltv,"static_haircut_profile":a.static_haircut_profile,"scenario":a.scenario,"start_date":str(a.start_date) if a.start_date else None,"end_date":str(a.end_date) if a.end_date else None,"manifest_checksum":content_hash(manifest) if manifest else None,"simulation_config_version":"v0.5.2","run_timestamp":datetime.now(timezone.utc).isoformat()}
    if a.dry_run:
        print(json.dumps({"dry_run":True,"scenarios":selected,"manifest_loaded":bool(manifest),"output_dir":str(out),"config":config},indent=2,sort_keys=True)); return
    # Full replay requires caller-provided cached bars; this runner emits deterministic empty evidence if not wired by caller.
    engine=HistoricalReplayEngine(manifest,a.seed); results=[{"scenario":s,"seed":a.seed,"records":[],"events":[],"note":"No cached bars supplied to CLI runner"} for s in selected]
    metrics=[compute_simulation_metrics(r,a.flat_ltv,manifest=manifest) for r in results]
    files=generate_evidence_package(results,metrics,str(out),config); print(json.dumps(files,indent=2,sort_keys=True))
if __name__=="__main__": main()
