"""CRI Intelligence M2: forward volatility and downside benchmarks on real data.

    python -m app.intelligence.benchmark --dataset <dir|gs://...> [--dataset ...] --out <dir|gs://...>

Evidence class: public historical (examined tier only; the loader refuses any
observation after the RC1 examined-through date). Research and shadow only.

Targets at each evaluation date t, horizon h sessions (default 10):
  * realised volatility  RV = sqrt(sum r_{t+1..t+h}^2)
  * downside: the 5% quantile of the h-session log return R = sum r_{t+1..t+h}

Models (all point-in-time; features use data up to and including t):
  * rc1          RC1's own instrument estimator (app.candidate.estimation,
                 estimate_instrument, frozen config, no group prior), sigma_used * sqrt(h)
  * har          HAR regression of log RV on log RV_1/5/22/66 (pooled OLS)
  * ridge        ridge regression on all features
  * gbt          gradient-boosted trees on all features (log RV)
  * hier         gbt + per-instrument intercept partially pooled toward zero with
                 weight n/(n + k): own evidence replaces pooled information as it accrues
  * gbt_q        direct gradient-boosted 5% quantile of R (downside only)
Volatility models produce the downside quantile as sigma_hat * z, with z the 5%
quantile of R / sigma_hat on the training set (each model calibrates its own z).

Splits: chronological train (<= 2023-12-31), validation (2024), test
(2025-01-01 .. boundary), with an h-session embargo at each boundary (targets
that overlap a later period are purged). 20% of instruments (hash-selected)
are held out entirely as unseen instruments.

Financial value (haircut equivalence, not the full RC1 lifecycle): each model
sets lending value LTV_i = 1 - k * sigma_hat_i; k is chosen on validation so
that P(R < -k sigma_hat) = 1% (equal ex-ante risk). On test we report the mean
supported LTV and the realised breach rate; and, for equal *realised* risk,
the mean LTV when k is set on test itself. Sharper calibrated forecasts support
more lending at the same breach rate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

H = 10
Q = 0.05
BREACH = 0.01
TRAIN_END = date(2023, 12, 31)
VAL_END = date(2024, 12, 31)
FEATURES = ["lrv1", "lrv5", "lrv22", "lrv66", "lewma", "ldown22", "lpark22", "gap22", "mom22", "dd66",
            "ladv20", "vtrend", "mkt_lrv22", "range_ok", "volume_ok", "sessions"]


# ------------------------------------------------------------------ data I/O

def _gcs_download(uri: str, dest: Path) -> Path:
    from app.analyst.llm import gcp_access_token
    bucket, _, prefix = uri[len("gs://"):].partition("/")
    dest.mkdir(parents=True, exist_ok=True)
    tok = gcp_access_token()
    for name in ("DATASET.json", "bars.csv.gz", "fx.csv.gz"):
        obj = urllib.parse.quote(f"{prefix.rstrip('/')}/{name}", safe="")
        req = urllib.request.Request(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{obj}?alt=media",
                                     headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=300) as r:  # nosec B310 - fixed GCS endpoint
            (dest / name).write_bytes(r.read())
    return dest


def _gcs_upload(uri: str, name: str, data: bytes) -> None:
    from app.analyst.llm import gcp_access_token
    bucket, _, prefix = uri[len("gs://"):].partition("/")
    obj = urllib.parse.quote(f"{prefix.rstrip('/')}/{name}", safe="")
    req = urllib.request.Request(
        f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?uploadType=media&name={obj}",
        data=data, method="POST",
        headers={"Authorization": f"Bearer {gcp_access_token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120):  # nosec B310 - fixed GCS endpoint
        pass


# ------------------------------------------------------------------- panel

@dataclass
class Panel:
    X: np.ndarray
    rv: np.ndarray          # forward realised h-session vol
    ret: np.ndarray         # forward h-session log return
    rc1: np.ndarray         # RC1 sigma_used * sqrt(h) (nan where not estimable)
    sym: np.ndarray
    day: np.ndarray         # ordinal dates of the evaluation date t
    end_day: np.ndarray     # ordinal date of the target's last observation (t + h sessions)
    market: np.ndarray
    adv_bucket: np.ndarray  # 0 low, 1 mid, 2 high liquidity (market-specific terciles at t)


def _roll_std(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(x.size, np.nan)
    if x.size >= w:
        c = np.cumsum(np.insert(x, 0, 0.0))
        c2 = np.cumsum(np.insert(x * x, 0, 0.0))
        n = w
        m = (c[n:] - c[:-n]) / n
        v = np.maximum((c2[n:] - c2[:-n]) / n - m * m, 0.0)
        out[w - 1:] = np.sqrt(v)
    return out


def _roll_mean(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(x.size, np.nan)
    if x.size >= w:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[w - 1:] = (c[w:] - c[:-w]) / w
    return out


def _rc1_sigma(rows: list[dict[str, Any]], i: int) -> float:
    """RC1's frozen instrument estimator at row i (point-in-time)."""
    from app.candidate.estimation import estimate_instrument
    from app.candidate.rc import rc_config
    cfg = rc_config()
    lo = max(0, i - cfg.history - 1)
    bars = [{"bar_date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["adj_close"],
             "volume": r["volume"], "range_reported": r["range_reported"], "volume_reported": r["volume_reported"]}
            for r in rows[lo:i + 1]]
    est = estimate_instrument("x", bars, rows[i]["date"], None, 0.0005, 0.005, cfg)
    return float(est.sigma_used) if est.sigma_used and math.isfinite(est.sigma_used) else math.nan


