"""Lending-capacity frontier v2 (RC2 research; Report 17 review, section 3).

Inputs: cohorts from the lifecycle validation runner parts ``frontier2-*`` and
``fstress-*`` (RC1 variant only; common random numbers across scales and cases;
contractual ceiling enforced; point-in-time settlement; retail and institutional
positions).

Corrections against v1 (``lifecycle_frontier``)
* Unique origination cohorts are counted separately from scale-level simulations.
* Scales are compared on matched cohorts (paired differences, common random numbers).
* Economics are reported principal-weighted (portfolio money) and borrower-weighted
  (mean per loan) separately.
* Interest: accrued and cash-received are distinguished; the P&L uses accrual less
  the full written-off balance, which equals cash received less principal lost.
* Funding and operating costs accrue on the funded principal outstanding in every
  session the loan is open, including maturity workouts.
* Rare losses: when few or no losses occur, the expected-loss bound is
  ``p_upper x severity``: ``p_upper`` is the exact one-sided 95% bound on the
  probability that an origination quarter (the dependence cluster) contains a
  loss; ``severity`` is the 95th percentile loss rate of losing loans observed
  under adverse scenarios at the same scale (or 100% if none lose). A degenerate
  zero-width interval is never reported as a bound.
* Capacity is chosen per portfolio and position size, not as one multiplier,
  subject to research appetite settings that are not approved risk appetite.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np

SESSIONS_PER_YEAR = 252.0


@dataclass(frozen=True)
class Economics:
    funding_rate: float
    opex_rate: float = 0.005
    hurdle_rate: float = 0.15
    capital_floor_rate: float = 0.02
    tail: float = 0.99

    def __post_init__(self) -> None:
        for k in ("funding_rate", "opex_rate", "hurdle_rate", "capital_floor_rate"):
            v = getattr(self, k)
            if not (math.isfinite(v) and 0.0 <= v < 1.0):
                raise ValueError(f"{k} must be in [0, 1), got {v}")
        if not 0.5 < self.tail < 1.0:
            raise ValueError("tail must be in (0.5, 1)")


@dataclass(frozen=True)
class ResearchAppetite:
    """Research settings only; production risk appetite is Tiva's decision."""

    max_expected_loss: float = 0.001         # bound on lifetime EL per unit principal
    max_liquidation_frequency: float = 0.10  # upper 95% bound
    max_stress_loss_probability: float | None = None   # in the adverse-scenario set

    def __post_init__(self) -> None:
        for k, v in asdict(self).items():
            if v is not None and not (math.isfinite(v) and 0.0 <= v <= 1.0):
                raise ValueError(f"{k} out of range: {v}")


# ------------------------------------------------------------------ rows

def loan_rows(cohorts: Iterable[dict[str, Any]], variant: str = "rc1") -> list[dict[str, Any]]:
    """One row per simulated loan with its labels; skipped cohorts are dropped."""
    out = []
    for c in cohorts:
        v = c.get("variants", {}).get(variant)
        if not isinstance(v, dict) or not v.get("principal") or "skipped" in v:
            continue
        lab = c.get("labels", {})
        principal = float(v["principal"])
        censored = bool(v.get("censored"))
        bal_loss = float(v.get("outstanding_debt", 0.0) if censored else (v.get("lender_loss") or 0.0))
        accrued = float(v.get("interest_accrued", 0.0))
        received = float(v.get("interest_received", accrued))
        cal = int(v.get("calendar_sessions") or v.get("sessions") or 1)
        funded = float(v.get("funded_principal_sessions") or principal * cal)
        out.append({
            "cohort_id": lab.get("cohort_id"), "origination": c.get("origination"),
            "market": lab.get("market"), "portfolio": lab.get("portfolio"), "size": lab.get("size"),
            "case": lab.get("case") or lab.get("scenario"), "behaviour": lab.get("behaviour"),
            "term": int(lab.get("term", c.get("term_sessions", 63))),
            "scale": float(lab.get("scale_of_rc_credit", lab.get("principal_scale", 1.0))),
            "binding": c.get("principal_binding", "scale"),
            "principal": principal, "censored": censored, "loss": bal_loss,
            # balance written off = unpaid principal + unpaid accrued interest
            "principal_loss": max(0.0, bal_loss - max(0.0, accrued - received)),
            "accrued": accrued, "received": received,
            "funded_years": funded / SESSIONS_PER_YEAR, "years": cal / SESSIONS_PER_YEAR,
            "liquidated": int(v.get("liquidation_episodes", 0)) > 0,
            "margin_calls": int(v.get("margin_calls", 0)),
            "exec_intervention": float(v.get("execution_costs_intervention", 0.0)),
            "exec_workout": float(v.get("execution_costs_workout", 0.0)),
            "maturity_outcome": v.get("maturity_outcome", ""),
            "recovery_sessions": max(v.get("recovery_sessions") or [0]),
        })
    return out


