"""RC2 research: ex-ante haircut calibration with dependence- and selection-aware
comparison against RC1 (Report 17 review, section 2).

What is decided ex ante
    A haircut for instrument i at decision date t is ``-z_t * sigma_t`` where
    ``sigma_t`` is the model's 10-session volatility forecast and ``z_t`` is
    the 1% lower quantile of standardised outcomes ``ret / sigma`` that were
    fully observed by t (``end_day <= t``) in a trailing three-year window of
    the same market. To avoid an optimistic small-sample quantile, ``z_t`` is
    the order statistic at the lower 25% point of Binomial(n, 1%): the
    calibrated haircut is conservative with ~75% confidence. Standardised
    outcomes on training dates use out-of-fold forecasts (year folds, purged
    of overlapping targets), so no in-sample residual enters a calibration.

What is compared (identical decision rows)
    * standalone volatility estimators under the same ex-ante rule: RC1's
      frozen estimator, HAR, ridge, ridge without the history-length feature
      (the specification proposed for freezing), gradient-boosted trees;
    * RC1's complete framework: the approved LTV RC1 itself grants a retail
      single-instrument position (its estimation, scenarios, liquidity and
      utilisation), evaluated point in time on a subsample of decision dates;
    * a characteristic-based selector (no country rule): ridge where the
      instrument's recent stale-print share is below tau, otherwise RC1's
      estimator; tau is chosen on the 2024 validation year only.

Inference
    * 1% pinball loss of the haircut (a proper scoring rule for an ex-ante
      quantile), breach rate with Kupiec test, mean LTV, QLIKE;
    * intervals from a moving-block bootstrap over 3-month calendar blocks:
      a block carries every instrument (cross-sectional dependence) and the
      overlapping 10-session horizons inside it;
    * model selection: Hansen's SPA test (consistent recentring) of the best
      of the standalone challengers against RC1, so picking the winner from
      several candidates is accounted for.

RC1 is not modified. Only examined data (to EXAMINED_THROUGH) is read.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from datetime import UTC, date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

import numpy as np

from app.intelligence.benchmark import (
    TRAIN_END,
    VAL_END,
    H,
    Panel,
    _gcs_download,
    _gcs_upload,
    _kupiec,
    _splits,
    build_panel,
    vol_floor,
)

EXPERIMENT = "intelligence.exante_calibration.v1"
BREACH = 0.01
CAL_YEARS = 3
CAL_MIN = 250
CAL_CONF = 0.75
BLOCK_DAYS = 91
SESSIONS_COL = 15        # log(history length): a time-trend proxy, dropped in ridge_ns
STALE_COL = 16           # share of zero-return sessions in the last 22
LADV_COL = 10
STANDALONE = ("rc1", "har", "ridge", "ridge_ns", "gbt")
CHALLENGERS = ("har", "ridge", "ridge_ns", "gbt")
TAU_GRID = (0.0, 0.05, 0.10, 0.20, 0.30, 0.50, 1.01)   # 0.0 = always RC1, 1.01 = always ridge_ns


# ------------------------------------------------------------- forecasts

def _fit(name: str, X: np.ndarray, y: np.ndarray, seed: int) -> Any:
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import LinearRegression, Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if name == "har":
        return LinearRegression().fit(X[:, :4], y)
    if name in ("ridge", "ridge_ns"):
        return make_pipeline(StandardScaler(), Ridge(alpha=1.0)).fit(_cols(name, X), y)
    if name == "gbt":
        return HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                             l2_regularization=1.0, random_state=seed).fit(X, y)
    raise ValueError(name)


def _cols(name: str, X: np.ndarray) -> np.ndarray:
    if name == "har":
        return X[:, :4]
    if name == "ridge_ns":
        return np.delete(X, SESSIONS_COL, axis=1)
    return X


def market_forecasts(p: Panel, sp: dict[str, np.ndarray], market: str, seed: int = 7) -> dict[str, np.ndarray]:
    """Volatility forecasts for every row of ``market``: out-of-fold (calendar-year
    folds, purged of targets overlapping the fold) on training rows, the model fitted
    on all training rows elsewhere. Floors are the market's training floor."""
    m = p.market == market
    tr = sp["train"] & m
    fl = vol_floor(p, sp["train"])
    y = np.log(np.maximum(p.rv, fl))
    years = np.array([date.fromordinal(int(d)).year for d in p.day])
    out: dict[str, np.ndarray] = {"floor": fl, "rc1": np.where(m, p.rc1, np.nan)}
    for name in CHALLENGERS:
        s = np.full(p.rv.size, np.nan)
        full = _fit(name, p.X[tr], y[tr], seed)
        rest = m & ~tr
        s[rest] = np.exp(full.predict(_cols(name, p.X[rest]) if name != "gbt" else p.X[rest]))
        for yr in np.unique(years[tr]):
            lo, hi = date(int(yr), 1, 1).toordinal(), date(int(yr), 12, 31).toordinal()
            fold = tr & (years == yr)
            fit_rows = tr & ((p.end_day < lo) | (p.day > hi))
            if fit_rows.sum() < 200:                         # too little to refit: not used for calibration
                continue
            ffl = vol_floor(p, fit_rows)                    # the fold's floor excludes the fold
            yf = np.log(np.maximum(p.rv, ffl))
            mdl = _fit(name, p.X[fit_rows], yf[fit_rows], seed)
            s[fold] = np.maximum(np.exp(mdl.predict(_cols(name, p.X[fold]) if name != "gbt" else p.X[fold])),
                                 ffl[fold])
        out[name] = np.where(m, np.maximum(s, np.where(tr, 0.0, fl)), np.nan)
    return out