def build_panel(datasets: list[Any], h: int = H, step: int = 5, min_hist: int = 260,
                rc1: bool = True) -> Panel:
    feats, rv, ret, rcs, syms, days, ends, mkts, advs = [], [], [], [], [], [], [], [], []
    market_rv: dict[tuple[str, int], list[float]] = {}
    per: list[tuple[str, str, list[dict[str, Any]], dict[str, np.ndarray]]] = []
    for ds in datasets:
        m = ds.meta["market"]
        for s, rows in ds.bars.items():
            if len(rows) < min_hist + h + 5:
                continue
            px = np.array([r["adj_close"] for r in rows])
            if np.any(px <= 0):
                continue
            r = np.diff(np.log(px), prepend=np.nan)
            r[0] = 0.0
            o = np.array([r_["open"] for r_ in rows])
            hi = np.array([r_["high"] for r_ in rows])
            lo = np.array([r_["low"] for r_ in rows])
            cl = np.array([r_["close"] for r_ in rows])
            vol = np.array([r_["volume"] for r_ in rows])
            gap = np.abs(np.log(np.maximum(o, 1e-12) / np.maximum(np.roll(cl, 1), 1e-12)))
            gap[0] = 0.0
            park = np.where((hi > 0) & (lo > 0), np.log(np.maximum(hi, 1e-12) / np.maximum(lo, 1e-12)) ** 2 / (4 * math.log(2)), 0.0)
            ew = np.empty(r.size)
            var = float(np.var(r[1:61])) + 1e-10
            for t, x in enumerate(r):
                var = 0.94 * var + 0.06 * x * x
                ew[t] = math.sqrt(var)
            down = np.minimum(r, 0.0)
            stale = (np.abs(r) < 1e-12).astype(float)
            pre = {"r": r, "rv1": np.abs(r), "rv5": _roll_std(r, 5), "rv22": _roll_std(r, 22),
                   "rv66": _roll_std(r, 66), "ew": ew, "down22": np.sqrt(_roll_mean(down * down, 22)),
                   "park22": np.sqrt(_roll_mean(park, 22)), "gap22": _roll_mean(gap, 22),
                   "mom22": np.concatenate([np.full(22, np.nan), np.log(px[22:] / px[:-22])]),
                   "dd66": np.array([px[i] / px[max(0, i - 65):i + 1].max() - 1 for i in range(px.size)]),
                   "adv20": _roll_mean(vol * cl, 20), "adv5": _roll_mean(vol * cl, 5),
                   "zero22": _roll_mean(stale, 22),
                   "range_ok": np.array([float(x["range_reported"]) for x in rows]),
                   "volume_ok": np.array([float(x["volume_reported"]) for x in rows])}
            per.append((m, s, rows, pre))
            for i in range(min_hist, len(rows) - h, step):
                market_rv.setdefault((m, rows[i]["date"].toordinal()), []).append(pre["rv22"][i])
    mkt_med = {k: float(np.nanmedian(v)) for k, v in market_rv.items()}
    for m, s, rows, pre in per:
        r = pre["r"]
        for i in range(min_hist, len(rows) - h, step):
            fut = r[i + 1:i + 1 + h]
            d = rows[i]["date"].toordinal()
            x = [math.log(pre["rv1"][i] + 1e-5), math.log(pre["rv5"][i] + 1e-5), math.log(pre["rv22"][i] + 1e-5),
                 math.log(pre["rv66"][i] + 1e-5), math.log(pre["ew"][i] + 1e-5), math.log(pre["down22"][i] + 1e-5),
                 math.log(pre["park22"][i] + 1e-5), pre["gap22"][i], pre["mom22"][i], pre["dd66"][i],
                 math.log(pre["adv20"][i] + 1.0), math.log((pre["adv5"][i] + 1.0) / (pre["adv20"][i] + 1.0)),
                 math.log(mkt_med.get((m, d), np.nan) + 1e-5), pre["range_ok"][i - 21:i + 1].mean(),
                 pre["volume_ok"][i - 21:i + 1].mean(), math.log(i), pre["zero22"][i]]
            if not np.all(np.isfinite(x)):
                continue
            feats.append(x)
            rv.append(math.sqrt(float(np.sum(fut * fut))))
            ret.append(float(np.sum(fut)))
            rcs.append(_rc1_sigma(rows, i) * math.sqrt(h) if rc1 else math.nan)
            syms.append(f"{m}:{s}")
            days.append(d)
            ends.append(rows[i + h]["date"].toordinal())
            mkts.append(m)
            advs.append(pre["adv20"][i])
    adv = np.array(advs)
    mk = np.array(mkts)
    dy = np.array(days)
    bucket = np.ones(adv.size, int)          # mid when a cross-section is too small to rank
    groups: dict[tuple[str, int], list[int]] = {}
    for j, (m, d) in enumerate(zip(mkts, days, strict=True)):
        groups.setdefault((m, d), []).append(j)
    for idx in groups.values():              # contemporaneous terciles within (market, date)
        if len(idx) >= 3:
            a = adv[idx]
            q1, q2 = np.quantile(a, [1 / 3, 2 / 3])
            bucket[idx] = np.digitize(a, [q1, q2])
    return Panel(np.array(feats), np.array(rv), np.array(ret), np.array(rcs), np.array(syms), dy,
                 np.array(ends), mk, bucket)


