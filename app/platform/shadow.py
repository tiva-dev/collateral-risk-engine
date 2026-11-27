"""Shadow model deployment: several versioned risk models evaluate the same
portfolios on the same inputs; only the baseline drives monitoring state.

Baseline   the engine the platform runs (main + confirmed correctness fixes).
Candidates CRI v0.7.0 (+fixes), a frozen snapshot in ``shadow_engines``;
           research methods A, B, B_gap and C from ``research.methodology_lab``.

Every result is persisted with its explanation. Nothing here changes account
state, emits margin events, sends webhooks or produces executable orders.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from typing import Any

from app.platform import codec
from app.platform.db import Database, current_connection

logger = logging.getLogger("cri.shadow")

V070_PACKAGE = "shadow_engines.cri_v070.app"
RESEARCH_HISTORY = 260
RESEARCH_MIN_HISTORY = 120


@dataclass
class ShadowInputs:
    tenant_id: str
    account: Any
    holdings: list[Any]
    market_data: dict[str, Any]
    now: datetime
    # Backtests pass the bars visible at the historical decision date.
    bars: dict[str, list[dict[str, Any]]] | None = None


@dataclass
class ShadowResult:
    model_id: str
    model_version: str
    role: str
    collateral_value: float | None = None
    loan_balance: float | None = None
    approved_credit: float | None = None
    available_credit: float | None = None
    capacity_ltv: float | None = None
    risk_state: str | None = None
    margin_call: bool = False
    estimated_recovery: float | None = None
    liquidation: list[dict[str, Any]] = field(default_factory=list)
    explanation: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _plain(x: Any) -> Any:
    if is_dataclass(x) and not isinstance(x, type):
        return {k: _plain(v) for k, v in asdict(x).items()}
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_plain(v) for v in x]
    if hasattr(x, "value") and not isinstance(x, int | float | str):
        return x.value
    if isinstance(x, datetime):
        return x.isoformat()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


# ------------------------------------------------------------ engine models

def explain_engine_evaluation(ev: Any) -> dict[str, Any]:
    """Why an engine version assigns its credit limit and state, from its own outputs."""
    assets = []
    for a in ev.asset_results:
        haircut_value = a.market_value * (a.base_ltv - a.effective_ltv) if a.eligible else a.market_value * a.base_ltv
        assets.append({
            "asset_id": a.asset_id,
            "market_value": a.market_value,
            "base_ltv": a.base_ltv,
            "effective_ltv": a.effective_ltv,
            "lendable_value": a.lendable_value,
            "credit_reduction_vs_base": haircut_value,
            "stressed_liquidation_value": a.stressed_liquidation_value,
            "liquidation_horizon_days": a.liquidation_horizon_days,
            "estimated_slippage_rate": a.estimated_slippage_rate,
            "risk_score": a.risk_score,
            "eligible": a.eligible,
            "data_unavailable_reason": getattr(a, "data_unavailable_reason", None),
            "risk_drivers": list(a.risk_drivers),
            "adjustments": _plain(a.adjustments),
            "notes": list(a.notes)[:8],
        })
    assets.sort(key=lambda x: -x["credit_reduction_vs_base"])
    tl = _plain(ev.trigger_levels)
    mv = ev.portfolio_market_value or 0.0
    balance = ev.current_outstanding_balance
    reasons = []
    if ev.approved_credit_limit <= 0:
        reasons.append("approved credit limit is zero: no asset was eligible or all lendable value was removed")
    if balance > ev.approved_credit_limit:
        reasons.append(
            f"outstanding balance {balance:,.2f} exceeds approved limit {ev.approved_credit_limit:,.2f}; "
            "no new borrowing")
    if ev.recovery_coverage_ratio is not None:
        reasons.append(
            f"stressed liquidation value covers {ev.recovery_coverage_ratio:.2f}x the obligation "
            f"(minimum required {ev.minimum_stressed_liquidation_value:,.2f})")
    if assets:
        top = assets[0]
        reasons.append(
            f"largest reduction from {top['asset_id']}: base LTV {top['base_ltv']:.0%} -> effective "
            f"{top['effective_ltv']:.0%} ({', '.join(top['risk_drivers'][:3]) or 'no drivers reported'})")
    return {
        "portfolio_market_value": mv,
        "risk_adjusted_collateral_value": ev.risk_adjusted_collateral_value,
        "approved_credit_limit": ev.approved_credit_limit,
        "current_ltv": balance / mv if mv else None,
        "approved_ltv": ev.approved_credit_limit / mv if mv else None,
        "stressed_liquidation_value": ev.stressed_liquidation_value,
        "recovery_coverage_ratio": ev.recovery_coverage_ratio,
        "dynamic_safety_requirement": ev.dynamic_safety_requirement,
        "trigger_levels": tl,
        "data_review_required": getattr(ev, "data_review_required", False),
        "reasons": reasons,
        "assets": assets,
    }


def _orders_from_evaluation(ev: Any) -> list[dict[str, Any]]:
    plan = getattr(ev, "recovery_plan", None)
    if isinstance(plan, dict) and plan.get("orders"):
        return [{"instrument": o.get("instrument_key") or o.get("asset_id"), "quantity": o.get("quantity"),
                 "expected_net_proceeds": o.get("expected_net_proceeds") or o.get("net_proceeds")}
                for o in plan["orders"]]
    lp = getattr(ev, "liquidation_plan", None)
    if lp is not None:
        return [{"instrument": getattr(o, "instrument_key", "") or o.asset_id, "quantity": o.quantity,
                 "expected_net_proceeds": o.estimated_cash_recovery} for o in lp.orders]
    return []


def _engine_result(model_id: str, version: str, role: str, ev: Any, loan_balance: float) -> ShadowResult:
    state = ev.margin_state.value if hasattr(ev.margin_state, "value") else str(ev.margin_state)
    mv = ev.portfolio_market_value or 0.0
    return ShadowResult(
        model_id=model_id, model_version=version, role=role,
        collateral_value=mv, loan_balance=loan_balance,
        approved_credit=ev.approved_credit_limit, available_credit=ev.current_available_credit,
        capacity_ltv=ev.approved_credit_limit / mv if mv else None,
        risk_state=state, margin_call=state in {"margin_call", "liquidation"},
        estimated_recovery=ev.stressed_liquidation_value,
        liquidation=_orders_from_evaluation(ev) if state in {"margin_call", "liquidation"} else [],
        explanation=_plain(explain_engine_evaluation(ev)),
    )


def baseline_model(inp: ShadowInputs) -> ShadowResult:
    from app.core.evaluator import CollateralRiskEngine
    from app.lifecycle.service import aggregate_holdings
    from app.version import RISK_MODEL_VERSION

    a = inp.account
    ev = CollateralRiskEngine(audit_logger=None).evaluate(
        a.account_ref, aggregate_holdings(inp.holdings), a.loan, a.policy, inp.market_data,
        execution_policy=a.liquidation_execution_policy, interest_policy=a.interest_policy, as_of=inp.now)
    return _engine_result("baseline", f"{RISK_MODEL_VERSION}+correctness-fixes", "baseline", ev, a.loan.balance)


def v070_model(inp: ShadowInputs) -> ShadowResult:
    import importlib

    evaluator = importlib.import_module(f"{V070_PACKAGE}.core.evaluator")
    lifecycle = importlib.import_module(f"{V070_PACKAGE}.lifecycle.service")
    version = importlib.import_module(f"{V070_PACKAGE}.version").RISK_MODEL_VERSION
    dropped: list[str] = []
    a = inp.account

    def t(x: Any) -> Any:
        return codec.translate(x, V070_PACKAGE, dropped)

    ev = evaluator.CollateralRiskEngine(audit_logger=None).evaluate(
        a.account_ref, lifecycle.aggregate_holdings(t(inp.holdings)), t(a.loan), t(a.policy),
        t(inp.market_data), execution_policy=t(a.liquidation_execution_policy),
        interest_policy=t(a.interest_policy), as_of=inp.now)
    out = _engine_result("cre_v070", f"{version}+correctness-fixes", "candidate", ev, a.loan.balance)
    if dropped:
        out.explanation["inputs_not_understood_by_this_version"] = sorted(set(dropped))
    return out


# ---------------------------------------------------------- research models

def _research_panel(inp: ShadowInputs) -> dict[str, Any]:
    import numpy as np

    from app.market_data.identity import InstrumentIdentity
    from app.markets.profiles import profile_for_exchange
    from app.platform.ingestion import load_bars
    from research.methodology_lab.real_data import corwin_schultz

    a = inp.account
    legs = []
    for h in inp.holdings:
        if h.asset_type.value == "cash":
            continue
        ident = InstrumentIdentity.from_holding(h)
        md = inp.market_data.get(h.asset_id)
        price = getattr(md, "last_price", None) if md is not None else None
        if not price:
            raise ValueError(f"no current price for {h.asset_id}")
        if ident.currency.upper() != a.loan_currency.upper():
            raise ValueError("research adapters do not convert currencies; loan and collateral currencies differ")
        prof = profile_for_exchange(ident.exchange)
        if prof is None:
            raise ValueError(f"no market profile for exchange {ident.exchange}")
        legs.append((ident.stable_key, h.quantity * price, prof, h, price))
    if not legs:
        raise ValueError("no non-cash collateral")
    bars = ({k: inp.bars[k][-(RESEARCH_HISTORY + 40):] for k, *_ in legs} if inp.bars is not None
            else {k: load_bars(k, RESEARCH_HISTORY + 40) for k, *_ in legs})
    dates = sorted(set().union(*[{b["bar_date"] for b in v} for v in bars.values()]))[-(RESEARCH_HISTORY + 1):]
    n, m = len(dates), len(legs)
    if n < RESEARCH_MIN_HISTORY:
        raise ValueError(f"insufficient stored history: {n} sessions (need {RESEARCH_MIN_HISTORY})")
    close = np.full((n, m), np.nan)
    high = np.full((n, m), np.nan)
    low = np.full((n, m), np.nan)
    ok = np.zeros((n, m), dtype=bool)
    value = np.full((n, m), np.nan)
    halted = np.zeros((n, m), dtype=bool)
    for j, (key, *_rest) in enumerate(legs):
        by_date = {b["bar_date"]: b for b in bars[key]}
        last = np.nan
        for i, d in enumerate(dates):
            b = by_date.get(d)
            if b is None:
                close[i, j] = last
                halted[i, j] = True
                continue
            last = b["close"]
            close[i, j] = b["close"]
            high[i, j], low[i, j] = b["high"] or np.nan, b["low"] or np.nan
            ok[i, j] = bool(b["range_reported"])
            if b["volume_reported"] and b["volume"]:
                value[i, j] = b["volume"] * b["close"]
    first_valid = np.argmax(~np.isnan(close).any(axis=1))
    close, high, low, ok, value, halted = (x[first_valid:] for x in (close, high, low, ok, value, halted))
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.diff(np.log(close), axis=0)
    r = np.nan_to_num(r)
    n = r.shape[0]
    if n < RESEARCH_MIN_HISTORY:
        raise ValueError(f"insufficient aligned history: {n} sessions")
    position_value = np.array([v for _, v, *_ in legs])
    total = float(position_value.sum())
    weights = position_value / total
    spread = np.zeros((n, m))
    adv = np.zeros((n, m))
    spread_source = []
    for j, (_key, _v, prof, _h, _p) in enumerate(legs):
        cs = corwin_schultz(high[1:, j], low[1:, j], ok[1:, j])
        observed = 0
        for i in range(n):
            w = cs[max(0, i - 20): i + 1]
            w = w[~np.isnan(w)]
            if w.size >= 5:
                spread[i, j] = max(prof.spread_floor, float(np.median(w)))
                observed += 1
            else:
                spread[i, j] = prof.spread_fallback
            v = value[1:][max(0, i - 20): i + 1, j]
            v = v[~np.isnan(v)]
            adv[i, j] = float(np.median(v)) / total if v.size >= 10 else np.nan
        spread_source.append("estimated" if observed else "profile_fallback")
    if np.isnan(adv[-1]).any():
        raise ValueError("insufficient reported volume to estimate liquidity")
    adv = np.where(np.isnan(adv), np.nanmedian(adv, axis=0), adv)
    return {"r": r, "spread": spread, "adv": adv, "halted": halted[1:], "weights": weights, "total": total,
            "legs": legs, "spread_source": spread_source, "sessions": n}


def _research_model(model_id: str, run: Callable[..., tuple[float, Any]]) -> Callable[[ShadowInputs], ShadowResult]:
    def model(inp: ShadowInputs) -> ShadowResult:
        import numpy as np

        from research.methodology_lab import lifecycle, methods

        a = inp.account
        panel = _research_panel(inp)
        asset_type = panel["legs"][0][3].asset_type.value
        f = methods.estimate_features(panel["r"], panel["spread"], np.maximum(panel["adv"], 1e-12),
                                      panel["halted"], None, panel["weights"], asset_type)
        prof = panel["legs"][0][2]
        participation = min(prof.max_participation_rate, a.policy.max_participation_rate or prof.max_participation_rate)
        pol = lifecycle.Policy(rate=float(getattr(a.interest_policy, "annual_interest_rate", 0.0) or 0.0),
                               participation=participation, settle_days=max(p.settlement_days for _, _, p, *_ in panel["legs"]))
        rng = np.random.default_rng(int(inp.now.strftime("%Y%m%d")))
        ell, detail = run(f, pol, rng)
        ceiling = min(a.policy.base_ltv.get(h.asset_type, 0.0) for *_x, h, _p in panel["legs"])
        ell_capped = min(ell, ceiling)
        v = panel["total"]
        loan = a.loan.balance
        approved = ell_capped * v
        ltv = loan / v if v else math.inf
        maintenance = lifecycle.Policy().psi * ell_capped
        if ell_capped <= 0:
            state = "ineligible"
        elif ltv <= ell_capped:
            state = "safe"
        elif ltv <= maintenance:
            state = "restricted"
        else:
            state = "margin_call"
        haircut = detail if isinstance(detail, float) else None
        cost = float(np.mean(f.spread * f.spread_stress_mult) / 2 + pol.fee)
        liquidation = []
        if state == "margin_call" and ell_capped > 0:
            # Sale value x restoring loan/collateral to the capacity ratio:
            # (L - x(1-c)) / (V - x) = ell  =>  x = (L - ell V) / (1 - c - ell)
            x = min(v, max(0.0, (loan - ell_capped * v) / max(1e-9, 1 - cost - ell_capped)))
            for (key, _pv, _p, _h, price), w in zip(panel["legs"], panel["weights"], strict=True):
                liquidation.append({"instrument": key, "quantity": x * w / price,
                                    "expected_net_proceeds": x * w * (1 - cost)})
        explanation = {
            "method": model_id,
            "capacity_ltv_uncapped": ell,
            "client_ceiling_ltv": ceiling,
            "capacity_ltv": ell_capped,
            "binding": "client ceiling" if ell > ceiling else "method capacity",
            "haircut": haircut,
            "current_ltv": ltv,
            "maintenance_ltv": maintenance,
            "sessions_used": panel["sessions"],
            "ewma_daily_vol": [float(x) for x in f.ewma_sigma],
            "long_run_daily_vol": [float(x) for x in f.long_sigma],
            "student_t_nu": f.nu,
            "spread": [float(x) for x in f.spread],
            "spread_source": panel["spread_source"],
            "adv_share_of_collateral": [float(x) for x in f.adv],
            "execution_days": [float(x) for x in methods.execution_days(f, pol)],
            "weights": [float(x) for x in panel["weights"]],
            "policy": asdict(pol),
            "c_detail": detail if isinstance(detail, dict) else None,
            "reasons": [
                (f"{model_id} capacity {ell:.1%} of collateral value; client ceiling {ceiling:.0%}; "
                 f"applied {ell_capped:.1%}"),
                f"current LTV {ltv:.1%}; maintenance trigger {maintenance:.1%} (1.15 x capacity)",
            ],
        }
        return ShadowResult(
            model_id=model_id, model_version=f"research-{model_id}-2026-11", role="candidate",
            collateral_value=v, loan_balance=loan, approved_credit=approved,
            available_credit=max(0.0, approved - loan), capacity_ltv=ell_capped, risk_state=state,
            margin_call=state == "margin_call",
            estimated_recovery=v * (1 - haircut) if haircut is not None else None,
            liquidation=liquidation, explanation=_plain(explanation),
        )

    return model


def _a(f: Any, pol: Any, rng: Any) -> tuple[float, Any]:
    from research.methodology_lab import methods

    return methods.approach_a(f, pol, 0.99, 0.95)


def _b(f: Any, pol: Any, rng: Any) -> tuple[float, Any]:
    from research.methodology_lab import methods

    return methods.approach_b(f, pol, 0.99, 0.95, rng, n_paths=1500)


def _b_gap(f: Any, pol: Any, rng: Any) -> tuple[float, Any]:
    from research.methodology_lab import methods

    return methods.approach_b(f, pol, 0.995, 0.95, rng, n_paths=1500, stress_share=0.25, with_gap=True)


def _c(f: Any, pol: Any, rng: Any) -> tuple[float, Any]:
    from research.methodology_lab import methods

    _, h_b = methods.approach_b(f, pol, 0.99, 0.95, rng, n_paths=1500)
    ell, stats = methods.approach_c(f, pol, methods.APPETITES["balanced"], 0.95, 126, rng, n_paths=300,
                                    haircut_b=h_b)
    return ell, {"appetite": "balanced", "haircut_b": h_b, **(stats or {})}


_PRIOR_CACHE: dict[tuple[str, str], Any] = {}


def candidate_priors(as_of: datetime, extra_bars: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Group priors per market from stored instruments with full histories,
    using only bars dated on or before ``as_of`` (point in time)."""
    import math as _m

    from app.candidate.config import CandidateConfig
    from app.candidate.estimation import group_prior, raw_daily_vol
    from app.markets.profiles import profile_for_exchange
    from app.platform.ingestion import load_bars

    cfg = CandidateConfig()
    day = as_of.date().isoformat()
    by_key: dict[str, list[dict[str, Any]]] = {}
    try:
        conn = current_connection()
        rows = conn.fetchall("SELECT stable_key, COUNT(*) AS n FROM market_bars WHERE bar_date <= %s "
                             "GROUP BY stable_key", (day,))
        keys = sorted(r["stable_key"] for r in rows if (r["n"] or 0) >= cfg.full_tail_sessions)
        cache_key = (day, "|".join(keys))
        if cache_key in _PRIOR_CACHE:
            return _PRIOR_CACHE[cache_key]
        for k in keys:
            by_key[k] = [b for b in load_bars(k, cfg.history + 1) if str(b["bar_date"])[:10] <= day]
    except Exception:  # noqa: BLE001 - priors are optional; the engine reports their absence
        cache_key = (day, "")
    for k, v in (extra_bars or {}).items():
        by_key.setdefault(k, [b for b in v if str(b["bar_date"])[:10] <= day])
    groups: dict[str, list[float]] = {}
    for k, v in by_key.items():
        prof = profile_for_exchange(k.split(":")[0])
        vol = raw_daily_vol(v, cfg) if prof is not None else None
        if prof is not None and vol:
            groups.setdefault(prof.code, []).append(_m.log(vol))
    out = {code: pr for code, lv in groups.items()
           if (pr := group_prior(lv, f"{code} instruments with >= {cfg.full_tail_sessions} sessions to {day}"))}
    _PRIOR_CACHE[cache_key] = out
    return out


