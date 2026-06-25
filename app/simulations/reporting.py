from __future__ import annotations
import csv, json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.historical_data.cache import content_hash
from app.historical_data.config import load_config

SIMULATION_CONFIG_VERSION = "v0.5C"

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

    scenario_rows="\n".join(f"| {m.get('scenario')} | {m.get('worst_shortfall')} | {m.get('fx_missing_events')} | {m.get('total_interest_accrued')} |" for m in metrics)
    w("official_validation_report.md", "# Official Validation Report\n\n## Executive Summary\nCache-first validation compares dynamic, flat LTV, and static haircut replay outputs before real provider-backed evidence is used.\n\n## Dataset Summary\nDataset inputs are read from normalized replay cache manifests.\n\n## Provider Coverage Summary\nSee provider_coverage_report.md.\n\n## Scenario Summary Table\n| Scenario | Worst Shortfall | FX Missing Events | Interest |\n|---|---:|---:|---:|\n"+scenario_rows+"\n\n## Baseline Comparison Table\nDynamic, flat LTV, and static haircut outputs are computed from replay time series.\n\n## Risk Outcome Table\nShortfall rates, warning dates, and liquidation frequencies are in the metrics CSV.\n\n## Credit Usability Table\nCredit capacity preserved metrics are reported per scenario.\n\n## Interest Impact Table\nInterest accrued and interest-driven shortfalls are reported per scenario.\n\n## Data Quality Issues\nMissing/stale data counters are surfaced in metrics.\n\n## FX Issues\nMissing and stale FX counters are surfaced in metrics.\n\n## Liquidation Plan Completeness\nCompleteness is tracked per run.\n\n## Key Limitations\nNo broker execution, live websocket monitoring, production database, or normal-CI provider calls.\n\n## Methodology Reference\nSee data_methodology.md and simulation_assumptions.md.\n")
    w("provider_coverage_report.md", "# Provider Coverage Report\n\n## Provider by Provider Coverage\nCoverage is sourced from dataset manifests.\n\n## Requested/Available/Missing\nRequested, available, and missing symbols are reported from manifest metadata.\n\n## Missing Symbols and Reasons\nSee manifest missing_symbol_reasons.\n\n## Earliest Available Date\nSee earliest_available_date_by_symbol.\n\n## Cache Status\nCache paths are checksum-validated and replay is cache-first.\n\n## API Quota Metadata\nQuota metadata is included when providers expose it; secrets are redacted.\n")
    w("data_methodology.md", "# Data Methodology\n\n## Providers\nAlpaca, Alpha Vantage, NGNMarket, and synthetic THIN where marked.\n\n## Cache-first Process\nNormal validation reads canonical normalized HistoricalSeries/HistoricalFXSeries cache files and does not call providers.\n\n## Historical Period\nConfigured by the dataset manifest and CLI date filters.\n\n## Data Transformations\nProvider bars are normalized to instrument, timestamp, OHLCV, currency, quality, and warnings.\n\n## FX Handling\nFX is date-indexed, nearest-prior, inverse-pair aware, and missing required FX zeroes loan-currency prices.\n\n## Missing Data Handling\nMissing FX/data is flagged and conservative data quality is applied.\n\n## Synthetic Data Assumptions\nTHIN is synthetic-only, deterministic by seed, and excluded from provider dataset requirements.\n")
    w("interest_accrual_methodology.md", "# Interest Accrual Methodology\n\nEngine-calculated scheduled accrual preserves principal, interest, and fees.\n")
    w("simulation_assumptions.md", "# Simulation Assumptions\n\n## Stress Overlay Definitions\nPrice gap, FX devaluation, volume collapse, spread widening, order-book thinning, trading halt, stale market data, missing FX, single-name crash, correlated selloff, and combined severe stress.\n\n## Baseline Definitions\nFlat LTV uses fixed collateral value times LTV. Static haircut applies fixed asset-class haircuts. Dynamic engine uses lifecycle safe credit limit and stressed liquidation value.\n\n## Interest Assumptions\nScenario loan terms drive scheduled accrual.\n\n## Liquidity/Order Book Assumptions\nSynthetic spreads and order books are deterministic from bars and stress settings.\n\n## Limitations\nNo broker execution, live websocket monitoring, production database, or normal-CI provider calls.\n")
    return files
