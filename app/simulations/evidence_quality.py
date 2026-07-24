from __future__ import annotations
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from app.simulations.config.official_validation_universe import FX_PAIRS, NGX_UNIVERSE, US_UNIVERSE
from app.simulations.scenarios.official_portfolios import official_portfolio_scenarios

SUPPORTED_LOAN_CURRENCIES={"USD","NGN","EUR"}

def _d(v):
    if isinstance(v,date): return v
    if isinstance(v,datetime): return v.date()
    if isinstance(v,str):
        try: return datetime.fromisoformat(v.replace('Z','+00:00')).date()
        except ValueError: return date.fromisoformat(v[:10])
    return None

def _manifest(m):
    return m if isinstance(m,dict) else getattr(m,'__dict__',{})

def _result(passed=True):
    return {"passed":passed,"warnings":[],"blocking_errors":[],"coverage_score":1.0,"missing_symbols":[],"missing_fx_pairs":[],"earliest_available_dates":{},"methodology_notes":[],"missing_symbol_policy":{}}

def validate_provider_coverage(manifest, required_symbols, required_fx_pairs):
    m=_manifest(manifest); r=_result(True)
    missing=set(m.get('missing_symbols') or [])
    earliest=m.get('earliest_available_date_by_symbol') or {}
    r['earliest_available_dates']=earliest
    req=[s for s in required_symbols if s!='THIN']
    r['missing_symbols']=[s for s in req if s in missing or s not in earliest]
    r['missing_fx_pairs']=[p for p in required_fx_pairs if p in missing or p not in earliest]
    denom=max(1,len(req)+len(required_fx_pairs))
    r['coverage_score']=max(0.0,1.0-(len(r['missing_symbols'])+len(r['missing_fx_pairs']))/denom)
    for s in r['missing_symbols']:
        classification='synthetic_allowed' if s=='THIN' else ('blocking' if s in US_UNIVERSE+NGX_UNIVERSE else 'non_blocking')
        r['missing_symbol_policy'][s]=classification
        (r['blocking_errors'] if classification=='blocking' else r['warnings']).append(f"{classification} missing symbol: {s}")
    for p in r['missing_fx_pairs']:
        r['blocking_errors'].append(f"blocking missing FX pair: {p}")
    r['methodology_notes']=list(m.get('methodology_notes') or [])
    r['passed']=not r['blocking_errors']
    return r

def validate_minimum_history_length(manifest, min_start_date):
    m=_manifest(manifest); r=_result(True); min_d=_d(min_start_date)
    for sym, val in (m.get('earliest_available_date_by_symbol') or {}).items():
        ed=_d(val)
        if ed and min_d and ed>min_d:
            r['warnings'].append(f"{sym} starts at {ed}, after requested {min_d}")
    r['earliest_available_dates']=m.get('earliest_available_date_by_symbol') or {}; return r

def validate_missing_symbol_policy(manifest):
    m=_manifest(manifest); r=_result(True)
    for s in m.get('missing_symbols') or []:
        cls='synthetic_allowed' if s=='THIN' else 'blocking'
        r['missing_symbol_policy'][s]=cls
        (r['warnings'] if cls!='blocking' else r['blocking_errors']).append(f"{cls} missing symbol: {s}")
    r['missing_symbols']=list(m.get('missing_symbols') or []); r['passed']=not r['blocking_errors']; return r

def validate_fx_coverage(manifest):
    return validate_provider_coverage(manifest, [], list((_manifest(manifest).get('fx_pairs') or FX_PAIRS)))

def validate_cache_paths_exist(manifest):
    m=_manifest(manifest); r=_result(True)
    for p in m.get('cache_paths') or []:
        if not Path(p).exists(): r['blocking_errors'].append(f"cache path missing: {p}")
    r['passed']=not r['blocking_errors']; return r