def _quarter(r: dict[str, Any]) -> str:
    d = r.get("origination")
    d = date.fromisoformat(str(d)[:10]) if d else None
    return f"{d.year}Q{(d.month - 1) // 3 + 1}" if d else "na"


def _wes(x: np.ndarray, w: np.ndarray, q: float) -> float:
    """Expected shortfall of x at level q under weights w (upper tail)."""
    if x.size == 0:
        return 0.0
    o = np.argsort(x)[::-1]
    xs, ws = x[o], w[o] / w.sum()
    cum = np.cumsum(ws)
    tail = 1 - q
    k = int(np.searchsorted(cum, tail))
    take = ws[:k + 1].copy()
    take[-1] -= max(0.0, cum[min(k, cum.size - 1)] - tail)
    return float((xs[:k + 1] * take).sum() / tail)


# ------------------------------------------------------------------ point

def point(rows: Sequence[dict[str, Any]], econ: Economics) -> dict[str, Any]:
    """Lifetime economics, principal-weighted and borrower-weighted."""
    if not rows:
        raise ValueError("no rows")
    P = np.array([r["principal"] for r in rows])
    loss = np.array([r["loss"] for r in rows])
    acc = np.array([r["accrued"] for r in rows])
    rec = np.array([r["received"] for r in rows])
    fy = np.array([r["funded_years"] for r in rows])
    yrs = np.array([r["years"] for r in rows])
    liq = np.array([r["liquidated"] for r in rows], float)
    xi = np.array([r["exec_intervention"] for r in rows])
    xw = np.array([r["exec_workout"] for r in rows])
    funding = econ.funding_rate * fy
    opex = econ.opex_rate * fy
    pnl = acc - loss - funding - opex                     # = received - principal lost - costs
    rate = loss / P
    out: dict[str, Any] = {"loans": len(rows), "unique_cohorts": len({r["cohort_id"] for r in rows}),
                           "censored": int(sum(r["censored"] for r in rows)),
                           "loss_events": int((rate > 1e-6).sum()), "liquidation_frequency": float(liq.mean()),
                           "margin_calls_per_loan": float(np.mean([r["margin_calls"] for r in rows])),
                           "ceiling_binding_share": float(np.mean([r["binding"] == "contractual_ceiling"
                                                                   for r in rows]))}
    for wname, w in (("principal_weighted", P), ("borrower_weighted", np.ones_like(P))):
        ww = w / w.sum()
        el = float((ww * rate).sum())
        es = _wes(rate, w, econ.tail)
        fyr = float((ww * fy / P).sum())                  # funded principal-years per unit principal
        cap = max(es - el, econ.capital_floor_rate)
        res: dict[str, Any] = {
            "expected_loss": el, f"es{round(econ.tail * 100)}_loss": es,
            "interest_accrued": float((ww * acc / P).sum()), "interest_received": float((ww * rec / P).sum()),
            "funding_cost": float((ww * funding / P).sum()), "opex": float((ww * opex / P).sum()),
            "funded_years_per_unit": fyr, "calendar_years": float((ww * yrs).sum()),
            "execution_cost_intervention": float((ww * xi / P).sum()),
            "execution_cost_workout": float((ww * xw / P).sum()),
            "net_profit": float((ww * pnl / P).sum()), "economic_capital": cap,
        }
        res["raroc"] = res["net_profit"] / (cap * fyr) if cap > 0 and fyr > 0 else None
        res["risk_adjusted_profit"] = res["net_profit"] - econ.hurdle_rate * cap * fyr
        out[wname] = res
    return out


