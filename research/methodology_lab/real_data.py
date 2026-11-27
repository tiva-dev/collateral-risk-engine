"""Real-data comparison of capacity methodologies (research, not evidence).

Inputs: the dataset of an Official Provider Validation Run, verified against
its checksummed snapshot by the workflow. No provider calls.

Design (review corrections of 25 Nov 2026):

* **No future-dependent selection.** A cohort exists if every asset has a
  full history window before the decision date and, in observed-liquidity
  mode, ADV observable at the decision date. Nothing after t0 decides
  inclusion; forward gaps become non-tradable days, and paths that end before
  the workout finishes are flagged ``censored``.
* **Corporate actions.** Only verified actions adjust prices (none verified
  yet). Extreme moves are kept as market moves; the builder's screen
  classifies them and its counts are reported.
* **Liquidity.** US: observed ADV (IEX venue volume, a lower bound). NGX:
  observed ADV exists only from August 2025, so NGX 2018-2025 is run under
  stated liquidity *scenarios* (the position takes k sessions to sell at 10%
  participation), fixed in advance, not taken from later data.
* **Per-method decisions.** Each method originates and monitors with its own
  standard. Rejections (capacity 0) and unlendable positions are reported
  per method; no method's decision excludes a cohort from another.
* **Two comparisons.** Policy-originated (each method lends its own LTV) and
  matched exposure (every method monitors the same loan at LTVs on a grid).
* **Terminal workout.** At maturity the borrower repays if covered; otherwise
  the lender sells through real execution after maturity (lifecycle.
  terminal_workout) instead of recovering estimated value instantly.
* **Frontiers.** A, B, B_gap and the NGX variants across confidence levels
  without a client ceiling; C across risk appetites; the flat benchmark
  across LTVs; v0.6.2 and v0.7.0 as points.
* **NGX variants.** *_vr: volatility scaled by sqrt(variance ratio) for
  stale, step-wise prices (multi-session aggregation); *_liq: stressed
  liquidity in the recovery window.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from . import lifecycle, methods
from .run_experiments import run_engine

HISTORY = 260
TERM = 126
TAIL = 63
TERM_CALENDAR = 183
STEP = 21
ALPHA = 0.99
CEILING = {"etf": 0.70, "listed_equity": 0.65}
FLAT = {"US": 0.50, "NGX": 0.30}
FRONTIER_ALPHAS = (0.95, 0.975, 0.99, 0.995, 0.999)
FLAT_GRID = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7)
MATCHED_LTV = (0.2, 0.35, 0.5, 0.65)
ENGINE_CHECK_EVERY = 10
MAX_EXEC_DAYS = 250
MIN_ADV_OBS = 10
ENGINE_V = 1_000_000.0
PRICE = 100.0
NGX = ["MTNN", "AIRTELAFRI", "DANGCEM", "BUACEMENT", "BUAFOODS", "GTCO", "ZENITHBANK",
       "ACCESSCORP", "UBA", "FIRSTHOLDCO", "SEPLAT", "OANDO", "NESTLE", "DANGSUGAR"]
US = ["SPY", "QQQ", "IWM", "TLT", "HYG", "AAPL", "MSFT", "NVDA", "TSLA", "JPM", "XOM"]
US_ETF = {"SPY", "QQQ", "IWM", "TLT", "HYG"}
POSITION = {"NGX": {"retail": 50e6, "institutional": 2e9},
            "US": {"retail": 250e3, "institutional": 25e6}}
LIQ_SCENARIOS = {"liq5d": 5, "liq20d": 20}  # sessions to sell the position at 10%
RATE = {"NGN": 0.25, "USD": 0.10}
SPREAD_FLOOR = {"NGX": 0.005, "US": 0.0002}
SPREAD_DEFAULT = {"NGX": 0.02, "US": 0.0005}


def _day(ts) -> date:
    return ts.date() if isinstance(ts, datetime) else ts


def corwin_schultz(high, low, ok):
    n = high.size
    out = np.full(n, np.nan)
    k = 3 - 2 * math.sqrt(2)
    for t in range(1, n):
        if not (ok[t] and ok[t - 1]) or min(low[t], low[t - 1]) <= 0:
            continue
        beta = math.log(high[t] / low[t]) ** 2 + math.log(high[t - 1] / low[t - 1]) ** 2
        gamma = math.log(max(high[t], high[t - 1]) / min(low[t], low[t - 1])) ** 2
        a = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
        out[t] = max(0.0, 2 * (math.exp(a) - 1) / (1 + math.exp(a)))
    return out


def build_panel(symbols, bars_by_symbol, market):
    from app.historical_data.corporate_actions import (
        VERIFIED_CORPORATE_ACTIONS,
        adjust_for_actions,
    )

    present = [s for s in symbols if bars_by_symbol.get(s)]
    by_sym = {}
    for s in present:
        adj = adjust_for_actions(bars_by_symbol[s], VERIFIED_CORPORATE_ACTIONS.get(s, []))
        by_sym[s] = {_day(b.timestamp): b for b in adj}
    counts = defaultdict(int)
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
            close[i, j], high[i, j], low[i, j] = b.close, b.high, b.low
            rng_ok[i, j] = b.has_reported_range()
            prov = b.field_provenance
            if prov.get("value_traded") == "reported" and b.value_traded:
                value[i, j] = float(b.value_traded)
            elif prov.get("volume", "reported") == "reported" and b.volume > 0:
                value[i, j] = b.volume * b.close
            if b.trade_status == "no_trade":
                halted[i, j] = True
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.diff(np.log(close), axis=0, prepend=np.nan)
    listed = ~np.isnan(r)
    r = np.where(listed, r, 0.0)
    spread = np.zeros((n, m))
    adv = np.full((n, m), np.nan)
    for j in range(m):
        cs = corwin_schultz(high[:, j], low[:, j], rng_ok[:, j])
        for i in range(n):
            w = cs[max(0, i - 20): i + 1]
            w = w[~np.isnan(w)]
            spread[i, j] = max(SPREAD_FLOOR[market], float(np.median(w))) if w.size >= 5 else SPREAD_DEFAULT[market]
            v = value[max(0, i - 20): i + 1, j]
            v = v[~np.isnan(v)]
            if v.size >= MIN_ADV_OBS:
                adv[i, j] = float(np.median(v))
    sig = np.zeros((n, m))
    var = np.nanvar(r[1:61], axis=0) + 1e-8
    for i in range(n):
        sig[i] = np.sqrt(var)
        var = methods.LAMBDA * var + (1 - methods.LAMBDA) * r[i] ** 2
    return {"market": market, "symbols": present, "dates": dates, "r": r, "listed": listed,
            "halted": halted, "spread": spread, "adv": adv, "sigma": sig}


def fx_series(fx_rates, pair, dates):
    rows = sorted((_day(x.timestamp), x.rate) for x in fx_rates.get(pair, []))
    if not rows:
        return None
    ds = [d for d, _ in rows]
    level = np.full(len(dates), np.nan)
    j = -1
    for i, d in enumerate(dates):
        while j + 1 < len(ds) and ds[j + 1] <= d:
            j += 1
        if j >= 0:
            level[i] = rows[j][1]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.diff(np.log(level), prepend=np.nan)


def engine_assets(r_hist, spread, adv_units, weights, types):
    assets = []
    for i, w in enumerate(weights):
        series = r_hist[:, i]
        price = np.exp(np.cumsum(series[-252:]))
        assets.append({
            "id": f"A{i}", "type": types[i], "price": PRICE, "quantity": w * ENGINE_V / PRICE,
            "spread": float(spread[i]), "adv_value": float(max(adv_units[i], 1e-9) * ENGINE_V),
            "vol30": float(series[-30:].std(ddof=1) * np.sqrt(252)),
            "vol90": float(series[-90:].std(ddof=1) * np.sqrt(252)),
            "vol252": float(series[-252:].std(ddof=1) * np.sqrt(252)),
            "mdd252": float(np.max(1 - price / np.maximum.accumulate(price))),
            "gap252": float(np.max(np.abs(np.expm1(series[-252:])))),
        })
    return assets


def run_book(task):
    (label, kind, panel, cols, size, loan_ccy, fx_ret, fx_label, liq_mode, v062, v070,
     seed, with_c, *rest) = task
    # Optional experiment options (intervention study): policy overrides and
    # switches to skip the slow engine and frontier sections.
    opts = rest[0] if rest else {}
    market = panel["market"]
    syms = [panel["symbols"][c] for c in cols]
    n = len(cols)
    weights = np.full(n, 1.0 / n)
    types = ["etf" if s in US_ETF else "listed_equity" for s in syms]
    asset_type = "etf" if all(t == "etf" for t in types) else "listed_equity"
    ceiling = CEILING[asset_type]
    pol = lifecycle.Policy(rate=RATE[loan_ccy], **opts.get("policy", {}))
    r_local = panel["r"][:, cols]
    r_loan = r_local - (np.nan_to_num(fx_ret)[:, None] if fx_ret is not None else 0.0)
    nd = len(panel["dates"])
    if liq_mode.startswith("liq"):
        k = LIQ_SCENARIOS[liq_mode]
        adv_units = np.broadcast_to(weights / (pol.participation * k), (nd, n)).copy()
    else:
        adv_units = panel["adv"][:, cols] / POSITION[market][size]
    starts = []
    for t0 in range(HISTORY, nd - TERM, STEP):
        if not panel["listed"][t0 - HISTORY + 1: t0, cols].all():
            continue
        if np.isnan(adv_units[t0 - 1]).any():
            continue  # liquidity unobservable at the decision date
        if fx_ret is not None and np.isnan(fx_ret[t0 - HISTORY + 1: t0]).any():
            continue
        starts.append(t0)
    adv_fwd = adv_units.copy()
    for j in range(n):  # last observation carried forward (as-of, no look-ahead)
        last = np.nan
        for i in range(nd):
            if np.isnan(adv_fwd[i, j]):
                adv_fwd[i, j] = last
            else:
                last = adv_fwd[i, j]
    adv_fwd = np.nan_to_num(adv_fwd, nan=0.0)
    info = {"book": label, "kind": kind, "size": size, "loan_ccy": loan_ccy, "fx": fx_label,
            "liquidity": liq_mode, "cohorts": len(starts), "symbols": syms}
    if not starts:
        return [], [], [], info
    rng = np.random.default_rng(seed)
    feats = {}
    reqs = []
    check_index = []
    for t0 in starts:
        hist = slice(t0 - HISTORY + 1, t0)
        f = methods.estimate_features(
            r_loan[hist], panel["spread"][hist][:, cols], np.maximum(adv_fwd[hist], 1e-12),
            panel["halted"][hist][:, cols],
            np.nan_to_num(fx_ret[hist]) if fx_ret is not None else None, weights, asset_type,
        )
        feats[t0] = f
        if opts.get("skip_engines"):
            continue
        for c in range(0, TERM, ENGINE_CHECK_EVERY):
            t = t0 + c
            reqs.append({"id": f"{t0}:{c}", "assets": engine_assets(
                r_loan[t - HISTORY + 1: t], panel["spread"][t - 1, cols], adv_fwd[t - 1], weights, types),
                "term_days": TERM_CALENDAR, "rate": RATE[loan_ccy]})
            check_index.append((t0, c))
    eng = {}
    for lbl, path in (("v062", v062), ("v070", v070)):
        if opts.get("skip_engines"):
            continue
        res = run_engine(path, reqs)
        eng[lbl] = {ci: (x["principal"] / x["mv"] if x["mv"] else 0.0)
                    for ci, x in zip(check_index, res, strict=True)}
    policy_rows, matched_rows, frontier_rows = [], [], []
    for t0 in starts:
        f = feats[t0]
        fwd = slice(t0, t0 + TERM)
        tail = slice(t0 + TERM, min(nd, t0 + TERM + TAIL))
        r_true = r_loan[fwd][None]
        halted = panel["halted"][fwd][:, cols][None]
        spread = panel["spread"][fwd][:, cols][None]
        adv = np.maximum(adv_fwd[fwd], 1e-9)[None]
        sig_d = panel["sigma"][fwd][:, cols][None]
        fx = np.nan_to_num(fx_ret[fwd])[None] if fx_ret is not None else None
        t_len = tail.stop - tail.start
        tail_arrays = None
        if t_len > 0:
            tail_arrays = (r_loan[tail][None], panel["halted"][tail][:, cols][None],
                           panel["spread"][tail][:, cols][None],
                           np.maximum(adv_fwd[tail], 1e-9)[None],
                           panel["sigma"][tail][:, cols][None],
                           np.nan_to_num(fx_ret[tail])[None] if fx_ret is not None else None)
        exec_days = float(methods.execution_days(f, pol).max())
        unlendable = exec_days > MAX_EXEC_DAYS
        cap_paths: dict[str, tuple[float, np.ndarray, str]] = {}
        for lbl in ("v062", "v070"):
            if lbl not in eng:
                continue
            vals = [eng[lbl][(t0, c)] for c in range(0, TERM, ENGINE_CHECK_EVERY)]
            path = np.repeat(vals, ENGINE_CHECK_EVERY)[:TERM][None]
            cap_paths[lbl] = (vals[0], path, "accepted" if vals[0] > 0 else "rejected")
        cap_paths["flat"] = (FLAT[market], np.full((1, TERM), FLAT[market]), "accepted")
        variants = {"A": ("A", f), "B": ("B", f), "B_gap": ("B_gap", f)}
        if market == "NGX":
            fvr = methods.multi_session_features(f)
            variants.update({"A_vr": ("A", fvr), "B_vr": ("B", fvr), "B_gap_vr": ("B_gap", fvr),
                             "B_gap_vr_liq": ("B_gap", methods.stressed_liquidity_features(fvr))})
        haircuts = {}
        for name, (base_m, ff) in variants.items():
            if float(methods.execution_days(ff, pol).max()) > MAX_EXEC_DAYS:
                cap_paths[name] = (0.0, np.zeros((1, TERM)), "unlendable")
                continue
            if base_m == "A":
                ell, h = methods.approach_a(ff, pol, ALPHA, ceiling)
            elif base_m == "B":
                ell, h = methods.approach_b(ff, pol, ALPHA, ceiling, rng, n_paths=1500)
            else:
                ell, h = methods.approach_b(ff, pol, 0.995, ceiling, rng, n_paths=1500,
                                            stress_share=0.25, with_gap=True)
            haircuts[name] = (h, ff)
            path = methods.dynamic_ratio(ell, h, ff, r_true, ceiling, pol) if ell > 0 else np.zeros((1, TERM))
            cap_paths[name] = (ell, path, "accepted" if ell > 0 else "rejected")
        if with_c and "B" in haircuts:
            h_b = haircuts["B"][0]
            ell_c, _ = methods.approach_c(f, pol, methods.APPETITES["balanced"], ceiling, TERM,
                                          rng, n_paths=300, haircut_b=h_b)
            path = methods.dynamic_ratio(ell_c, h_b, f, r_true, ceiling, pol) if ell_c > 0 else np.zeros((1, TERM))
            cap_paths["C_balanced"] = (ell_c, path, "accepted" if ell_c > 0 else "rejected")

        def simulate(ell0, path, f=f, r_true=r_true, halted=halted, spread=spread, adv=adv,
                     sig_d=sig_d, fx=fx, tail_arrays=tail_arrays):
            out = lifecycle.simulate_loans(ell0, f.weights, r_true, halted, spread, adv, sig_d,
                                           path, fx, pol)
            cf = lifecycle.simulate_loans(ell0, f.weights, r_true, halted, spread, adv, sig_d,
                                          path, fx, replace(pol, sizing="none"))
            res = {"first_sale": int(out["first_sale"][0]), "calls": int(out["calls"][0])}
            for key, o in (("", out), ("cf_", cf)):
                if tail_arrays is not None:
                    w = lifecycle.terminal_workout(o, *tail_arrays, pol)
                    res[key + "loss"] = float(w["loss"][0])
                    res[key + "defaulted"] = bool(w["defaulted_at_maturity"][0])
                    res[key + "censored"] = bool(w["censored"][0])
                else:
                    res[key + "loss"] = float(o["loss"][0])
                    res[key + "defaulted"] = bool(o["loss"][0] > 0)
                    res[key + "censored"] = True
            fs = res["first_sale"]
            cush = cf["cushion_path"][0]
            res["unnecessary"] = bool(fs >= 0 and cush[fs:].min() >= 0 and res["cf_loss"] <= 1e-9)
            return res

        base = {"market": market, "book": label, "kind": kind, "size": size, "loan_ccy": loan_ccy,
                "fx": fx_label, "liquidity": liq_mode, "t0": panel["dates"][t0].isoformat(),
                "symbol": syms[0] if n == 1 else "portfolio", "exec_days": exec_days}
        for method, (ell0, path, status) in cap_paths.items():
            row = {**base, "method": method, "status": status, "ltv0": float(ell0)}
            if status == "accepted":
                row.update(simulate(ell0, path))
                row["loss_rate"] = row["loss"] / ell0
            policy_rows.append(row)
        for lv in MATCHED_LTV:
            for method, (ell0, path, status) in cap_paths.items():
                mpath = path if status == "accepted" else np.zeros((1, TERM))
                row = {**base, "method": method, "ltv": lv, "capacity_at_origination": float(ell0),
                       **simulate(lv, mpath)}
                row["loss_rate"] = row["loss"] / lv
                matched_rows.append(row)
        if not unlendable and not opts.get("skip_frontier"):
            for name, (_h, ff) in haircuts.items():
                if name not in ("A", "B", "B_gap", "B_gap_vr"):
                    continue
                for a in FRONTIER_ALPHAS:
                    if name.startswith("A"):
                        ell, h = methods.approach_a(ff, pol, a, 0.95)
                    elif name == "B":
                        ell, h = methods.approach_b(ff, pol, a, 0.95, rng, n_paths=1000)
                    else:
                        ell, h = methods.approach_b(ff, pol, a, 0.95, rng, n_paths=1000,
                                                    stress_share=0.25, with_gap=True)
                    if ell <= 0:
                        frontier_rows.append({**base, "method": name, "param": a, "ltv0": 0.0,
                                              "status": "rejected"})
                        continue
                    path = methods.dynamic_ratio(ell, h, ff, r_true, 0.95, pol)
                    frontier_rows.append({**base, "method": name, "param": a, "ltv0": ell,
                                          "status": "accepted", **simulate(ell, path)})
            for lv in FLAT_GRID:
                frontier_rows.append({**base, "method": "flat", "param": lv, "ltv0": lv,
                                      "status": "accepted", **simulate(lv, np.full((1, TERM), lv))})
            if with_c and "B" in haircuts:
                h_b = haircuts["B"][0]
                for app_name, app in methods.APPETITES.items():
                    ell_c, _ = methods.approach_c(f, pol, app, 0.95, TERM, rng, n_paths=300,
                                                  haircut_b=h_b)
                    if ell_c <= 0:
                        frontier_rows.append({**base, "method": "C", "param": app_name,
                                              "ltv0": 0.0, "status": "rejected"})
                        continue
                    path = methods.dynamic_ratio(ell_c, h_b, f, r_true, 0.95, pol)
                    frontier_rows.append({**base, "method": "C", "param": app_name, "ltv0": ell_c,
                                          "status": "accepted", **simulate(ell_c, path)})
            for lbl in ("v062", "v070"):
                if lbl not in cap_paths:
                    continue
                ell0, path, status = cap_paths[lbl]
                if status == "accepted":
                    frontier_rows.append({**base, "method": lbl, "param": "engine", "ltv0": ell0,
                                          "status": "accepted", **simulate(ell0, path)})
    return policy_rows, matched_rows, frontier_rows, info


def _es(x, q=0.95):
    if not len(x):
        return 0.0
    k = max(1, math.ceil((1 - q) * len(x)))
    return float(np.sort(x)[-k:].mean())


def summarise_policy(rows):
    g = defaultdict(list)
    for r in rows:
        g[(r["market"], r["kind"], r["liquidity"], r["size"], r["loan_ccy"], r["fx"], r["method"])].append(r)
    out = []
    for key, rs in sorted(g.items()):
        acc = [r for r in rs if r["status"] == "accepted"]
        ltv = np.array([r["ltv0"] for r in acc]) if acc else np.zeros(0)
        loss = np.array([r["loss"] for r in acc]) if acc else np.zeros(0)
        lr = np.array([r["loss_rate"] for r in acc]) if acc else np.zeros(0)
        liq = sum(r["first_sale"] >= 0 for r in acc)
        out.append({
            "market": key[0], "kind": key[1], "liquidity": key[2], "size": key[3],
            "loan_ccy": key[4], "fx": key[5], "method": key[6], "cohorts": len(rs),
            "rejected": sum(r["status"] == "rejected" for r in rs),
            "unlendable": sum(r["status"] == "unlendable" for r in rs),
            "accepted": len(acc),
            "median_ltv": round(float(np.median(ltv)), 4) if acc else None,
            "loss_events": int((loss > 1e-9).sum()),
            "loss_per_principal_pct": round(100 * float(loss.sum() / max(ltv.sum(), 1e-12)), 4) if acc else None,
            "es95_loss_rate_pct": round(100 * _es(lr), 3) if acc else None,
            "worst_loss_rate_pct": round(100 * float(lr.max()), 3) if acc else None,
            "liquidations": int(liq),
            "unnecessary_share": round(sum(r["unnecessary"] for r in acc) / liq, 3) if liq else None,
            "maturity_defaults": sum(r["defaulted"] for r in acc),
            "censored": sum(r["censored"] and r["loss"] > 0 for r in acc),
        })
    return out


def summarise_matched(rows):
    g = defaultdict(list)
    for r in rows:
        g[(r["market"], r["kind"], r["liquidity"], r["size"], r["loan_ccy"], r["fx"], r["ltv"], r["method"])].append(r)
    out = []
    for key, rs in sorted(g.items()):
        loss = np.array([r["loss"] for r in rs])
        lr = np.array([r["loss_rate"] for r in rs])
        liq = sum(r["first_sale"] >= 0 for r in rs)
        out.append({
            "market": key[0], "kind": key[1], "liquidity": key[2], "size": key[3], "loan_ccy": key[4],
            "fx": key[5], "ltv": key[6], "method": key[7], "loans": len(rs),
            "rejected_by_method": sum(r["capacity_at_origination"] <= 0 for r in rs),
            "loss_events": int((loss > 1e-9).sum()),
            "loss_per_principal_pct": round(100 * float(loss.mean() / key[6]), 4),
            "worst_loss_rate_pct": round(100 * float(lr.max()), 3),
            "cf_loss_per_principal_pct": round(100 * float(np.mean([r["cf_loss"] for r in rs]) / key[6]), 4),
            "liquidation_rate": round(liq / len(rs), 3),
            "unnecessary_share": round(sum(r["unnecessary"] for r in rs) / liq, 3) if liq else None,
        })
    return out


def summarise_frontier(rows):
    g = defaultdict(list)
    for r in rows:
        g[(r["market"], r["kind"], r["liquidity"], r["size"], r["method"], str(r["param"]))].append(r)
    out = []
    for key, rs in sorted(g.items()):
        acc = [r for r in rs if r["status"] == "accepted"]
        ltv_all = np.array([r["ltv0"] for r in rs])
        loss = np.array([r["loss"] for r in acc]) if acc else np.zeros(0)
        lr = np.array([r["loss"] / r["ltv0"] for r in acc]) if acc else np.zeros(0)
        out.append({
            "market": key[0], "kind": key[1], "liquidity": key[2], "size": key[3], "method": key[4],
            "param": key[5], "cohorts": len(rs), "rejected": len(rs) - len(acc),
            "mean_capacity": round(float(ltv_all.mean()), 4),
            "loss_per_principal_pct": round(100 * float(loss.sum() / max(ltv_all.sum(), 1e-12)), 4),
            "loss_per_collateral_bp": round(1e4 * float(loss.sum() / len(rs)), 3),
            "worst_loss_rate_pct": round(100 * float(lr.max()), 3) if acc else 0.0,
            "liquidation_rate": round(sum(r["first_sale"] >= 0 for r in acc) / max(len(acc), 1), 3),
        })
    return out


def write_csv(path, rows):
    if not rows:
        return
    keys = sorted({k for r in rows for k in r})
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def print_table(title, rows, cols):
    print(f"\n#### {title}")
    print("\t".join(cols))
    for r in rows:
        print("\t".join(str(r.get(c)) for c in cols))


def main() -> None:
    from app.simulations.run_official_validation import _load_replay_inputs

    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--v062", required=True)
    ap.add_argument("--v070", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--market", choices=["NGX", "US"], required=True)
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    a = ap.parse_args()
    manifest = json.loads(Path(a.manifest).read_text())
    bars, fx_rates = _load_replay_inputs(manifest)
    _, alt_fx = _load_replay_inputs(dict(manifest, cache_paths=manifest.get("alternate_fx_cache_paths", [])))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    syms = NGX if a.market == "NGX" else US
    panel = build_panel(syms, bars, a.market)
    tasks = []
    books = [([j], f"{a.market}:{s}", "single") for j, s in enumerate(panel["symbols"])]
    books.append((list(range(len(panel["symbols"]))), f"{a.market}:portfolio", "portfolio"))
    ccy = "NGN" if a.market == "NGX" else "USD"
    modes = [("observed", s) for s in ("retail", "institutional")]
    if a.market == "NGX":
        modes += [(m, "scenario") for m in LIQ_SCENARIOS]
    seed = a.seed
    for cols, label, kind in books:
        for liq, size in modes:
            seed += 1
            with_c = kind == "portfolio" or label.endswith((":TSLA", ":ZENITHBANK", ":MTNN", ":NVDA"))
            tasks.append((label, kind, panel, cols, size, ccy, None, "none", liq, a.v062, a.v070, seed, with_c))
    if a.market == "NGX":
        sel = (manifest.get("fx_source_selection") or {}).get("USD/NGN", {})
        tables = {sel.get("selected_provider"): fx_rates}
        for c in sel.get("candidates", []):
            tables.setdefault(c, alt_fx)
        for src, table in tables.items():
            fx = fx_series(table, ("USD", "NGN"), panel["dates"])
            if fx is None or not np.isfinite(fx).any():
                continue
            seed += 1
            tasks.append((f"NGX:portfolio:USD@{src}", "portfolio_usd", panel,
                          list(range(len(panel["symbols"]))), "scenario", "USD", fx, src,
                          "liq20d", a.v062, a.v070, seed, False))
    with Pool(a.workers) as pool:
        results = pool.map(run_book, tasks)
    policy = [r for res in results for r in res[0]]
    matched = [r for res in results for r in res[1]]
    frontier = [r for res in results for r in res[2]]
    infos = [res[3] for res in results]
    write_csv(out / "policy_rows.csv", policy)
    write_csv(out / "matched_rows.csv", matched)
    write_csv(out / "frontier_rows.csv", frontier)
    ps, ms, fs = summarise_policy(policy), summarise_matched(matched), summarise_frontier(frontier)
    write_csv(out / "policy_summary.csv", ps)
    write_csv(out / "matched_summary.csv", ms)
    write_csv(out / "frontier_summary.csv", fs)
    screen = manifest.get("corporate_action_screen") or {}
    meta = {"market": a.market, "books": infos, "sessions": len(panel["dates"]),
            "first": panel["dates"][0].isoformat(), "last": panel["dates"][-1].isoformat(),
            "corporate_action_screen": {
                s: {k: sum(1 for m in v.get("moves", []) if m["classification"] == k)
                    for k in ("verified_corporate_action", "disclosure_candidate", "market_move")}
                for s, v in screen.items() if s in syms}}
    (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
    print(json.dumps({k: meta[k] for k in ("market", "sessions", "first", "last")}))
    print(json.dumps(meta["corporate_action_screen"]))
    print_table("POLICY-ORIGINATED", ps, ["kind", "liquidity", "size", "loan_ccy", "fx", "method", "cohorts",
                "rejected", "unlendable", "accepted", "median_ltv", "loss_events", "loss_per_principal_pct",
                "es95_loss_rate_pct", "worst_loss_rate_pct", "liquidations", "unnecessary_share",
                "maturity_defaults", "censored"])
    print_table("MATCHED EXPOSURE", ms,
                ["kind", "liquidity", "size", "fx", "ltv", "method", "loans", "rejected_by_method", "loss_events",
                 "loss_per_principal_pct", "cf_loss_per_principal_pct", "worst_loss_rate_pct",
                 "liquidation_rate", "unnecessary_share"])
    print_table("FRONTIER", fs, ["kind", "liquidity", "size", "method", "param", "cohorts", "rejected",
                "mean_capacity", "loss_per_principal_pct", "loss_per_collateral_bp", "worst_loss_rate_pct",
                "liquidation_rate"])
    tsla = [r for r in policy if r["symbol"] == "TSLA"]
    if tsla:
        print_table("TSLA BY COHORT (policy-originated)",
                    sorted(tsla, key=lambda r: (r["size"], r["t0"], r["method"])),
                    ["size", "t0", "method", "status", "ltv0", "first_sale", "loss_rate", "defaulted", "censored"])


if __name__ == "__main__":
    main()
