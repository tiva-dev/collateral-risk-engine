"""Preliminary comparison of capacity methods on provider-backed history.

RESEARCH ONLY. Not validation evidence (ADR-002/ADR-006). Uses the dataset
built by the official provider workflow (snapshot-verified), with the same
lifecycle mechanics and methods as the synthetic study:

* rolling originations every STEP sessions, HISTORY sessions of history known
  at the decision date, TERM sessions of forward path;
* single-asset loans per instrument, plus an equal-weight portfolio per market;
* NGX portfolio also as a USD loan (NGN collateral) under each FX source;
* methods: v0.6.2 engine, v0.7.0 engine, flat benchmark, A, B, B_gap;
  maintenance on the common B-based standard (trigger=common).

Data handling, all reported in the output:
* NGX prices are not corporate-action adjusted. Days with a one-day move
  beyond SUSPECT_MOVE are flagged as suspected corporate actions or data
  errors, and every cohort whose history or term touches one is excluded.
  This is a data screen, not a price-limit model.
* Sessions where an instrument has no bar, or a bar with zero volume/trades,
  are treated as non-tradable (halted) and carry the last price.
* Spreads come from the Corwin-Schultz high-low estimator using only bars
  whose high and low were reported (field provenance), with floors.
* ADV is the trailing 21-session median of *reported* value traded (NGX) or
  volume x close (US), needing MIN_ADV_OBS reported sessions. NGNMarket
  leaves volume null for most of its history, so NGX cohorts start only once
  liquidity is observable at the decision date; within a cohort's window,
  missing ADV is carried forward as-of (share reported per book). Alpaca's
  IEX feed reports IEX-venue volume only, so US ADV is a lower bound.

Usage:
    python -m research.methodology_lab.real_data MANIFEST --v062 DIR --v070 DIR --out DIR
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import numpy as np

from . import lifecycle, methods
from .run_experiments import CAP, FLAT, run_engine

HISTORY = 260
TERM = 126
TERM_CALENDAR = 183
STEP = 21
ALPHA = 0.99
SUSPECT_MOVE = 0.25
MAX_EXEC_DAYS = 250
MIN_ADV_OBS = 10
ENGINE_V = 1_000_000.0
PRICE = 100.0

NGX = [
    "MTNN", "AIRTELAFRI", "DANGCEM", "BUACEMENT", "BUAFOODS", "GTCO", "ZENITHBANK",
    "ACCESSCORP", "UBA", "FIRSTHOLDCO", "SEPLAT", "OANDO", "NESTLE", "DANGSUGAR",
]
NGX_EXCLUDED = {"NB": "fails coverage (ratio 0.80, 408 gaps, 93% of bars close-only)"}
US = ["SPY", "QQQ", "IWM", "TLT", "HYG", "AAPL", "MSFT", "NVDA", "TSLA", "JPM", "XOM"]
US_ETF = {"SPY", "QQQ", "IWM", "TLT", "HYG"}
POSITION = {"NGX": {"retail": 50e6, "institutional": 2e9}, "US": {"retail": 250e3, "institutional": 25e6}}
RATE = {"NGN": 0.25, "USD": 0.10}
SPREAD_FLOOR = {"NGX": 0.005, "US": 0.0002}
SPREAD_DEFAULT = {"NGX": 0.02, "US": 0.0005}


def _day(ts) -> date:
    return ts.date() if isinstance(ts, datetime) else ts


def corwin_schultz(high: np.ndarray, low: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """Daily Corwin-Schultz spread estimate; NaN where either day lacks a range."""
    n = high.size
    out = np.full(n, np.nan)
    k = 3 - 2 * math.sqrt(2)
    for t in range(1, n):
        if not (ok[t] and ok[t - 1]) or min(low[t], low[t - 1]) <= 0:
            continue
        beta = math.log(high[t] / low[t]) ** 2 + math.log(high[t - 1] / low[t - 1]) ** 2
        hh, ll = max(high[t], high[t - 1]), min(low[t], low[t - 1])
        gamma = math.log(hh / ll) ** 2
        a = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
        out[t] = max(0.0, 2 * (math.exp(a) - 1) / (1 + math.exp(a)))
    return out


def build_panel(symbols: list[str], bars_by_symbol: dict, market: str) -> dict:
    present = [s for s in symbols if bars_by_symbol.get(s)]
    by_sym = {s: {_day(b.timestamp): b for b in bars_by_symbol[s]} for s in present}
    counts: dict[date, int] = defaultdict(int)
    for s in present:
        for d in by_sym[s]:
            counts[d] += 1
    dates = sorted(d for d, c in counts.items() if c >= 0.5 * len(present))
    n, m = len(dates), len(present)
    close = np.full((n, m), np.nan)
    high = np.full((n, m), np.nan)
    low = np.full((n, m), np.nan)
    rng_ok = np.zeros((n, m), bool)
    value = np.full((n, m), np.nan)
    halted = np.zeros((n, m), bool)
    for j, s in enumerate(present):
        last = np.nan
        for i, d in enumerate(dates):
            b = by_sym[s].get(d)
            if b is None:
                close[i, j] = last
                halted[i, j] = True
                continue
            last = b.close
            close[i, j] = b.close
            high[i, j], low[i, j] = b.high, b.low
            rng_ok[i, j] = b.has_reported_range()
            prov = b.field_provenance
            if prov.get("value_traded") == "reported" and b.value_traded:
                value[i, j] = float(b.value_traded)
            elif prov.get("volume", "reported") == "reported" and b.volume > 0:
                value[i, j] = b.volume * b.close
            else:
                value[i, j] = np.nan  # liquidity not reported for this bar
            if b.trade_status == "no_trade":
                halted[i, j] = True
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.diff(np.log(close), axis=0, prepend=np.nan)
    listed = ~np.isnan(r)
    r = np.where(listed, r, 0.0)
    suspect = listed & (np.abs(np.expm1(r)) > SUSPECT_MOVE)
    spread = np.zeros((n, m))
    adv = np.zeros((n, m))
    for j in range(m):
        cs = corwin_schultz(high[:, j], low[:, j], rng_ok[:, j])
        for i in range(n):
            w = cs[max(0, i - 20) : i + 1]
            w = w[~np.isnan(w)]
            spread[i, j] = (
                max(SPREAD_FLOOR[market], float(np.median(w))) if w.size >= 5
                else SPREAD_DEFAULT[market]
            )
            v = value[max(0, i - 20) : i + 1, j]
            v = v[~np.isnan(v)]
            # ADV needs at least MIN_ADV_OBS reported sessions in the window.
            adv[i, j] = float(np.median(v)) if v.size >= MIN_ADV_OBS else np.nan
    sig = np.zeros((n, m))
    var = np.nanvar(r[1:61], axis=0) + 1e-8
    for i in range(n):
        sig[i] = np.sqrt(var)
        var = methods.LAMBDA * var + (1 - methods.LAMBDA) * r[i] ** 2
    return {
        "symbols": present, "dates": dates, "r": r, "listed": listed, "suspect": suspect,
        "halted": halted, "spread": spread, "adv": adv, "sigma": sig,
        "range_reported_share": float(rng_ok[listed].mean()) if listed.any() else 0.0,
        "liquidity_reported_share": float((~np.isnan(value))[listed].mean()) if listed.any() else 0.0,
        "first_adv": {
            s: (dates[int(np.argmax(~np.isnan(adv[:, j])))].isoformat()
                if (~np.isnan(adv[:, j])).any() else None)
            for j, s in enumerate(present)
        },
    }


def fx_series(fx_rates: dict, pair: tuple[str, str], dates: list[date]) -> tuple[np.ndarray, int]:
    """As-of daily log change of the pair on the panel dates and the max staleness."""
    rows = sorted((_day(x.timestamp), x.rate) for x in fx_rates.get(pair, []))
    if not rows:
        return np.full(len(dates), np.nan), -1
    ds = [d for d, _ in rows]
    vals = [v for _, v in rows]
    level = np.full(len(dates), np.nan)
    stale = 0
    j = -1
    for i, d in enumerate(dates):
        while j + 1 < len(ds) and ds[j + 1] <= d:
            j += 1
        if j >= 0:
            level[i] = vals[j]
            stale = max(stale, (d - ds[j]).days)
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = np.diff(np.log(level), prepend=np.nan)
    return ret, stale


def engine_request(rid: str, f: methods.Features, types: list[str], rate: float) -> dict:
    assets = []
    for a, t in zip(f.engine_inputs, types, strict=True):
        assets.append(
            {
                "id": a["id"], "type": t, "price": PRICE,
                "quantity": a["weight"] * ENGINE_V / PRICE, "spread": a["spread"],
                "adv_value": a["adv_value_ratio"] * ENGINE_V,
                "vol30": a["vol30"], "vol90": a["vol90"], "vol252": a["vol252"],
                "mdd252": a["mdd252"], "gap252": a["gap252"],
            }
        )
    return {"id": rid, "assets": assets, "term_days": TERM_CALENDAR, "rate": rate}


def cohorts(
    panel: dict, cols: list[int], fx_ret: np.ndarray | None, require_adv: bool = True
) -> list[int]:
    n = len(panel["dates"])
    out = []
    for t0 in range(HISTORY, n - TERM, STEP):
        window = slice(t0 - HISTORY, t0 + TERM)
        if not panel["listed"][t0 - HISTORY + 1 : t0 + TERM, cols].all():
            continue
        if panel["suspect"][window][:, cols].any():
            continue
        if fx_ret is not None and np.isnan(fx_ret[t0 - HISTORY + 1 : t0 + TERM]).any():
            continue
        if require_adv and np.isnan(panel["adv"][t0 - 1, cols]).any():
            continue  # liquidity unobservable at the decision date
        out.append(t0)
    return out


def run_book(
    label: str, market: str, panel: dict, cols: list[int], size: str, loan_ccy: str,
    fx_ret: np.ndarray | None, v062: str, v070: str, seed: int,
    adv_mode: str = "strict",
) -> tuple[list[dict], dict]:
    """adv_mode: "strict" uses only ADV observable at the decision date;
    "backfill" sets ADV before its first observation to that first observed
    value. Backfill uses future information and is labelled as such."""
    syms = [panel["symbols"][c] for c in cols]
    n = len(cols)
    weights = np.full(n, 1.0 / n)
    position = POSITION[market][size]
    types = ["etf" if s in US_ETF else "listed_equity" for s in syms]
    asset_type = "etf" if all(t == "etf" for t in types) else "listed_equity"
    cap = CAP[asset_type]
    flat = FLAT[market]
    pol = lifecycle.Policy(rate=RATE[loan_ccy])
    r_local = panel["r"][:, cols]
    r_loan = r_local - (fx_ret[:, None] if fx_ret is not None else 0.0)
    adv_raw = panel["adv"][:, cols]
    adv_filled = adv_raw.copy()
    if adv_mode == "backfill":
        for j in range(adv_filled.shape[1]):
            ok = ~np.isnan(adv_filled[:, j])
            if ok.any():
                first = int(np.argmax(ok))
                adv_filled[:first, j] = adv_filled[first, j]
    for j in range(adv_filled.shape[1]):
        last = np.nan
        for i in range(adv_filled.shape[0]):
            if np.isnan(adv_filled[i, j]):
                adv_filled[i, j] = last
            else:
                last = adv_filled[i, j]
    adv_units = np.nan_to_num(adv_filled, nan=0.0) / position
    starts = cohorts(panel, cols, fx_ret, require_adv=adv_mode == "strict")
    info = {"book": label, "cohorts": len(starts), "symbols": syms, "adv_mode": adv_mode}
    if starts:
        windows = np.concatenate(
            [adv_raw[t0 - HISTORY + 1 : t0 + TERM] for t0 in starts]
        )
        info["adv_as_of_filled_share"] = round(float(np.isnan(windows).mean()), 4)
    if not starts:
        return [], info
    feats, reqs = [], []
    for k, t0 in enumerate(starts):
        hist = slice(t0 - HISTORY + 1, t0)
        f = methods.estimate_features(
            r_loan[hist], panel["spread"][hist][:, cols], adv_units[hist],
            panel["halted"][hist][:, cols],
            fx_ret[hist] if fx_ret is not None else None, weights, asset_type,
        )
        feats.append(f)
        reqs.append(engine_request(f"{label}-{k}", f, types, RATE[loan_ccy]))
    eng = {"v062": run_engine(v062, reqs), "v070": run_engine(v070, reqs)}
    rng = np.random.default_rng(seed)
    rows = []
    for k, (t0, f) in enumerate(zip(starts, feats, strict=True)):
        fwd = slice(t0, t0 + TERM)
        r_true = r_loan[fwd][None]
        halted = panel["halted"][fwd][:, cols][None]
        spread = panel["spread"][fwd][:, cols][None]
        adv = np.maximum(adv_units[fwd], 1e-9)[None]
        sig_d = panel["sigma"][fwd][:, cols][None]
        fx = fx_ret[fwd][None] if fx_ret is not None else None
        r_obs_fwd = r_true
        ratios: dict[str, float] = {}
        for lbl in ("v062", "v070"):
            res = eng[lbl][k]
            ratios[lbl] = res["principal"] / res["mv"] if res["mv"] else 0.0
        ratios["flat"] = flat
        exec_days = float(methods.execution_days(f, pol).max())
        if exec_days > MAX_EXEC_DAYS:
            # Position cannot be sold within MAX_EXEC_DAYS at the participation
            # cap. The risk-based methods lend nothing and there is no common
            # maintenance standard to monitor the others against, so the
            # cohort is recorded as unlendable and excluded from all methods.
            info["unlendable_cohorts"] = info.get("unlendable_cohorts", 0) + 1
            continue
        else:
            ratios["A"], _ = methods.approach_a(f, pol, ALPHA, cap)
            ell_b, h_b = methods.approach_b(f, pol, ALPHA, cap, rng, n_paths=2000)
            ratios["B"] = ell_b
            ratios["B_gap"], _ = methods.approach_b(
                f, pol, 0.995, cap, rng, n_paths=2000, stress_share=0.25, with_gap=True
            )
        growth = 1 + pol.rate * methods.recovery_days(f, pol) / 252.0
        maint = methods.dynamic_ratio((1 - h_b) / growth, h_b, f, r_obs_fwd, 10.0, pol)
        for method, ell0 in ratios.items():
            if ell0 <= 0:
                rows.append(
                    {
                        "book": label, "market": market, "size": size, "loan_ccy": loan_ccy,
                        "t0": panel["dates"][t0].isoformat(), "method": method, "ltv0": 0.0,
                        "loss": 0.0, "loss_rate": 0.0, "liquidated": False,
                        "unnecessary": False, "cf_loss": 0.0, "exec_days": exec_days,
                        "symbol": syms[0] if n == 1 else "portfolio",
                    }
                )
                continue
            out = lifecycle.simulate_loans(
                ell0, f.weights, r_true, halted, spread, adv, sig_d, maint, fx, pol
            )
            cf = lifecycle.simulate_loans(
                ell0, f.weights, r_true, halted, spread, adv, sig_d, maint, fx,
                lifecycle.Policy(rate=pol.rate, sizing="none"),
            )
            first_sale = int(out["first_sale"][0])
            cushion_cf = cf["cushion_path"][0]
            rows.append(
                {
                    "book": label, "market": market, "size": size, "loan_ccy": loan_ccy,
                    "t0": panel["dates"][t0].isoformat(), "method": method,
                    "ltv0": float(ell0), "loss": float(out["loss"][0]),
                    "loss_rate": float(out["loss"][0] / ell0) if ell0 > 0 else 0.0,
                    "liquidated": first_sale >= 0,
                    "unnecessary": bool(first_sale >= 0 and cushion_cf[first_sale:].min() >= 0),
                    "cf_loss": float(cf["loss"][0]),
                    "exec_days": exec_days, "symbol": "+".join(syms) if n == 1 else "portfolio",
                }
            )
    return rows, info


def summarise(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["market"], r["book_kind"], r["size"], r["loan_ccy"], r["method"])].append(r)
    out = []
    for (market, kind, size, ccy, method), g in sorted(groups.items()):
        ltv = np.array([x["ltv0"] for x in g])
        loss = np.array([x["loss"] for x in g])
        principal = ltv.sum()
        liq = sum(x["liquidated"] for x in g)
        out.append(
            {
                "market": market, "book": kind, "size": size, "loan_ccy": ccy, "method": method,
                "loans": len(g), "median_ltv": round(float(np.median(ltv)), 4),
                "mean_ltv": round(float(ltv.mean()), 4),
                "loss_events": int((loss > 1e-9).sum()),
                "loss_per_principal_pct": round(100 * float(loss.sum() / max(principal, 1e-12)), 4),
                "worst_loss_rate_pct": round(100 * max(x["loss_rate"] for x in g), 3),
                "liquidations": int(liq),
                "unnecessary_share": round(sum(x["unnecessary"] for x in g) / liq, 3) if liq else None,
            }
        )
    return out


def main() -> None:
    from app.simulations.run_official_validation import _load_replay_inputs

    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--v062", required=True)
    ap.add_argument("--v070", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20261125)
    a = ap.parse_args()
    manifest = json.loads(Path(a.manifest).read_text())
    bars, fx_rates = _load_replay_inputs(manifest)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    meta: dict = {"excluded": NGX_EXCLUDED, "books": [], "data": {}}
    # Alternate FX caches are kept out of replay; load them separately here.
    alt_manifest = dict(manifest, cache_paths=manifest.get("alternate_fx_cache_paths", []))
    _, alt_fx = _load_replay_inputs(alt_manifest)
    for market, syms in (("NGX", NGX), ("US", US)):
        panel = build_panel(syms, bars, market)
        meta["data"][market] = {
            "sessions": len(panel["dates"]),
            "first": panel["dates"][0].isoformat(), "last": panel["dates"][-1].isoformat(),
            "suspect_days": {
                s: int(panel["suspect"][:, j].sum()) for j, s in enumerate(panel["symbols"])
            },
            "halted_share": round(float(panel["halted"][panel["listed"]].mean()), 4),
            "range_reported_share": round(panel["range_reported_share"], 4),
            "liquidity_reported_share": round(panel["liquidity_reported_share"], 4),
            "first_adv": panel["first_adv"],
            "median_spread": round(float(np.median(panel["spread"][panel["listed"]])), 5),
        }
        books = [([j], f"{market}:{s}", "single") for j, s in enumerate(panel["symbols"])]
        books.append((list(range(len(panel["symbols"]))), f"{market}:portfolio", "portfolio"))
        modes = ("strict", "backfill") if market == "NGX" else ("strict",)
        for mode in modes:
            suffix = "" if mode == "strict" else "_advbackfill"
            for cols, label, kind in books:
                for size in ("retail", "institutional"):
                    ccy = "NGN" if market == "NGX" else "USD"
                    rws, info = run_book(label, market, panel, cols, size, ccy, None,
                                         a.v062, a.v070, a.seed, adv_mode=mode)
                    for r in rws:
                        r["book_kind"] = kind + suffix
                    rows += rws
                    meta["books"].append({**info, "size": size, "loan_ccy": ccy})
        if market == "NGX":
            cols = list(range(len(panel["symbols"])))
            selected = (manifest.get("fx_source_selection") or {}).get("USD/NGN", {})
            chosen = selected.get("selected_provider")
            others = [c for c in selected.get("candidates", []) if c != chosen]
            sources = [(chosen, fx_rates)] + [(o, alt_fx) for o in others]
            for source, table in sources:
                fx_ret, stale = fx_series(table, ("USD", "NGN"), panel["dates"])
                if not np.isfinite(fx_ret).any():
                    # The selected source may be the alternate table.
                    continue
                meta["data"][f"USD/NGN@{source}"] = {"max_staleness_days": stale}
                for size in ("retail", "institutional"):
                    rws, info = run_book(f"NGX:portfolio:USD@{source}", market, panel, cols,
                                         size, "USD", fx_ret, a.v062, a.v070, a.seed,
                                         adv_mode="backfill")
                    for r in rws:
                        r["book_kind"] = f"portfolio_usd_{source}_advbackfill"
                    rows += rws
                    meta["books"].append({**info, "size": size, "loan_ccy": f"USD@{source}"})
    with (out / "real_data_rows.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    summary = summarise(rows)
    with (out / "real_data_summary.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)
    (out / "real_data_meta.json").write_text(json.dumps(meta, indent=2, default=str))
    print(json.dumps(meta["data"], indent=2))
    print(json.dumps([b for b in meta["books"] if "portfolio" in b["book"]], indent=2))
    cols = ["market", "book", "size", "loan_ccy", "method", "loans", "median_ltv",
            "loss_events", "loss_per_principal_pct", "worst_loss_rate_pct", "liquidations",
            "unnecessary_share"]
    print("\t".join(cols))
    for s in summary:
        print("\t".join(str(s[c]) for c in cols))
    # Where losses come from: by origination year and by instrument.
    print("LOSS BREAKDOWN (loss events / loss per principal %)")
    for kind in ("single", "single_advbackfill"):
        for market in ("NGX", "US"):
            for size in ("retail", "institutional"):
                sel = [r for r in rows if r["book_kind"] == kind and r["market"] == market
                       and r["size"] == size]
                if not sel:
                    continue
                for method in ("v070", "flat", "A", "B", "B_gap"):
                    rs = [r for r in sel if r["method"] == method]
                    by_year: dict[str, list] = defaultdict(lambda: [0, 0.0, 0.0])
                    by_sym: dict[str, list] = defaultdict(lambda: [0, 0.0])
                    for r in rs:
                        y = by_year[r["t0"][:4]]
                        y[0] += r["loss"] > 1e-9
                        y[1] += r["loss"]
                        y[2] += r["ltv0"]
                        if r["loss"] > 1e-9:
                            by_sym[r["symbol"]][0] += 1
                            by_sym[r["symbol"]][1] += r["loss_rate"]
                    years = " ".join(
                        f"{k}:{v[0]}/{100 * v[1] / max(v[2], 1e-12):.2f}"
                        for k, v in sorted(by_year.items()) if v[0]
                    )
                    syms_txt = " ".join(
                        f"{k}:{v[0]}(max {100 * v[1] / v[0]:.1f}% avg)"
                        for k, v in sorted(by_sym.items(), key=lambda kv: -kv[1][0])[:5]
                    )
                    print(f"  {market} {kind} {size} {method}: years[{years}] names[{syms_txt}]")
    unl = [b for b in meta["books"] if b.get("unlendable_cohorts")]
    print("UNLENDABLE COHORTS (excluded from all methods):",
          json.dumps([{k: b[k] for k in ("book", "size", "adv_mode", "unlendable_cohorts")}
                      for b in unl]))


if __name__ == "__main__":
    main()
