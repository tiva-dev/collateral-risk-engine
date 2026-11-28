"""CRI Optimizer: recovery-conditional frontier, appetite-to-capacity, allocation."""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise

import numpy as np
import pytest

from app.candidate import CandidateEngine
from app.candidate.rc import rc_config
from app.core.models import Loan, Policy
from app.credit.interest import InterestPolicy
from app.liquidation.policy import LiquidationExecutionPolicy
from app.optimizer import (
    PRESETS,
    LendingEconomics,
    Opportunity,
    RiskAppetite,
    allocate,
    capacity_for_appetite,
    frontier,
    shortfall_metrics,
)
from app.tests.test_candidate_engine import setup

CFG = replace(rc_config(), n_paths=600)


@pytest.fixture(scope="module")
def evals():
    hs, md, bars, asof = setup()
    out = []
    for seed in (11, 12, 13):
        out.append(CandidateEngine(CFG).evaluate(
            "t", hs, Loan(0.0), Policy.default(), md, bars=bars, execution_policy=LiquidationExecutionPolicy(),
            interest_policy=InterestPolicy(0.2), as_of=asof, seed=seed))
    return out


def test_frontier_is_monotone_and_labelled_conditional(evals) -> None:
    f = frontier(evals, n_points=25)
    assert f.view == "recovery_conditional" and "not a lifetime" in f.notes[0]
    assert f.model_version.startswith("candidate") and f.replicas == 3
    p = [pt["p_shortfall"] for pt in f.points]
    el = [pt["expected_shortfall"] for pt in f.points]
    assert all(b >= a - 1e-12 for a, b in pairwise(p))
    assert all(b >= a - 1e-9 for a, b in pairwise(el))
    assert f.points[0]["p_shortfall"] == 0.0 and f.points[-1]["p_shortfall"] > 0.5
    assert f.points[10]["p_shortfall_mc_sd"] is not None


def test_shortfall_metrics_exact_on_known_distribution() -> None:
    c = np.linspace(50, 150, 101)      # proceeds uniform on [50, 150]
    m = shortfall_metrics(c, 100.0, alpha=0.9)
    assert m["p_shortfall"] == pytest.approx(0.5, abs=0.01)
    assert m["expected_shortfall"] == pytest.approx(12.5, rel=0.03)   # E[(100-C)+] = 50^2/(2*100)
    assert m["cvar"] == pytest.approx(45.0, rel=0.03)                 # worst 10% of losses: 40..50


def test_appetite_capacity_orders_presets_and_respects_limits(evals) -> None:
    caps = {k: capacity_for_appetite(evals, a)["risk_supported_capacity"] for k, a in PRESETS.items()}
    assert 0 < caps["conservative"] <= caps["balanced"] <= caps["aggressive"]
    r = capacity_for_appetite(evals, PRESETS["balanced"])
    m = r["metrics_at_capacity"]
    a = PRESETS["balanced"]
    assert m["p_shortfall"] <= a.max_p_shortfall + 1e-9
    assert m["expected_shortfall_rate"] <= a.max_expected_shortfall_rate + 1e-9
    assert r["mc_sd"] is not None and r["mc_range"][0] * 0.95 <= r["risk_supported_capacity"] <= r["mc_range"][1] * 1.05
    lim = capacity_for_appetite(evals, PRESETS["balanced"], institutional_limit=r["risk_supported_capacity"] * 0.5)
    assert lim["binding"] == "institutional_limit"
    assert lim["capacity"] == pytest.approx(r["risk_supported_capacity"] * 0.5, rel=0.05)
    assert lim["risk_supported_capacity"] == pytest.approx(r["risk_supported_capacity"], rel=0.05)  # reported apart


def test_economics_requires_explicit_intervention_assumption(evals) -> None:
    f0 = frontier(evals[:1], n_points=5, econ=LendingEconomics(0.18, 0.10))
    assert f0.points[2]["economics"]["expected_credit_cost"] is None
    f1 = frontier(evals[:1], n_points=5, econ=LendingEconomics(0.18, 0.10, intervention_probability=0.1))
    e = f1.points[2]["economics"]
    assert e["expected_credit_cost"] >= 0 and "caller assumption" in e["assumptions"]["intervention_probability_source"]


def test_capacity_beyond_rc1_is_reported_not_applied(evals) -> None:
    r = capacity_for_appetite(evals, RiskAppetite(max_p_shortfall=0.05))
    assert r["rc1_approved_credit_limit"] == pytest.approx(evals[0].approved_credit_limit)