# ------------------------------------------------------------------ models

def _splits(p: Panel, h: int = H) -> dict[str, np.ndarray]:
    """Purged by each target's actual final observation date: no training target
    reaches past the training end, no validation target past the validation end."""
    te, ve = TRAIN_END.toordinal(), VAL_END.toordinal()
    unseen = np.array([int(hashlib.sha256(s.encode()).hexdigest(), 16) % 5 == 0 for s in p.sym])
    return {"train": (p.end_day <= te) & ~unseen,
            "val": (p.day > te) & (p.end_day <= ve) & ~unseen,
            "test": (p.day > ve) & ~unseen,
            "test_unseen": (p.day > ve) & unseen,
            "unseen": unseen}


def fit_predict(p: Panel, sp: dict[str, np.ndarray], seed: int = 7) -> dict[str, dict[str, np.ndarray]]:
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import LinearRegression, Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    tr = sp["train"]
    fl = vol_floor(p, tr)
    y = np.log(np.maximum(p.rv, fl))
    out: dict[str, dict[str, np.ndarray]] = {"_floor": {"floor": fl}}
    har = LinearRegression().fit(p.X[tr][:, :4], y[tr])
    out["har"] = {"sigma": np.maximum(np.exp(har.predict(p.X[:, :4])), fl)}
    ridge = make_pipeline(StandardScaler(), Ridge(alpha=1.0)).fit(p.X[tr], y[tr])
    out["ridge"] = {"sigma": np.maximum(np.exp(ridge.predict(p.X)), fl)}
    gbt = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, l2_regularization=1.0,
                                        random_state=seed).fit(p.X[tr], y[tr])
    g = gbt.predict(p.X)
    out["gbt"] = {"sigma": np.maximum(np.exp(g), fl)}
    # Hierarchical: instrument intercepts from training residuals, shrunk by n/(n+k);
    # k chosen on validation; unseen instruments get the pooled prediction.
    res = y - g
    best = (math.inf, 0.0)
    for k in (5.0, 20.0, 50.0, 100.0, 250.0, 1000.0):
        adj = _shrunk_intercepts(p.sym, res, tr, k)
        a = np.array([adj.get(s, 0.0) for s in p.sym])
        mse = float(np.mean((y[sp["val"]] - g[sp["val"]] - a[sp["val"]]) ** 2))
        best = min(best, (mse, k))
    adj = _shrunk_intercepts(p.sym, res, tr, best[1])
    out["hier"] = {"sigma": np.maximum(np.exp(g + np.array([adj.get(s, 0.0) for s in p.sym])), fl),
                   "k": np.array([best[1]])}
    out["rc1"] = {"sigma": p.rc1}
    gq = HistGradientBoostingRegressor(loss="quantile", quantile=Q, max_iter=300, learning_rate=0.05,
                                       max_leaf_nodes=31, random_state=seed).fit(p.X[tr], p.ret[tr])
    out["gbt_q"] = {"q": gq.predict(p.X)}
    for d in out.values():
        if "sigma" in d:
            z = np.full(p.rv.size, np.nan)
            for m in np.unique(p.market):           # calibrated per market on training rows
                ok = tr & (p.market == m) & np.isfinite(d["sigma"]) & (d["sigma"] > 0)
                if ok.any():
                    z[p.market == m] = float(np.quantile(p.ret[ok] / d["sigma"][ok], Q))
            d["q"] = d["sigma"] * z
            d["z"] = np.array([float(np.nanmean(np.unique(z[np.isfinite(z)])))])
    return out


