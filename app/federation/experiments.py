"""Repeated federated-learning experiments (Report 17 review, section 5).

Grid: seeds x participant counts x heterogeneity x privacy configurations. For
each run: mean holdout log loss across institutions of a well-tuned local model
(L2 by 5-fold CV), a well-tuned centralised model, the federated global model
and the personalised federated model; and the smallest institution's result.
Across seeds: mean paired difference against the tuned local baseline with a
95% interval (t), and the share of seeds in which the configuration improves.

Synthetic institutions only. No institutional data is accessed.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any

import numpy as np

from app.federation.simulate import institutions, run

CONFIGS: dict[str, dict[str, Any]] = {
    "no_dp_weighted": {"noise_multiplier": 0.0, "weight_cap": 1500},
    "dp_z1_uniform": {"noise_multiplier": 1.0, "weight_cap": 1},
    "dp_z2_uniform": {"noise_multiplier": 2.0, "weight_cap": 1},
}


def one(seed: int, count: int, hetero: float, config: str, eps_limit: float = 20.0) -> dict[str, Any]:
    insts = institutions(count, hetero, seed)
    cfg = CONFIGS[config]
    res = run(seed, rounds=15, noise_multiplier=float(cfg["noise_multiplier"]), eps_limit=eps_limit,
              weight_cap=int(cfg["weight_cap"]), insts=insts, demos=False)
    comp = res["5_6_comparison"]
    rows = {k: v for k, v in comp.items() if not k.startswith("_")}
    small = min(rows, key=lambda k: rows[k]["train_rows"])
    out = {"seed": seed, "count": count, "heterogeneity": hetero, "config": config,
           "rounds": len(res["2_4_rounds"]), "epsilon": (res.get("privacy") or {}).get("coordinator_epsilon"),
           "epsilon_agent_worst": (res.get("privacy") or {}).get("agent_epsilon_worst_case"),
           "mean": comp["_mean_log_loss"], "smallest": {m: rows[small][m]["log_loss"]
                                                       for m in ("local", "central", "federated", "personal")},
           "smallest_rows": rows[small]["train_rows"]}
    return out


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    from scipy.stats import t as tdist
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in results:
        groups.setdefault((r["config"], r["count"], r["heterogeneity"]), []).append(r)
    out = {}
    for (cfg, cnt, het), rs in sorted(groups.items()):
        line: dict[str, Any] = {"seeds": len(rs), "rounds_mean": float(np.mean([r["rounds"] for r in rs])),
                                "epsilon_mean": None if rs[0]["epsilon"] is None
                                else float(np.mean([r["epsilon"] for r in rs]))}
        for scope in ("mean", "smallest"):
            for m in ("central", "federated", "personal"):
                d = np.array([r[scope][m] - r[scope]["local"] for r in rs])   # < 0: better than tuned local
                half = float(tdist.ppf(0.975, d.size - 1) * d.std(ddof=1) / math.sqrt(d.size)) if d.size > 1 else math.nan
                line[f"{scope}_{m}_minus_local"] = {"mean": float(d.mean()), "ci95": [float(d.mean() - half),
                                                                                      float(d.mean() + half)],
                                                    "share_better": float(np.mean(d < 0))}
            line[f"{scope}_local_log_loss"] = float(np.mean([r[scope]["local"] for r in rs]))
        out[f"{cfg}|n{cnt}|h{het}"] = line
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--counts", default="3,6,12")
    ap.add_argument("--heterogeneity", default="0.5,1.0,2.0")
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    from multiprocessing import Pool
    t0 = time.time()
    jobs = [(s, int(c), float(h), cfg) for s in range(1, a.seeds + 1) for c in a.counts.split(",")
            for h in a.heterogeneity.split(",") for cfg in a.configs.split(",")]
    with Pool(a.workers) as pool:
        results = pool.starmap(one, jobs)
    report = {"experiment": "federation.repeated.v1", "jobs": len(jobs), "summary": summarise(results),
              "results": results, "seconds": round(time.time() - t0, 1)}
    blob = json.dumps(report, default=float)
    if a.out.startswith("gs://"):
        from app.intelligence.benchmark import _gcs_upload
        _gcs_upload(a.out, "federation_experiments.json", blob.encode())
    elif a.out:
        with open(a.out, "w") as f:
            f.write(blob)
    print("CRI_FEDERATION_EXPERIMENTS " + json.dumps({"jobs": len(jobs), "seconds": report["seconds"]}), flush=True)


if __name__ == "__main__":
    main()