def candidate_model(inp: ShadowInputs, overrides: tuple[tuple[str, Any], ...] = (),
                    model_id: str = "candidate") -> ShadowResult:
    from dataclasses import replace as _replace

    from app.candidate import CandidateConfig, CandidateEngine
    from app.candidate.engine import data_key
    from app.platform.ingestion import load_bars

    a = inp.account
    keys = {data_key(h) for h in inp.holdings if h.asset_type.value != "cash"}
    bars = ({k: inp.bars.get(k, []) for k in keys} if inp.bars is not None
            else {k: load_bars(k, 501) for k in keys})
    engine = CandidateEngine(_replace(CandidateConfig(), **dict(overrides)), priors=candidate_priors(inp.now, inp.bars))
    ev = engine.evaluate(a.account_ref, inp.holdings, a.loan, a.policy, inp.market_data, bars=bars,
                         execution_policy=a.liquidation_execution_policy, interest_policy=a.interest_policy,
                         as_of=inp.now)
    state = ev.margin_state.value
    plan = ev.recovery_plan or {}
    return ShadowResult(
        model_id=model_id, model_version=ev.model_version + ("+" + ",".join(k for k, _ in overrides) if overrides else ""),
        role="candidate",
        collateral_value=ev.portfolio_market_value, loan_balance=ev.current_outstanding_balance,
        approved_credit=ev.approved_credit_limit, available_credit=ev.current_available_credit,
        capacity_ltv=ev.threshold_ltv.get("initial_capacity"), risk_state=state,
        margin_call=state in {"margin_call", "liquidation"},
        estimated_recovery=ev.thresholds.get("margin_call_proceeds"),
        liquidation=[{"instrument": o["instrument_key"], "quantity": o["quantity"],
                      "expected_net_proceeds": o["expected_net_proceeds"]} for o in plan.get("orders", [])],
        explanation=_plain({
            "method": "candidate adaptive thresholds",
            "thresholds": ev.thresholds, "threshold_ltv": ev.threshold_ltv, "cure_amount": ev.cure_amount,
            "threshold_reasoning": ev.threshold_reasoning, "data_status": ev.data_status,
            "deterioration": ev.deterioration,
            "assets": [{"asset_id": x.asset_id, "market_value": x.market_value, "sellable": x.eligible,
                        "lending_value": x.lending_value_eligible, "participation_rate": x.participation_rate,
                        "execution_cost_rate": x.estimated_slippage_rate, "annual_vol_used": x.risk_score,
                        "data_status": x.data_status, "notes": x.notes} for x in ev.asset_results],
            "data_review_required": ev.data_review_required, "restriction": ev.restriction,
            "instruments": ev.instruments, "recovery_plan": plan or None, "parameters": ev.parameters,
            "reasons": ev.reasons,
        }),
    )