# ------------------------------------------------------------- calibration

def exante_z(p: Panel, sigma: np.ndarray, market: str, breach: float = BREACH,
             years: int = CAL_YEARS, n_min: int = CAL_MIN, conf: float = CAL_CONF) -> np.ndarray:
    """z_t per row (nan where fewer than n_min outcomes were observable at t)."""
    from scipy.stats import binom
    m = (p.market == market) & np.isfinite(sigma) & (sigma > 0)
    z = np.full(p.rv.size, np.nan)
    if not m.any():
        return z
    u_all = p.ret / np.where(m, sigma, np.nan)
    pool = np.where(m)[0]
    order = pool[np.argsort(p.end_day[pool], kind="stable")]
    ends = p.end_day[order]
    days_pool = p.day[order]
    u_sorted_by_end = u_all[order]
    window = round(365.25 * years)
    for d in np.unique(p.day[m]):
        k = int(np.searchsorted(ends, d, side="right"))       # outcomes known at the close of d
        if k == 0:
            continue
        sel = days_pool[:k] >= d - window
        u = u_sorted_by_end[:k][sel]
        n = u.size
        if n < n_min:
            continue
        j = max(1, int(binom.ppf(1 - conf, n, breach)))     # conservative order statistic (1-indexed)
        z[(p.day == d) & m] = float(np.partition(u, j - 1)[j - 1])
    return z


# ------------------------------------------------------------- RC1 complete framework

NAN3 = (math.nan, math.nan, math.nan)


