"""Candidate engine: adaptive thresholds, safeguards and lifecycle validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import pytest

from app.candidate import CandidateConfig, CandidateEngine
from app.candidate.engine import data_key
from app.candidate.lifecycle import DEFAULT_VARIANTS, aggregate, run_cohort
from app.core.enums import AssetType, MarginState
from app.core.models import Holding, Loan, MarketData, Policy
from app.credit.interest import InterestPolicy
from app.liquidation.policy import LiquidationExecutionPolicy
from app.markets.hierarchical import Prior

CFG = CandidateConfig(n_paths=600)


def make_bars(seed: int, n: int = 400, vol: float = 0.02, volume: float = 2e6, start: date = date(2024, 1, 1),
              crash: set[int] | None = None) -> list[dict]:
    rng = np.random.default_rng(seed)
    d, out, p = start, [], 50.0
    while len(out) < n:
        if d.weekday() < 5:
            r = rng.standard_t(4) * vol * np.sqrt(0.5)
            if crash and len(out) in crash:
                r = -0.15
            p *= float(np.exp(r))
            out.append({"bar_date": d, "open": p, "high": p * 1.01, "low": p * 0.99, "close": p,
                        "volume": volume, "range_reported": True, "volume_reported": True})
        d += timedelta(days=1)
    return out


def setup(vol_b: float = 0.03, n_b: int = 400, cash: float = 50_000.0, ccy_b: str = "USD"):
    ba, bb = make_bars(1), make_bars(2, n=n_b, vol=vol_b, volume=3e5)
    last = ba[-1]["bar_date"]
    asof = datetime.combine(last, time(21), UTC)
    hs = [Holding("AAA", AssetType.LISTED_EQUITY, 20_000, "USD", "NYSE"),
          Holding("BBB", AssetType.LISTED_EQUITY, 10_000, ccy_b, "NASDAQ")]
    if cash:
        hs.append(Holding("USD", AssetType.CASH, cash, "USD", "CASH"))
    md = {hs[0].stable_key: MarketData("AAA", ba[-1]["close"], bid=ba[-1]["close"] * 0.999,
                                       ask=ba[-1]["close"] * 1.001, timestamp=asof),
          hs[1].stable_key: MarketData("BBB", bb[-1]["close"], bid=bb[-1]["close"] * 0.998,
                                       ask=bb[-1]["close"] * 1.002, timestamp=asof)}
    bars = {data_key(hs[0]): ba, data_key(hs[1]): bb}
    return hs, md, bars, asof


def ev(hs, md, bars, asof, balance=0.0, cfg=CFG, ex=None, rate=0.2, priors=None, policy=None, **kw):
    return CandidateEngine(cfg, priors).evaluate(
        "t", hs, Loan(balance), policy or Policy.default(), md, bars=bars,
        execution_policy=ex or LiquidationExecutionPolicy(), interest_policy=InterestPolicy(rate),
        as_of=asof, seed=11, **kw)


def test_thresholds_are_ordered_and_ceiling_caps_only_capacity() -> None:
    hs, md, bars, asof = setup()
    r = ev(hs, md, bars, asof)
    t = r.thresholds
    assert t["initial_capacity"] <= t["initial_capacity_uncapped"] <= t["early_warning"] <= t["margin_call"]
    assert t["margin_call"] <= t["forced_recovery"]
    tight = Policy(base_ltv={AssetType.LISTED_EQUITY: 0.2, AssetType.CASH: 0.5})
    r2 = ev(hs, md, bars, asof, policy=tight)
    assert r2.approved_credit_limit < r.approved_credit_limit
    assert r2.thresholds["margin_call"] == pytest.approx(t["margin_call"])  # risk levels not set by the client


def test_pledged_cash_counts_at_par() -> None:
    hs, md, bars, asof = setup(cash=0.0)
    without = ev(hs, md, bars, asof).thresholds["margin_call"]
    hs2, *_ = setup(cash=100_000.0)
    with_cash = ev(hs2, md, bars, asof).thresholds["margin_call"]
    assert with_cash - without == pytest.approx(100_000.0, rel=0.02)


def test_more_volatility_less_capacity() -> None:
    lo = ev(*setup(vol_b=0.015)).threshold_ltv["margin_call"]
    hi = ev(*setup(vol_b=0.06)).threshold_ltv["margin_call"]
    assert hi < lo


def test_short_history_is_penalised_and_pooled() -> None:
    hs, md, bars, asof = setup()
    key = data_key(hs[1])
    full = ev(hs, md, bars, asof)
    short_bars = dict(bars, **{key: bars[key][-45:]})
    short = ev(hs, md, short_bars, asof)
    inst = {i["key"]: i for i in short.instruments}[key]
    assert inst["effective_observations"] < 45
    assert inst["empirical_tail_weight"] < 1
    assert inst["daily_vol_used"] > inst["daily_vol_posterior"]       # credible bound
    f_inst = {i["key"]: i for i in full.instruments}[key]
    # The uncertainty margin is wider for the short history.
    assert inst["daily_vol_used"] / inst["daily_vol_own"] > f_inst["daily_vol_used"] / f_inst["daily_vol_own"]
    prior = Prior(mu_log_vol=float(np.log(0.03)), tau2=0.05, n_instruments=20, source="test")
    pooled = ev(hs, md, short_bars, asof, priors={"US": prior})
    p_inst = {i["key"]: i for i in pooled.instruments}[key]
    assert 0 < p_inst["pooling_weight_own"] < 1
    too_short = dict(bars, **{key: bars[key][-10:]})
    r = ev(hs, md, too_short, asof)
    assert not next(a for a in r.asset_results if a.asset_id == "BBB").lending_value_eligible


def test_missing_price_is_data_review_without_sale() -> None:
    hs, md, bars, asof = setup()
    md = {k: v for k, v in md.items() if "BBB" not in k}
    r = ev(hs, md, bars, asof, balance=5_000_000.0)
    assert r.data_review_required
    assert r.margin_state == MarginState.RESTRICT_NEW_BORROWING
    assert r.recovery_plan is None


def test_cross_currency_is_restricted() -> None:
    hs, md, bars, asof = setup(ccy_b="EUR")
    r = ev(hs, md, bars, asof)
    assert r.restriction == "cross_currency_collateral"
    assert r.approved_credit_limit == 0
    assert r.margin_state != MarginState.SAFE


def test_stable_identity_separates_venues() -> None:
    hs, md, bars, asof = setup()
    twin = Holding("AAA", AssetType.LISTED_EQUITY, 1_000, "USD", "NASDAQ")
    md2 = dict(md)
    md2[twin.stable_key] = MarketData("AAA", 50.0, bid=49.9, ask=50.1, timestamp=asof)
    r = ev([*hs, twin], md2, bars, asof)   # no NASDAQ:AAA bars stored
    a = next(x for x in r.asset_results if x.instrument_key == twin.stable_key)
    assert a.data_key == "NASDAQ:AAA:USD:LISTED_EQUITY"
    assert not a.lending_value_eligible


def test_horizon_interest_and_settlement_lower_thresholds() -> None:
    hs, md, bars, asof = setup()
    base = ev(hs, md, bars, asof).thresholds["margin_call"]
    longer = ev(hs, md, bars, asof, ex=LiquidationExecutionPolicy(margin_call_grace_observations=5)).thresholds
    assert longer["margin_call"] < base
    dearer = ev(hs, md, bars, asof, rate=0.9).thresholds["margin_call"]
    assert dearer < base
    slower = ev(hs, md, bars, asof, ex=LiquidationExecutionPolicy(settlement_delay_observations=5)).thresholds
    assert slower["margin_call"] < base


def test_reproducible() -> None:
    hs, md, bars, asof = setup()
    assert ev(hs, md, bars, asof).thresholds == ev(hs, md, bars, asof).thresholds


def test_margin_call_plan_is_partial_and_restores() -> None:
    hs, md, bars, asof = setup()
    t = ev(hs, md, bars, asof).thresholds
    bal = (t["margin_call"] + t["forced_recovery"]) / 2
    r = ev(hs, md, bars, asof, balance=bal)
    assert r.margin_state in {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}
    plan = r.recovery_plan
    assert plan is not None and plan["action"] == "restore_coverage"
    assert plan["restores_safety_standard"]
    sold = sum(o["expected_gross_proceeds"] for o in plan["orders"])
    assert 0 < sold < r.securities_value * 0.9   # partial, not a full liquidation
    assert r.cure_amount == pytest.approx(bal - t["restoration_target"])


def test_no_lookahead_in_thresholds() -> None:
    hs, md, bars, asof = setup()
    extended = {k: [*v, *[dict(v[-1], bar_date=v[-1]["bar_date"] + timedelta(days=i), close=1.0)
                         for i in range(1, 30)]] for k, v in bars.items()}
    assert ev(hs, md, extended, asof).thresholds == ev(hs, md, bars, asof).thresholds


def _cohort_inputs():
    ba = make_bars(1, n=560, crash={430, 431, 433})
    bb = make_bars(2, n=560, vol=0.03, volume=3e5, crash={430, 432})
    hs = [Holding("AAA", AssetType.LISTED_EQUITY, 20_000, "USD", "NYSE"),
          Holding("BBB", AssetType.LISTED_EQUITY, 10_000, "USD", "NASDAQ"),
          Holding("USD", AssetType.CASH, 50_000, "USD", "CASH")]
    return hs, {data_key(hs[0]): ba, data_key(hs[1]): bb}, [b["bar_date"] for b in ba]


def test_lifecycle_matched_exposure_and_common_random_numbers() -> None:
    hs, bars, dates = _cohort_inputs()
    c = run_cohort(holdings=hs, bars=bars, origination=dates[400], term_sessions=63, seed=3,
                   config=CandidateConfig(n_paths=400), workout_sessions=40)
    principals = {v["principal"] for v in c["variants"].values()}
    assert len(principals) == 1                        # matched initial exposure
    assert set(c["variants"]) == {v.name for v in DEFAULT_VARIANTS}
    again = run_cohort(holdings=hs, bars=bars, origination=dates[400], term_sessions=63, seed=3,
                       config=CandidateConfig(n_paths=400), workout_sessions=40)
    assert again == c                                  # reproducible
    # The crash forces interventions in this cohort.
    assert any(v["liquidation_episodes"] or v["margin_calls"] for v in c["variants"].values())
    for v in c["variants"].values():
        assert v["interest_accrued"] > 0
        assert v["lender_loss"] >= 0


def test_lifecycle_aggregate_metrics() -> None:
    hs, bars, dates = _cohort_inputs()
    cohorts = [run_cohort(holdings=hs, bars=bars, origination=dates[i], term_sessions=40, seed=i,
                          config=CandidateConfig(n_paths=300), workout_sessions=30) for i in (380, 400)]
    agg = aggregate(cohorts)
    assert agg["cohorts"] == 2
    for m in agg["variants"].values():
        for k in ("loss_rate", "liquidation_frequency", "borrower_execution_cost_share", "time_restricted",
                  "mean_capacity_share_of_principal"):
            assert m[k] >= 0


def test_candidate_config_marked_unapproved() -> None:
    assert "not approved" in CFG.to_dict()["status"]
    assert replace(CFG, restoration="early_warning").restoration == "early_warning"