MODELS: dict[str, Callable[[ShadowInputs], ShadowResult]] = {
    "baseline": baseline_model,
    "cre_v070": v070_model,
    "A": _research_model("A", _a),
    "B": _research_model("B", _b),
    "B_gap": _research_model("B_gap", _b_gap),
    "C_balanced": _research_model("C_balanced", _c),
    "candidate": candidate_model,
    # Research variant (27 Nov review): adaptive headroom, long-run restoration
    # volatility, concentration add-on, delayed value of suspended instruments.
    "candidate_combined": lambda inp: candidate_model(
        inp, (("headroom_mode", "adaptive"), ("restoration_volatility", "long_run"),
              ("concentration_control", True), ("suspension_value", "delayed")), "candidate_combined"),
    # CRI v1.0 Release Candidate 1 (app/candidate/rc.py). Shadow only; never drives accounts.
    "candidate_rc": lambda inp: _rc_model(inp),
}


def _rc_model(inp: ShadowInputs) -> ShadowResult:
    from dataclasses import replace

    from app.candidate.rc import RC_ID, RC_OVERRIDES

    r = candidate_model(inp, RC_OVERRIDES, "candidate_rc")
    return replace(r, model_version=f"{RC_ID} ({r.model_version.split('+')[0]})")


def build_inputs(services: Any, tenant_id: str, account: Any, now: datetime) -> ShadowInputs:
    svc = services.service(tenant_id)
    life = services.lifecycle_engine
    aggregation = svc._normalize_market_data(account)
    market_data = life._market_data_with_pledged_cash(aggregation.to_core_market_data(), account.loan_currency)
    holdings = life._holdings_with_pledged_cash(account.holdings, account.pledged_cash_balance, account.loan_currency)
    return ShadowInputs(tenant_id, account, holdings, market_data, now)