def vol_floor(p: Panel, tr: np.ndarray, pct: float = 0.05) -> np.ndarray:
    """Per-market minimum meaningful h-session volatility: the 5th percentile of
    positive training realised volatility. Stale-price windows (rv == 0, common on
    NGX) otherwise produce a degenerate log target and an undefined QLIKE."""
    fl = np.full(p.rv.size, 1e-4)
    for m in np.unique(p.market):
        pos = tr & (p.market == m) & (p.rv > 0)
        if pos.any():
            fl[p.market == m] = max(1e-4, float(np.quantile(p.rv[pos], pct)))
    return fl


def _shrunk_intercepts(sym: np.ndarray, res: np.ndarray, mask: np.ndarray, k: float) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for s, e in zip(sym[mask], res[mask], strict=True):
        acc.setdefault(s, []).append(float(e))
    return {s: (len(v) / (len(v) + k)) * float(np.mean(v)) for s, v in acc.items()}


# --------------------------------------------------------------- evaluation

def _pinball(y: np.ndarray, q: np.ndarray, a: float = Q) -> float:
    d = y - q
    return float(np.mean(np.maximum(a * d, (a - 1) * d)))


def _kupiec(hits: int, n: int, p: float = Q) -> float:
    from scipy.stats import chi2
    if n == 0:
        return math.nan
    x = hits / n
    if x in (0.0, 1.0):
        ll = n * math.log(1 - p) if x == 0 else n * math.log(p)
        return float(1 - chi2.cdf(-2 * ll, 1))
    lr = -2 * ((n - hits) * math.log(1 - p) + hits * math.log(p) - (n - hits) * math.log(1 - x) - hits * math.log(x))
    return float(1 - chi2.cdf(lr, 1))


