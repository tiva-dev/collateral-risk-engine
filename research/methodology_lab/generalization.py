"""Cross-market generalization of asset-level risk estimation (research).

Question: can CRI onboard a market without a market-specific model? We
simulate onboarding by withholding an entire market (and, separately, an
instrument group within a market): every held-out instrument is given only
``k`` sessions of history at each decision date, as if newly listed or newly
covered, and we forecast its forward risk with:

  raw          the instrument's own k-session estimate only
  pooled       partial pooling across the held-out instruments (cross-section
               of the new market only; no other market used)
  hierarchical empirical-Bayes shrinkage toward priors learned from the
               training market (asset-group level), plus a market-level
               random effect estimated from the held-out cross-section

Forecast targets at each decision date t (no information after t is used):
  - forward 21-session realised volatility (QLIKE loss, log bias)
  - 10-session 99% loss quantile (VaR) exceedance rate (nominal 1%)
  - frequency of |one-day move| >= 8% over the next 63 sessions

The same universal estimator is applied in every market; only data differ.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy import stats

from . import real_data as rd

KS = (20, 60, 120)
STEP = 21
WARM = 260
FWD_VOL = 21
FWD_VAR = 10
FWD_GAP = 63
GAP = 0.08
GROUPS = {
    "NGX": {"banks": {"GTCO", "ZENITHBANK", "ACCESSCORP", "UBA", "FIRSTHOLDCO", "FBNH", "STANBIC", "FIDELITYBK"}},
    "US": {"etf": set(rd.US_ETF)},
}


def _panel(manifest_path: str, market: str) -> dict:
    manifest = json.loads(Path(manifest_path).read_text())
    from app.simulations.run_official_validation import _load_replay_inputs

    bars, _ = _load_replay_inputs(manifest)
    return rd.build_panel(rd.NGX if market == "NGX" else rd.US, bars, market)


def _features(r: np.ndarray) -> tuple[float, float, float]:
    """log daily vol, excess kurtosis (winsorised), gap frequency from a window."""
    x = r[np.isfinite(r)]
    sd = max(float(np.std(x, ddof=1)), 1e-6)
    z = np.clip(x / sd, -10, 10)
    return float(np.log(sd)), float(max(stats.kurtosis(z, fisher=True), 0.0)), float(np.mean(np.abs(x) >= GAP))


def train_priors(panel: dict, symbols: set[str] | None = None) -> dict:
    """Group-level priors from full histories of the training instruments."""
    r = panel["r"]
    rows = []
    for j, s in enumerate(panel["symbols"]):
        if symbols is not None and s not in symbols:
            continue
        x = r[panel["listed"][:, j], j]
        if x.size < WARM:
            continue
        lv, ku, gf = _features(x[-756:])
        rows.append((lv, ku, gf, s))
    if not rows:
        raise ValueError("no training instruments")
    lv = np.array([x[0] for x in rows])
    ku = np.array([x[1] for x in rows])
    gf = np.array([x[2] for x in rows])
    return {"mu_logvol": float(lv.mean()), "tau2_logvol": float(max(lv.var(ddof=1), 1e-4)),
            "kurtosis": float(np.median(ku)), "gap_freq": float(gf.mean()), "n": len(rows)}


def nu_from_kurtosis(k: float) -> float:
    return float(np.clip(4 + 6 / max(k, 1e-3), 3.0, 30.0))


def evaluate(test: dict, test_symbols: list[int], priors: dict | None, k: int) -> dict:
    r = test["r"]
    listed = test["listed"]
    nd = r.shape[0]
    out = {m: {"qlike": [], "logbias": [], "exceed": [], "gap_err": []} for m in ("raw", "pooled", "hierarchical")}
    for t in range(WARM, nd - FWD_GAP, STEP):
        cols = [j for j in test_symbols if listed[t - k:t, j].all() and listed[t:t + FWD_GAP, j].all()]
        if len(cols) < 3:
            continue
        hist = r[t - k:t, cols]
        logsd = np.log(np.maximum(hist.std(axis=0, ddof=1), 1e-6))
        v_i = 1.0 / (2 * (k - 1))  # sampling variance of a log sample s.d.
        # pooled: cross-section of the held-out market only
        mu_x = float(logsd.mean())
        tau2_x = float(max(logsd.var(ddof=1) - v_i, 1e-4))
        w_x = tau2_x / (tau2_x + v_i)
        pooled = w_x * logsd + (1 - w_x) * mu_x
        if priors is not None:
            # market random effect: held-out cross-section mean vs training prior, shrunk by its precision
            delta_hat = mu_x - priors["mu_logvol"]
            prec = len(cols) / (priors["tau2_logvol"] + v_i)
            delta = delta_hat * prec / (prec + 1.0 / max(priors["tau2_logvol"], 1e-4))
            prior_i = priors["mu_logvol"] + delta
            w = priors["tau2_logvol"] / (priors["tau2_logvol"] + v_i)
            hier = w * logsd + (1 - w) * prior_i
            nu_h = nu_from_kurtosis(priors["kurtosis"])
            gap_h = priors["gap_freq"]
        else:
            hier = pooled
            nu_h = 5.0
            gap_h = float(np.mean(np.abs(hist) >= GAP))
        fwd = r[t:t + FWD_VOL, cols]
        real_var = np.maximum(fwd.var(axis=0, ddof=1), 1e-10)
        loss10 = -np.expm1(r[t:t + FWD_VAR, cols].sum(axis=0))
        gap_real = np.mean(np.abs(r[t:t + FWD_GAP, cols]) >= GAP, axis=0)
        for name, lsd, nu, gap_pred in (
            ("raw", logsd, nu_from_kurtosis(float(np.median([stats.kurtosis(h, fisher=True) for h in hist.T]))),
             np.mean(np.abs(hist) >= GAP, axis=0)),
            ("pooled", pooled, 5.0, np.full(len(cols), float(np.mean(np.abs(hist) >= GAP)))),
            ("hierarchical", hier, nu_h, np.full(len(cols), gap_h)),
        ):
            pred_var = np.exp(2 * lsd)
            q = stats.t.ppf(0.99, nu) * np.sqrt((nu - 2) / nu) * np.sqrt(pred_var * FWD_VAR)
            var99 = -np.expm1(-q)
            ratio = real_var / pred_var
            out[name]["qlike"].extend((ratio - np.log(ratio) - 1).tolist())
            out[name]["logbias"].extend((0.5 * np.log(ratio)).tolist())
            out[name]["exceed"].extend((loss10 > var99).astype(float).tolist())
            out[name]["gap_err"].extend(np.abs(np.asarray(gap_pred) - gap_real).tolist())
    return {m: {"n": len(v["qlike"]), "qlike": float(np.mean(v["qlike"])) if v["qlike"] else None,
                "log_vol_bias": float(np.mean(v["logbias"])) if v["logbias"] else None,
                "var99_exceedance": float(np.mean(v["exceed"])) if v["exceed"] else None,
                "gap_freq_abs_error": float(np.mean(v["gap_err"])) if v["gap_err"] else None}
            for m, v in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    panels = {m: _panel(a.manifest, m) for m in ("NGX", "US")}
    results: dict = {"withhold_market": {}, "withhold_group": {}, "within_market_full_history": {}}
    for test_m, train_m in (("NGX", "US"), ("US", "NGX")):
        priors = train_priors(panels[train_m])
        idx = list(range(len(panels[test_m]["symbols"])))
        results["withhold_market"][f"train {train_m} -> test {test_m}"] = {
            "priors": priors, **{f"k={k}": evaluate(panels[test_m], idx, priors, k) for k in KS}}
        own = train_priors(panels[test_m])
        results["within_market_full_history"][test_m] = {
            "priors": own, **{f"k={k}": evaluate(panels[test_m], idx, own, k) for k in KS}}
    for m, groups in GROUPS.items():
        p = panels[m]
        for g, members in groups.items():
            held = [j for j, s in enumerate(p["symbols"]) if s in members]
            rest = {s for s in p["symbols"] if s not in members}
            if len(held) < 3 or len(rest) < 3:
                results["withhold_group"][f"{m}:{g}"] = {"skipped": f"held={len(held)} rest={len(rest)}"}
                continue
            priors = train_priors(p, rest)
            results["withhold_group"][f"{m}:{g}"] = {
                "priors": priors, **{f"k={k}": evaluate(p, held, priors, k) for k in KS}}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "generalization.json").write_text(json.dumps(results, indent=2))
    print("#### GENERALIZATION")
    print("experiment\tk\testimator\tn\tqlike\tlog_vol_bias\tvar99_exceed\tgap_abs_err")
    for section, exps in results.items():
        for name, res in exps.items():
            for k in KS:
                block = res.get(f"k={k}")
                if not block:
                    continue
                for est, x in block.items():
                    if x["n"]:
                        print(f"{section}:{name}\t{k}\t{est}\t{x['n']}\t{x['qlike']:.4f}\t{x['log_vol_bias']:+.4f}\t"
                              f"{x['var99_exceedance']:.4f}\t{x['gap_freq_abs_error']:.4f}")


if __name__ == "__main__":
    main()