def evaluate_all(inp: ShadowInputs, models: dict[str, Callable[[ShadowInputs], ShadowResult]] | None = None) -> list[ShadowResult]:
    out = []
    for model_id, fn in (models or MODELS).items():
        try:
            out.append(fn(inp))
        except Exception as exc:  # noqa: BLE001 - a candidate failure must not affect others
            out.append(ShadowResult(model_id=model_id, model_version="unknown",
                                    role="baseline" if model_id == "baseline" else "candidate",
                                    error=f"{type(exc).__name__}: {exc}"[:500]))
    return out


def persist(run_id: str, tenant_id: str, account_ref: str, results: list[ShadowResult], now: datetime) -> None:
    conn = current_connection()
    for r in results:
        conn.execute(
            "INSERT INTO shadow_evaluations (evaluation_id, run_id, tenant_id, account_ref, model_id, model_version, "
            "role, evaluated_at, collateral_value, loan_balance, approved_credit, available_credit, capacity_ltv, "
            "risk_state, margin_call, estimated_recovery, liquidation, explanation, error) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("sev_" + uuid.uuid4().hex[:20], run_id, tenant_id, account_ref, r.model_id, r.model_version, r.role,
             now.isoformat(), r.collateral_value, r.loan_balance, r.approved_credit, r.available_credit,
             r.capacity_ltv, r.risk_state, 1 if r.margin_call else 0, r.estimated_recovery,
             json.dumps(_plain(r.liquidation)), json.dumps(_plain(r.explanation), default=str), r.error))


