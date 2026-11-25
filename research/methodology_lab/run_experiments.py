"""Rolling-cohort comparison of v0.6.2, v0.7.0, a flat benchmark and A/B/C.

SYNTHETIC DATA ONLY (see dgp.py). Every method sees the same simulated
history at each origination date and is evaluated on the same realised
forward path under the same lifecycle mechanics (lifecycle.py).

Usage:
    python -m research.methodology_lab.run_experiments --out results.json \
        --v062 /path/to/v0.6.2/checkout --v070 /path/to/v0.7.0/checkout
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import zlib
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from . import dgp, lifecycle, methods

HISTORY = 500
TERM = 126            # trading days (about six months)
TERM_CALENDAR = 183
STEP = 10
CAP = {"etf": 0.70, "listed_equity": 0.65, "high_volatility_equity": 0.65}
FLAT = {"US": 0.50, "NGX": 0.30}
ENGINE_V = 1_000_000.0
PRICE = 100.0


def run_engine(checkout: str, requests: list[dict]) -> list[dict]:
    env = dict(os.environ, PYTHONPATH=checkout)
    worker = Path(__file__).with_name("engine_worker.py")
    proc = subprocess.run(
        [sys.executable, str(worker)],
        input=json.dumps(requests), capture_output=True, text=True, cwd=checkout,
        env=env, check=True,
    )
    return json.loads(proc.stdout)


def engine_request(rid: str, inputs: list[dict], vol_mult: float = 1.0) -> dict:
    assets = []
    for a in inputs:
        assets.append(
            {
                "id": a["id"], "type": a["type"], "price": PRICE,
                "quantity": a["weight"] * ENGINE_V / PRICE,
                "spread": a["spread"],
                "adv_value": a["weight"] * ENGINE_V / max(a["size_ratio"], 1e-9)
                * a["adv_mult"],
                "vol30": a["vol30"] * vol_mult, "vol90": a["vol90"] * vol_mult,
                "vol252": a["vol252"] * vol_mult, "mdd252": a["mdd252"],
                "gap252": a["gap252"],
            }
        )
    return {"id": rid, "assets": assets, "term_days": TERM_CALENDAR, "rate": 0.10}


def rolling_vol(r: np.ndarray, window: int) -> np.ndarray:
    out = np.full(r.shape, np.nan)
    c1 = np.cumsum(np.vstack([np.zeros(r.shape[1]), r]), axis=0)
    c2 = np.cumsum(np.vstack([np.zeros(r.shape[1]), r**2]), axis=0)
    for t in range(window, r.shape[0] + 1):
        s1 = c1[t] - c1[t - window]
        s2 = c2[t] - c2[t - window]
        out[t - 1] = np.sqrt(np.maximum(s2 - s1**2 / window, 0) / (window - 1))
    return out * np.sqrt(252)


def observed_returns(sim: dict) -> np.ndarray:
    """Returns as a monitor sees them: zero while halted, catch-up on reopening."""
    r = sim["r_loan"]
    obs = np.zeros_like(r)
    carry = np.zeros(r.shape[1])
    for t in range(r.shape[0]):
        carry += r[t]
        open_ = ~sim["halted"][t]
        obs[t] = np.where(open_, carry, 0.0)
        carry = np.where(open_, 0.0, carry)
    return obs


def combo_task(args: tuple) -> list[dict]:
    (profile_name, size, reps, cohorts, c_every, v062, v070, seed, appetite_names,
     trigger, alpha, grace, psi, with_bgap) = args
    profile = dgp.PROFILES[profile_name]
    size_ratio = profile.size_ratio[size]
    weights = np.full(profile.n_assets, 1.0 / profile.n_assets)
    base_adv = weights / size_ratio
    pol = lifecycle.Policy(grace=grace, psi=psi)
    rows: list[dict] = []
    cap = CAP[profile.asset_type]
    flat = FLAT[profile.market]
    # Engine vol-response grids for monitoring (per profile and size).
    grid_m = np.geomspace(0.3, 4.0, 15)
    grid_inputs = [
        {
            "id": f"A{i}", "type": profile.asset_type, "weight": float(weights[i]),
            "spread": profile.spread[i], "size_ratio": size_ratio, "adv_mult": 1.0,
            "vol30": profile.vol[i], "vol90": profile.vol[i], "vol252": profile.vol[i],
            "mdd252": 0.2, "gap252": 0.05,
        }
        for i in range(profile.n_assets)
    ]
    grid_req = [engine_request(f"g{k}", grid_inputs, m) for k, m in enumerate(grid_m)]
    grids = {}
    for label, checkout in (("v062", v062), ("v070", v070)):
        res = run_engine(checkout, grid_req)
        grids[label] = np.array([x["principal"] / x["mv"] for x in res])
    base_vol = float(np.sqrt(np.mean(np.asarray(profile.vol) ** 2)))
    for rep in range(reps):
        rng = np.random.default_rng([seed, rep, zlib.crc32(profile_name.encode()), zlib.crc32(size.encode())])
        days = HISTORY + TERM + cohorts * STEP
        sim = dgp.simulate(profile, days, rng)
        r_obs = observed_returns(sim)
        adv_all = sim["adv_mult"] * base_adv
        vmax = np.fmax.reduce(
            [rolling_vol(r_obs, 30), rolling_vol(r_obs, 90), rolling_vol(r_obs, 252)]
        )
        port_vmax = np.sqrt(np.nanmean(vmax**2, axis=1))
        cohort_data = []
        engine_reqs = []
        for k in range(cohorts):
            t0 = HISTORY + k * STEP
            hist = slice(t0 - HISTORY, t0)
            f = methods.estimate_features(
                r_obs[hist], sim["spread"][hist], adv_all[hist], sim["halted"][hist],
                sim["fx_ret"][hist] if profile.fx else None, weights, profile.asset_type,
            )
            inputs = []
            for i, a in enumerate(f.engine_inputs):
                inputs.append(
                    {**a, "size_ratio": size_ratio, "adv_mult": float(sim["adv_mult"][t0 - 1, i])}
                )
            engine_reqs.append(engine_request(f"c{k}", inputs))
            cohort_data.append((t0, f))
        eng = {label: run_engine(path, engine_reqs) for label, path in (("v062", v062), ("v070", v070))}
        for k, (t0, f) in enumerate(cohort_data):
            fwd = slice(t0, t0 + TERM)
            r_true = sim["r_loan"][fwd][None]
            halted = sim["halted"][fwd][None]
            spread = sim["spread"][fwd][None]
            adv = adv_all[fwd][None]
            sig_d = sim["sigma_true"][fwd][None]
            fx = sim["fx_ret"][fwd][None] if profile.fx else None
            r_obs_fwd = r_obs[fwd][None]
            m0 = port_vmax[t0 - 1] / base_vol
            m_path = port_vmax[fwd] / base_vol
            ratios: dict[str, tuple[float, np.ndarray]] = {}
            for label in ("v062", "v070"):
                res = eng[label][k]
                ell0 = res["principal"] / res["mv"]
                g0 = np.interp(m0, grid_m, grids[label])
                gp = np.interp(m_path, grid_m, grids[label])
                ratios[label] = (ell0, (ell0 * gp / max(g0, 1e-9))[None])
            ratios["flat"] = (flat, np.full((1, TERM), flat))
            ell_a, h_a = methods.approach_a(f, pol, alpha, cap)
            growth = 1 + pol.rate * methods.recovery_days(f, pol) / 252.0
            uncapped = {"A": (1 - h_a) / growth}
            ratios["A"] = (ell_a, methods.dynamic_ratio(ell_a, h_a, f, r_obs_fwd, cap, pol))
            ell_b, h_b = methods.approach_b(f, pol, alpha, cap, rng, n_paths=2000)
            uncapped["B"] = (1 - h_b) / growth
            if with_bgap:
                ell_g, h_g = methods.approach_b(
                    f, pol, 0.995, cap, rng, n_paths=2000, stress_share=0.25, with_gap=True
                )
                uncapped["B_gap"] = (1 - h_g) / growth
            ratios["B"] = (ell_b, methods.dynamic_ratio(ell_b, h_b, f, r_obs_fwd, cap, pol))
            if with_bgap:
                ratios["B_gap"] = (ell_g, methods.dynamic_ratio(ell_g, h_g, f, r_obs_fwd, cap, pol))
            maint_ell0 = uncapped["B"]  # common initial standard; call at psi x this
            maint_path = methods.dynamic_ratio(maint_ell0, h_b, f, r_obs_fwd, 10.0, pol)
            c_stats = {}
            if k % c_every == 0:
                for name in appetite_names:
                    ell_c, st = methods.approach_c(
                        f, pol, methods.APPETITES[name], cap, TERM, rng,
                        n_paths=500, haircut_b=h_b,
                        maintenance_ell0=maint_ell0 if trigger == "common" else None,
                    )
                    ratios[f"C_{name}"] = (
                        ell_c, methods.dynamic_ratio(ell_c, h_b, f, r_obs_fwd, cap, pol)
                    )
                    c_stats[name] = st
            for method, (ell0, own_path) in ratios.items():
                ell_path = maint_path if trigger == "common" else own_path
                for sizing in ("corrected", "cash_cure"):
                    if sizing == "cash_cure" and method not in ("A", "v070"):
                        continue
                    p = replace_policy(pol, sizing)
                    out = lifecycle.simulate_loans(
                        ell0, f.weights, r_true, halted, spread, adv, sig_d,
                        ell_path, fx, p,
                    )
                    cf = lifecycle.simulate_loans(
                        ell0, f.weights, r_true, halted, spread, adv, sig_d,
                        ell_path, fx, replace_policy(pol, "none"),
                    )
                    first_sale = int(out["first_sale"][0])
                    cushion_cf = cf["cushion_path"][0]
                    rows.append(
                        {
                            "profile": profile_name, "size": size, "rep": rep,
                            "cohort": k, "t0": t0, "method": method, "sizing": sizing, "trigger": trigger,
                            "ltv0": float(ell0),
                            "ltv0_uncapped": float(uncapped.get(method, ell0)),
                            "recovery_days": float(methods.recovery_days(f, pol)),
                            "regime_at_origination": int(sim["regime"][t0 - 1]),
                            "stress_days_in_term": int(sim["regime"][fwd].sum()),
                            "loss": float(out["loss"][0]),
                            "loss_rate": float(out["loss"][0] / ell0) if ell0 > 0 else 0.0,
                            "calls": int(out["calls"][0]),
                            "cancelled_calls": int(out["cancelled_calls"][0]),
                            "episodes": int(out["liquidation_episodes"][0]),
                            "first_call": int(out["first_call"][0]),
                            "first_sale": first_sale,
                            "last_sale": int(out["last_sale"][0]),
                            "sold_value": float(out["sold_value"][0]),
                            "execution_cost": float(out["execution_cost"][0]),
                            "cf_loss": float(cf["loss"][0]),
                            "cf_first_underwater": int(cf["first_underwater"][0]),
                            "unnecessary_strict": bool(
                                first_sale >= 0 and cushion_cf[first_sale:].min() >= 0
                            ),
                            "unnecessary_terminal": bool(
                                first_sale >= 0 and cushion_cf[-1] >= 0
                            ),
                            "c_inner": c_stats.get(method[2:], {}) if method.startswith("C_") else {},
                        }
                    )
    return rows


def replace_policy(pol: lifecycle.Policy, sizing: str) -> lifecycle.Policy:
    from dataclasses import replace

    return replace(pol, sizing=sizing)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--v062", required=True)
    ap.add_argument("--v070", required=True)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--cohorts", type=int, default=50)
    ap.add_argument("--c-every", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--profiles", default=",".join(dgp.PROFILES))
    ap.add_argument("--sizes", default="retail,institutional")
    ap.add_argument("--appetites", default="conservative,balanced")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--trigger", choices=["own", "common"], default="common")
    ap.add_argument("--alpha", type=float, default=0.99)
    ap.add_argument("--grace", type=int, default=2)
    ap.add_argument("--psi", type=float, default=1.15)
    ap.add_argument("--with-bgap", action="store_true")
    a = ap.parse_args()
    tasks = [
        (p, s, a.reps, a.cohorts, a.c_every, a.v062, a.v070, a.seed, a.appetites.split(","),
         a.trigger, a.alpha, a.grace, a.psi, a.with_bgap)
        for p in a.profiles.split(",")
        for s in a.sizes.split(",")
    ]
    start = time.time()
    rows: list[dict] = []
    with Pool(a.workers) as pool:
        for chunk in pool.imap_unordered(combo_task, tasks):
            rows.extend(chunk)
            print(f"{len(rows)} rows after {time.time() - start:.0f}s", flush=True)
    Path(a.out).write_text(json.dumps({"rows": rows, "config": vars(a)}))


if __name__ == "__main__":
    main()
