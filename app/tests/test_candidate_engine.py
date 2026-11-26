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
        for k in ("expected_loss", "loss_probability", "liquidation_frequency", "borrower_execution_cost_share",
                  "time_restricted", "mean_capacity_share_of_principal", "unnecessary_liquidation_share"):
            assert m[k] >= 0


def test_candidate_config_marked_unapproved() -> None:
    assert "not approved" in CFG.to_dict()["status"]
    assert replace(CFG, restoration="early_warning").restoration == "early_warning"


def test_daily_bar_quotes_without_bid_are_usable_and_flagged() -> None:
    hs, md, bars, asof = setup()
    nobid = {k: replace(v, bid=None, ask=None) for k, v in md.items()}
    r = ev(hs, nobid, bars, asof)
    assert r.approved_credit_limit > 0
    assert all(any("bid not observed" in x for x in i["reasons"]) for i in r.instruments)


# ------------------------------------------------- review corrections (27 Nov)

def _sim(level: np.ndarray, growth: np.ndarray | None = None):
    from app.candidate.engine import _Sim

    _m, d1, n = level.shape
    return _Sim(["k"] * n, np.ones(n), np.ones(n), np.zeros(n), np.zeros(n), level, np.full_like(level, 0.01),
                np.ones_like(level), growth if growth is not None else np.ones(d1 + 5), d1 - 1, 1, True)