def run_all(db: Database, services: Any, models: dict[str, Callable[[ShadowInputs], ShadowResult]] | None = None) -> dict[str, Any]:
    from app.platform.context import Principal, reset_principal, set_principal

    run_id = "shr_" + uuid.uuid4().hex[:16]
    started = datetime.now(UTC)
    with db.transaction("*") as conn:
        tenants = [r["tenant_id"] for r in conn.fetchall("SELECT tenant_id FROM tenants WHERE status='active'")]
    n_acc = n_eval = 0
    errors: list[dict[str, str]] = []
    for tenant in tenants:
        token = set_principal(Principal(tenant_id=tenant, role="system", key_id="shadow"))
        try:
            with db.transaction(tenant):
                accounts = services.service(tenant).account_repo.list_active()
            for account in accounts:
                now = datetime.now(UTC)
                try:
                    with db.transaction(tenant):
                        inp = build_inputs(services, tenant, account, now)
                        results = evaluate_all(inp, models)
                        persist(run_id, tenant, account.account_ref, results, now)
                    n_acc += 1
                    n_eval += len(results)
                    errors.extend({"account": f"{tenant}/{account.account_ref}", "model": r.model_id,
                                   "error": r.error} for r in results if r.error)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"account": f"{tenant}/{account.account_ref}", "error": str(exc)[:300]})
        finally:
            reset_principal(token)
    with db.transaction("*") as conn:
        conn.execute("INSERT INTO shadow_runs (run_id, started_at, finished_at, accounts, evaluations, errors) "
                     "VALUES (%s,%s,%s,%s,%s,%s)", (run_id, started.isoformat(), datetime.now(UTC).isoformat(),
                                                    n_acc, n_eval, json.dumps(errors)))
    for e in errors:
        logger.warning("shadow_model_error %s", json.dumps(e))
    return {"run_id": run_id, "accounts": n_acc, "evaluations": n_eval, "errors": errors}


