"""Print a compact, data-free summary of a dataset manifest.

Coverage verdicts, FX source selection, provider reconciliation and OHLC
imputation counts. No prices are printed, so the output can go to CI logs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(path: str) -> int:
    m = json.loads(Path(path).read_text())
    cov = m.get("coverage_validation") or {}
    print("== dataset", m.get("dataset_id"), m.get("start_date"), "->", m.get("end_date"))
    print("== coverage summary", json.dumps(cov.get("summary"), sort_keys=True))
    print(f"{'series':26} {'status':6} {'first':10} {'last':10} {'exp':>5} {'obs':>5} "
          f"{'ratio':>6} {'lagS':>5} {'gaps':>4} {'maxgap':>6} first failure / warning")
    for name, r in sorted((cov.get("series") or {}).items()):
        gaps = r.get("gaps") or []
        note = (r.get("failures") or r.get("warnings") or [""])[0]
        print(
            f"{name:26} {r['status']:6} {r.get('first_observation')!s:10} "
            f"{r.get('last_observation')!s:10} {r['expected_sessions']:>5} "
            f"{r['observed_expected_sessions']:>5} {r['coverage_ratio']:>6.3f} "
            f"{r['start_lag_sessions']:>5} {len(gaps):>4} "
            f"{max((g['sessions'] for g in gaps), default=0):>6} {note[:110]}"
        )
    closures = cov.get("inferred_market_closures") or {}
    for market, days in closures.items():
        print(f"== inferred {market} closures ({len(days)}):", ", ".join(sorted(days)))
    print("== fx source selection")
    for pair, s in sorted((m.get("fx_source_selection") or {}).items()):
        print(f"  {pair}: {s.get('selected_provider')} ({s.get('reason')}); candidates={s.get('candidates')}")
    print("== fx reconciliation")
    for pair, r in sorted((m.get("fx_reconciliation") or {}).items()):
        print(f"  {pair}: {json.dumps(r, sort_keys=True)}")
    print("== OHLC imputation")
    for sym, s in sorted((m.get("imputation_summary") or {}).items()):
        print(f"  {sym:12} bars={s['bars']:>5} imputed={s['imputed_by_field']} "
              f"imputed_range_bars={s['bars_with_imputed_range']} no_trade={s['no_trade_bars']} "
              f"volume_missing={s.get('volume_missing_bars')} first_volume={s.get('first_reported_volume')}")
    print("== missing", m.get("missing_symbols"))
    for k, v in sorted((m.get("missing_symbol_reasons") or {}).items()):
        print(f"  {k}: {v}")
    print("== quota", json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'provider_metadata'}
                                  | {"meta": v.get("provider_metadata")}
                                  for k, v in (m.get("provider_quota_metadata") or {}).items()},
                                 default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