def rare_loss_bound(rows: Sequence[dict[str, Any]], severity_rows: Sequence[dict[str, Any]] = (),
                    conf: float = 0.95) -> dict[str, Any]:
    """Expected-loss upper bound robust to few or zero observed losses (see module doc)."""
    from scipy.stats import beta
    clusters: dict[str, bool] = {}
    for r in rows:
        clusters[_quarter(r)] = clusters.get(_quarter(r), False) or (r["loss"] / r["principal"] > 1e-6)
    n, k = len(clusters), sum(clusters.values())
    p_upper = float(beta.ppf(conf, k + 1, n - k)) if n > k else 1.0
    losing = [r["loss"] / r["principal"] for r in list(rows) + list(severity_rows)
              if r["loss"] / r["principal"] > 1e-6]
    sev = float(np.quantile(losing, 0.95)) if len(losing) >= 5 else (max(losing) if losing else 1.0)
    source = ("observed and adverse-scenario losses" if len(losing) >= 5 else
              "maximum observed loss (fewer than 5 losing loans)" if losing else "total loss (no losing loan)")
    return {"clusters": n, "clusters_with_loss": k, "cluster_loss_probability_upper": p_upper,
            "severity": sev, "severity_basis": source, "severity_n": len(losing),
            "expected_loss_upper": p_upper * sev}


def clustered_ci(rows: Sequence[dict[str, Any]], fn: Any, reps: int = 400, seed: int = 5) -> list[float]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(_quarter(r), []).append(r)
    keys = list(groups)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(reps):
        sample = [r for k in rng.choice(keys, len(keys)) for r in groups[k]]
        vals.append(fn(sample))
    return [float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))]


def paired_delta(rows: Sequence[dict[str, Any]], scale: float, base: float = 1.0, econ: Economics | None = None,
                 reps: int = 400, seed: int = 9) -> dict[str, Any]:
    """Matched-cohort differences (scale - base) per unit of the base principal: identical
    origination, path and random numbers, so the interval reflects the scale effect only."""
    by: dict[tuple[Any, ...], dict[float, dict[str, Any]]] = {}
    for r in rows:
        by.setdefault((r["cohort_id"], r["case"], r["behaviour"], r["term"]), {})[r["scale"]] = r
    pairs = [(v[base], v[scale]) for v in by.values() if base in v and scale in v]
    if not pairs:
        return {"pairs": 0}
    econ = econ or Economics(0.05)

    def pnl(r: dict[str, Any]) -> float:
        return r["accrued"] - r["loss"] - (econ.funding_rate + econ.opex_rate) * r["funded_years"]

    def stat(ps: Sequence[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, float]:
        b = sum(p[0]["principal"] for p in ps)
        return {"exposure_ratio": sum(p[1]["principal"] for p in ps) / b,
                "delta_loss_per_base": sum(p[1]["loss"] - p[0]["loss"] for p in ps) / b,
                "delta_net_profit_per_base": sum(pnl(p[1]) - pnl(p[0]) for p in ps) / b,
                "delta_liquidation_frequency": float(np.mean([p[1]["liquidated"] - p[0]["liquidated"] for p in ps]))}
    pt = stat(pairs)
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for p in pairs:
        groups.setdefault(_quarter(p[0]), []).append(p)
    keys = list(groups)
    rng = np.random.default_rng(seed)
    boots: dict[str, list[float]] = {k: [] for k in pt}
    for _ in range(reps):
        s = stat([p for k in rng.choice(keys, len(keys)) for p in groups[k]])
        for k, v in s.items():
            boots[k].append(v)
    return {"pairs": len(pairs), "unique_cohorts": len({p[0]["cohort_id"] for p in pairs}), **pt,
            "ci95": {k: [float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975))] for k, v in boots.items()}}


# ------------------------------------------------------------------ frontier and capacity

def frontier(rows: Sequence[dict[str, Any]], econ: Economics, stress: Sequence[dict[str, Any]] = (),
             reps: int = 400) -> list[dict[str, Any]]:
    out = []
    for s in sorted({r["scale"] for r in rows}):
        rs = [r for r in rows if r["scale"] == s]
        p = point(rs, econ)
        st = [r for r in stress if r["scale"] == s]
        p["rare_loss"] = rare_loss_bound(rs, st)
        p["liquidation_frequency_ci95"] = clustered_ci(rs, lambda x: float(np.mean([r["liquidated"] for r in x])),
                                                       reps)
        p["expected_loss_pw_ci95"] = clustered_ci(
            rs, lambda x: sum(r["loss"] for r in x) / sum(r["principal"] for r in x), reps)
        p["expected_loss_upper"] = max(p["expected_loss_pw_ci95"][1], p["rare_loss"]["expected_loss_upper"])
        pw = p["principal_weighted"]
        p["risk_adjusted_profit_per_rc1_credit"] = s * pw["risk_adjusted_profit"] if rs else None
        if st:
            p["stress_loss_probability"] = float(np.mean([r["loss"] / r["principal"] > 1e-6 for r in st]))
        out.append({"scale": s, **p})
    return out


