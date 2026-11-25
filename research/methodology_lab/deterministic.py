"""Deterministic capacity maps on identical inputs: v0.6.2, v0.7.0 and Approach A.

Sections:
  factors       v0.6.2 per-factor decomposition versus volatility (overlap study)
  volatility    capacity versus volatility, single listed equity, small position
  correlation   ten names, same volatility, varying pairwise correlation
  size          single name, position size relative to ADV
  cure          external repayment vs collateral-funded cure, closed form

Approach A is evaluated with stylised features (Student-t nu=5, daily
monitoring, grace 2 days, 10% participation, 1-day-old EWMA equal to the long
run vol) so that only the varied input changes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

from . import lifecycle, methods
from .run_experiments import ENGINE_V, PRICE, TERM_CALENDAR

POL = lifecycle.Policy()


def engine(checkout: str, requests: list[dict]) -> list[dict]:
    worker = Path(__file__).with_name("engine_worker.py")
    out = subprocess.run(
        [sys.executable, str(worker)], input=json.dumps(requests), text=True,
        capture_output=True, cwd=checkout, env=dict(os.environ, PYTHONPATH=checkout),
        check=True,
    )
    return json.loads(out.stdout)


def req(rid: str, n: int, vol: float, size_ratio: float, spread: float = 0.0005,
        asset_type: str = "listed_equity") -> dict:
    w = 1.0 / n
    return {
        "id": rid, "term_days": TERM_CALENDAR, "rate": 0.10,
        "assets": [
            {
                "id": f"A{i}", "type": asset_type, "price": PRICE,
                "quantity": w * ENGINE_V / PRICE, "spread": spread,
                "adv_value": w * ENGINE_V / size_ratio, "vol30": vol, "vol90": vol,
                "vol252": vol, "mdd252": 0.2, "gap252": 0.05,
            }
            for i in range(n)
        ],
    }


def features(n: int, vol: float, rho: float, size_ratio: float, spread: float = 0.0005) -> methods.Features:
    w = np.full(n, 1.0 / n)
    corr = np.full((n, n), rho)
    np.fill_diagonal(corr, 1.0)
    sd = np.full(n, vol / np.sqrt(252))
    return methods.Features(
        weights=w, r_hist=np.zeros((1, n)), ewma_sigma=sd, long_sigma=sd,
        resid=np.zeros((1, n)), stressed_corr=corr, nu=5.0, spread=np.full(n, spread),
        spread_stress_mult=2.5, adv=w / size_ratio, halt_freq=0.0, halt_mean_days=0.0,
        fx_sigma=0.0, engine_inputs=[],
    )


def approach_a_ratio(f: methods.Features, cap: float = 1.0, alpha: float = 0.99) -> float:
    ell, _ = methods.approach_a(f, POL, alpha, cap)
    return ell


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v062", required=True)
    ap.add_argument("--v070", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out: dict = {}
    vols = [0.15, 0.25, 0.35, 0.50, 0.75, 1.00]
    for label, path in (("v062", a.v062), ("v070", a.v070)):
        worker = Path(__file__).with_name("v062_factor_worker.py")
        res = subprocess.run(
            [sys.executable, str(worker)], input=json.dumps(vols), text=True,
            capture_output=True, cwd=path, env=dict(os.environ, PYTHONPATH=path), check=True,
        )
        out[f"factors_{label}"] = json.loads(res.stdout)
    # Capacity versus volatility (single name, 0.5% of ADV).
    vol_rows = []
    reqs = [req(f"v{k}", 1, v, 0.005) for k, v in enumerate(vols)]
    e62, e70 = engine(a.v062, reqs), engine(a.v070, reqs)
    for k, v in enumerate(vols):
        vol_rows.append(
            {
                "vol": v, "v062": e62[k]["principal"] / e62[k]["mv"],
                "v070": e70[k]["principal"] / e70[k]["mv"],
                "A_uncapped": approach_a_ratio(features(1, v, 0.0, 0.005)),
                "A_alpha995_uncapped": approach_a_ratio(features(1, v, 0.0, 0.005), alpha=0.995),
            }
        )
    out["volatility"] = vol_rows
    # Correlation: ten names, 30% vol each.
    corr_rows = []
    rhos = [0.0, 0.2, 0.5, 0.8, 0.95]
    reqs = [req(f"r{k}", 10, 0.30, 0.01) for k in range(len(rhos))]
    e62, e70 = engine(a.v062, reqs), engine(a.v070, reqs)
    for k, rho in enumerate(rhos):
        corr_rows.append(
            {
                "rho": rho, "v062": e62[k]["principal"] / e62[k]["mv"],
                "v070": e70[k]["principal"] / e70[k]["mv"],
                "A_uncapped": approach_a_ratio(features(10, 0.30, rho, 0.01)),
                "single_name_A_uncapped": approach_a_ratio(features(1, 0.30, 0.0, 0.01)),
            }
        )
    out["correlation"] = corr_rows
    # Size relative to ADV, single name 30% vol.
    size_rows = []
    sizes = [0.01, 0.1, 0.5, 1.0, 3.0, 10.0]
    reqs = [req(f"s{k}", 1, 0.30, s) for k, s in enumerate(sizes)]
    e62, e70 = engine(a.v062, reqs), engine(a.v070, reqs)
    for k, s in enumerate(sizes):
        f = features(1, 0.30, 0.0, s)
        size_rows.append(
            {
                "position_over_adv": s, "v062": e62[k]["principal"] / e62[k]["mv"],
                "v070": e70[k]["principal"] / e70[k]["mv"],
                "A_uncapped": approach_a_ratio(f),
                "A_recovery_days": methods.recovery_days(f, POL),
            }
        )
    out["size"] = size_rows
    # Cure algebra: S stressed value, O obligation, c target coverage, theta net proceeds per unit S removed.
    cure_rows = []
    for s_val, o_val, c, theta in [
        (110_000, 100_000, 1.2, 1.0), (110_000, 100_000, 1.2, 1.05),
        (110_000, 100_000, 1.2, 0.95), (115_000, 100_000, 1.3, 1.0),
        (95_000, 100_000, 1.2, 1.0),
    ]:
        cash = max(0.0, o_val - s_val / c)
        cov_after_cash_sized_sale = (s_val - cash) / (o_val - theta * cash)
        denom = c * theta - 1
        sale = (c * o_val - s_val) / denom if denom > 0 else float("inf")
        feasible = sale <= s_val
        cure_rows.append(
            {
                "S": s_val, "O": o_val, "c": c, "theta": theta,
                "external_repayment": round(cash, 2),
                "collateral_injection": round(max(0.0, c * o_val - s_val), 2),
                "coverage_if_cash_amount_sold": round(cov_after_cash_sized_sale, 4),
                "collateral_funded_sale": round(sale, 2) if feasible else "infeasible: full liquidation",
            }
        )
    out["cure"] = cure_rows
    Path(a.out).write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