def test_allocation_respects_budget_and_concentration() -> None:
    def curve(margin: float, risk: float):
        return lambda b: margin * b - risk * b * b
    opps = [Opportunity("a", "tech", 100.0, curve(0.08, 0.0004)),
            Opportunity("b", "tech", 100.0, curve(0.07, 0.0002)),
            Opportunity("c", "banks", 100.0, curve(0.05, 0.0001)),
            Opportunity("d", "banks", 100.0, curve(-0.01, 0.0))]          # never worth lending
    r = allocate(opps, total_capital=150.0, max_share_per_borrower=0.5, max_share_per_group={"tech": 0.6})
    al = r["allocations"]
    assert r["status"] == "optimal" and r["total"] <= 150 + 1e-6
    assert al["d"] == pytest.approx(0.0, abs=1e-6)
    assert al["a"] + al["b"] <= 0.6 * 150 + 1e-6 and max(al.values()) <= 75 + 1e-6


def test_minimum_return_is_enforced_or_refused(evals) -> None:
    a = RiskAppetite(max_p_shortfall=0.05, min_risk_adjusted_return=0.15)
    with pytest.raises(ValueError):
        capacity_for_appetite(evals, a)
    with pytest.raises(ValueError):
        capacity_for_appetite(evals, a, econ=LendingEconomics(0.18, 0.10))      # no intervention assumption
    free = capacity_for_appetite(evals, RiskAppetite(max_p_shortfall=0.05))["risk_supported_capacity"]
    econ = LendingEconomics(0.18, 0.10, capital_ratio=0.10, intervention_probability=0.25)
    tight = capacity_for_appetite(evals, a, econ=econ)
    assert tight["risk_supported_capacity"] <= free + 1e-6
    m = tight["metrics_at_capacity"]
    if m is not None:
        from app.optimizer import economics
        assert economics(m, econ)["raroc"] >= 0.15 - 1e-6


# ------------------------------------------------------------- M4 lifecycle

