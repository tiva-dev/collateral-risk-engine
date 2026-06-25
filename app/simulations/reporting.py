from __future__ import annotations
import csv, json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.historical_data.cache import content_hash
from app.historical_data.config import load_config

SIMULATION_CONFIG_VERSION = "v0.5B"

def generate_evidence_package(results:list[dict[str,Any]], metrics:list[dict[str,Any]], output_dir:str|None=None, config:dict[str,Any]|None=None) -> dict[str,str]:
    out=Path(output_dir or load_config().simulation_output_dir); out.mkdir(parents=True, exist_ok=True)
    manifest={"artifact":"official_validation_manifest","simulation_config_version":SIMULATION_CONFIG_VERSION,"run_timestamp":datetime.now(timezone.utc).isoformat(),"config":config or {},"result_count":len(results),"metrics_checksum":content_hash(metrics)}
    files={}
    def w(name, text):
        p=out/name; p.write_text(text); files[name]=str(p)
    w("official_validation_manifest.json", json.dumps(manifest,indent=2,sort_keys=True))
    w("official_validation_metrics.json", json.dumps(metrics,indent=2,sort_keys=True,default=str))
    csvp=out/"official_validation_metrics.csv"
    keys=sorted({k for m in metrics for k in m})
    with csvp.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=keys); writer.writeheader(); writer.writerows(metrics)
    files[csvp.name]=str(csvp)
    w("official_validation_report.md", "# Official Validation Report\n\n"+"\n".join(f"- {m.get('scenario')}: worst shortfall {m.get('worst_shortfall')}" for m in metrics))
    w("provider_coverage_report.md", "# Provider Coverage Report\n\nCoverage is sourced from dataset manifests.\n")
    w("data_methodology.md", "# Data Methodology\n\nHistorical cache-first datasets with provider warnings and checksums.\n")
    w("interest_accrual_methodology.md", "# Interest Accrual Methodology\n\nEngine-calculated scheduled accrual preserves principal, interest, and fees.\n")
    w("simulation_assumptions.md", "# Simulation Assumptions\n\nSynthetic stresses include gaps, FX shocks, liquidity collapse, stale data, and order-book thinning.\n")
    return files