def test_first_passage_catches_intra_horizon_breach() -> None:
    # Half the paths dip 30% on session 2 and fully recover by session 5.
    m = 100
    path = np.ones((m, 6, 1))
    path[: m // 2, 2, 0] = 0.7
    sim = _sim(path)
    eng = CandidateEngine(CFG)
    fp = eng._first_passage_level(sim, np.array([1.0]), 0.0, 1.0, 5, 0.95)
    terminal = float(np.quantile(path[:, 5, 0], 0.05))
    assert terminal == pytest.approx(1.0)
    assert fp == pytest.approx(0.7)


def test_capacity_falls_with_headroom_horizon() -> None:
    hs, md, bars, asof = setup()
    caps = [ev(hs, md, bars, asof, cfg=replace(CFG, headroom_sessions=h)).thresholds["initial_capacity_uncapped"]
            for h in (1, 5, 20)]
    assert caps[0] >= caps[1] >= caps[2]


def test_obligation_growth_follows_contract() -> None:
    from app.candidate.engine import obligation_growth

    t0 = datetime(2026, 1, 1, 21, tzinfo=UTC)
    simple = obligation_growth(Loan(100.0), InterestPolicy(0.365), t0, 10)
    assert simple[5] == pytest.approx(1 + 0.365 * 7 / 365)
    comp = obligation_growth(Loan(100.0), InterestPolicy(0.365, accrual_frequency="daily", compounding="compound",
                                                         schedule_anchor=t0), t0, 10)
    assert comp[10] > simple[10]
    a360 = obligation_growth(Loan(100.0), InterestPolicy(0.365, day_count_convention="actual_360"), t0, 10)
    assert a360[5] > simple[5]
    # Accrued interest does not earn simple interest: growth relative to the balance is lower.
    with_accrued = obligation_growth(Loan(100.0, accrued_interest=20.0), InterestPolicy(0.365), t0, 10)
    assert with_accrued[5] < simple[5]
    client = obligation_growth(Loan(100.0), InterestPolicy(0.365, interest_accrual_mode="client_supplied"), t0, 10)
    assert client[5] == pytest.approx(simple[5])


def test_stressed_liquidity_lowers_thresholds_and_price_limits_apply() -> None:
    hs, md, bars, asof = setup()
    # Institutional size: the exit takes many sessions, so liquidity matters.
    hs = [replace(h, quantity=h.quantity * 60) if h.asset_type != AssetType.CASH else h for h in hs]
    calm = ev(hs, md, bars, asof, cfg=replace(CFG, volume_drawdown_elasticity=0.0, liquidity_noise_sd=0.01))
    stressed = ev(hs, md, bars, asof, cfg=replace(CFG, volume_drawdown_elasticity=4.0, liquidity_noise_sd=0.8))
    assert stressed.thresholds["margin_call"] < calm.thresholds["margin_call"]
    assert stressed.thresholds["execution_sessions_p95_mc"] >= calm.thresholds["execution_sessions_p95_mc"]
    ngx = [Holding("ZEN", AssetType.LISTED_EQUITY, 200_000, "NGN", "NGX")]
    b = make_bars(5, vol=0.06, volume=5e5)
    mdn = {ngx[0].stable_key: MarketData("ZEN", b[-1]["close"], timestamp=asof)}
    r = CandidateEngine(CFG).evaluate("n", ngx, Loan(0.0, currency="NGN"), Policy.default(), mdn,
                                      bars={data_key(ngx[0]): b}, interest_policy=InterestPolicy(0.25),
                                      as_of=asof, seed=3)
    assert r.parameters["scenario_liquidity"]["limit_down_sessions"] > 0
    assert r.parameters["settlement_sessions"] == 2


def test_restoration_never_gives_lending_value_to_excluded_assets() -> None:
    hs, md, bars, asof = setup()
    key = data_key(hs[1])
    short = dict(bars, **{key: bars[key][-15:]})          # BBB: sellable, no lending value
    r = ev(hs, md, short, asof)
    bbb = next(a for a in r.asset_results if a.asset_id == "BBB")
    assert bbb.eligible and not bbb.lending_value_eligible
    t = r.thresholds
    bal = (t["margin_call"] + t["forced_recovery"]) / 2
    r2 = ev(hs, md, short, asof, balance=bal)
    plan = r2.recovery_plan
    assert plan is not None
    # Whatever is sold, the restored state is judged on lendable collateral only:
    # residual obligation must be within AAA + cash capacity alone.
    a_only = ev([hs[0], hs[2]], md, short, asof).thresholds["initial_capacity_uncapped"]
    assert plan["projected_residual_obligation"] <= a_only * 1.05


def test_stale_material_data_blocks_borrowing_and_is_not_a_sale_trigger() -> None:
    hs, md, bars, asof = setup()
    key = next(k for k in md if "AAA" in k)
    stale = dict(md)
    stale[key] = replace(md[key], timestamp=asof - timedelta(days=3))   # 3 calendar / >1 business day
    fresh = ev(hs, md, bars, asof)
    bal = fresh.thresholds["initial_capacity"] * 0.5
    r = ev(hs, stale, bars, asof, balance=bal)
    assert r.data_status == "degraded"
    assert r.approved_credit_limit <= bal + 1e-6 and r.current_available_credit == 0
    assert r.margin_state == MarginState.RESTRICT_NEW_BORROWING
    # A balance that breaches only because AAA lost lending value is data deterioration.
    bal2 = (fresh.thresholds["initial_capacity"] + fresh.thresholds["early_warning"]) / 2
    r2 = ev(hs, stale, bars, asof, balance=bal2)
    if r2.thresholds["margin_call"] < bal2:
        assert r2.deterioration == "data" and r2.recovery_plan is None


def test_planner_uses_candidate_execution_model() -> None:
    hs, md, bars, asof = setup()
    t = ev(hs, md, bars, asof).thresholds
    r = ev(hs, md, bars, asof, balance=(t["margin_call"] + t["forced_recovery"]) / 2)
    est = {i["key"]: i for i in r.instruments}
    for o in r.recovery_plan["orders"]:
        a = next(x for x in r.asset_results if x.instrument_key == o["instrument_key"])
        e = est[a.data_key]
        ref_bid = md[o["instrument_key"]].bid
        impact = CFG.impact_coefficient * np.sqrt(a.participation_rate) * e["daily_vol_used"]
        extra = e["spread"] / 2 * (CFG.spread_stress_multiplier - 1)
        expected_unit = ref_bid * (1 - extra - impact) * (1 - LiquidationExecutionPolicy().execution_cost_rate)
        assert o["expected_net_proceeds"] / o["quantity"] == pytest.approx(expected_unit, rel=2e-3)
        assert o["participation_rate"] == pytest.approx(a.participation_rate)


@pytest.mark.parametrize("seed", range(6))
def test_thresholds_coherent_across_structures(seed: int) -> None:
    rng = np.random.default_rng(seed)
    exch = ["NYSE", "NGX"][seed % 2]
    ccy = "USD" if exch == "NYSE" else "NGN"
    n = int(rng.integers(1, 4))
    hs, bars, md = [], {}, {}
    for j in range(n):
        b = make_bars(100 + seed * 10 + j, vol=float(rng.uniform(0.01, 0.05)), volume=float(rng.uniform(1e4, 1e6)))
        h = Holding(f"S{j}", AssetType.LISTED_EQUITY, float(rng.uniform(1e3, 2e5)), ccy, exch)
        hs.append(h)
        bars[data_key(h)] = b
        md[h.stable_key] = MarketData(h.asset_id, b[-1]["close"], timestamp=datetime.combine(b[-1]["bar_date"],
                                                                                            time(21), UTC))
    if rng.random() < 0.5:
        hs.append(Holding(ccy, AssetType.CASH, float(rng.uniform(0, 1e6)), ccy, "CASH"))
    asof = datetime.combine(max(b[-1]["bar_date"] for b in bars.values()), time(21), UTC)
    ex = LiquidationExecutionPolicy(margin_call_grace_observations=int(rng.integers(0, 4)),
                                    max_participation_rate=float(rng.uniform(0.02, 0.1)))
    r = CandidateEngine(CFG).evaluate("p", hs, Loan(0.0, currency=ccy), Policy.default(), md, bars=bars,
                                      execution_policy=ex, interest_policy=InterestPolicy(float(rng.uniform(0, .4))),
                                      as_of=asof, seed=seed)
    t = r.thresholds
    assert 0 <= t["initial_capacity"] <= t["initial_capacity_uncapped"] + 1e-6
    assert t["initial_capacity_uncapped"] <= t["early_warning"] + 1e-6
    assert t["early_warning"] <= t["margin_call"] + 1e-6 <= t["forced_recovery"] + 2e-6
    assert t["margin_call"] <= r.portfolio_market_value + 1e-6
    big = [replace(h, quantity=h.quantity * 50) if h.asset_type != AssetType.CASH else h for h in hs]
    rb = CandidateEngine(CFG).evaluate("p", big, Loan(0.0, currency=ccy), Policy.default(), md, bars=bars,
                                       execution_policy=ex, interest_policy=InterestPolicy(0.1), as_of=asof, seed=seed)
    sec = r.securities_value or 1
    assert (rb.thresholds["margin_call"] - rb.cash_value) / (rb.securities_value or 1) <= \
        (t["margin_call"] - r.cash_value) / sec + 1e-6


def test_lifecycle_sells_through_the_planner() -> None:
    hs, bars, dates = _cohort_inputs()
    c = run_cohort(holdings=hs, bars=bars, origination=dates[400], term_sessions=63, seed=3,
                   config=CandidateConfig(n_paths=300), workout_sessions=40)
    liq = [e for v in c["variants"].values() for e in v["events"] if e["event"] == "liquidation_decided"]
    assert liq
    for e in liq:
        assert e["orders"]
        assert all(q <= 20_000 + 1e-6 for q in e["orders"].values())


def test_suspension_is_economic_not_data_deterioration() -> None:
    hs, md, bars, asof = setup()
    key = data_key(hs[0])
    k_md = next(k for k in md if "AAA" in k)
    fresh = ev(hs, md, bars, asof)
    # AAA (the largest holding) stops trading: last bar 6 business days ago.
    susp = dict(bars, **{key: bars[key][:-6]})
    md2 = dict(md)
    md2[k_md] = replace(md[k_md], timestamp=asof - timedelta(days=8))
    bal = (fresh.thresholds["initial_capacity"] + fresh.thresholds["margin_call"]) / 2
    r = ev(hs, md2, susp, asof, balance=bal)
    a = next(x for x in r.asset_results if x.asset_id == "AAA")
    assert a.data_status == "not_trading" and not a.lending_value_eligible
    assert r.data_status == "ok" and not r.data_review_required
    assert r.deterioration in {None, "economic"}
    if r.margin_state in {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}:
        assert r.deterioration == "economic" and r.recovery_plan is not None


# ------------------------------------------- lifecycle regression tests (27 Nov)

def _crash_cohort(**kw):
    hs, bars, dates = _cohort_inputs()
    return run_cohort(holdings=hs, bars=bars, origination=dates[400], term_sessions=63, seed=3,
                      config=CandidateConfig(n_paths=300), **{"workout_sessions": 40, **kw})


def test_no_same_session_execution() -> None:
    c = _crash_cohort()
    seen = 0
    for v in c["variants"].values():
        decided = {e["session"] for e in v["events"] if e["event"] == "liquidation_decided"}
        for e in v["events"]:
            if e["event"] == "first_fill":
                seen += 1
                assert e["session"] > e["episode"] and e["episode"] in decided
    assert seen


def test_market_state_is_isolated_per_pass() -> None:
    from app.candidate.lifecycle import _Feed

    hs, bars, dates = _cohort_inputs()
    keys = {h.stable_key: data_key(h) for h in hs if h.asset_type != AssetType.CASH}
    series = {dk: {b["bar_date"]: b for b in bars[dk]} for dk in keys.values()}
    a, b = _Feed(hs, keys, series), _Feed(hs, keys, series)
    a.market(dates[-1])
    md = b.market(dates[100])
    assert all(m.timestamp.date() == dates[100] for m in md.values())


def test_limit_down_close_does_not_fill() -> None:
    from app.candidate.lifecycle import limit_locked

    assert limit_locked(100.0, 90.0, 0.10)
    assert not limit_locked(100.0, 91.0, 0.10)
    assert not limit_locked(100.0, 50.0, None)


def test_default_at_maturity_and_censoring() -> None:
    hs, bars, dates = _cohort_inputs()
    thin = {k: [dict(b, volume=1.0) if b["bar_date"] > dates[400] else b for b in v] for k, v in bars.items()}
    c = run_cohort(holdings=hs, bars=thin, origination=dates[400], term_sessions=30, seed=1, maturity="default",
                   config=CandidateConfig(n_paths=200), workout_sessions=3)
    for v in c["variants"].values():
        assert v["defaulted_at_maturity"]
        assert v["censored"] and v["lender_loss"] is None and v["outstanding_debt"] > 0
        assert v["residual_collateral_estimate"] > 0


def test_recovery_counts_only_settled_proceeds() -> None:
    c = _crash_cohort(maturity="default")
    for v in c["variants"].values():
        assert v["realized_recovery"] <= v["executed_gross"] - v["execution_costs"] + 1e-6


def test_partial_repayment_scenario() -> None:
    c = _crash_cohort(maturity="partial:0.5")
    for v in c["variants"].values():
        if v["borrower_repayment"] > 0:
            assert not v["repaid_at_maturity"] and v["defaulted_at_maturity"]


def test_research_switches_behave() -> None:
    hs, md, bars, asof = setup()
    base = ev(hs, md, bars, asof).thresholds
    adaptive_h = ev(hs, md, bars, asof, cfg=replace(CFG, headroom_mode="adaptive")).thresholds
    assert adaptive_h["headroom_sessions"] >= base["headroom_sessions"]
    assert adaptive_h["initial_capacity_uncapped"] <= base["initial_capacity_uncapped"] + 1e-6
    conc = ev(hs[:1] + hs[2:], md, bars, asof, cfg=replace(CFG, concentration_control=True)).thresholds
    plain = ev(hs[:1] + hs[2:], md, bars, asof).thresholds
    assert conc["concentration_addon"] > 0 and conc["initial_capacity_uncapped"] < plain["initial_capacity_uncapped"]
    lr = ev(hs, md, bars, asof, cfg=replace(CFG, restoration_volatility="long_run")).thresholds
    assert lr["restoration_uncapped"] <= lr["early_warning"] + 1e-6


def test_suspended_instrument_delayed_value_not_lendable_not_sellable() -> None:
    hs, md, bars, asof = setup()
    key = data_key(hs[0])
    k_md = next(k for k in md if "AAA" in k)
    susp = dict(bars, **{key: bars[key][:-6]})
    md2 = dict(md)
    md2[k_md] = replace(md[k_md], timestamp=asof - timedelta(days=8))
    zero = ev(hs, md2, susp, asof).thresholds
    delayed = ev(hs, md2, susp, asof, cfg=replace(CFG, suspension_value="delayed")).thresholds
    assert delayed["delayed_recovery_value"] > 0
    assert delayed["margin_call"] > zero["margin_call"]                     # economic value recognised
    assert delayed["initial_capacity"] == pytest.approx(zero["initial_capacity"], rel=1e-6)  # not lendable
    r = ev(hs, md2, susp, asof, cfg=replace(CFG, suspension_value="delayed"))
    assert not next(a for a in r.asset_results if a.asset_id == "AAA").eligible        # not sellable now
    old = dict(bars, **{key: bars[key][:-80]})
    md3 = dict(md)
    md3[k_md] = replace(md[k_md], timestamp=asof - timedelta(days=5))
    imp = ev(hs, md3, old, asof, cfg=replace(CFG, suspension_value="delayed"))
    assert "impaired" in next(a for a in imp.asset_results if a.asset_id == "AAA").notes
    assert imp.thresholds["delayed_recovery_value"] == 0