def evaluate(p: Panel, sp: dict[str, np.ndarray], preds: dict[str, dict[str, np.ndarray]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for seg in ("test", "test_unseen"):
        base = sp[seg] & np.isfinite(p.rc1)          # common sample: every model has a forecast
        res: dict[str, Any] = {"n": int(base.sum())}
        fl = preds["_floor"]["floor"]
        res["zero_realised_share"] = float(np.mean(p.rv[base] <= 0))
        for name, d in preds.items():
            if name.startswith("_"):
                continue
            r: dict[str, Any] = {}
            q = d["q"][base]
            y = p.ret[base]
            r["pinball_q05"] = _pinball(y, q)
            hits = int(np.sum(y < q))
            r["exceedance_rate"] = hits / max(1, y.size)
            r["kupiec_p"] = _kupiec(hits, y.size)
            if "sigma" in d:
                s = d["sigma"][base]
                v = np.maximum(p.rv[base], fl[base]) ** 2
                r["qlike"] = float(np.mean(v / s ** 2 - np.log(v / s ** 2 + 1e-12) - 1))
                r["log_rv_rmse"] = float(np.sqrt(np.mean((np.log(p.rv[base] + 1e-6) - np.log(s + 1e-6)) ** 2)))
                # haircut equivalence at 1% breach: k from validation (ex ante), and k from test (ex post)
                vmask = sp["val"] & np.isfinite(d["sigma"]) & np.isfinite(p.rc1)
                kv = -float(np.quantile(p.ret[vmask] / d["sigma"][vmask], BREACH)) if vmask.any() else math.nan
                kt = -float(np.quantile(y / s, BREACH))
                ltv_v = np.clip(1 - kv * s, 0, 1)
                r["haircut_k_val"] = kv
                r["mean_ltv_ex_ante"] = float(ltv_v.mean())
                r["breach_rate_ex_ante"] = float(np.mean(y < -kv * s))
                r["mean_ltv_equal_realised_risk"] = float(np.clip(1 - kt * s, 0, 1).mean())
                r["by_liquidity"] = {lbl: {"n": int((p.adv_bucket[base] == b).sum()),
                                           "mean_ltv_ex_ante": float(ltv_v[p.adv_bucket[base] == b].mean())
                                           if (p.adv_bucket[base] == b).any() else None,
                                           "breach_rate_ex_ante": float(np.mean((y < -kv * s)[p.adv_bucket[base] == b]))
                                           if (p.adv_bucket[base] == b).any() else None}
                                     for b, lbl in ((0, "low"), (1, "mid"), (2, "high"))}
                r["by_market"] = {m: {"n": int((p.market[base] == m).sum()),
                                      "qlike": float(np.mean((v / s ** 2 - np.log(v / s ** 2 + 1e-12) - 1)[p.market[base] == m])),
                                      "exceedance_rate": float(np.mean((y < q)[p.market[base] == m])),
                                      "mean_ltv_ex_ante": float(ltv_v[p.market[base] == m].mean()),
                                      "breach_rate_ex_ante": float(np.mean((y < -kv * s)[p.market[base] == m]))}
                                  for m in sorted(set(p.market[base]))}
            res[name] = r
        out[seg] = res
    return out


def paired_bootstrap(p: Panel, sp: dict[str, np.ndarray], preds: dict[str, dict[str, np.ndarray]],
                     a: str, b: str = "rc1", n: int = 500, seed: int = 11) -> dict[str, Any]:
    """QLIKE(a) - QLIKE(b) on test with a block bootstrap over calendar months (dependence-aware)."""
    base = sp["test"] & np.isfinite(p.rc1)
    v = np.maximum(p.rv[base], preds["_floor"]["floor"][base]) ** 2

    def ql(s: np.ndarray) -> np.ndarray:
        return v / s ** 2 - np.log(v / s ** 2 + 1e-12) - 1
    diff = ql(preds[a]["sigma"][base]) - ql(preds[b]["sigma"][base])
    month = (p.day[base] // 30)
    blocks = {m: np.where(month == m)[0] for m in np.unique(month)}
    keys = list(blocks)
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n):
        idx = np.concatenate([blocks[keys[j]] for j in rng.integers(0, len(keys), len(keys))])
        stats.append(float(diff[idx].mean()))
    lo, hi = np.quantile(stats, [0.025, 0.975])
    return {"mean": float(diff.mean()), "ci95": [float(lo), float(hi)], "blocks": len(keys)}


def main() -> None:
    from app.learning.datasets import load_research_dataset
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp())
    dss = []
    for i, d in enumerate(x for arg in a.dataset for x in arg.split("+")):
        path = _gcs_download(d, tmp / f"ds{i}") if d.startswith("gs://") else Path(d)
        dss.append(load_research_dataset(path))
    panel = build_panel(dss, step=a.step)
    sp = _splits(panel)
    preds = fit_predict(panel, sp, seed=a.seed)
    ev = evaluate(panel, sp, preds)
    boot = {m: paired_bootstrap(panel, sp, preds, m) for m in ("har", "ridge", "gbt", "hier")}
    report = {"experiment": "intelligence.forward_vol_downside.v2", "horizon_sessions": H, "quantile": Q,
              "breach_target": BREACH, "step_sessions": a.step, "seed": a.seed,
              "datasets": [ds.meta["dataset_id"] for ds in dss],
              "data_through": max(ds.meta["last_date"] for ds in dss), "evidence_class": "public_historical",
              "panel": {"rows": int(panel.X.shape[0]), "instruments": len(set(panel.sym)),
                        **{k: int(v.sum()) for k, v in sp.items()}},
              "hier_shrinkage_k": float(preds["hier"]["k"][0]),
              "z_q05": {k: float(v["z"][0]) for k, v in preds.items() if "z" in v},
              "vol_floor_by_market": {m: float(preds["_floor"]["floor"][panel.market == m][0])
                                      for m in sorted(set(panel.market))},
              "results": ev, "qlike_minus_rc1_block_bootstrap": boot, "seconds": round(time.time() - t0, 1)}
    blob = json.dumps(report, indent=1, default=float).encode()
    if a.out.startswith("gs://"):
        _gcs_upload(a.out, "benchmark.json", blob)
    else:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / "benchmark.json").write_bytes(blob)
    print("CRI_INTELLIGENCE_BENCHMARK " + json.dumps(report, default=float), flush=True)


if __name__ == "__main__":
    main()
