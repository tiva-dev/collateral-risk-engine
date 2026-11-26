"""Lifecycle validation of intervention policies on matched loans.

A cohort is one origination: collateral, principal, term, interest terms.
Every variant receives the same cohort (matched initial exposure), the same
market path and the same random numbers (cure draws, engine scenario seeds):
common random numbers. Only the intervention policy (and, for cure
sensitivity, the borrower's cure probability) differs.

Each session, per variant:

1. contractual interest accrues with the production interest engine
   (capitalization anchored at origination);
2. settled sale proceeds repay the loan;
3. the variant's thresholds are computed from information available that day
   (adaptive: the candidate engine; fixed: psi x origination LTV);
4. the account is classified (safe, restricted, early warning, margin call,
   forced recovery; data review holds the account without a sale);
5. a margin call opens a grace period; the borrower cures with the variant's
   cure probability (same uniform draw for every variant), paying down to the
   restoration target; otherwise, after grace (immediately when forced),
6. the UNIFIED RECOVERY PLANNER sizes the sale through the candidate engine
   (the actual recovery decision, with the variant's restoration target);
7. orders execute against the observed (or, in simulated runs, synthetic)
   session: participation cap of that session's volume, close less the
   session's half-spread, square-root impact on realised volatility, fee;
   sessions without a bar or volume do not fill; proceeds settle after the
   market's settlement lag; when orders complete and settle the account is
   re-evaluated and, if still in breach, re-planned;
8. at maturity the borrower repays a covered account not in recovery;
   otherwise the remaining collateral is worked out and any shortfall is
   lender loss.

Each liquidation episode is also checked against the no-intervention
counterfactual on the same path: it is "unnecessary" if, had nothing been
sold, the collateral's net value would have covered the obligation at every
remaining session through maturity.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import numpy as np

from app.candidate.config import CandidateConfig
from app.candidate.engine import CandidateEngine, data_key
from app.core.enums import AssetType, MarginState
from app.core.models import Holding, Loan, MarketData, Policy
from app.credit.interest import (
    InterestPolicy,
    accrue_scheduled_periods,
    apply_repayment,
)
from app.liquidation.policy import LiquidationExecutionPolicy
from app.markets.profiles import profile_for_exchange

BAD = {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str                                  # adaptive | fixed
    psi: float = 1.15                          # fixed: margin call at psi x origination LTV
    restoration: str = "initial_capacity"      # adaptive: initial_capacity | early_warning
    cure_probability: float | None = None      # None: the cohort's default


DEFAULT_VARIANTS = (
    Variant("fixed_psi_1.15", "fixed", psi=1.15),
    Variant("fixed_psi_1.30", "fixed", psi=1.30),
    Variant("adaptive", "adaptive", restoration="initial_capacity"),
    Variant("adaptive_restore_warning", "adaptive", restoration="early_warning"),
)


def cure_sensitivity(probabilities: tuple[float, ...] = (0.0, 0.25, 0.5, 0.9)) -> tuple[Variant, ...]:
    out = []
    for p in probabilities:
        out += [Variant(f"fixed_psi_1.15@cure{p}", "fixed", psi=1.15, cure_probability=p),
                Variant(f"adaptive@cure{p}", "adaptive", cure_probability=p)]
    return tuple(out)


@dataclass
class Outcome:
    variant: str
    principal: float
    initial_ltv: float
    lender_loss: float = 0.0
    repaid_at_maturity: bool = False
    sessions: int = 0
    sessions_restricted: int = 0
    sessions_warning: int = 0
    sessions_margin_call: int = 0
    sessions_forced: int = 0
    sessions_data_review: int = 0
    margin_calls: int = 0
    forced_events: int = 0
    cures: int = 0
    cure_payments: float = 0.0
    liquidation_episodes: int = 0
    unnecessary_liquidations: int = 0
    liquidated_value: float = 0.0
    execution_costs: float = 0.0
    no_fill_sessions: int = 0
    recovery_sessions: list[int] = field(default_factory=list)
    residual_after_recovery: list[float] = field(default_factory=list)
    interest_accrued: float = 0.0
    capacity_sum: float = 0.0
    final_balance: float = 0.0
    censored: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mean_capacity"] = self.capacity_sum / self.sessions if self.sessions else None
        d.pop("capacity_sum")
        d["events"] = self.events[:40]
        return d


def _date(x: Any) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def _half_spread(b: dict[str, Any], floor: float, fallback: float) -> float:
    hi, lo = float(b.get("high") or 0), float(b.get("low") or 0)
    if b.get("range_reported") and hi > 0 and lo > 0 and hi >= lo:
        return max(floor / 2, min(0.05, math.log(hi / lo) / 4))
    return fallback / 2


def run_cohort(
    *,
    holdings: list[Holding],
    bars: dict[str, list[dict[str, Any]]],
    origination: date,
    term_sessions: int,
    variants: tuple[Variant, ...] = DEFAULT_VARIANTS,
    policy: Policy | None = None,
    interest: InterestPolicy | None = None,
    execution: LiquidationExecutionPolicy | None = None,
    cure_probability: float = 0.5,
    seed: int = 0,
    config: CandidateConfig | None = None,
    priors: dict[str, Any] | None = None,
    principal: float | None = None,
    workout_sessions: int = 60,
) -> dict[str, Any]:
    """Run all variants on one matched cohort with common random numbers."""
    policy = policy or Policy.default()
    interest = interest or InterestPolicy(0.20, accrual_frequency="monthly", compounding="compound")
    execution = execution or LiquidationExecutionPolicy()
    cfg = config or CandidateConfig(n_paths=400)
    ccy = holdings[0].currency.upper()
    cash_keys = {h.stable_key for h in holdings if h.asset_type == AssetType.CASH}
    keys = {h.stable_key: data_key(h) for h in holdings if h.asset_type != AssetType.CASH}
    series = {dk: {_date(b["bar_date"]): b for b in bars.get(dk, [])} for dk in keys.values()}
    sorted_bars = {dk: sorted(series[dk].items()) for dk in keys.values()}
    all_dates = sorted(set().union(*[set(s) for s in series.values()])) if series else []
    future = [d for d in all_dates if d > origination]
    if len(future) < term_sessions:
        raise ValueError(f"only {len(future)} sessions after origination; term needs {term_sessions}")
    path = future[: term_sessions + workout_sessions]
    censored_path = len(path) < term_sessions + workout_sessions
    rng = np.random.default_rng(seed)
    cure_draws = rng.random(len(path))
    engine_seed = int(rng.integers(0, 2**40))
    profiles = {h.stable_key: profile_for_exchange(h.exchange) for h in holdings if h.asset_type != AssetType.CASH}
    settle = max([execution.settlement_delay_observations] + [p.settlement_days for p in profiles.values() if p])
    vol20: dict[str, dict[date, float]] = {}
    for dk, rows in sorted_bars.items():
        closes = np.array([float(b["close"]) for _, b in rows])
        r = np.diff(np.log(closes)) if closes.size > 1 else np.zeros(0)
        vol20[dk] = {rows[j + 1][0]: float(r[max(0, j - 19): j + 1].std()) if j >= 4 else 0.02 for j in range(r.size)}
    last_bar_day: dict[str, date] = {}

    def market(d: date, last: dict[str, float]) -> dict[str, MarketData]:
        out = {}
        for h in holdings:
            if h.asset_type == AssetType.CASH:
                continue
            b = series[keys[h.stable_key]].get(d)
            if b:
                last[h.stable_key] = float(b["close"])
                last_bar_day[h.stable_key] = d
            px = last.get(h.stable_key)
            if not px:
                continue
            ts = datetime.combine(last_bar_day.get(h.stable_key, d), time(21), UTC)
            out[h.stable_key] = MarketData(h.asset_id, px, timestamp=ts)
        return out

    engines = {r: CandidateEngine(replace(cfg, restoration=r), priors) for r in ("initial_capacity", "early_warning")}
    cache: dict[tuple[Any, ...], Any] = {}

    def visible(d: date) -> dict[str, list[dict[str, Any]]]:
        return {dk: [b for bd, b in rows if bd <= d] for dk, rows in sorted_bars.items()}

    def evaluate(qty: dict[str, float], d: date, md: dict[str, MarketData]) -> Any:
        """Thresholds depend on collateral and date, not on the balance."""
        k = (d, tuple(sorted((a, round(b, 6)) for a, b in qty.items())))
        if k not in cache:
            hs = [replace(h, quantity=qty.get(h.stable_key, 0.0)) for h in holdings if qty.get(h.stable_key, 0.0) > 0]
            cache[k] = engines["initial_capacity"].evaluate(
                "lifecycle", hs, Loan(0.0, currency=ccy), policy, md, bars=visible(d), execution_policy=execution,
                interest_policy=interest, as_of=datetime.combine(d, time(21), UTC), seed=engine_seed, plan=False)
        return cache[k]

    def plan(v: Variant, qty: dict[str, float], loan: Loan, d: date, md: dict[str, MarketData], state: MarginState,
             ltv0: float, anchored: InterestPolicy) -> tuple[dict[str, float], bool]:
        """The actual candidate recovery decision through the unified planner."""
        hs = [replace(h, quantity=qty.get(h.stable_key, 0.0)) for h in holdings if qty.get(h.stable_key, 0.0) > 0]
        override = None
        if v.kind == "fixed":
            def override(q: np.ndarray, c: float, sim: Any) -> float:
                return ltv0 * (float(q @ sim.price) + c)
        eng = engines["early_warning" if v.restoration == "early_warning" else "initial_capacity"]
        ev = eng.evaluate("lifecycle", hs, loan, policy, md, bars=visible(d), execution_policy=execution,
                          interest_policy=anchored, as_of=datetime.combine(d, time(21), UTC), seed=engine_seed,
                          plan=True, restoration_override=override, force_plan_state=state)
        orders: dict[str, float] = {}
        for o in (ev.recovery_plan or {}).get("orders", []):
            orders[o["instrument_key"]] = orders.get(o["instrument_key"], 0.0) + float(o["quantity"])
        return orders, bool(ev.data_review_required)

    md0 = market(origination, {})
    q0 = {h.stable_key: h.quantity for h in holdings}
    cash0 = sum(h.quantity for h in holdings if h.asset_type == AssetType.CASH)
    v0 = sum(q0[k] * md0[k].last_price for k in md0) + cash0
    base = evaluate(q0, origination, md0)
    p0 = float(principal if principal is not None else base.approved_credit_limit)
    if p0 <= 0:
        return {"origination": origination.isoformat(), "skipped": "no lending capacity at origination",
                "reasons": base.reasons[:6]}
    anchored = replace(interest, schedule_anchor=datetime.combine(origination, time(21), UTC))
    ltv0 = p0 / v0

    # No-intervention counterfactual on the same path: net collateral value
    # (2% below the close, assumption) minus the contractual obligation.
    margin_cf = np.zeros(len(path))
    lp: dict[str, float] = {k: md0[k].last_price for k in md0}
    loan_cf, prev = Loan(p0, currency=ccy), datetime.combine(origination, time(21), UTC)
    for i, d in enumerate(path):
        now = datetime.combine(d, time(21), UTC)
        loan_cf, _ = accrue_scheduled_periods(loan_cf, anchored, prev, now)
        prev = now
        md = market(d, lp)
        val = cash0 + sum(q0[k] * m.last_price * 0.98 for k, m in md.items())
        margin_cf[i] = val - loan_cf.balance

    def unnecessary(i: int) -> bool:
        return bool(margin_cf[i:term_sessions].min() > 0) if i < term_sessions else False

    results = {}
    for v in variants:
        cure_p = cure_probability if v.cure_probability is None else v.cure_probability
        loan = Loan(p0, currency=ccy)
        qty = dict(q0)
        out = Outcome(v.name, p0, ltv0, censored=censored_path)
        last_px: dict[str, float] = {k: md0[k].last_price for k in md0}
        prev_t = datetime.combine(origination, time(21), UTC)
        episode: str | None = None       # None | margin_call | selling | workout
        grace_left = 0
        orders: dict[str, float] = {}
        pending: list[tuple[int, float]] = []
        sale_start = -1
        matured = False
        for i, d in enumerate(path):
            now = datetime.combine(d, time(21), UTC)
            before = loan.balance
            loan, _ = accrue_scheduled_periods(loan, anchored, prev_t, now)
            out.interest_accrued += loan.balance - before
            prev_t = now
            for _, amt in [x for x in pending if x[0] <= i]:
                loan, _ = apply_repayment(loan, amt)
            pending = [x for x in pending if x[0] > i]
            md = market(d, last_px)
            cash = sum(q for k, q in qty.items() if k in cash_keys)
            value = cash + sum(qty.get(k, 0.0) * m.last_price for k, m in md.items())
            bal = loan.balance
            net_bal = max(0.0, bal - sum(a for _, a in pending))
            if bal <= 0.01 and not pending:
                break
            if i >= term_sessions and not matured:
                matured = True
                if episode is None:
                    out.repaid_at_maturity = True
                    loan = Loan(0.0, currency=ccy)
                    out.events.append({"session": i, "date": d.isoformat(), "event": "repaid_at_maturity",
                                       "balance": bal})
                    break
                episode = "workout"
                orders = {k: q for k, q in qty.items() if k not in cash_keys and q > 0}
            if episode != "workout":
                if v.kind == "adaptive":
                    ev = evaluate(qty, d, md)
                    th = ev.thresholds
                    b0, b_ew, b_mc, b_f = (th["initial_capacity"], th["early_warning"], th["margin_call"],
                                           th["forced_recovery"])
                    target = th["initial_capacity_uncapped"] if v.restoration == "initial_capacity" else b_ew
                    # Data deterioration never triggers a sale: if the breach disappears on the
                    # latest reliable data (or a price is missing), the account is held for review.
                    te = ev.thresholds_economic
                    data_hold = ev.data_status == "missing" or (
                        ev.data_status == "degraded" and net_bal <= te["margin_call"])
                else:
                    b0 = ltv0 * value
                    b_ew = b0
                    b_mc = v.psi * ltv0 * value
                    b_f = math.inf
                    target = b0
                    data_hold = False
                out.sessions += 1
                out.capacity_sum += b0
                state = (MarginState.LIQUIDATION if net_bal > b_f else MarginState.MARGIN_CALL if net_bal > b_mc
                         else MarginState.WATCH if net_bal > b_ew else MarginState.RESTRICT_NEW_BORROWING
                         if net_bal > b0 + 1e-9 else MarginState.SAFE)
                if data_hold and state in BAD:
                    out.sessions_data_review += 1
                    state = MarginState.RESTRICT_NEW_BORROWING
                out.sessions_restricted += state != MarginState.SAFE
                out.sessions_warning += state == MarginState.WATCH
                out.sessions_margin_call += state == MarginState.MARGIN_CALL
                out.sessions_forced += state == MarginState.LIQUIDATION
                if episode == "selling" and sale_start >= 0 and not orders and not pending:
                    # Orders done and settled: restored, or re-planned.
                    out.recovery_sessions.append(i - sale_start)
                    out.residual_after_recovery.append(max(0.0, net_bal - target))
                    if state not in BAD:
                        episode, sale_start = None, -1
                        out.events.append({"session": i, "date": d.isoformat(), "event": "restored",
                                           "balance": net_bal})
                    else:
                        sale_start = -1   # plan again below
                if episode is None and state == MarginState.LIQUIDATION:
                    out.forced_events += 1
                    episode = "selling"
                elif episode is None and state == MarginState.MARGIN_CALL:
                    episode, grace_left = "margin_call", execution.margin_call_grace_observations
                    out.margin_calls += 1
                    out.events.append({"session": i, "date": d.isoformat(), "event": "margin_call",
                                       "balance": net_bal, "threshold": b_mc})
                elif episode == "margin_call":
                    if state not in BAD:
                        episode = None
                    elif state == MarginState.LIQUIDATION:
                        out.forced_events += 1
                        episode = "selling"
                    elif grace_left > 0:
                        grace_left -= 1
                        if cure_draws[i] < cure_p:
                            pay = max(0.0, net_bal - target)
                            loan, _ = apply_repayment(loan, pay)
                            out.cures += 1
                            out.cure_payments += pay
                            out.events.append({"session": i, "date": d.isoformat(), "event": "cure", "amount": pay})
                            episode = None
                            continue
                    else:
                        episode = "selling"
                if episode == "selling" and sale_start < 0:
                    plan_state = state if state in BAD else MarginState.MARGIN_CALL
                    orders, review = plan(v, qty, loan, d, md, plan_state, ltv0, anchored)
                    if not orders:
                        # No plan: data review holds the account (no sale); otherwise
                        # nothing is sellable by the planner today and nothing is sold.
                        out.events.append({"session": i, "date": d.isoformat(),
                                           "event": "data_review_hold" if review else "no_executable_plan"})
                        episode, sale_start = (None, -1)
                        continue
                    sale_start = i
                    out.liquidation_episodes += 1
                    out.unnecessary_liquidations += unnecessary(i)
                    out.events.append({"session": i, "date": d.isoformat(), "event": "liquidation",
                                       "balance": net_bal, "orders": {k: round(q, 4) for k, q in orders.items()}})
            if episode not in ("selling", "workout") or not orders:
                continue
            # Execute today's orders against the session.
            for k in list(orders):
                q = min(orders[k], qty.get(k, 0.0))
                if q <= 0:
                    orders.pop(k)
                    continue
                b = series[keys[k]].get(d)
                prof = profiles[k]
                vol_shares = float(b["volume"]) if b and b.get("volume_reported") and b.get("volume") else 0.0
                part = min(prof.max_participation_rate, execution.max_participation_rate) if prof else 0.0
                if not b or vol_shares <= 0 or prof is None:
                    out.no_fill_sessions += 1
                    continue
                sell = min(q, part * vol_shares)
                hs = _half_spread(b, prof.spread_floor, prof.spread_fallback)
                impact = cfg.impact_coefficient * vol20[keys[k]].get(d, 0.02) * math.sqrt(part)
                px = float(b["close"])
                proceeds = sell * px * max(0.0, 1 - hs - impact - execution.execution_cost_rate)
                qty[k] = qty.get(k, 0.0) - sell
                orders[k] = q - sell
                if orders[k] <= 1e-9:
                    orders.pop(k)
                pending.append((i + settle, proceeds))
                out.liquidated_value += sell * px
                out.execution_costs += sell * px - proceeds
            if episode == "workout" and not orders and not pending:
                break
        for _, amt in pending:
            loan, _ = apply_repayment(loan, amt)
        if not out.repaid_at_maturity:
            cash = sum(q for k, q in qty.items() if k in cash_keys)
            loan, _ = apply_repayment(loan, cash)
            out.final_balance = loan.balance
            remaining = {k: q for k, q in qty.items() if k not in cash_keys and q > 1e-9}
            if loan.balance > 0.01:
                if remaining:
                    # Workout window exhausted: remaining collateral at the last
                    # close less 3% (assumption); any shortfall is loss.
                    lastv = sum(q * last_px.get(k, 0.0) * 0.97 for k, q in remaining.items())
                    out.lender_loss = max(0.0, loan.balance - lastv)
                    out.censored = True
                else:
                    out.lender_loss = loan.balance
        results[v.name] = out.to_dict()
    return {"origination": origination.isoformat(), "principal": p0, "collateral_value": v0,
            "initial_ltv": ltv0, "term_sessions": term_sessions, "cure_probability": cure_probability,
            "seed": seed, "settlement_sessions": settle, "variants": results, "censored": censored_path,
            "candidate_initial_reasons": base.reasons[:4]}


def _block_bootstrap(values: np.ndarray, block: int, reps: int, rng: np.random.Generator) -> np.ndarray:
    n = values.size
    if n == 0:
        return np.zeros(reps)
    block = max(1, min(block, n))
    nb = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(reps, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(reps, -1)[:, :n]
    return values[idx].mean(axis=1)


def aggregate(cohorts: list[dict[str, Any]], spacing_sessions: int | None = None,
              term_sessions: int | None = None, seed: int = 7) -> dict[str, Any]:
    """Metrics per variant with 95% intervals from a moving-block bootstrap over
    origination order (block = term / spacing, so overlapping cohorts are
    resampled together). Effective independent cohorts = n / block."""
    done = [c for c in cohorts if "variants" in c]
    names = sorted({n for c in done for n in c["variants"]})
    block = max(1, math.ceil((term_sessions or 1) / max(spacing_sessions or term_sessions or 1, 1)))
    rng = np.random.default_rng(seed)
    out: dict[str, Any] = {"cohorts": len(done), "skipped": len(cohorts) - len(done), "block": block,
                           "effective_independent_cohorts": len(done) / block, "variants": {}}
    for n in names:
        rows = [c["variants"][n] for c in done if n in c["variants"]]
        principal = np.array([r["principal"] for r in rows])
        loss = np.array([r["lender_loss"] for r in rows]) / principal

        def ci(x: np.ndarray) -> list[float]:
            b = _block_bootstrap(x, block, 2000, rng)
            return [float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))]

        def es(x: np.ndarray, q: float) -> float:
            k = max(1, math.ceil((1 - q) * x.size))
            return float(np.sort(x)[-k:].mean())

        has_loss = (loss > 1e-6).astype(float)
        liq = np.array([r["liquidation_episodes"] > 0 for r in rows], dtype=float)
        calls = np.array([r["margin_calls"] for r in rows], dtype=float)
        episodes = float(sum(r["liquidation_episodes"] for r in rows))
        sess = np.array([max(r["sessions"], 1) for r in rows], dtype=float)
        rec = [x for r in rows for x in r["recovery_sessions"]]
        resid = [x for r in rows for x in r["residual_after_recovery"]]
        out["variants"][n] = {
            "loans": len(rows),
            "mean_capacity_share_of_principal": float(np.mean([(r["mean_capacity"] or 0) / r["principal"]
                                                               for r in rows])),
            "loss_probability": float(has_loss.mean()), "loss_probability_ci95": ci(has_loss),
            "expected_loss": float(loss.mean()), "expected_loss_ci95": ci(loss),
            "es95_loss": es(loss, 0.95), "es99_loss": es(loss, 0.99), "worst_loss": float(loss.max()),
            "margin_calls_per_loan": float(calls.mean()), "margin_calls_ci95": ci(calls),
            "liquidation_frequency": float(liq.mean()), "liquidation_frequency_ci95": ci(liq),
            "unnecessary_liquidation_share": (sum(r["unnecessary_liquidations"] for r in rows) / episodes
                                              if episodes else 0.0),
            "borrower_execution_cost_share": float(sum(r["execution_costs"] for r in rows) / principal.sum()),
            "liquidated_share_of_principal": float(sum(r["liquidated_value"] for r in rows) / principal.sum()),
            "cure_payments_share": float(sum(r["cure_payments"] for r in rows) / principal.sum()),
            "recovery_sessions_median": float(np.median(rec)) if rec else None,
            "recovery_sessions_p95": float(np.percentile(rec, 95)) if rec else None,
            "residual_after_recovery_share": float(sum(resid) / principal.sum()) if resid else 0.0,
            "time_restricted": float(np.mean(np.array([r["sessions_restricted"] for r in rows]) / sess)),
            "time_margin_call_or_forced": float(np.mean(
                np.array([r["sessions_margin_call"] + r["sessions_forced"] for r in rows]) / sess)),
            "data_review_sessions": int(sum(r["sessions_data_review"] for r in rows)),
            "no_fill_sessions": int(sum(r["no_fill_sessions"] for r in rows)),
            "repaid_at_maturity": float(np.mean([r["repaid_at_maturity"] for r in rows])),
            "censored": int(sum(r["censored"] for r in rows)),
        }
    return out


def simulated_bars(bars: list[dict[str, Any]], returns: np.ndarray, start: date,
                   volume_multiplier: np.ndarray | None = None, provider: str = "synthetic") -> list[dict[str, Any]]:
    """Observed bars up to ``start`` followed by a SYNTHETIC path (labelled).
    A non-finite return marks a suspended session (no bar)."""
    hist = [b for b in bars if _date(b["bar_date"]) <= start]
    px = float(hist[-1]["close"])
    vols = [float(b["volume"]) for b in hist[-20:] if b.get("volume_reported") and b.get("volume")]
    vol = float(np.median(vols)) if vols else 0.0
    hl = float(np.median([math.log(b["high"] / b["low"]) for b in hist[-20:]
                          if b.get("range_reported") and b.get("low")] or [0.01]))
    d, out = start, list(hist)
    for i, r in enumerate(returns):
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if not np.isfinite(r):
            continue
        px *= math.exp(float(r))
        vm = float(volume_multiplier[i]) if volume_multiplier is not None else 1.0
        out.append({"bar_date": d, "open": px, "high": px * math.exp(hl / 2), "low": px * math.exp(-hl / 2),
                    "close": px, "volume": vol * vm, "range_reported": True, "volume_reported": vol * vm > 0,
                    "provider": provider})
    return out