def rc1_full_ltv_task(args: tuple[Any, ...]) -> tuple[float, float, float]:
    """RC1's own LTVs for a retail single-instrument position at row i:
    (approved = min(risk capacity, contractual ceiling) x utilisation,
     risk capacity x utilisation without the ceiling, contractual ceiling x utilisation)."""
    rows, i, market, symbol, prior = args
    from dataclasses import replace

    from app.candidate.engine import CandidateEngine, data_key
    from app.candidate.rc import rc_config
    from app.core.enums import AssetType
    from app.core.models import Holding, Loan, MarketData, Policy
    from app.credit.interest import InterestPolicy
    from app.liquidation.policy import LiquidationExecutionPolicy
    from research.methodology_lab.candidate_validation import (
        CCY,
        EXCH,
        NGX_VOLUME_FROM,
        POSITION,
        RATE,
        US_ETF,
    )

    cfg = rc_config()
    d = rows[i]["date"]
    price = float(rows[i]["adj_close"])
    position = POSITION[market]["retail"]
    bars = []
    for r in rows[max(0, i - 500):i + 1]:
        close = float(r["adj_close"])
        vol = float(r["volume"] or 0.0)
        reported = bool(r["volume_reported"]) and vol > 0 and not (market == "NGX" and r["date"] < NGX_VOLUME_FROM)
        if not reported and market == "NGX":
            vol, reported = position / close / (20 * 0.10), True        # stated assumption, as in validation
        ratio = close / float(r["close"]) if r["close"] else 1.0
        hi, lo = float(r["high"]) * ratio, float(r["low"]) * ratio
        bars.append({"bar_date": r["date"], "open": float(r["open"]) * ratio, "high": hi, "low": lo,
                     "close": close, "volume": vol, "volume_reported": reported,
                     "range_reported": bool(r["range_reported"]) and hi > lo > 0})
    at = AssetType.ETF if symbol in US_ETF else AssetType.LISTED_EQUITY
    h = Holding(symbol, at, position / price, CCY[market], EXCH[market])
    asof = datetime.combine(d, dtime(21), UTC)
    md = {h.stable_key: MarketData(symbol, price, timestamp=asof)}
    try:
        eng = CandidateEngine(replace(cfg), prior)
        ev = eng.evaluate("exante", [h], Loan(1.0, currency=CCY[market]), Policy.default(), md,
                          bars={data_key(h): bars}, execution_policy=LiquidationExecutionPolicy(),
                          interest_policy=InterestPolicy(RATE[market], accrual_frequency="monthly",
                                                         compounding="compound"),
                          as_of=asof, seed=11, plan=False)
    except Exception:  # noqa: BLE001 - recorded as missing, never silently imputed
        return NAN3
    if ev.data_status != "ok" or not ev.portfolio_market_value:
        return NAN3
    v, u = ev.portfolio_market_value, cfg.capacity_utilisation
    unc, ceil = ev.thresholds["initial_capacity_uncapped"], ev.thresholds["client_ceiling"]
    return (float(max(0.0, min(unc, ceil)) * u / v), float(max(0.0, unc) * u / v), float(ceil * u / v))


def rc1_full(p: Panel, dss: list[Any], mask: np.ndarray, workers: int = 4) -> np.ndarray:
    """(n, 3) array: approved, uncapped and ceiling LTVs (nan outside mask)."""
    from multiprocessing import Pool

    from research.methodology_lab.candidate_validation import priors_at
    out = np.full((p.rv.size, 3), np.nan)
    by_market = {ds.meta["market"]: ds for ds in dss}
    prior_cache: dict[tuple[str, int], Any] = {}
    all_bars: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for m, ds in by_market.items():
        all_bars[m] = {s: [{"bar_date": r["date"], "close": r["adj_close"]} for r in rows]
                       for s, rows in ds.bars.items()}
    tasks, idx = [], []
    for j in np.where(mask)[0]:
        m, s = str(p.sym[j]).split(":", 1)
        key = (m, int(p.day[j]))
        if key not in prior_cache:
            prior_cache[key] = priors_at(m, all_bars[m], date.fromordinal(int(p.day[j])))
        assert p.row_idx is not None
        tasks.append((by_market[m].bars[s], int(p.row_idx[j]), m, s, prior_cache[key]))
        idx.append(j)
    with Pool(workers) as pool:
        vals = pool.map(rc1_full_ltv_task, tasks, chunksize=4)
    if idx:
        out[np.array(idx, int)] = np.array(vals, float)
    return out


# ------------------------------------------------------------- inference

def _blocks(day: np.ndarray) -> list[np.ndarray]:
    b = (day - day.min()) // BLOCK_DAYS
    return [np.where(b == k)[0] for k in np.unique(b)]