def validate_evidence_package(files):
    mapping=files if isinstance(files,dict) else {Path(f).name:str(f) for f in files}
    required=['official_validation_manifest.json','official_validation_metrics.json','official_validation_metrics.csv','official_validation_report.md','provider_coverage_report.md','data_methodology.md','interest_accrual_methodology.md','simulation_assumptions.md']
    r=_result(True)
    for name in required:
        p=Path(mapping.get(name,name))
        if not p.exists(): r['blocking_errors'].append(f"missing evidence file: {name}")
        elif p.stat().st_size==0: r['blocking_errors'].append(f"empty evidence file: {name}")
    metrics_path=Path(mapping.get('official_validation_metrics.json','official_validation_metrics.json'))
    if metrics_path.exists():
        metrics=json.loads(metrics_path.read_text())
        if not metrics: r['blocking_errors'].append('no scenario metrics present')
        scenarios={}
        for m in metrics:
            for k in ['scenario','stress_name','fx_missing_events','total_interest_accrued','provider_coverage_by_symbol','dynamic_engine_versus_static_ltv_outcome_table']:
                if k not in m or m[k] in (None,''):
                    r['blocking_errors'].append(f"placeholder/empty metric {k} in {m.get('scenario')}")
                elif isinstance(m[k],dict) and m[k].get('status')=='not_applicable' and not m[k].get('reason'):
                    r['blocking_errors'].append(f"N/A metric {k} requires a reason in {m.get('scenario')}")
            table=m.get('dynamic_engine_versus_static_ltv_outcome_table')
            if table == []: r['blocking_errors'].append(f"empty dynamic/static/flat comparison table in {m.get('scenario')}")
            elif isinstance(table,list) and not all(all(x in row for x in ('dynamic_shortfall','flat_ltv_shortfall','static_haircut_shortfall')) for row in table): r['blocking_errors'].append(f"baseline comparison rows missing in {m.get('scenario')}")
            base=m.get('base_scenario') or str(m.get('scenario','')).split('::')[0]
            scenarios.setdefault(base,0); scenarios[base] += len(table) if isinstance(table,list) else 0
            if m.get('provider_coverage_by_symbol')=={} and _provider_backed(mapping): r['blocking_errors'].append(f"provider coverage empty in provider-backed run for {m.get('scenario')}")
        for scenario,count in scenarios.items():
            if not count: r['blocking_errors'].append(f"QA cannot prove scenario produced a result: {scenario}")
    r['passed']=not r['blocking_errors']; r['coverage_score']=0.0 if r['blocking_errors'] else 1.0; return r

def _provider_backed(mapping):
    p=Path(mapping.get('official_validation_manifest.json','official_validation_manifest.json'))
    try:
        m=json.loads(p.read_text()); provider=str(m.get('provider') or m.get('dataset_manifest',{}).get('provider') or '')
        return bool(provider and provider not in {'synthetic','dry_run'})
    except (OSError, ValueError, AttributeError): return False

def scenario_eligibility(manifest, scenario_names=None, allow_synthetic=False, stress='all'):
    m=_manifest(manifest); earliest=m.get('earliest_available_date_by_symbol') or {}; missing=set(m.get('missing_symbols') or [])
    scenarios=official_portfolio_scenarios(); names=list(scenarios) if not scenario_names or scenario_names==['all'] else scenario_names
    eligible=[]; ineligible=[]
    for name in names:
        sc=scenarios[name]; reasons=[]
        if sc.loan_currency not in SUPPORTED_LOAN_CURRENCIES: reasons.append('unsupported loan currency')
        if getattr(sc.loan_terms,'annual_interest_rate',-1) < 0: reasons.append('invalid interest terms')
        for h in sc.holdings:
            if h.asset_id=='THIN' and allow_synthetic: continue
            if h.asset_id in missing or h.asset_id not in earliest: reasons.append(f"missing bars for {h.asset_id}")
        if stress!='missing_fx' and not allow_synthetic:
            available=set(m.get('fx_pairs') or []) | {k for k in earliest if '/' in k}
            for currency in sorted({h.currency for h in sc.holdings if h.currency != sc.loan_currency}):
                direct=f"{currency}/{sc.loan_currency}"; inverse=f"{sc.loan_currency}/{currency}"
                if direct not in available and inverse not in available: reasons.append(f"required FX coverage missing: {direct} or {inverse}")
        item={"scenario":name,"reasons":reasons,"recommended_action":"run validation" if not reasons else "refresh provider dataset or allow documented synthetic gap"}
        (eligible if not reasons else ineligible).append(item)
    return {"eligible_scenarios":eligible,"ineligible_scenarios":ineligible}
