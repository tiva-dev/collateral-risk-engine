"""Full-lifecycle risk-capacity frontier and lending economics (M4).

Input: cohorts from the lifecycle validation runner (``frontier`` part,
``--variant-set rc1``): matched loans originated at multiples of RC1's own
approved credit, simulated through monitoring, margin calls, cures,
refinancing or maturity, and recovery on the *historical* paths of the
examined data. Every number here is a lifetime, unconditional outcome per loan
of principal: unlike the recovery-conditional frontier it already includes the
probability that an intervention happens at all.

Economics per unit of principal over the loan's life (T = sessions/252):
  income            interest accrued (simulated, from the lifecycle)
  funding cost      funding_rate * T
  operating cost    opex_rate * T
  expected loss     mean lifetime lender loss (censored loans at their
                    conservative upper bound: full outstanding debt)
  economic capital  ES99(lifetime loss) - expected loss, floored at
                    capital_floor_rate (regulatory-style minimum)
  RAROC             (income - funding - opex - EL) / (capital * T)
  risk-adj. profit  income - funding - opex - EL - hurdle * capital * T

Lifetime loss and capital replace the provisional recovery-conditional proxy.
Funding, operating, hurdle and floor rates are explicit assumptions
(``LifecycleEconomics``); the production values are Tiva's decision.
Uncertainty: clustered bootstrap over origination quarters (shared shocks).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np


@dataclass(frozen=True)
class LifecycleEconomics:
    funding_rate: float                 # annual cost of funds
    opex_rate: float = 0.005            # annual operating cost per unit principal
    hurdle_rate: float = 0.15           # annual cost of capital
    capital_floor_rate: float = 0.02    # minimum capital per unit principal
    tail: float = 0.99                  # ES level for economic capital

    def __post_init__(self) -> None:
        for k in ("funding_rate", "opex_rate", "hurdle_rate", "capital_floor_rate"):
            v = getattr(self, k)
            if not (math.isfinite(v) and 0.0 <= v < 1.0):
                raise ValueError(f"{k} must be in [0, 1), got {v}")
        if not 0.5 < self.tail < 1.0:
            raise ValueError("tail must be in (0.5, 1)")


@dataclass(frozen=True)
class LifetimeAppetite:
    """Institution limits on *lifetime* outcomes per unit principal."""

    max_expected_loss: float | None = None          # e.g. 0.002
    max_es99_loss: float | None = None              # e.g. 0.05
    max_loss_probability: float | None = None       # e.g. 0.01
    max_liquidation_frequency: float | None = None  # share of loans with any forced sale
    min_raroc: float | None = None

    def __post_init__(self) -> None:
        for k, v in asdict(self).items():
            if v is not None and (not math.isfinite(v) or v < 0 or (k != "min_raroc" and v > 1)):
                raise ValueError(f"{k} out of range: {v}")


def _loss_rows(rows: Sequence[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    loss = np.array([((r.get("lender_loss") or 0.0) if not r.get("censored") else r.get("outstanding_debt", 0.0))
                     / r["principal"] for r in rows], float)
    years = np.array([max(r.get("sessions", 0), 1) / 252.0 for r in rows], float)
    income = np.array([r.get("interest_accrued", 0.0) / r["principal"] for r in rows], float)
    liq = np.array([r.get("liquidation_episodes", 0) > 0 for r in rows], float)
    return loss, years, income, liq


def _es(x: np.ndarray, q: float) -> float:
    k = max(1, math.ceil((1 - q) * x.size))
    return float(np.sort(x)[-k:].mean())


def point(rows: Sequence[dict[str, Any]], econ: LifecycleEconomics) -> dict[str, Any]:
    if not rows:
        return {"loans": 0}
    loss, years, income, liq = _loss_rows(rows)
    t = float(years.mean())
    el = float(loss.mean())
    es = _es(loss, econ.tail)
    capital = max(es - el, econ.capital_floor_rate)
    inc = float(income.mean())
    fund = econ.funding_rate * t
    opex = econ.opex_rate * t
    net = inc - fund - opex - el
    return {"loans": len(rows), "censored": int(sum(bool(r.get("censored")) for r in rows)),
            "mean_years": t, "expected_loss": el, f"es{int(econ.tail * 100)}_loss": es,
            "loss_probability": float(np.mean(loss > 1e-6)), "liquidation_frequency": float(liq.mean()),
            "margin_calls_per_loan": float(np.mean([r.get("margin_calls", 0) for r in rows])),
            "borrower_execution_cost": float(np.mean([r.get("execution_costs", 0.0) / r["principal"] for r in rows])),
            "interest_income": inc, "funding_cost": fund, "operating_cost": opex,
            "economic_capital": capital, "net_margin_after_el": net,
            "raroc": net / (capital * t) if capital > 0 and t > 0 else None,
            "risk_adjusted_profit": net - econ.hurdle_rate * capital * t}


def _quarter(r: dict[str, Any]) -> str:
    d = r.get("origination")
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return f"{d.year}Q{(d.month - 1) // 3 + 1}" if isinstance(d, date) else "na"


def frontier(cohorts: Sequence[dict[str, Any]], econ: LifecycleEconomics, variant: str = "rc1",
             reps: int = 400, seed: int = 5) -> list[dict[str, Any]]:
    """One point per principal scale, with quarter-clustered bootstrap intervals."""
    by_scale: dict[float, list[dict[str, Any]]] = {}
    for c in cohorts:
        scale = c.get("labels", {}).get("scale_of_rc_credit", c.get("labels", {}).get("principal_scale"))
        v = c.get("variants", {}).get(variant)
        if scale is None or not isinstance(v, dict) or not v.get("principal"):
            continue
        by_scale.setdefault(float(scale), []).append({**v, "origination": c.get("origination")})
    rng = np.random.default_rng(seed)
    out = []
    for scale in sorted(by_scale):
        rows = by_scale[scale]
        p = point(rows, econ)
        clusters: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            clusters.setdefault(_quarter(r), []).append(r)
        keys = list(clusters)
        boots: dict[str, list[float]] = {"expected_loss": [], "risk_adjusted_profit": [], "liquidation_frequency": []}
        for _ in range(reps):
            sample = [r for k in rng.choice(keys, len(keys)) for r in clusters[k]]
            b = point(sample, econ)
            for k, vals in boots.items():
                vals.append(b[k])
        p["ci95"] = {k: [float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975))] for k, v in boots.items()}
        p["clusters"] = len(keys)
        out.append({"scale_of_rc1_credit": scale, **p})
    return out


def supported_scale(points: Sequence[dict[str, Any]], appetite: LifetimeAppetite,
                    institutional_max_scale: float | None = None) -> dict[str, Any]:
    """Largest tested scale whose point estimates *and* upper 95% bounds meet the
    appetite, and the profit-maximising scale. Institutional limits are applied
    separately and reported as such."""
    def ok(p: dict[str, Any], use_upper: bool) -> bool:
        el = p["ci95"]["expected_loss"][1] if use_upper else p["expected_loss"]
        lf = p["ci95"]["liquidation_frequency"][1] if use_upper else p["liquidation_frequency"]
        es_key = next(k for k in p if k.startswith("es") and k.endswith("_loss"))
        return ((appetite.max_expected_loss is None or el <= appetite.max_expected_loss)
                and (appetite.max_es99_loss is None or p[es_key] <= appetite.max_es99_loss)
                and (appetite.max_loss_probability is None or p["loss_probability"] <= appetite.max_loss_probability)
                and (appetite.max_liquidation_frequency is None or lf <= appetite.max_liquidation_frequency)
                and (appetite.min_raroc is None or (p["raroc"] is not None and p["raroc"] >= appetite.min_raroc)))
    if not points:
        raise ValueError("no frontier points")
    point_ok = [p["scale_of_rc1_credit"] for p in points if ok(p, False)]
    robust_ok = [p["scale_of_rc1_credit"] for p in points if ok(p, True)]
    best = max(points, key=lambda p: p["risk_adjusted_profit"])
    risk_scale = max(robust_ok) if robust_ok else None
    final = risk_scale if institutional_max_scale is None or risk_scale is None else min(risk_scale,
                                                                                         institutional_max_scale)
    return {"appetite": asdict(appetite), "max_scale_point_estimate": max(point_ok) if point_ok else None,
            "max_scale_robust_upper95": risk_scale, "institutional_max_scale": institutional_max_scale,
            "supported_scale": final,
            "binding": None if final is None else ("institutional_limit" if final != risk_scale else "risk_appetite"),
            "profit_maximising_scale": best["scale_of_rc1_credit"],
            "note": "scales are multiples of RC1's approved credit; tested grid only (no extrapolation)"}