def block_ci(x: np.ndarray, day: np.ndarray, reps: int = 2000, seed: int = 11) -> list[float]:
    """95% interval for mean(x): moving blocks of two adjacent 3-month calendar blocks."""
    blocks = _blocks(day)
    nb = len(blocks)
    if nb < 3:
        return [math.nan, math.nan]
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(reps):
        starts = rng.integers(0, nb - 1, size=math.ceil(nb / 2))
        idx = np.concatenate([np.concatenate([blocks[s], blocks[s + 1]]) for s in starts])
        stats.append(float(x[idx].mean()))
    lo, hi = np.quantile(stats, [0.025, 0.975])
    return [float(lo), float(hi)]


def spa_test(loss_bench: np.ndarray, losses: dict[str, np.ndarray], day: np.ndarray, reps: int = 2000,
             seed: int = 13) -> dict[str, Any]:
    """Hansen (2005) SPA with consistent recentring, H0: no challenger beats the benchmark.
    Bootstrap by moving 3-month calendar blocks (dependence across instruments and
    overlapping horizons)."""
    names = list(losses)
    d = np.stack([loss_bench - losses[k] for k in names])          # >0: challenger better
    n = d.shape[1]
    dbar = d.mean(axis=1)
    blocks = _blocks(day)
    nb = len(blocks)
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(reps):
        starts = rng.integers(0, nb - 1, size=math.ceil(nb / 2))
        idx = np.concatenate([np.concatenate([blocks[s], blocks[s + 1]]) for s in starts])
        boots.append(d[:, idx].mean(axis=1))
    bm = np.array(boots)
    omega = np.sqrt(n) * bm.std(axis=0) + 1e-12
    t_obs = max(0.0, float(np.max(np.sqrt(n) * dbar / omega)))
    # SPA_c: challengers far below the benchmark are not recentred (Hansen 2005, g_c).
    g = np.where(np.sqrt(n) * dbar / omega >= -np.sqrt(2 * math.log(math.log(max(n, 3)))), dbar, 0.0)
    t_boot = np.maximum(0.0, np.max(np.sqrt(n) * (bm - g) / omega, axis=1))
    best = names[int(np.argmax(np.sqrt(n) * dbar / omega))]
    return {"statistic": t_obs, "p_value": float(np.mean(t_boot >= t_obs)), "best": best,
            "mean_loss_reduction": {k: float(v) for k, v in zip(names, dbar, strict=True)}, "reps": reps}


def pinball(y: np.ndarray, q: np.ndarray, a: float = BREACH) -> np.ndarray:
    e = y - q
    return np.maximum(a * e, (a - 1) * e)


def qlike(rv: np.ndarray, s: np.ndarray, fl: np.ndarray) -> np.ndarray:
    v = np.maximum(rv, fl) ** 2
    return v / s ** 2 - np.log(v / s ** 2 + 1e-12) - 1


# ------------------------------------------------------------- experiment

def haircuts(p: Panel, sp: dict[str, np.ndarray], markets: list[str], seed: int = 7) -> dict[str, dict[str, np.ndarray]]:
    """Ex-ante haircut quantile q = z_t * sigma_t (a negative log return) and LTV per model."""
    sig: dict[str, np.ndarray] = {k: np.full(p.rv.size, np.nan) for k in STANDALONE}
    floor = np.full(p.rv.size, np.nan)
    for m in markets:
        f = market_forecasts(p, sp, m, seed)
        mm = p.market == m
        floor[mm] = f["floor"][mm]
        for k in STANDALONE:
            sig[k][mm] = f[k][mm]
    out: dict[str, dict[str, np.ndarray]] = {"_floor": {"floor": floor}}
    for k in STANDALONE:
        z = np.full(p.rv.size, np.nan)
        for m in markets:
            zm = exante_z(p, sig[k], m)
            z[p.market == m] = zm[p.market == m]
        q = z * sig[k]
        out[k] = {"sigma": sig[k], "z": z, "q": q, "ltv": np.clip(1 + q, 0, 1)}
    return out