# ------------------------------------------------------------- comparisons

def latest_by_account(tenant_id: str, account_ref: str) -> list[dict[str, Any]]:
    conn = current_connection()
    row = conn.fetchone("SELECT run_id FROM shadow_evaluations e WHERE tenant_id=%s AND account_ref=%s "
                        "AND NOT EXISTS (SELECT 1 FROM excluded_runs x WHERE x.kind='shadow' AND x.run_id=e.run_id "
                        "AND x.model_id='*') ORDER BY evaluated_at DESC LIMIT 1", (tenant_id, account_ref))
    if not row:
        return []
    rows = conn.fetchall("SELECT * FROM shadow_evaluations WHERE tenant_id=%s AND account_ref=%s AND run_id=%s "
                         "ORDER BY role, model_id", (tenant_id, account_ref, row["run_id"]))
    for r in rows:
        r["liquidation"] = json.loads(r["liquidation"])
        r["explanation"] = json.loads(r["explanation"])
    return rows


def disagreements(rows: list[dict[str, Any]], credit_tolerance: float = 0.10) -> list[dict[str, Any]]:
    base = next((r for r in rows if r["model_id"] == "baseline" and not r.get("error")), None)
    if base is None:
        return []
    out = []
    for r in rows:
        if r["model_id"] == "baseline" or r.get("error"):
            continue
        items = []
        if r["risk_state"] != base["risk_state"]:
            items.append(f"state {base['risk_state']} (baseline) vs {r['risk_state']}")
        bc, cc = base["approved_credit"] or 0.0, r["approved_credit"] or 0.0
        if max(bc, cc) > 0 and abs(cc - bc) / max(bc, cc) > credit_tolerance:
            items.append(f"approved credit {bc:,.0f} (baseline) vs {cc:,.0f} ({(cc - bc) / max(bc, 1e-9):+.0%})")
        if bool(r["margin_call"]) != bool(base["margin_call"]):
            items.append("margin-call decision differs")
        if items:
            out.append({"model_id": r["model_id"], "account_ref": r["account_ref"], "differences": items,
                        "candidate_reasons": (r["explanation"] or {}).get("reasons", []),
                        "baseline_reasons": (base["explanation"] or {}).get("reasons", [])})
    return out