def _cohorts(seed: int = 1):
    from datetime import date as _d
    rng = np.random.default_rng(seed)
    out = []
    for q in range(16):
        orig = _d(2019 + q // 4, 1 + 3 * (q % 4), 15)
        shock = rng.normal()                      # shared quarter shock
        for scale in (0.75, 1.0, 1.25, 1.5, 2.0):
            for _ in range(8):
                p = 100.0
                lossp = max(0.0, 0.002 * (scale ** 4) + 0.01 * max(shock, 0) * scale ** 3 + rng.normal(0, 0.002))
                loss = p * lossp if rng.random() < min(1.0, 0.05 * scale ** 2) else 0.0
                out.append({"origination": orig.isoformat(), "labels": {"scale_of_rc_credit": scale},
                            "variants": {"rc1": {"principal": p, "lender_loss": loss, "censored": False,
                                                 "outstanding_debt": 0.0, "sessions": 63,
                                                 "interest_accrued": p * 0.10 * 0.25,
                                                 "liquidation_episodes": int(loss > 0), "margin_calls": int(loss > 0),
                                                 "execution_costs": 0.1 * loss}}})
    return out


def test_lifecycle_frontier_economics_and_appetite() -> None:
    from app.optimizer.lifecycle_frontier import (
        LifecycleEconomics,
        LifetimeAppetite,
        frontier,
        supported_scale,
    )
    econ = LifecycleEconomics(funding_rate=0.05)
    pts = frontier(_cohorts(), econ, reps=100)
    assert [p["scale_of_rc1_credit"] for p in pts] == [0.75, 1.0, 1.25, 1.5, 2.0]
    el = [p["expected_loss"] for p in pts]
    assert el[0] < el[-1]
    for p in pts:
        lo, hi = p["ci95"]["expected_loss"]
        assert lo <= p["expected_loss"] <= hi and p["clusters"] == 16
        assert p["economic_capital"] >= 0.02 and p["interest_income"] == pytest.approx(0.025)
        assert p["risk_adjusted_profit_per_rc1_credit"] == pytest.approx(p["scale_of_rc1_credit"] * p["risk_adjusted_profit"])
    s = supported_scale(pts, LifetimeAppetite(max_expected_loss=0.002))
    assert s["max_scale_robust_upper95"] is None or s["max_scale_robust_upper95"] <= s["max_scale_point_estimate"]
    lim = supported_scale(pts, LifetimeAppetite(max_expected_loss=1.0), institutional_max_scale=1.0)
    assert lim["supported_scale"] == 1.0 and lim["binding"] == "institutional_limit"


def test_lifecycle_inputs_are_validated() -> None:
    from app.optimizer.lifecycle_frontier import (
        LifecycleEconomics,
        LifetimeAppetite,
        frontier,
        supported_scale,
    )
    for bad in ({"funding_rate": -0.01}, {"funding_rate": float("nan")}, {"funding_rate": 0.05, "tail": 1.0}):
        with pytest.raises(ValueError):
            LifecycleEconomics(**bad)
    with pytest.raises(ValueError):
        LifetimeAppetite(max_expected_loss=-1)
    with pytest.raises(ValueError):
        supported_scale([], LifetimeAppetite())
    assert frontier([{"labels": {}, "variants": {}}], LifecycleEconomics(0.05)) == []


def test_appetite_parameters_and_edge_cases_are_validated() -> None:
    import numpy as _np

    from app.optimizer.frontier import _proceeds_sample, shortfall_metrics
    with pytest.raises(ValueError):
        _proceeds_sample([1.0, 2.0])                       # missing simulation
    m = shortfall_metrics(_np.array([0.0, 0.0, 0.0]), 0.0)  # zero balance
    assert m["expected_shortfall_rate"] == 0.0 and m["p_shortfall"] == 0.0
    m2 = shortfall_metrics(_np.zeros(100), 50.0)            # worthless collateral
    assert m2["p_shortfall"] == 1.0 and m2["expected_shortfall"] == 50.0


def test_quantile_view_matches_raw_samples(monkeypatch) -> None:
    """Review finding 3: the frontier's quantile interpolation against RC1's own raw
    Monte Carlo recovery samples (captured where the engine summarises them)."""
    import app.candidate.engine as engine_mod
    from app.optimizer.frontier import _proceeds_sample
    hs, md, bars, asof = setup()
    orig = np.quantile
    cap: dict[str, np.ndarray] = {}

    def spy(a, q, *args, **kw):
        if np.ndim(q) == 1 and len(q) == 201:
            cap["raw"] = np.array(a, float)
        return orig(a, q, *args, **kw)
    monkeypatch.setattr(engine_mod.np, "quantile", spy)
    e = CandidateEngine(replace(rc_config(), n_paths=2000)).evaluate(
        "t", hs, Loan(0.0), Policy.default(), md, bars=bars, execution_policy=LiquidationExecutionPolicy(),
        interest_policy=InterestPolicy(0.2), as_of=asof, seed=11)
    monkeypatch.setattr(engine_mod.np, "quantile", orig)
    raw, interp = cap["raw"], _proceeds_sample(e.coverage_grace_quantiles)
    for b in np.quantile(raw, [0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5]):
        x, y = shortfall_metrics(raw, b), shortfall_metrics(interp, b)
        assert abs(x["p_shortfall"] - y["p_shortfall"]) <= 0.005 + 1e-9
        assert abs(x["expected_shortfall_rate"] - y["expected_shortfall_rate"]) <= 0.001
        assert abs(x["cvar_rate"] - y["cvar_rate"]) <= 0.01            # alpha 0.95 default
    with pytest.raises(ValueError):
        RiskAppetite(alpha=0.99)
    with pytest.raises(ValueError):
        frontier([e], alpha=0.99)


def test_execution_cost_is_principal_weighted_and_split() -> None:
    """A loan with tiny credit whose collateral is worked out must not dominate the cost share."""
    from app.optimizer.lifecycle_frontier import LifecycleEconomics, point
    rows = [{"principal": 1_000_000.0, "lender_loss": 0.0, "sessions": 63, "interest_accrued": 10_000.0,
             "execution_costs": 10_000.0, "execution_costs_intervention": 4_000.0,
             "execution_costs_workout": 6_000.0, "liquidation_episodes": 1},
            {"principal": 1_000.0, "lender_loss": 100.0, "sessions": 63, "interest_accrued": 10.0,
             "execution_costs": 5_000.0, "execution_costs_intervention": 0.0,
             "execution_costs_workout": 5_000.0, "liquidation_episodes": 0}]
    p = point(rows, LifecycleEconomics(funding_rate=0.05))
    assert abs(p["borrower_execution_cost"] - 15_000 / 1_001_000) < 1e-12     # not mean(1%, 500%)
    assert abs(p["execution_cost_intervention"] + p["execution_cost_maturity_workout"]
               - p["borrower_execution_cost"]) < 1e-12
    assert abs(p["expected_loss_principal_weighted"] - 100 / 1_001_000) < 1e-12
    assert abs(p["expected_loss"] - 0.05) < 1e-12                               # per-loan mean kept
    legacy = [{k: v for k, v in r.items() if k != "execution_costs_workout"} for r in rows]
    assert point(legacy, LifecycleEconomics(funding_rate=0.05))["execution_cost_maturity_workout"] is None
