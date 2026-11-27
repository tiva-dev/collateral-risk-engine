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