def summary(tenant_id: str, since: datetime) -> dict[str, Any]:
    conn = current_connection()
    rows = conn.fetchall(
        "SELECT model_id, model_version, role, COUNT(*) AS evaluations, SUM(margin_call) AS margin_calls, "
        "AVG(capacity_ltv) AS avg_capacity_ltv, SUM(approved_credit) AS total_approved_credit, "
        "AVG(estimated_recovery / NULLIF(collateral_value, 0)) AS avg_recovery_ratio, "
        "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors "
        "FROM shadow_evaluations e WHERE tenant_id=%s AND evaluated_at >= %s "
        "AND NOT EXISTS (SELECT 1 FROM excluded_runs x WHERE x.kind='shadow' AND x.run_id=e.run_id "
        "AND (x.model_id=e.model_id OR x.model_id='*')) "
        "GROUP BY model_id, model_version, role ORDER BY role, model_id", (tenant_id, since.isoformat()))
    excluded = conn.fetchall("SELECT run_id, model_id, reason, recorded_at FROM excluded_runs WHERE kind='shadow' "
                             "ORDER BY recorded_at")
    for r in rows:
        ok = (r["evaluations"] or 0) - (r["errors"] or 0)
        r["margin_call_frequency"] = (r["margin_calls"] or 0) / ok if ok else None
    runs = conn.fetchall("SELECT DISTINCT run_id FROM shadow_evaluations WHERE tenant_id=%s AND evaluated_at >= %s",
                         (tenant_id, since.isoformat()))
    n_dis: dict[str, int] = {}
    accounts = conn.fetchall("SELECT DISTINCT account_ref FROM shadow_evaluations WHERE tenant_id=%s", (tenant_id,))
    for a in accounts:
        for d in disagreements(latest_by_account(tenant_id, a["account_ref"])):
            n_dis[d["model_id"]] = n_dis.get(d["model_id"], 0) + 1
    for r in rows:
        r["accounts_disagreeing_with_baseline_now"] = n_dis.get(r["model_id"], 0)
    return {"since": since.isoformat(), "runs": len(runs), "models": rows,
            "excluded_runs": excluded,
            "note": "excluded runs are omitted from these figures and kept in the evaluation history"}
