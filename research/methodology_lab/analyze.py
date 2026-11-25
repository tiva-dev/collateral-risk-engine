"""Aggregate rolling-cohort results with statistical uncertainty.

Loss frequencies use Clopper-Pearson 95% intervals. Because cohorts from the
same simulated path overlap in time, intervals are also reported with an
effective sample size of one cohort per loan term per replication (the
conservative "independent cohorts" count), which widens them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

TERM, STEP = 126, 10


def cp_interval(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    a = 1 - level
    lo = 0.0 if k == 0 else stats.beta.ppf(a / 2, k, n - k + 1)
    hi = 1.0 if k == n else stats.beta.ppf(1 - a / 2, k + 1, n - k)
    return float(lo), float(hi)


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for (profile, size, method), g in df.groupby(["profile", "size", "method"]):
        n = len(g)
        reps = g["rep"].nunique()
        n_eff = max(1, round(n * STEP / TERM))
        k = int((g["loss"] > 1e-9).sum())
        lo, hi = cp_interval(k, n)
        k_eff = round(k * n_eff / n)
        _, hi_eff = cp_interval(k_eff, n_eff)
        liq = g["episodes"] > 0
        sold = g["sold_value"].sum()
        lr = g["loss_rate"].to_numpy()
        tail_n = max(1, int(np.ceil(0.025 * n)))
        tail = np.sort(lr)[-tail_n:]
        lead = (g["cf_first_underwater"] - g["first_call"])[
            (g["cf_first_underwater"] >= 0) & (g["first_call"] >= 0)
        ]
        dur = (g["last_sale"] - g["first_sale"] + 1)[g["first_sale"] >= 0]
        out.append(
            {
                "profile": profile, "size": size, "method": method, "cohorts": n,
                "replications": reps, "effective_independent": n_eff,
                "ltv_median": g["ltv0"].median(), "ltv_p10": g["ltv0"].quantile(0.1),
                "ltv_p90": g["ltv0"].quantile(0.9),
                "ltv_uncapped_median": g["ltv0_uncapped"].median(),
                "loss_events": k, "loss_freq": k / n, "loss_freq_ci_lo": lo,
                "loss_freq_ci_hi": hi, "loss_freq_ci_hi_effective": hi_eff,
                "expected_loss_rate": float(lr.mean()),
                "es975_loss_rate": float(tail.mean()),
                "max_loss_rate": float(lr.max()),
                "liquidation_freq": float(liq.mean()),
                "unnecessary_strict_share": float(g.loc[liq, "unnecessary_strict"].mean()) if liq.any() else float("nan"),
                "unnecessary_terminal_share": float(g.loc[liq, "unnecessary_terminal"].mean()) if liq.any() else float("nan"),
                "calls_per_loan": float(g["calls"].mean()),
                "cancelled_call_share": float(g["cancelled_calls"].sum() / max(g["calls"].sum(), 1)),
                "execution_cost_bps_of_sold": float(1e4 * g["execution_cost"].sum() / sold) if sold > 0 else float("nan"),
                "recovery_days_median": float(dur.median()) if len(dur) else float("nan"),
                "lead_time_days_median": float(lead.median()) if len(lead) else float("nan"),
                "cf_loss_freq": float((g["cf_loss"] > 1e-9).mean()),
                "stress_cohort_share": float((g["stress_days_in_term"] > 0).mean()),
            }
        )
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    rows = json.loads(Path(a.results).read_text())["rows"]
    df = pd.DataFrame(rows)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    main_df = df[df.sizing == "corrected"]
    summary = summarize(main_df)
    summary.to_csv(out / "summary_by_profile.csv", index=False)
    pooled = summarize(main_df.assign(profile="ALL", size="ALL"))
    pooled.to_csv(out / "summary_pooled.csv", index=False)
    by_size = summarize(main_df.assign(profile="ALL"))
    by_size.to_csv(out / "summary_by_size.csv", index=False)
    stress = summarize(main_df[main_df.stress_days_in_term > 0].assign(profile="STRESS", size="ALL"))
    stress.to_csv(out / "summary_stress_cohorts.csv", index=False)
    sizing = summarize(df[df.method.isin(["A", "v070"])].assign(profile="ALL", size="ALL", method=lambda x: x.method + ":" + x.sizing))
    sizing.to_csv(out / "summary_cure_sizing.csv", index=False)
    print(pooled.to_string())


if __name__ == "__main__":
    main()
