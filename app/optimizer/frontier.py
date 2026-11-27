"""Recovery-conditional risk-capacity frontier and appetite-to-capacity.

Source: the frozen RC1 engine's ``coverage_grace_quantiles``: the quantiles of
recovery proceeds *if recovery starts after the grace period*, already net of
execution costs, market impact, suspensions and settlement in RC1's scenario
set. Nothing in RC1 is modified or re-estimated here.

For a loan balance B at recovery start, the conditional shortfall is
L(B) = max(B - C, 0), with C the recovery proceeds. This module reports, per B:

* ``p_shortfall``   P(L > 0 | recovery after grace)
* ``expected_shortfall``  E[L | recovery after grace]
* ``cvar``          mean of the worst (1 - alpha) share of L (tail severity)

These are **recovery-conditional** quantities. They are NOT unconditional or
lifetime loss probabilities and NOT upper bounds on lifecycle losses: lifetime
loss also depends on the probability that an intervention happens at all, on
borrower cures, repayments and refinancing, which is the full-lifecycle
frontier (milestone M4).

Appetite-to-capacity returns the largest balance satisfying the institution's
conditional-risk limits, then applies independent institutional and
contractual limits separately (they are never folded into the risk numbers).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

VIEW = "recovery_conditional"
_U = (np.arange(4000) + 0.5) / 4000   # quantile-level grid for integration


def _proceeds_sample(quantiles: Sequence[float]) -> np.ndarray:
    q = np.asarray(quantiles, float)
    if q.size < 3:
        raise ValueError("coverage quantiles missing (RC1 evaluation without simulation)")
    levels = np.linspace(0.0, 1.0, q.size)
    return np.interp(_U, levels, np.maximum.accumulate(q))


def shortfall_metrics(proceeds: np.ndarray, balance: float, alpha: float = 0.99) -> dict[str, float]:
    loss = np.maximum(balance - proceeds, 0.0)
    k = max(1, round((1 - alpha) * loss.size))
    tail = np.sort(loss)[-k:]
    return {"balance": float(balance), "p_shortfall": float(np.mean(loss > 1e-9)),
            "expected_shortfall": float(loss.mean()), "cvar": float(tail.mean()),
            "expected_shortfall_rate": float(loss.mean() / balance) if balance > 0 else 0.0,
            "cvar_rate": float(tail.mean() / balance) if balance > 0 else 0.0}


@dataclass(frozen=True)
class RiskAppetite:
    """Institution-defined limits, all on the recovery-conditional view.

    Every field is optional; ``None`` means "no limit of this kind". Presets in
    ``PRESETS`` are illustrative research configurations, not Tiva's
    production risk appetite (that decision is reserved to Tiva)."""

    name: str = "custom"
    max_p_shortfall: float | None = None          # e.g. 0.02
    max_expected_shortfall_rate: float | None = None   # E[L]/B, e.g. 0.002
    max_cvar_rate: float | None = None            # CVaR_alpha / B
    alpha: float = 0.99
    min_risk_adjusted_return: float | None = None # on the economics view, if supplied


PRESETS = {
    "conservative": RiskAppetite("conservative", max_p_shortfall=0.005, max_expected_shortfall_rate=0.0005,
                                 max_cvar_rate=0.05),
    "balanced": RiskAppetite("balanced", max_p_shortfall=0.02, max_expected_shortfall_rate=0.002, max_cvar_rate=0.10),
    "aggressive": RiskAppetite("aggressive", max_p_shortfall=0.05, max_expected_shortfall_rate=0.006,
                               max_cvar_rate=0.20),
}


@dataclass(frozen=True)
class LendingEconomics:
    """Annualised economics; amounts in loan currency.

    ``intervention_probability`` converts conditional shortfall into an
    expected credit cost. In the recovery-conditional view it is an explicit
    research assumption supplied by the caller (labelled as such in outputs);
    the full-lifecycle frontier (M4) replaces it with a simulated estimate."""

    interest_rate: float            # borrower rate
    funding_rate: float             # institution cost of funds
    term_years: float = 1.0
    operating_cost_rate: float = 0.0
    capital_ratio: float = 0.0      # capital held per unit balance (e.g. regulatory)
    hurdle_rate: float = 0.12       # cost of capital
    intervention_probability: float | None = None


def economics(m: dict[str, float], econ: LendingEconomics) -> dict[str, Any]:
    b, t = m["balance"], econ.term_years
    p_int = econ.intervention_probability
    income = b * econ.interest_rate * t
    funding = b * econ.funding_rate * t
    opex = b * econ.operating_cost_rate * t
    credit_cost = (p_int * m["expected_shortfall"]) if p_int is not None else None
    # Capital: the larger of the stated ratio and the conditional tail loss
    # weighted by the intervention probability (economic capital proxy).
    econ_cap = (p_int * m["cvar"]) if p_int is not None else 0.0
    capital = max(econ.capital_ratio * b, econ_cap)
    capital_charge = capital * econ.hurdle_rate * t
    profit = None if credit_cost is None else income - funding - opex - credit_cost
    raroc = (profit / (capital * t)) if (profit is not None and capital > 0) else None
    return {"income": income, "funding_cost": funding, "operating_cost": opex, "expected_credit_cost": credit_cost,
            "capital": capital, "capital_charge": capital_charge,
            "risk_adjusted_profit": None if profit is None else profit - capital_charge,
            "raroc": raroc,
            "assumptions": {"intervention_probability": p_int,
                            "intervention_probability_source": "caller assumption (recovery-conditional view)"
                            if p_int is not None else "not supplied: credit cost not computed"}}


@dataclass
class Frontier:
    view: str
    account_ref: str
    model_version: str
    collateral_value: float
    points: list[dict[str, Any]]
    replicas: int
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _proceeds_from(evals: Sequence[Any]) -> list[np.ndarray]:
    return [_proceeds_sample(e.coverage_grace_quantiles) for e in evals]


def frontier(evals: Sequence[Any], balances: Sequence[float] | None = None, alpha: float = 0.99,
             econ: LendingEconomics | None = None, n_points: int = 41) -> Frontier:
    """Frontier over loan balances. ``evals`` are RC1 evaluations of the same
    portfolio with different Monte Carlo seeds (one is allowed); the spread
    across replicas is reported as Monte Carlo uncertainty."""
    if not evals:
        raise ValueError("at least one evaluation required")
    e0 = evals[0]
    samples = _proceeds_from(evals)
    if balances is None:
        hi = float(np.quantile(np.concatenate(samples), 0.995)) * 1.2
        grid = [float(x) for x in np.linspace(0.0, max(hi, 1.0), n_points)[1:]]
    else:
        grid = [float(x) for x in balances]
    pts = []
    for b in grid:
        per = [shortfall_metrics(s, float(b), alpha) for s in samples]
        row: dict[str, Any] = {"balance": float(b),
                               "ltv": float(b / e0.portfolio_market_value) if e0.portfolio_market_value else None}
        for k in ("p_shortfall", "expected_shortfall", "cvar", "expected_shortfall_rate", "cvar_rate"):
            v = np.array([p[k] for p in per])
            row[k] = float(v.mean())
            row[k + "_mc_sd"] = float(v.std(ddof=1)) if v.size > 1 else None
        if econ is not None:
            row["economics"] = economics({**per[0], **{k: row[k] for k in ("expected_shortfall", "cvar")}}, econ)
        pts.append(row)
    return Frontier(VIEW, e0.account_ref, e0.model_version, float(e0.portfolio_market_value), pts, len(evals),
                    notes=[("recovery-conditional: assumes recovery starts after the grace period; not a lifetime "
                            "loss probability and not an upper bound on lifecycle losses"),
                           f"tail measure: CVaR at alpha={alpha}",
                           "Monte Carlo uncertainty: standard deviation across seed replicas" if len(evals) > 1
                           else "single replica: Monte Carlo uncertainty not estimated"])


def _feasible(m: dict[str, float], a: RiskAppetite, econ: LendingEconomics | None = None) -> bool:
    if a.min_risk_adjusted_return is not None:
        if econ is None:
            raise ValueError("min_risk_adjusted_return requires LendingEconomics")
        raroc = economics(m, econ)["raroc"]
        if raroc is None or raroc < a.min_risk_adjusted_return:
            return False
    return ((a.max_p_shortfall is None or m["p_shortfall"] <= a.max_p_shortfall)
            and (a.max_expected_shortfall_rate is None or m["expected_shortfall_rate"] <= a.max_expected_shortfall_rate)
            and (a.max_cvar_rate is None or m["cvar_rate"] <= a.max_cvar_rate))


def _max_balance(proceeds: np.ndarray, a: RiskAppetite, upper: float,
                 econ: LendingEconomics | None = None) -> float:
    """Largest B in [0, upper] meeting the appetite. Every metric is monotone
    non-decreasing in B (rates too, for B above the proceeds floor), so a
    bisection on feasibility is exact up to tolerance; guarded by a scan."""
    if upper <= 0:
        return 0.0
    grid = np.linspace(upper / 400, upper, 400)
    ok = [b for b in grid if _feasible(shortfall_metrics(proceeds, b, a.alpha), a, econ)]
    if not ok:
        return 0.0
    lo, hi = max(ok), min([b for b in grid if b > max(ok)], default=max(ok))
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if _feasible(shortfall_metrics(proceeds, mid, a.alpha), a, econ):
            lo = mid
        else:
            hi = mid
    return float(lo)


def capacity_for_appetite(evals: Sequence[Any], appetite: RiskAppetite,
                          institutional_limit: float | None = None,
                          econ: LendingEconomics | None = None) -> dict[str, Any]:
    """Highest balance supported by the appetite, with Monte Carlo spread.

    The risk-supported capacity and the independent institutional limit are
    reported separately; the binding one is named."""
    if appetite.min_risk_adjusted_return is not None and (econ is None or econ.intervention_probability is None):
        raise ValueError("a minimum risk-adjusted return needs LendingEconomics with an explicit "
                         "intervention_probability assumption")
    samples = _proceeds_from(evals)
    pooled = np.concatenate(samples)
    upper = float(pooled.max()) * 1.5
    # Point estimate on the pooled replicas (largest Monte Carlo sample); the
    # per-replica capacities measure Monte Carlo spread only.
    risk_cap = _max_balance(pooled, appetite, upper, econ)
    caps = np.array([_max_balance(s, appetite, upper, econ) for s in samples])
    final = risk_cap if institutional_limit is None else min(risk_cap, institutional_limit)
    e0 = evals[0]
    at = shortfall_metrics(pooled, risk_cap, appetite.alpha) if risk_cap > 0 else None
    return {"view": VIEW, "appetite": asdict(appetite), "model_version": e0.model_version,
            "risk_supported_capacity": risk_cap,
            "risk_supported_ltv": risk_cap / e0.portfolio_market_value if e0.portfolio_market_value else None,
            "mc_sd": float(caps.std(ddof=1)) if caps.size > 1 else None,
            "mc_range": [float(caps.min()), float(caps.max())], "replicas": int(caps.size),
            "institutional_limit": institutional_limit, "capacity": final,
            "binding": "institutional_limit" if (institutional_limit is not None and institutional_limit < risk_cap)
            else "risk_appetite",
            "metrics_at_capacity": at,
            "rc1_approved_credit_limit": float(e0.approved_credit_limit),
            "note": "recovery-conditional appetite; RC1's own approved limit is shown for comparison and is "
                    "unchanged by this calculation"}
