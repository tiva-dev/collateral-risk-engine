"""Intervention thresholds and unnecessary-liquidation reduction (research).

Same real-data books, cohorts and capacity paths as real_data.py, simulated
under alternative intervention policies. The capacity methods are unchanged;
only the monitoring response varies:

  psi    margin call when obligation > psi x capacity (maintenance headroom)
  grace  sessions between the margin call and the first sale (cure window)
  cure_p probability the borrower cures with cash during grace

Outputs per market, policy variant and method: liquidation rate, share of
liquidations that were unnecessary (no loss and no breach afterwards without
any sale), mean and worst lender loss, for policy-originated loans and for a
matched 0.50 exposure. Research only; no policy is approved by this.
"""

from __future__ import annotations

import argparse
import json
import os
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from . import real_data as rd

VARIANTS = {
    "psi1.15_g2": {"psi": 1.15, "grace": 2},
    "psi1.30_g2": {"psi": 1.30, "grace": 2},
    "psi1.50_g2": {"psi": 1.50, "grace": 2},
    "psi1.15_g5": {"psi": 1.15, "grace": 5},
    "psi1.30_g5": {"psi": 1.30, "grace": 5},
    "psi1.30_g5_cure50": {"psi": 1.30, "grace": 5, "cure_p": 0.5},
}
METHODS = ("A", "B", "B_gap", "flat")


def summarise(rows: list[dict], key: str) -> dict:
    acc = [r for r in rows if r.get("status", "accepted") == "accepted" and "loss" in r]
    if not acc:
        return {"n": 0}
    sold = [r for r in acc if r["first_sale"] >= 0]
    losses = np.array([r["loss_rate"] for r in acc])
    return {
        "n": len(acc),
        "liquidation_rate": len(sold) / len(acc),
        "unnecessary_share": (sum(r["unnecessary"] for r in sold) / len(sold)) if sold else 0.0,
        "mean_loss_per_principal": float(losses.mean()),
        "worst_loss_per_principal": float(losses.max()),
        "loss_events": int((losses > 1e-9).sum()),
        "mean_ltv0": float(np.mean([r.get(key, 0.0) for r in acc])),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--out", required=True)
    ap.add_argument("--market", choices=["NGX", "US"], required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    a = ap.parse_args()
    manifest = json.loads(Path(a.manifest).read_text())
    from app.simulations.run_official_validation import _load_replay_inputs

    bars, _fx = _load_replay_inputs(manifest)
    panel = rd.build_panel(rd.NGX if a.market == "NGX" else rd.US, bars, a.market)
    ccy = "NGN" if a.market == "NGX" else "USD"
    liq = ("liq20d", "scenario") if a.market == "NGX" else ("observed", "institutional")
    tasks, labels = [], []
    seed = 20261126
    for name, pol in VARIANTS.items():
        for j, s in enumerate(panel["symbols"]):
            seed += 1
            tasks.append((f"{a.market}:{s}", "single", panel, [j], liq[1], ccy, None, "none", liq[0],
                          None, None, seed, False, {"policy": pol, "skip_engines": True, "skip_frontier": True}))
            labels.append(name)
    with Pool(a.workers) as pool:
        results = pool.map(rd.run_book, tasks)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict = {}
    for name in VARIANTS:
        pol_rows = [r for lab, res in zip(labels, results, strict=True) if lab == name for r in res[0]]
        mat_rows = [r for lab, res in zip(labels, results, strict=True) if lab == name for r in res[1]]
        summary[name] = {
            m: {"policy_originated": summarise([r for r in pol_rows if r["method"] == m], "ltv0"),
                "matched_0.50": summarise([r for r in mat_rows if r["method"] == m and abs(r["ltv"] - 0.5) < 1e-9],
                                          "ltv")}
            for m in METHODS
        }
    (out / f"intervention_{a.market}.json").write_text(json.dumps(summary, indent=2))
    print(f"#### INTERVENTION {a.market}")
    print("variant\tmethod\tbasis\tn\tliq_rate\tunnecessary\tmean_loss\tworst_loss\tloss_events\tmean_ltv0")
    for name, by_m in summary.items():
        for m, d in by_m.items():
            for basis, x in d.items():
                if x.get("n"):
                    print(f"{name}\t{m}\t{basis}\t{x['n']}\t{x['liquidation_rate']:.3f}\t{x['unnecessary_share']:.3f}\t"
                          f"{x['mean_loss_per_principal']:.5f}\t{x['worst_loss_per_principal']:.4f}\t"
                          f"{x['loss_events']}\t{x['mean_ltv0']:.3f}")


if __name__ == "__main__":
    main()
