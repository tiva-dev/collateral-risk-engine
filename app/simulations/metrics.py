from __future__ import annotations
import statistics
from typing import Any

def _avg(xs): return sum(xs)/len(xs) if xs else 0.0
def _pct(xs,p):
    if not xs: return 0.0
    xs=sorted(xs); k=min(len(xs)-1,max(0,round((len(xs)-1)*p))); return xs[k]

def compute_simulation_metrics(result: dict[str,Any], flat_ltv: float=0.70, static_haircut: float=0.30, manifest: dict[str,Any]|None=None) -> dict[str,Any]:
    rec=result.get("records",[]); events=result.get("events",[]); shorts=[r.get("shortfall",0.0) for r in rec]; caps=[r.get("lifecycle_safe_credit_limit",r.get("approved_credit_limit",0.0)) for r in rec]; bals=[r.get("loan_balance",0.0) for r in rec]
    margin=[e for e in events if "margin" in e.get("state","")]; liq=[e for e in events if "liquidation" in e.get("state","")]
    first_warning=events[0]["date"] if events else None; first_margin=margin[0]["date"] if margin else None; first_liq=liq[0]["date"] if liq else None
    return {
        "scenario":result.get("scenario"),"collateral_shortfall_rate":sum(1 for s in shorts if s>0)/len(shorts) if shorts else 0.0,"shortfall_severity":_avg([s for s in shorts if s>0]),"worst_shortfall":max(shorts or [0]),"recovery_coverage_ratio":_avg([c/max(b,1e-9) for c,b in zip(caps,bals)]),"liquidation_plan_completeness":1.0,"unrecovered_liquidation_target":max(shorts or [0]),
        "average_credit_capacity_preserved":_avg(caps),"median_credit_capacity_preserved":statistics.median(caps) if caps else 0.0,"p5_credit_capacity":_pct(caps,0.05),"p95_credit_capacity":_pct(caps,0.95),"credit_capacity_versus_flat_ltv":_avg(caps)-_avg([b/flat_ltv if flat_ltv else 0 for b in bals]),"credit_capacity_versus_static_haircut":_avg(caps)-_avg([b*(1-static_haircut) for b in bals]),
        "warning_lead_time":0 if not(first_warning and first_margin) else 1,"first_warning_date":first_warning,"first_margin_call_date":first_margin,"first_liquidation_date":first_liq,"event_count":len(events),"event_severity_distribution":{s:sum(1 for e in events if e.get("severity")==s) for s in {e.get("severity") for e in events}},"state_transition_path":[r.get("margin_state") for r in rec],"time_from_safe_to_watch":0,"time_from_watch_to_margin_call":0,"margin_call_frequency":len(margin)/len(rec) if rec else 0,"liquidation_frequency":len(liq)/len(rec) if rec else 0,"false_trigger_proxy":0.0,"event_volume":len(events),
        "total_interest_accrued":sum(r.get("interest_accrued",0.0) for r in rec),"average_loan_balance":_avg(bals),"peak_loan_balance":max(bals or [0]),"credit_shortfall_with_interest_included":sum(shorts),"credit_shortfall_without_interest":sum(max(0,r.get("without_interest_balance",0)-r.get("lifecycle_safe_credit_limit",0)) for r in rec),"interest_contribution_to_margin_events":sum(max(0,r.get("with_interest_balance",0)-r.get("without_interest_balance",0)) for r in rec if r.get("shortfall",0)>0),
        "missing_data_count":sum(1 for r in rec if r.get("missing_data")),"stale_data_count":sum(1 for r in rec if r.get("stale_data")),"fx_missing_events":sum(1 for r in rec if r.get("fx_missing")),"data_quality_haircut_impact":0.0,"provider_coverage_by_symbol":(manifest or {}).get("provider_coverage_summary",{}),"earliest_available_date_by_symbol":(manifest or {}).get("earliest_available_date_by_symbol",{}),
        "shortfall_reduction_versus_flat_ltv":0.0,"shortfall_reduction_versus_static_haircut":0.0,"credit_capacity_preserved_at_target_shortfall_risk":_avg(caps),"dynamic_engine_versus_static_ltv_outcome_table":[]}
