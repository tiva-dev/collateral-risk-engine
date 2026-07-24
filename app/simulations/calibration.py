from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

def _avg(xs): return sum(xs)/len(xs) if xs else 0.0
def _diagnostic(rows, keys):
    for key in keys:
        values=[r[key] for r in rows if isinstance(r.get(key),(int,float))]
        if values: return {'status':'available','metric':key,'value':_avg(values)}
    return {'status':'unavailable','reason':f"none of {', '.join(keys)} are present"}

def generate_calibration_diagnostics(metrics, output_dir=None):
    by=defaultdict(list)
    for m in metrics: by[m.get('base_scenario') or m.get('scenario')].append(m)
    scenarios={}; over=[]; under=[]
    for name, rows in by.items():
        d={
            'average_approved_credit_by_scenario': _diagnostic(rows,['average_approved_credit']),
            'average_lifecycle_safe_credit_limit_by_scenario': _diagnostic(rows,['average_lifecycle_safe_credit_limit']),
            'credit_capacity_preserved_by_scenario': _diagnostic(rows,['average_credit_capacity_preserved','credit_capacity_preserved_at_target_shortfall_risk']),
            'shortfall_rate_by_scenario': _avg([r.get('collateral_shortfall_rate',0) for r in rows]),
            'worst_shortfall_by_scenario': max([r.get('worst_shortfall',0) for r in rows] or [0]),
            'margin_call_frequency_by_scenario': _avg([r.get('margin_call_frequency',0) for r in rows]),
            'liquidation_frequency_by_scenario': _avg([r.get('liquidation_frequency',0) for r in rows]),
        }
        notes=[]
        if d['shortfall_rate_by_scenario']==0 and any(r.get('credit_capacity_versus_flat_ltv',0)<0 and r.get('credit_capacity_versus_static_haircut',0)<0 for r in rows): notes.append('dynamic capacity low versus flat/static with zero shortfalls')
        if d['shortfall_rate_by_scenario']==0 and d['margin_call_frequency_by_scenario']>0.20: notes.append('excessive margin calls with no shortfalls')
        if any(r.get('missing_data_count',0)>0 and r.get('average_credit_capacity_preserved',0)==0 for r in rows): notes.append('available credit collapse due to data quality')
        if d['shortfall_rate_by_scenario']>0: notes.append('dynamic engine still produces shortfalls')
        if any(r.get('liquidation_plan_completeness',1)<1 for r in rows): notes.append('liquidation plan incomplete')
        if any((r.get('warning_lead_time') or 0)<1 and r.get('collateral_shortfall_rate',0)>0 for r in rows): notes.append('warning lead time too short')
        if any(r.get('fx_missing_events',0)>0 and r.get('worst_shortfall',0)>0 for r in rows): notes.append('FX/data gaps drive unprotected exposure')
        d['over_conservatism_indicators']=[n for n in notes if 'shortfalls' in n or 'data quality' in n]
        d['under_protection_indicators']=[n for n in notes if n not in d['over_conservatism_indicators']]
        d['suggested_calibration_review_areas']=notes or ['no automatic calibration change recommended']
        scenarios[name]=d
        over+=d['over_conservatism_indicators']; under+=d['under_protection_indicators']
    payload={'scenarios':scenarios,'over_conservatism_indicators':over,'under_protection_indicators':under,'suggested_calibration_review_areas':sorted(set(over+under)) or ['review evidence; do not auto-change model parameters']}
    if output_dir:
        out=Path(output_dir); out.mkdir(parents=True,exist_ok=True)
        (out/'calibration_diagnostics.json').write_text(json.dumps(payload,indent=2,sort_keys=True,default=str))
        lines=['# Calibration Diagnostics','', 'This pack reports diagnostics only and does not change model parameters automatically.','']
        for name,d in scenarios.items(): lines += [f'## {name}', f"- Shortfall rate: {d['shortfall_rate_by_scenario']}", f"- Worst shortfall: {d['worst_shortfall_by_scenario']}", f"- Review areas: {', '.join(d['suggested_calibration_review_areas'])}", '']
        (out/'calibration_diagnostics.md').write_text('\n'.join(lines))
    return payload
