"""Extended lifecycle validation of the candidate engine (research evidence).

Inputs: the dataset of an Official Provider Validation Run (checksummed
snapshot verified by the workflow). No provider calls. Every number in the
output is labelled by what it rests on:

* observed     daily bars from the providers, as delivered (coverage is
               reported per symbol; nothing is claimed beyond it);
* estimated    statistics the engine estimates from observed bars;
* assumption   stated assumptions (e.g. NGX volume before August 2025 is
               not reported; historical NGX runs use a stated liquidity
               scenario: the retail position sells in 20 sessions at 10%);
* synthetic    simulated stress paths appended after an observed origination.

Design
* Matched initial exposure and common random numbers across variants
  (app.candidate.lifecycle.run_cohort).
* Held-out periods: the window 2026-02-16 onward was examined in the
  previous round (staging lifecycle run); everything before it is reported
  separately as held out. Engine parameters are research defaults authorised
  before this run, not fitted here.
* Overlapping cohorts: 95% intervals from a moving-block bootstrap over
  origination order with block = term / spacing.
* Priors for hierarchical pooling are point in time: other instruments of the
  same market with >= 250 sessions before the origination date.

Usage (workflow): python -m research.methodology_lab.candidate_validation MANIFEST --part hist-US --out out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import date
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

from app.candidate.config import CandidateConfig
from app.candidate.engine import data_key
from app.candidate.estimation import group_prior, raw_daily_vol
from app.candidate.lifecycle import (
    DEFAULT_VARIANTS,
    RESEARCH_VARIANTS,
    STRESS_VARIANTS,
    Variant,
    aggregate,
    clustered_interval,
    cure_sensitivity,
    metrics,
    run_cohort,
    simulated_bars,
    variant_rows,
    zero_event_bound,
)
from app.candidate.rc import RC_OVERRIDES
from app.core.enums import AssetType
from app.core.models import Holding, Policy
from app.credit.interest import InterestPolicy
from app.liquidation.policy import LiquidationExecutionPolicy

DEV_START = date(2026, 2, 16)
# Data tiers (docs/governance/DATA_TIERS.md). Every historical session in this
# dataset has been examined in earlier CRI research, so none of it is locked
# unseen validation data. Locked data begins after LOCK_DATE and is reserved.
LOCK_DATE = date(2026, 11, 26)
ALL_VARIANTS = DEFAULT_VARIANTS + RESEARCH_VARIANTS
COMPARE = (DEFAULT_VARIANTS[0], DEFAULT_VARIANTS[1], DEFAULT_VARIANTS[2], RESEARCH_VARIANTS[-1])
# 27 Nov ablation set: v2026.11.6 baseline, fixed references, the rejected hysteresis/staged
# reference, and each 2026.11.7 mechanism alone and combined (rc_candidate).
RC_SET = (DEFAULT_VARIANTS[0], DEFAULT_VARIANTS[1], DEFAULT_VARIANTS[2], RESEARCH_VARIANTS[4]) + STRESS_VARIANTS
# Frozen RC confirmation set (CRI-1.0.0-rc1 at its own utilisation) against the
# v2026.11.6 baseline and the fixed references, all at matched exposure.
RC1_SET = (DEFAULT_VARIANTS[0], DEFAULT_VARIANTS[1], DEFAULT_VARIANTS[2],
           Variant("rc1", "adaptive", engine=tuple((k, v) for k, v in RC_OVERRIDES if k != "capacity_utilisation")))
# Policy-originated subset: each adaptive variant lends its own capacity.
POLICY_SET = (DEFAULT_VARIANTS[0], DEFAULT_VARIANTS[2]) + tuple(
    v for v in STRESS_VARIANTS if v.name in ("adaptive_headroom_cal", "adaptive_concentration_cal",
                                             "adaptive_suspension_value", "rc_candidate"))
CFG = CandidateConfig(n_paths=400)
# Principal scales (multiples of RC1's approved credit under --variant-set rc1) for the
# full-lifecycle frontier; --scales overrides.
FRONTIER_SCALES: tuple[float, ...] = (0.85, 0.925, 1.0, 1.15)
# Frontier v2 (RC2 research, Report 17 review): fine grid around the promising region,
# common random numbers across scales and cases, contractual ceiling enforced,
# point-in-time settlement, retail and institutional positions.
WORKOUT_SCALES: tuple[float, ...] = (1.0, 1.25)
FRONTIER2_SCALES: tuple[float, ...] = (0.9, 1.0, 1.1, 1.2, 1.25, 1.3, 1.4, 1.5)
FRONTIER2_TERM_SCALES: tuple[float, ...] = (1.0, 1.25)
FRONTIER2_TERMS: tuple[int, ...] = (21, 126)
FSTRESS_SCALES: tuple[float, ...] = (1.0, 1.1, 1.25, 1.5)
FSTRESS_KINDS = ("base", "combined", "correlated_drawdown", "prolonged_suspension", "execution_failure",
                 "settlement_delay")
SETTLEMENT_DELAY_STRESS = 5          # extra sessions before proceeds settle
EXCH = {"US": "NYSE", "NGX": "NGX"}
CCY = {"US": "USD", "NGX": "NGN"}
RATE = {"US": 0.10, "NGX": 0.25}
POSITION = {"US": {"retail": 250e3, "institutional": 25e6}, "NGX": {"retail": 50e6, "institutional": 2e9}}
NGX_VOLUME_FROM = date(2025, 8, 1)
US_ETF = {"SPY", "QQQ", "IWM", "TLT", "HYG"}
PORTFOLIOS = {
    "US": {"concentrated_TSLA": {"TSLA": 1.0}, "concentrated_NVDA": {"NVDA": 1.0},
           "diversified": {"AAPL": .2, "MSFT": .2, "JPM": .2, "XOM": .2, "SPY": .2},
           "etf": {"SPY": .5, "QQQ": .3, "TLT": .2}},
    "NGX": {"banks": {"GTCO": .25, "ZENITHBANK": .25, "ACCESSCORP": .25, "UBA": .25},
            "mixed": {"MTNN": .25, "DANGCEM": .25, "GTCO": .25, "SEPLAT": .25},
            "telco": {"MTNN": .6, "AIRTELAFRI": .4}},
}
STRESS = ("base", "overnight_gap", "consecutive_adverse", "suspension", "liquidity_collapse", "combined",
          "correlated_drawdown", "short_history")


def to_bar_dicts(market: str, symbol: str, bars: list[Any], liquidity_sessions: int | None,
                 position_value: float) -> list[dict[str, Any]]:
    out = []
    for b in bars:
        d = b.timestamp.date() if hasattr(b.timestamp, "date") else b.timestamp
        close = float(b.adjusted_close or b.close)
        vol = float(b.volume or 0.0)
        reported = vol > 0 and not (market == "NGX" and d < NGX_VOLUME_FROM)
        src = "observed"
        if not reported and liquidity_sessions:
            # Stated assumption: the position sells in `liquidity_sessions` sessions at 10%.
            vol = position_value / close / (liquidity_sessions * 0.10)
            reported, src = True, f"assumption_liq{liquidity_sessions}d"
        hi, lo = float(b.high or close), float(b.low or close)
        out.append({"bar_date": d, "open": float(b.open or close), "high": hi, "low": lo, "close": close,
                    "volume": vol, "volume_reported": reported, "range_reported": hi > lo > 0,
                    "volume_source": src})
    return out


def coverage(bars_by_symbol: dict[str, list[Any]], market: str) -> dict[str, Any]:
    out = {}
    for s, v in bars_by_symbol.items():
        if not v:
            continue
        ds = [b.timestamp.date() if hasattr(b.timestamp, "date") else b.timestamp for b in v]
        vol = [d for d, b in zip(ds, v, strict=True) if (b.volume or 0) > 0
               and not (market == "NGX" and d < NGX_VOLUME_FROM)]
        out[s] = {"first": min(ds).isoformat(), "last": max(ds).isoformat(), "sessions": len(ds),
                  "volume_reported_sessions": len(vol), "provider": getattr(v[0], "provider_name", "?")}
    return out


def priors_at(market: str, all_bars: dict[str, list[dict[str, Any]]], d: date) -> dict[str, Any]:
    lv = []
    for v in all_bars.values():
        vis = [b for b in v if b["bar_date"] <= d]
        x = raw_daily_vol(vis[-501:], CFG)
        if x:
            lv.append(math.log(x))
    p = group_prior(lv, f"{market} dataset instruments with >= 250 sessions to {d}")
    return {market: p} if p else {}


def holdings_for(market: str, weights: dict[str, float], prices: dict[str, float], size: str) -> list[Holding]:
    total = POSITION[market][size]
    out = []
    for s, w in weights.items():
        at = AssetType.ETF if s in US_ETF else AssetType.LISTED_EQUITY
        out.append(Holding(s, at, total * w / prices[s], CCY[market], EXCH[market]))
    return out


# --------------------------------------------------------------- synthetic

def stress_paths(hist: dict[str, list[dict[str, Any]]], keys: list[str], kind: str, days: int,
                 rng: np.random.Generator) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """SYNTHETIC future log returns and volume multipliers per instrument.

    base: block bootstrap (10-session blocks) of the joint observed returns of
    the last 500 sessions. Overlays are added to the same base path, so every
    stress scenario differs from base only by the overlay."""
    rets = {}
    common = sorted(set.intersection(*[{b["bar_date"] for b in hist[k]} for k in keys]))[-501:]
    for k in keys:
        by = {b["bar_date"]: b["close"] for b in hist[k]}
        c = np.array([by[d] for d in common])
        rets[k] = np.diff(np.log(c))
    n = len(next(iter(rets.values())))
    blocks = math.ceil(days / 10)
    starts = rng.integers(0, max(1, n - 10), size=blocks)
    idx = np.concatenate([np.arange(s, s + 10) for s in starts])[:days]
    sig = {k: float(np.std(rets[k][-250:])) for k in keys}
    port = sum(rets[k] for k in keys) / len(keys)
    worst = float(np.min(port))
    out = {k: rets[k][idx].copy() for k in keys}
    vm = {k: np.ones(days) for k in keys}
    t0 = int(rng.integers(3, 25))
    if kind == "overnight_gap":
        g = min(-0.10, 1.5 * worst)
        for k in keys:
            out[k][t0] += g
    elif kind == "consecutive_adverse":
        for k in keys:
            out[k][t0:t0 + 5] = -2.5 * sig[k]
    elif kind == "suspension":
        big = keys[0]
        cum = float(out[big][t0:t0 + 10].sum()) - 3 * sig[big] * math.sqrt(10)
        out[big][t0:t0 + 10] = np.nan
        out[big][min(days - 1, t0 + 10)] += cum
    elif kind == "liquidity_collapse":
        for k in keys:
            vm[k][t0:t0 + 30] = 0.1
            out[k][t0:t0 + 5] -= 1.5 * sig[k]
    elif kind == "correlated_drawdown":
        for k in keys:
            out[k][t0:t0 + 20] = out[k][t0:t0 + 20] * 2 + math.log(0.7) / 20
            vm[k][t0:t0 + 20] = 0.5
    elif kind == "prolonged_suspension":
        # The largest holding cannot trade for 30 sessions and reopens 3 sigma*sqrt(30) lower.
        big = keys[0]
        cum = float(out[big][t0:t0 + 30].sum()) - 3 * sig[big] * math.sqrt(30)
        out[big][t0:t0 + 30] = np.nan
        out[big][min(days - 1, t0 + 30)] += cum
    elif kind == "execution_failure":
        # No executable volume for 20 sessions while prices drift down 1.5 sigma per session for 5.
        for k in keys:
            vm[k][t0:t0 + 20] = 0.0
            out[k][t0:t0 + 5] -= 1.5 * sig[k]
    elif kind == "combined":
        # Gap, then a correlated drawdown with collapsing liquidity, and a suspension
        # of the largest holding in the middle of it.
        g = min(-0.10, 1.5 * worst)
        for k in keys:
            out[k][t0] += g
            out[k][t0 + 1:t0 + 21] = out[k][t0 + 1:t0 + 21] * 2 + math.log(0.75) / 20
            vm[k][t0:t0 + 30] = 0.2
        big = keys[0]
        s0 = t0 + 5
        cum = float(np.nansum(out[big][s0:s0 + 8])) - 2 * sig[big] * math.sqrt(8)
        out[big][s0:s0 + 8] = np.nan
        out[big][min(days - 1, s0 + 8)] += cum
    return out, vm


# --------------------------------------------------------------- tasks

def run_task(t: dict[str, Any]) -> dict[str, Any]:
    try:
        ex = LiquidationExecutionPolicy()
        if t.get("settlement_delay"):
            from dataclasses import replace as _replace
            ex = _replace(ex, settlement_delay_observations=int(t["settlement_delay"]))
        res = run_cohort(holdings=t["holdings"], bars=t["bars"], origination=t["origination"],
                         term_sessions=t["term"], variants=t["variants"], policy=Policy.default(),
                         interest=InterestPolicy(t["rate"], accrual_frequency="monthly", compounding="compound"),
                         execution=ex, cure_probability=t.get("cure", 0.5),
                         ceiling_cap=t.get("ceiling_cap", False), settlement=t.get("settlement", "fixed"),
                         seed=t["seed"], config=CFG, priors=t["priors"], workout_sessions=40,
                         maturity=t.get("maturity", "repay"), principal_scale=t.get("scale", 1.0),
                         origination_mode=t.get("origination_mode", "matched"),
                         origination_engine=t.get("origination_engine", ()),
                         workout_policy=t.get("workout_policy", "sell_all"),
                         refinance_policy=t.get("refinance_policy", "refuse"))
    except ValueError as exc:
        res = {"origination": t["origination"].isoformat(), "skipped": str(exc)}
    res["group"] = t["group"]
    res["labels"] = t["labels"]
    return res


def build_tasks(part: str, bars_by_symbol: dict[str, list[Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    market = part.split("-")[1]
    mode = part.split("-")[0]
    syms = sorted({s for p in PORTFOLIOS[market].values() for s in p})
    missing = [s for s in syms if not bars_by_symbol.get(s)]
    tasks: list[dict[str, Any]] = []
    seed = 1000
    for pname, weights in PORTFOLIOS[market].items():
        if any(s in missing for s in weights):
            continue
        sizes = (("retail", "institutional") if mode in ("hist", "sim", "frontier2", "fstress", "workout")
                 else ("retail",))
        for size in sizes:
            liq = 20 if market == "NGX" else None
            bars = {}
            hs0 = holdings_for(market, weights, {s: 1.0 for s in weights}, size)
            for h in hs0:
                bars[data_key(h)] = to_bar_dicts(market, h.asset_id, bars_by_symbol[h.asset_id], liq,
                                                 POSITION[market][size] * weights[h.asset_id])
            refs = {h.asset_id: data_key(h) for h in hs0}
            dates = sorted(set.intersection(*[{b["bar_date"] for b in bars[refs[s]]} for s in weights]))
            all_market = {s: to_bar_dicts(market, s, bars_by_symbol[s], None, 1.0) for s in syms
                          if bars_by_symbol.get(s)}
            spacing, warm = 21, 250
            orig_idx = list(range(warm, len(dates) - 63, spacing))
            for oi in orig_idx:
                d = dates[oi]
                prices = {s: next(b["close"] for b in reversed(bars[refs[s]]) if b["bar_date"] <= d) for s in weights}
                hs = holdings_for(market, weights, prices, size)
                period = ("previously_examined_2026" if d >= DEV_START else "previously_examined_pre_2026_02_16")
                pri = priors_at(market, all_market, d)
                base_labels = {"market": market, "portfolio": pname, "size": size, "period": period,
                               "data": "observed" + ("+assumption_liq20d" if market == "NGX" else "")}
                if mode == "hist":
                    seed += 1
                    tasks.append({"holdings": hs, "bars": bars, "origination": d, "term": 63, "rate": RATE[market],
                                  "variants": RC_SET, "seed": seed, "priors": pri,
                                  "group": f"hist|{pname}|{size}|{period}", "labels": base_labels})
                    tasks.append({"holdings": hs, "bars": bars, "origination": d, "term": 63, "rate": RATE[market],
                                  "variants": POLICY_SET, "seed": seed, "priors": pri, "origination_mode": "policy",
                                  "group": f"histpolicy|{pname}|{size}|{period}",
                                  "labels": {**base_labels, "origination": "policy"}})
                elif mode == "repay" and pname in ("concentrated_TSLA", "diversified", "banks", "mixed"):
                    for mat, cure in (("default", 0.5), ("partial:0.5", 0.5), ("refinance", 0.5), ("default", 0.0)):
                        seed += 1
                        tag = "zero_voluntary" if cure == 0.0 else mat
                        tasks.append({"holdings": hs, "bars": bars, "origination": d, "term": 63,
                                      "rate": RATE[market], "variants": RC_SET, "seed": seed, "priors": pri,
                                      "maturity": mat, "cure": cure, "group": f"repay|{tag}|{pname}|{period}",
                                      "labels": {**base_labels, "maturity": tag}})
                elif mode == "frontier":
                    for scale in FRONTIER_SCALES:
                        seed += 1
                        tasks.append({"holdings": hs, "bars": bars, "origination": d, "term": 63,
                                      "rate": RATE[market], "variants": RC_SET, "seed": seed, "priors": pri,
                                      "maturity": "refinance", "scale": scale,
                                      "group": f"frontier|scale{scale}|{pname}|{size}",
                                      "labels": {**base_labels, "principal_scale": scale, "maturity": "refinance"}})
                elif mode == "frontier2":
                    cid = f"{market}|{pname}|{size}|{d.isoformat()}"
                    cseed = 50_000_000 + int(hashlib.sha256(cid.encode()).hexdigest()[:7], 16)  # CRN per cohort
                    common = {"holdings": hs, "bars": bars, "origination": d, "rate": RATE[market],
                              "variants": RC_SET, "seed": cseed, "priors": pri, "ceiling_cap": True,
                              "settlement": "point_in_time"}
                    cases = [("base", 63, "refinance", 0.5, s_) for s_ in FRONTIER2_SCALES]
                    cases += [("adverse_maturity_zero_cure", 63, "default", 0.0, s_) for s_ in FRONTIER2_SCALES]
                    cases += [("term", tm, "refinance", 0.5, s_) for tm in FRONTIER2_TERMS
                              for s_ in FRONTIER2_TERM_SCALES if oi + tm < len(dates)]
                    for case, term, mat, cure, scale in cases:
                        tasks.append({**common, "term": term, "maturity": mat, "cure": cure, "scale": scale,
                                      "group": f"f2|{case}|term{term}|scale{scale}|{pname}|{size}",
                                      "labels": {**base_labels, "principal_scale": scale, "case": case,
                                                 "term": term, "maturity": mat, "cure": cure, "cohort_id": cid,
                                                 "settlement": "point_in_time", "ceiling": "enforced"}})
                elif mode == "workout":
                    # RC2 maturity-workout optimisation: every policy on the same cohort, path and
                    # seed (common random numbers), point-in-time settlement, contractual ceiling.
                    cid = f"{market}|{pname}|{size}|{d.isoformat()}"
                    cseed = 60_000_000 + int(hashlib.sha256(cid.encode()).hexdigest()[:7], 16)
                    common = {"holdings": hs, "bars": bars, "origination": d, "rate": RATE[market], "term": 63,
                              "variants": RC_SET, "seed": cseed, "priors": pri, "ceiling_cap": True,
                              "settlement": "point_in_time"}
                    cases = [("default_zero_cure", "default", 0.0, "refuse", wp)
                             for wp in ("sell_all", "selective", "planner")]
                    cases += [("refinance", "refinance", 0.5, rp, wp)
                              for rp, wp in (("refuse", "sell_all"), ("refuse", "selective"), ("paydown", "selective"))]
                    for scale in WORKOUT_SCALES:
                        for case, mat, cure, rp, wp in cases:
                            tasks.append({**common, "maturity": mat, "cure": cure, "scale": scale,
                                          "workout_policy": wp, "refinance_policy": rp,
                                          "group": f"wo|{case}|{rp}|{wp}|scale{scale}|{pname}|{size}",
                                          "labels": {**base_labels, "principal_scale": scale, "case": case,
                                                     "maturity": mat, "cure": cure, "cohort_id": cid,
                                                     "workout_policy": wp, "refinance_policy": rp,
                                                     "settlement": "point_in_time", "ceiling": "enforced"}})
                elif mode == "sens" and pname in ("concentrated_TSLA", "diversified", "banks", "mixed"):
                    for rate, term in ((0.05, 63), (0.30, 63), (RATE[market], 21), (RATE[market], 126)):
                        if oi + term >= len(dates):
                            continue
                        seed += 1
                        tasks.append({"holdings": hs, "bars": bars, "origination": d, "term": term, "rate": rate,
                                      "variants": DEFAULT_VARIANTS, "seed": seed, "priors": pri,
                                      "group": f"sens|{pname}|rate{rate}|term{term}|{period}", "labels": base_labels})
                    seed += 1
                    tasks.append({"holdings": hs, "bars": bars, "origination": d, "term": 63, "rate": RATE[market],
                                  "variants": cure_sensitivity(), "seed": seed, "priors": pri, "maturity": "refinance",
                                  "group": f"cure|{pname}|{period}", "labels": base_labels})
            if mode == "fstress":
                rng = np.random.default_rng(97 + 1000 * list(PORTFOLIOS[market]).index(pname)
                                            + (0 if size == "retail" else 1))
                held = [oi for oi in orig_idx if dates[oi] < DEV_START]
                picks = [held[i] for i in np.linspace(0, len(held) - 1, min(10, len(held))).astype(int)] if held else []
                for oi in picks:
                    d = dates[oi]
                    prices = {s: next(b["close"] for b in reversed(bars[refs[s]]) if b["bar_date"] <= d)
                              for s in weights}
                    hs = holdings_for(market, weights, prices, size)
                    pri = priors_at(market, all_market, d)
                    keys = [refs[s] for s in weights]
                    for rep_ in range(2):
                        path_seed = int(rng.integers(0, 2**31))
                        cid = f"{market}|{pname}|{size}|{d.isoformat()}|rep{rep_}"
                        cseed = 60_000_000 + path_seed % 10**7                   # CRN per stress cohort
                        for kind in FSTRESS_KINDS:
                            prng = np.random.default_rng(path_seed)               # same base path for every overlay
                            r, vm = stress_paths(bars, keys, "base" if kind == "settlement_delay" else kind, 130, prng)
                            sb = {k: simulated_bars(bars[k], r[k], d, vm[k]) for k in keys}
                            for beh, mat, cure in (("refinance_cure50", "refinance", 0.5),
                                                   ("default_zero_cure", "default", 0.0)):
                                for scale in FSTRESS_SCALES:
                                    lab = {**base_labels, "period": "held_out_origination",
                                           "data": "observed history + synthetic future", "scenario": kind,
                                           "behaviour": beh, "path_seed": path_seed, "principal_scale": scale,
                                           "cohort_id": cid, "settlement": "point_in_time", "ceiling": "enforced"}
                                    tasks.append({"holdings": hs, "bars": sb, "origination": d, "term": 63,
                                                  "rate": RATE[market], "variants": RC_SET, "seed": cseed,
                                                  "priors": pri, "maturity": mat, "cure": cure, "scale": scale,
                                                  "ceiling_cap": True, "settlement": "point_in_time",
                                                  "settlement_delay": SETTLEMENT_DELAY_STRESS
                                                  if kind == "settlement_delay" else 0,
                                                  "group": f"fs|{kind}|{beh}|scale{scale}|{pname}|{size}",
                                                  "labels": lab})
            if mode == "sim":
                rng = np.random.default_rng(7 + len(tasks))
                held = [oi for oi in orig_idx if dates[oi] < DEV_START]
                picks = [held[i] for i in np.linspace(0, len(held) - 1, min(10, len(held))).astype(int)] if held else []
                for oi in picks:
                    d = dates[oi]
                    prices = {s: next(b["close"] for b in reversed(bars[refs[s]]) if b["bar_date"] <= d)
                              for s in weights}
                    hs = holdings_for(market, weights, prices, size)
                    pri = priors_at(market, all_market, d)
                    keys = [refs[s] for s in weights]
                    for rep in range(2):
                        path_seed = int(rng.integers(0, 2**31))
                        for kind in STRESS:
                            prng = np.random.default_rng(path_seed)   # same base path for every overlay
                            r, vm = stress_paths(bars, keys, "base" if kind == "short_history" else kind, 130, prng)
                            sb = {k: simulated_bars(bars[k], r[k], d, vm[k]) for k in keys}
                            if kind == "short_history":
                                k0 = keys[0]
                                # Short-history instrument: only 40 observed sessions at origination.
                                sb[k0] = ([b for b in sb[k0] if b["bar_date"] <= d][-40:]
                                          + [b for b in sb[k0] if b["bar_date"] > d])
                            seed += 1
                            lab = {**base_labels, "period": "held_out_origination",
                                   "data": "observed history + synthetic future", "scenario": kind,
                                   "path_seed": path_seed}
                            tasks.append({"holdings": hs, "bars": sb, "origination": d, "term": 63,
                                          "rate": RATE[market], "variants": RC_SET, "seed": seed,
                                          "priors": pri, "group": f"sim|{kind}|{pname}|{size}", "labels": lab})
                            tasks.append({"holdings": hs, "bars": sb, "origination": d, "term": 63,
                                          "rate": RATE[market], "variants": POLICY_SET, "seed": seed,
                                          "priors": pri, "origination_mode": "policy",
                                          "group": f"simpolicy|{kind}|{pname}|{size}",
                                          "labels": {**lab, "origination": "policy"}})
    info = {"market": market, "mode": mode, "missing_symbols": missing,
            "coverage": coverage({s: bars_by_symbol.get(s, []) for s in syms}, market)}
    return tasks, info


def main() -> None:
    from app.simulations.run_official_validation import _load_replay_inputs

    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--part", required=True, choices=[f"{m}-{k}" for m in ("hist", "sens", "sim", "repay", "frontier",
                                                                          "frontier2", "fstress", "workout")
                                                       for k in ("US", "NGX")])
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--variant-set", default="ablation", choices=["ablation", "rc1"])
    ap.add_argument("--shard", default="0/1", help="i/n: run every n-th task starting at i (partitioned jobs)")
    ap.add_argument("--paths", type=int, default=0, help="Monte Carlo paths (default: the runner's 400)")
    ap.add_argument("--scales", default="", help="frontier principal scales, comma-separated")
    a = ap.parse_args()
    if a.scales:
        global FRONTIER_SCALES
        FRONTIER_SCALES = tuple(float(x) for x in a.scales.split(","))
    manifest = json.loads(Path(a.manifest).read_text())
    bars, _fx = _load_replay_inputs(manifest)
    tasks, info = build_tasks(a.part, bars)
    if a.variant_set == "rc1":
        # Confirmation of the frozen RC: matched exposure at the RC's utilisation (0.925),
        # one variant set, no policy-originated duplicates.
        util = dict(RC_OVERRIDES)["capacity_utilisation"]
        rc_engine = tuple((k, v) for k, v in RC_OVERRIDES if k != "capacity_utilisation")
        only_rc1 = a.part.startswith(("frontier2", "fstress", "workout"))        # v2: the RC1 variant alone
        tasks = [dict(t, variants=(RC1_SET[-1],) if only_rc1 else RC1_SET, scale=t.get("scale", 1.0) * util,
                      origination_engine=rc_engine,
                      labels={**t["labels"], "scale_of_rc_credit": t.get("scale", 1.0),
                              "effective_scale_of_capacity": t.get("scale", 1.0) * util})
                 for t in tasks if t.get("origination_mode", "matched") == "matched"]
        info["variant_set"] = "rc1"
        info["origination"] = "every policy lends RC1's own approved credit (RC1 engine capacity x 0.925)"
    si, sn = (int(x) for x in a.shard.split("/"))
    tasks = tasks[si::sn]
    info["shard"] = a.shard
    if a.paths:
        global CFG
        CFG = CandidateConfig(n_paths=a.paths)
        info["n_paths"] = a.paths
    else:
        info["n_paths"] = CFG.n_paths
    if a.limit:
        tasks = tasks[: a.limit]
    print(json.dumps({"part": a.part, "tasks": len(tasks), **info}, default=str)[:4000], flush=True)
    with Pool(a.workers) as pool:
        results = pool.map(run_task, tasks, chunksize=1)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        groups.setdefault(r["group"], []).append(r)
    summary = {}
    for g, rows in sorted(groups.items()):
        rows.sort(key=lambda x: x["origination"])
        spacing = 21 if g.startswith(("hist", "sens", "cure", "f2")) else 63
        term = next((x["term_sessions"] for x in rows if "term_sessions" in x), 63)
        summary[g] = {"labels": rows[0]["labels"], **aggregate(rows, spacing_sessions=spacing, term_sessions=term),
                      "window": [rows[0]["origination"], rows[-1]["origination"]],
                      "skipped_reasons": sorted({x.get("skipped", "") for x in rows if "skipped" in x})[:5]}
    (out / f"{a.part}_summary.json").write_text(json.dumps({"info": info, "groups": summary}, default=str, indent=1))
    (out / f"{a.part}_cohorts.json").write_text(json.dumps(results, default=str))
    # One compact line per group (artifact storage may be unreachable for readers).
    keep = ("loans", "mean_capacity_share_of_principal", "loss_probability", "loss_probability_ci95", "expected_loss",
            "expected_loss_ci95", "es99_loss", "worst_loss", "margin_calls_per_loan", "liquidation_frequency",
            "liquidation_frequency_ci95", "unnecessary_liquidation_share", "borrower_execution_cost_share",
            "executed_share_of_principal", "realized_recovery_share", "cure_payments_share",
            "recovery_sessions_median", "recovery_sessions_p95", "recovery_completion",
            "residual_after_recovery_share", "time_restricted", "repeated_margin_call_share",
            "time_margin_call_or_forced", "data_review_sessions", "no_fill_sessions", "censored",
            "censored_outstanding_share", "censored_loss_lower_bound_mean", "es95_loss", "completed",
            "expected_loss_conservative_upper", "loss_probability_conservative_upper", "pending_at_cutoff_share",
            "initial_ltv_mean", "clusters", "clusters_with_loss", "cluster_loss_probability_upper95", "note")

    def rnd(x: Any) -> Any:
        if isinstance(x, float):
            return round(x, 5)
        if isinstance(x, list):
            return [rnd(v) for v in x]
        return x

    for g, sm in summary.items():
        line = {"group": g, "labels": sm["labels"], "cohorts": sm["cohorts"], "skipped": sm["skipped"],
                "eff": round(sm["effective_independent_cohorts"], 1), "window": sm["window"],
                "skipped_reasons": sm["skipped_reasons"][:2],
                "v": {n: {k: rnd(m[k]) for k in keep if k in m} for n, m in sm["variants"].items()}}
        print("RESULT " + json.dumps(line, default=str), flush=True)

    # Pooled across portfolios and sizes, with intervals that resample clusters of
    # loans originated in the same quarter (shared shocks, same paths, overlaps).
    pools: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        parts = r["group"].split("|")
        key = "|".join(parts[:2]) if parts[0] in ("sim", "simpolicy", "repay", "frontier", "f2", "fs") else \
            f"{parts[0]}|{parts[-1]}" if parts[0] in ("hist", "histpolicy") else \
            "|".join(parts[:5]) if parts[0] == "wo" else parts[0]   # wo|case|refinance|workout|scale
        pools.setdefault(key, []).append(r)
    # Compact per-cohort records (no event logs), gzip+base64, so partitioned jobs can be
    # pooled exactly in analysis: one COHORTS line per 200 cohorts.
    import base64
    import gzip
    compact = []
    for c in results:
        c2 = {k: v for k, v in c.items() if k != "variants"}
        c2["variants"] = {n: ({k: v for k, v in vv.items() if k != "events"} if isinstance(vv, dict) else vv)
                          for n, vv in c.get("variants", {}).items()}
        compact.append(c2)
    for j in range(0, len(compact), 200):
        blob = base64.b64encode(gzip.compress(json.dumps(compact[j:j + 200], default=str).encode())).decode()
        print(f"COHORTS {a.part} {a.shard} {j} {blob}", flush=True)
    for key, rows in sorted(pools.items()):
        names = sorted({n for c in rows if "variants" in c for n in c["variants"]})
        line = {"pool": key, "part": a.part, "cohorts": sum(1 for c in rows if "variants" in c), "v": {}}
        for n in names:
            vr = variant_rows(rows, n)
            m = metrics(vr)
            m.update(zero_event_bound(vr))
            m["loss_probability_ci95"] = clustered_interval(vr, "loss_event")
            m["expected_loss_ci95"] = clustered_interval(vr, "loss")
            m["liquidation_frequency_ci95"] = clustered_interval(vr, "liquidation")
            line["v"][n] = {k: rnd(m[k]) for k in keep if k in m}
        print("POOLED " + json.dumps(line, default=str), flush=True)


if __name__ == "__main__":
    main()
