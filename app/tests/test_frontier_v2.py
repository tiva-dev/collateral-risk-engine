"""Lending-capacity frontier v2 (Report 17 review, section 3)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from app.optimizer.frontier_v2 import (
    Economics,
    ResearchAppetite,
    _wes,
    capacity,
    frontier,
    loan_rows,
    paired_delta,
    point,
    rare_loss_bound,
)


def cohort(cid: str, d: date, scale: float, principal: float, loss: float = 0.0, accrued: float = 100.0,
           received: float | None = None, liq: int = 0, portfolio: str = "p", size: str = "retail",
           sessions: int = 63) -> dict:
    return {"origination": d.isoformat(), "principal_binding": "scale",
            "labels": {"cohort_id": cid, "market": "US", "portfolio": portfolio, "size": size, "case": "base",
                       "term": 63, "scale_of_rc_credit": scale},
            "variants": {"rc1": {"principal": principal, "lender_loss": loss, "censored": False,
                                 "interest_accrued": accrued,
                                 "interest_received": accrued if received is None else received,
                                 "calendar_sessions": sessions, "sessions": sessions,
                                 "funded_principal_sessions": principal * sessions,
                                 "liquidation_episodes": liq, "margin_calls": liq,
                                 "execution_costs_intervention": 0.0, "execution_costs_workout": 0.0}}}


def book(n_q: int = 24, scales=(1.0, 1.25), loss_at: float | None = None, portfolio: str = "p") -> list[dict]:
    out = []
    for i in range(n_q * 3):
        d = date(2019, 1, 15) + timedelta(days=30 * i)
        for s in scales:
            loss = 500.0 * s if loss_at is not None and s >= loss_at and i % 7 == 0 else 0.0
            out.append(cohort(f"{portfolio}{i}", d, s, 10_000.0 * s, loss=loss, liq=int(loss > 0),
                              portfolio=portfolio))
    return out


def test_zero_losses_give_a_non_degenerate_bound() -> None:
    rows = loan_rows(book())
    rb = rare_loss_bound([r for r in rows if r["scale"] == 1.0])
    assert rb["clusters_with_loss"] == 0
    assert rb["cluster_loss_probability_upper"] > 0.05          # ~1 - 0.05**(1/24)
    assert rb["severity"] == 1.0 and rb["expected_loss_upper"] > 0.05
    stressed = [dict(r, loss=0.08 * r["principal"]) for r in rows[:10]]
    rb2 = rare_loss_bound([r for r in rows if r["scale"] == 1.0], stressed)
    assert rb2["severity"] == pytest.approx(0.08) and rb2["expected_loss_upper"] < rb["expected_loss_upper"]


def test_principal_and_borrower_weighting_differ() -> None:
    d = date(2020, 1, 1)
    rows = loan_rows([cohort("a", d, 1.0, 1_000_000.0), cohort("b", d, 1.0, 1_000.0, loss=500.0)])
    p = point(rows, Economics(0.05))
    assert p["borrower_weighted"]["expected_loss"] == pytest.approx(0.25)
    assert p["principal_weighted"]["expected_loss"] == pytest.approx(500 / 1_001_000)
    assert p["unique_cohorts"] == 2


def test_accrued_and_received_interest_and_funded_cost() -> None:
    d = date(2020, 1, 1)
    c = cohort("a", d, 1.0, 10_000.0, loss=300.0, accrued=200.0, received=50.0, sessions=126)
    c["variants"]["rc1"]["funded_principal_sessions"] = 10_000.0 * 63      # repaid halfway
    r = loan_rows([c])[0]
    assert r["principal_loss"] == pytest.approx(300 - 150)                  # write-off less unpaid interest
    p = point([r], Economics(0.05, opex_rate=0.0))
    pw = p["principal_weighted"]
    assert pw["interest_accrued"] == pytest.approx(0.02) and pw["interest_received"] == pytest.approx(0.005)
    assert pw["funding_cost"] == pytest.approx(0.05 * 63 / 252)            # on funded balance, not term
    cash = (50.0 - r["principal_loss"]) / 10_000 - pw["funding_cost"]
    assert pw["net_profit"] == pytest.approx(cash)                          # accrual P&L == cash P&L


def test_weighted_expected_shortfall() -> None:
    x = np.array([0.0] * 98 + [0.5, 1.0])
    assert _wes(x, np.ones(100), 0.99) == pytest.approx(1.0)
    assert _wes(x, np.ones(100), 0.98) == pytest.approx(0.75)


def test_paired_delta_uses_matched_cohorts() -> None:
    rows = loan_rows(book(loss_at=1.25))
    dlt = paired_delta(rows, 1.25)
    assert dlt["pairs"] == 72 and dlt["exposure_ratio"] == pytest.approx(1.25)
    assert dlt["delta_loss_per_base"] > 0 and dlt["ci95"]["delta_loss_per_base"][0] > 0


def test_capacity_is_portfolio_specific() -> None:
    safe = loan_rows(book(portfolio="safe", scales=(1.0, 1.25, 1.5)))
    risky = loan_rows(book(portfolio="risky", scales=(1.0, 1.25, 1.5), loss_at=1.25))
    stress = [dict(r, loss=0.05 * r["principal"]) for r in safe[:20]] + \
             [dict(r, loss=0.05 * r["principal"]) for r in risky[:20]]
    cap = capacity(safe + risky, Economics(0.05), ResearchAppetite(max_expected_loss=0.01,
                                                                   max_liquidation_frequency=0.10),
                   stress=stress, reps=100)
    per = cap["per_group"]
    assert per["safe|retail"]["supported_scale"] == 1.5
    assert per["risky|retail"]["supported_scale"] == 1.0
    assert cap["universal_scale"] == 1.0
    m = cap["methodologies"]
    assert m["portfolio_specific"]["exposure_per_rc1_credit"] > m["universal_multiplier"]["exposure_per_rc1_credit"]


def test_frontier_reports_unique_cohorts_and_scale_runs() -> None:
    rows = loan_rows(book(scales=(1.0, 1.1, 1.2)))
    f = frontier(rows, Economics(0.05), reps=50)
    assert [p["scale"] for p in f] == [1.0, 1.1, 1.2]
    assert all(p["unique_cohorts"] == 72 and p["loans"] == 72 for p in f)


def test_exposure_uses_actual_capped_principal() -> None:
    d = date(2020, 1, 1)
    rows = loan_rows([cohort("a", d, 1.0, 10_000.0), cohort("a", d, 1.5, 11_000.0),     # ceiling binds
                      cohort("b", d, 1.0, 10_000.0), cohort("b", d, 1.5, 15_000.0)])
    f = {p["scale"]: p for p in frontier(rows, Economics(0.05), reps=20)}
    assert f[1.5]["exposure_per_rc1_credit"] == pytest.approx(26_000 / 20_000)
    assert f[1.5]["risk_adjusted_profit_per_rc1_credit"] == pytest.approx(
        1.3 * f[1.5]["principal_weighted"]["risk_adjusted_profit"])


def test_binding_constraint_names_only_failing_limits() -> None:
    from app.optimizer.frontier_v2 import _binding
    front = {1.0: {}, 1.25: {"expected_loss_upper": 0.0005, "liquidation_frequency_ci95": [0.0, 0.05],
                             "stress_loss_probability": 0.3}}
    msg = _binding(front, 1.0, ResearchAppetite(max_expected_loss=0.001, max_liquidation_frequency=0.1,
                                                max_stress_loss_probability=0.1))
    assert msg.startswith("stress loss probability") and "EL" not in msg and "liquidation" not in msg