def capacity(rows: Sequence[dict[str, Any]], econ: Economics, appetite: ResearchAppetite,
             stress: Sequence[dict[str, Any]] = (), keys: tuple[str, ...] = ("portfolio", "size"),
             reps: int = 300) -> dict[str, Any]:
    """Portfolio-specific capacity: per group the largest tested scale whose robust bounds meet
    the research appetite; compared with the best single universal multiplier and with RC1."""
    def ok(p: dict[str, Any]) -> bool:
        return (p["expected_loss_upper"] <= appetite.max_expected_loss
                and p["liquidation_frequency_ci95"][1] <= appetite.max_liquidation_frequency
                and (appetite.max_stress_loss_probability is None
                     or p.get("stress_loss_probability", 0.0) <= appetite.max_stress_loss_probability))
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(tuple(r[k] for k in keys), []).append(r)
    sgroups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in stress:
        sgroups.setdefault(tuple(r[k] for k in keys), []).append(r)
    per: dict[str, Any] = {}
    fronts = {}
    for g, rs in sorted(groups.items()):
        f = frontier(rs, econ, sgroups.get(g, ()), reps)
        fronts[g] = {p["scale"]: p for p in f}
        good = [p["scale"] for p in f if ok(p)]
        sel = max(good) if good else None
        per["|".join(map(str, g))] = {
            "supported_scale": sel,
            "binding_constraint": None if sel is None else _binding(fronts[g], sel, ok),
            "tested_scales": sorted(fronts[g]),
            "at_selected": None if sel is None else _summary(fronts[g][sel]),
            "at_rc1": _summary(fronts[g][1.0]) if 1.0 in fronts[g] else None}
    common = sorted(set.intersection(*[set(v) for v in fronts.values()])) if fronts else []
    universal = max([s for s in common if all(ok(fronts[g][s]) for g in fronts)], default=None)

    def total(choice: dict[tuple[Any, ...], float | None]) -> dict[str, Any]:
        exp, rap, el = 0.0, 0.0, 0.0
        for g, s in choice.items():
            if s is None:
                continue
            p = fronts[g][s]
            exp += s
            rap += p["risk_adjusted_profit_per_rc1_credit"]
            el += s * p["expected_loss_upper"]
        n = len(choice)
        return {"exposure_per_rc1_credit": exp / n, "risk_adjusted_profit_per_rc1_credit": rap / n,
                "expected_loss_upper_per_rc1_credit": el / n, "groups_lending": sum(v is not None for v in choice.values())}
    specific = {g: v["supported_scale"] for g, v in zip(fronts, per.values(), strict=True)}
    return {"appetite": asdict(appetite), "economics": asdict(econ), "per_group": per,
            "universal_scale": universal,
            "methodologies": {"rc1": total({g: 1.0 if 1.0 in fronts[g] else None for g in fronts}),
                              "universal_multiplier": total({g: universal for g in fronts}),
                              "portfolio_specific": total(specific)},
            "note": "research appetite settings, not approved risk appetite; tested grid only"}


def _binding(front: dict[float, dict[str, Any]], sel: float, ok: Any) -> str:
    higher = [s for s in sorted(front) if s > sel]
    if not higher:
        return "largest tested scale"
    p = front[higher[0]]
    reasons = []
    if p["expected_loss_upper"] > 0:
        reasons.append(f"EL bound {p['expected_loss_upper']:.4f} at {higher[0]}")
    reasons.append(f"liquidation upper {p['liquidation_frequency_ci95'][1]:.3f} at {higher[0]}")
    return "; ".join(reasons)


def _summary(p: dict[str, Any]) -> dict[str, Any]:
    pw, bw = p["principal_weighted"], p["borrower_weighted"]
    return {"loans": p["loans"], "unique_cohorts": p["unique_cohorts"], "liquidation_frequency": p["liquidation_frequency"],
            "liquidation_upper": p["liquidation_frequency_ci95"][1], "expected_loss_pw": pw["expected_loss"],
            "expected_loss_upper": p["expected_loss_upper"], "rare_loss": p["rare_loss"],
            "net_profit_pw": pw["net_profit"], "net_profit_bw": bw["net_profit"],
            "interest_accrued_pw": pw["interest_accrued"], "interest_received_pw": pw["interest_received"],
            "funded_years_pw": pw["funded_years_per_unit"],
            "rap_per_rc1_credit": p["risk_adjusted_profit_per_rc1_credit"],
            "ceiling_binding_share": p["ceiling_binding_share"]}