def choose_tau(p: Panel, sp: dict[str, np.ndarray], hc: dict[str, dict[str, np.ndarray]],
               max_breach: float = 0.015) -> dict[str, Any]:
    """Selector threshold from the 2024 validation year only: highest mean LTV whose
    validation breach rate does not exceed max_breach (RC1 if none qualifies)."""
    v = sp["val"] & np.isfinite(hc["rc1"]["q"]) & np.isfinite(hc["ridge_ns"]["q"])
    grid = []
    best = (0.0, -1.0)
    for tau in TAU_GRID:
        q = np.where(p.X[:, STALE_COL] < tau, hc["ridge_ns"]["q"], hc["rc1"]["q"])
        br = float(np.mean(p.ret[v] < q[v])) if v.any() else math.nan
        ltv = float(np.clip(1 + q[v], 0, 1).mean()) if v.any() else math.nan
        grid.append({"tau": tau, "val_breach": br, "val_mean_ltv": ltv})
        if br <= max_breach and ltv > best[1]:
            best = (tau, ltv)
    return {"tau": best[0], "grid": grid, "rule": f"highest validation LTV with breach <= {max_breach:.3f}"}


def evaluate_rows(p: Panel, rows: np.ndarray, hc: dict[str, dict[str, np.ndarray]],
                  models: list[str]) -> dict[str, Any]:
    fl = hc["_floor"]["floor"]
    y = p.ret[rows]
    res: dict[str, Any] = {"n": int(rows.sum()), "instruments": len(set(p.sym[rows])),
                           "first_day": date.fromordinal(int(p.day[rows].min())).isoformat() if rows.any() else None,
                           "blocks_3m": len(_blocks(p.day[rows])) if rows.any() else 0}
    base_pin = pinball(y, hc["rc1"]["q"][rows])
    base_ltv = hc["rc1"]["ltv"][rows]
    for k in models:
        q = hc[k]["q"][rows]
        ltv = hc[k]["ltv"][rows]
        br = (y < q).astype(float)
        r: dict[str, Any] = {"breach_rate": float(br.mean()), "kupiec_p": _kupiec(int(br.sum()), br.size, BREACH),
                             "mean_ltv": float(ltv.mean()), "pinball_1pct": float(pinball(y, q).mean())}
        if k != "rc1":
            r["mean_ltv_minus_rc1"] = float((ltv - base_ltv).mean())
            r["mean_ltv_minus_rc1_ci95"] = block_ci(ltv - base_ltv, p.day[rows])
            r["breach_minus_rc1"] = float(br.mean() - np.mean(y < hc["rc1"]["q"][rows]))
            r["breach_minus_rc1_ci95"] = block_ci(br - (y < hc["rc1"]["q"][rows]), p.day[rows])
            r["pinball_minus_rc1_ci95"] = block_ci(pinball(y, q) - base_pin, p.day[rows])
        if "sigma" in hc[k] and np.isfinite(hc[k]["sigma"][rows]).all():
            ql = qlike(p.rv[rows], hc[k]["sigma"][rows], fl[rows])
            r["qlike"] = float(ql.mean())
            if k != "rc1":
                r["qlike_minus_rc1_ci95"] = block_ci(ql - qlike(p.rv[rows], hc["rc1"]["sigma"][rows], fl[rows]),
                                                     p.day[rows])
        res[k] = r
    return res


