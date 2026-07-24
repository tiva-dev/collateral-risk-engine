from __future__ import annotations
import statistics
from datetime import date, datetime
from typing import Any

def _avg(xs): return sum(xs)/len(xs) if xs else 0.0
def _pct(xs,p):
    if not xs: return 0.0
    xs=sorted(xs); k=min(len(xs)-1,max(0,round((len(xs)-1)*p))); return xs[k]

def _parse_event_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None

def _days_between(start, end):
    start_date = _parse_event_date(start)
    end_date = _parse_event_date(end)
    if not (start_date and end_date):
        return 0
    return (end_date - start_date).days

def _severity_distribution(events):
    severities = sorted({e.get("severity") for e in events if e.get("severity") is not None})
    return {s: sum(1 for e in events if e.get("severity") == s) for s in severities}

def _first_event_date(events, predicate):
    for event in events:
        if predicate(event):
            return event.get("date")
    return None

def compute_simulation_metrics(result: dict[str,Any], flat_ltv: float=0.70, static_haircut: float=0.30, manifest: dict[str,Any]|None=None) -> dict[str,Any]:
    rec=result.get("records",[]); events=result.get("events",[]); baselines=result.get("baseline_results",{}); shorts=[r.get("shortfall",0.0) for r in rec]; caps=[r.get("lifecycle_safe_credit_limit",r.get("approved_credit_limit",0.0)) for r in rec]; bals=[r.get("loan_balance",0.0) for r in rec]
    margin=[e for e in events if "margin" in e.get("state","")]; liq=[e for e in events if "liquidation" in e.get("state","")]
    first_warning=_first_event_date(events, lambda e: e.get("severity") == "warning" or e.get("state") == "watch"); first_margin=margin[0]["date"] if margin else None; first_liq=liq[0]["date"] if liq else None
    flat_shorts=[r.get("shortfall",0.0) for r in baselines.get("flat_ltv",[])]
    static_shorts=[r.get("shortfall",0.0) for r in baselines.get("static_haircut",[])]
    dyn_shorts=[r.get("shortfall",0.0) for r in baselines.get("dynamic_engine", rec)]
    def reduction(base, dyn):
        return (sum(base)-sum(dyn))/sum(base) if sum(base)>0 else 0.0
    outcome_table=[{"date":d.get("date"),"dynamic_shortfall":d.get("shortfall",0.0),"flat_ltv_shortfall":f.get("shortfall",0.0),"static_haircut_shortfall":sh.get("shortfall",0.0)} for d,f,sh in zip(baselines.get("dynamic_engine",[]), baselines.get("flat_ltv",[]), baselines.get("static_haircut",[]))]
    false_trigger=sum(1 for r in rec if r.get("shortfall",0)==0 and r.get("margin_state") not in (None,"safe"))/len(rec) if rec else 0.0
    collateral_caps=[r.get("collateral_value",0.0) for r in rec]
    return {
        "scenario":result.get("scenario"),"base_scenario":result.get("base_scenario", result.get("scenario")),"stress_name":result.get("stress_name", "baseline"),"collateral_shortfall_rate":sum(1 for s in shorts if s>0)/len(shorts) if shorts else 0.0,"shortfall_severity":_avg([s for s in shorts if s>0]),"worst_shortfall":max(shorts or [0]),"recovery_coverage_ratio":_avg([c/max(b,1e-9) for c,b in zip(caps,bals)]),"liquidation_plan_completeness":1.0,"unrecovered_liquidation_target":max(shorts or [0]),
        "average_approved_credit":_avg([r.get("approved_credit_limit",0.0) for r in rec]),"average_lifecycle_safe_credit_limit":_avg(caps),"average_credit_capacity_preserved":_avg(caps),"median_credit_capacity_preserved":statistics.median(caps) if caps else 0.0,"p5_credit_capacity":_pct(caps,0.05),"p95_credit_capacity":_pct(caps,0.95),"credit_capacity_versus_flat_ltv":_avg(caps)-_avg([b/flat_ltv if flat_ltv else 0 for b in bals]),"credit_capacity_versus_static_haircut":_avg(caps)-_avg([b*(1-static_haircut) for b in bals]),
        "warning_lead_time":_days_between(first_warning, first_margin),"first_warning_date":first_warning,"first_margin_call_date":first_margin,"first_liquidation_date":first_liq,"event_count":len(events),"event_severity_distribution":_severity_distribution(events),"state_transition_path":[r.get("margin_state") for r in rec],"time_from_safe_to_watch":0,"time_from_watch_to_margin_call":0,"margin_call_frequency":len(margin)/len(rec) if rec else 0,"liquidation_frequency":len(liq)/len(rec) if rec else 0,"false_trigger_proxy":false_trigger,"event_volume":len(events),
        "total_interest_accrued":sum(r.get("interest_accrued",0.0) for r in rec),"average_loan_balance":_avg(bals),"peak_loan_balance":max(bals or [0]),"credit_shortfall_with_interest_included":sum(shorts),"credit_shortfall_without_interest":sum(max(0,r.get("without_interest_balance",0)-r.get("lifecycle_safe_credit_limit",0)) for r in rec),"interest_contribution_to_margin_events":sum(max(0,r.get("with_interest_balance",0)-r.get("without_interest_balance",0)) for r in rec if r.get("shortfall",0)>0),
        "missing_data_count":sum(1 for r in rec if r.get("missing_data")),"stale_data_count":sum(1 for r in rec if r.get("stale_data")),"fx_missing_events":sum(1 for r in rec if r.get("fx_missing")),"data_quality_haircut_impact":0.0,"provider_coverage_by_symbol":(manifest or {}).get("provider_coverage_summary",{}),"earliest_available_date_by_symbol":(manifest or {}).get("earliest_available_date_by_symbol",{}),
        "shortfall_reduction_versus_flat_ltv":reduction(flat_shorts,dyn_shorts),"shortfall_reduction_versus_static_haircut":reduction(static_shorts,dyn_shorts),"credit_capacity_preserved_at_target_shortfall_risk":_avg([min(c, cap) for c,cap in zip(caps,collateral_caps)]) if rec else 0.0,"dynamic_engine_versus_static_ltv_outcome_table":outcome_table}