def liquidity(p: Panel, sp: dict[str, np.ndarray], markets: list[str]) -> dict[str, Any]:
    """Forward 20-session traded value: RC1-style trailing ADV (naive) against ridge."""
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    out: dict[str, Any] = {}
    if p.fadv is None:
        return out
    for m in markets:
        mm = (p.market == m) & np.isfinite(p.fadv)
        tr, te = sp["train"] & mm, sp["test"] & mm
        if tr.sum() < 100 or te.sum() < 30:
            out[m] = {"skipped": "insufficient reported volume"}
            continue
        naive = p.X[:, LADV_COL]
        mdl = make_pipeline(StandardScaler(), Ridge(alpha=1.0)).fit(p.X[tr], p.fadv[tr])
        pred = mdl.predict(p.X[te])
        e_n, e_r = np.abs(p.fadv[te] - naive[te]), np.abs(p.fadv[te] - pred)
        out[m] = {"n_test": int(te.sum()), "mae_log_naive": float(e_n.mean()), "mae_log_ridge": float(e_r.mean()),
                  "bias_log_naive": float((naive[te] - p.fadv[te]).mean()),
                  "bias_log_ridge": float((pred - p.fadv[te]).mean()),
                  "mae_diff_ci95": block_ci(e_r - e_n, p.day[te]),
                  "overstatement_share_naive": float(np.mean(naive[te] > p.fadv[te]))}
    return out


def ngx_diagnostics(p: Panel, rows: np.ndarray, hc: dict[str, dict[str, np.ndarray]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    nm = rows & (p.market == "NGX")
    if not nm.any():
        return out
    stale = p.X[:, STALE_COL]
    out["zero_realised_share"] = float(np.mean(p.rv[nm] <= 0))
    out["range_reported_share"] = float(np.mean(p.X[nm, 13]))
    out["volume_reported_share"] = float(np.mean(p.X[nm, 14]))
    for lbl, sel in (("stale_0", stale == 0), ("stale_0_20", (stale > 0) & (stale <= 0.2)),
                     ("stale_over_20", stale > 0.2)):
        r = nm & sel
        if r.sum() < 20:
            out[lbl] = {"n": int(r.sum())}
            continue
        out[lbl] = {"n": int(r.sum()), **{k: {"breach": float(np.mean(p.ret[r] < hc[k]["q"][r])),
                                               "mean_ltv": float(hc[k]["ltv"][r].mean())}
                                           for k in ("rc1", "ridge_ns")}}
    return out


def main() -> None:
    from app.learning.datasets import load_research_dataset
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, help="'+'-joined gs:// or local dataset directories")
    ap.add_argument("--out", required=True)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--full-every", type=int, default=2, help="RC1 complete framework on every k-th test row")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp())
    dss = []
    for i, d in enumerate(a.dataset.split("+")):
        path = _gcs_download(d, tmp / f"ds{i}") if d.startswith("gs://") else Path(d)
        dss.append(load_research_dataset(path))
    p = build_panel(dss, step=a.step)
    sp = _splits(p)
    markets = sorted(set(p.market))
    hc = haircuts(p, sp, markets, a.seed)
    sel = choose_tau(p, sp, hc)
    q_sel = np.where(p.X[:, STALE_COL] < sel["tau"], hc["ridge_ns"]["q"], hc["rc1"]["q"])
    hc["selector"] = {"q": q_sel, "ltv": np.clip(1 + q_sel, 0, 1)}
    ok = np.all([np.isfinite(hc[k]["q"]) for k in STANDALONE], axis=0)
    test_rows = {seg: sp[seg] & ok for seg in ("test", "test_unseen")}
    # RC1 complete framework on a fixed subsample of test rows (every k-th, by position)
    full_mask = np.zeros(p.rv.size, bool)
    for seg in ("test", "test_unseen"):
        idx = np.where(test_rows[seg])[0]
        full_mask[idx[::a.full_every]] = True
    t1 = time.time()
    full = rc1_full(p, dss, full_mask)
    ltv_full, ltv_unc, ceiling = full[:, 0], full[:, 1], full[:, 2]
    hc["rc1_full"] = {"q": np.log(np.maximum(ltv_full, 1e-9)), "ltv": ltv_full}   # haircut as a log-return level
    hc["rc1_full_uncapped"] = {"q": np.log(np.maximum(ltv_unc, 1e-9)), "ltv": ltv_unc}
    for k in ("rc1", "ridge_ns", "selector"):               # the same contractual ceiling for every method
        lc = np.minimum(hc[k]["ltv"], ceiling)
        hc[k + "_ceiling"] = {"q": np.log(np.maximum(lc, 1e-9)), "ltv": lc}
    full_seconds = time.time() - t1
    models = list(STANDALONE) + ["selector"]
    results: dict[str, Any] = {}
    for seg, rows in test_rows.items():
        results[seg] = {"pooled": evaluate_rows(p, rows, hc, models)}
        for m in markets:
            results[seg][m] = evaluate_rows(p, rows & (p.market == m), hc, models)
        fr = rows & full_mask & np.isfinite(ltv_full)
        results[seg]["rc1_full_subsample"] = {
            m: {**evaluate_rows(p, fr & (p.market == m), hc,
                                ["rc1", "ridge_ns", "selector", "rc1_full", "rc1_full_uncapped",
                                 "rc1_ceiling", "ridge_ns_ceiling", "selector_ceiling"]),
                "ceiling_binding_share": float(np.mean((ltv_unc >= ceiling - 1e-12)[fr & (p.market == m)]))
                if (fr & (p.market == m)).any() else None}
            for m in markets}
    spa: dict[str, Any] = {}
    for m in markets:
        r = test_rows["test"] & (p.market == m)
        fl = hc["_floor"]["floor"]
        spa[m] = {"qlike": spa_test(qlike(p.rv[r], hc["rc1"]["sigma"][r], fl[r]),
                                    {k: qlike(p.rv[r], hc[k]["sigma"][r], fl[r]) for k in CHALLENGERS}, p.day[r]),
                  "pinball_1pct": spa_test(pinball(p.ret[r], hc["rc1"]["q"][r]),
                                           {k: pinball(p.ret[r], hc[k]["q"][r]) for k in CHALLENGERS}, p.day[r])}
    report = {
        "experiment": EXPERIMENT, "horizon_sessions": H, "breach_target": BREACH, "step_sessions": a.step,
        "calibration": {"window_years": CAL_YEARS, "min_outcomes": CAL_MIN, "confidence": CAL_CONF,
                        "training_residuals": "out-of-fold (calendar-year folds, purged)"},
        "splits": {"train_end": TRAIN_END.isoformat(), "val_end": VAL_END.isoformat()},
        "datasets": [ds.meta["dataset_id"] for ds in dss], "data_through": max(ds.meta["last_date"] for ds in dss),
        "evidence_class": "public_historical",
        "panel": {"rows": int(p.X.shape[0]), "instruments": len(set(p.sym)), **{k: int(v.sum()) for k, v in sp.items()},
                  "test_rows_all_models": {k: int(v.sum()) for k, v in test_rows.items()},
                  "rc1_full_evaluated": int(full_mask.sum()), "rc1_full_valid": int(np.isfinite(ltv_full).sum())},
        "comparison_note": ("rc1_full* are RC1's own decisions; their breach is a 10-session price fall beyond "
                            "1 - LTV, an approximation of RC1's multi-horizon criterion. *_ceiling apply RC1's "
                            "contractual ceiling x utilisation to every method."),
        "selector": sel, "results": results, "spa": spa,
        "liquidity_forecast": liquidity(p, sp, markets),
        "ngx_diagnostics": {seg: ngx_diagnostics(p, rows, hc) for seg, rows in test_rows.items()},
        "seconds": {"total": round(time.time() - t0, 1), "rc1_full": round(full_seconds, 1)},
    }
    blob = json.dumps(report, indent=1, default=float).encode()
    if a.out.startswith("gs://"):
        _gcs_upload(a.out, "exante.json", blob)
    else:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / "exante.json").write_bytes(blob)
    print("CRI_EXANTE_CALIBRATION " + json.dumps({k: report[k] for k in ("experiment", "panel", "seconds")},
                                                 default=float), flush=True)


if __name__ == "__main__":
    main()
