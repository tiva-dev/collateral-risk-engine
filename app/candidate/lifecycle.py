"""Lifecycle validation of intervention policies on matched loans.

A cohort is one origination: a collateral portfolio, a loan principal and a
term. Every intervention variant in a comparison receives the same cohort
(same origination date, collateral and principal: matched initial exposure)
and the same market path and the same random numbers (borrower cure draws,
scenario seeds): common random numbers. Only the intervention policy differs.

Each session the simulator, per variant:

1. accrues contractual interest with the production interest engine
   (capitalization boundaries anchored at origination);
2. applies sale proceeds that have settled;
3. computes the variant's thresholds from information available that day;
4. classifies the account (safe, restricted, early warning, margin call,
   forced recovery) and records time in each state;
5. on a margin call starts a grace period; the borrower cures with the
   cohort's cure probability (same draw for every variant), paying the
   amount that restores the variant's restoration target; otherwise, after
   grace, and immediately in forced recovery, collateral is sold;
6. executes sales at that session's observed close less the stressed
   half-spread, square-root impact and fees, limited by the participation
   cap of that session's reported volume; proceeds settle after the market's
   settlement lag;
7. at maturity the borrower repays an account that is not in recovery;
   otherwise the remaining collateral is worked out and any shortfall is
   lender loss.

Paths are either observed bars (historical lifecycle backtest) or filtered
historical simulations appended after the origination date (simulated
lifecycle, labelled as such). Simulated paths never replace missing
observations in a historical run: a session with no observed bar is a
session with no trade.
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


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str                      # adaptive | fixed
    psi: float = 1.15              # fixed: margin call at psi x origination LTV
    restoration: str = "initial_capacity"   # adaptive: initial_capacity | early_warning
    forced_psi: float | None = None          # fixed: forced recovery at forced_psi x origination LTV


DEFAULT_VARIANTS = (
    Variant("fixed_psi_1.15", "fixed", psi=1.15),
    Variant("fixed_psi_1.30", "fixed", psi=1.30),
    Variant("adaptive", "adaptive", restoration="initial_capacity"),
    Variant("adaptive_restore_warning", "adaptive", restoration="early_warning"),
)


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
    margin_calls: int = 0
    cures: int = 0
    cure_payments: float = 0.0
    liquidation_episodes: int = 0
    liquidated_value: float = 0.0
    execution_costs: float = 0.0
    interest_accrued: float = 0.0
    capacity_sum: float = 0.0
    final_balance: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mean_capacity"] = self.capacity_sum / self.sessions if self.sessions else None
        d.pop("capacity_sum")
        d["events"] = self.events[:40]
        return d


@dataclass
class _State:
    loan: Loan
    qty: dict[str, float]
    episode: str | None = None      # None | margin_call | forced
    grace_left: int = 0
    pending: list[tuple[int, float]] = field(default_factory=list)   # (due index, amount)
    selling: bool = False


def _bar(series: dict[date, dict[str, Any]], d: date) -> dict[str, Any] | None:
    return series.get(d)


def _half_spread(b: dict[str, Any], floor: float, fallback: float) -> float:
    hi, lo = float(b.get("high") or 0), float(b.get("low") or 0)
    if b.get("range_reported") and hi > 0 and lo > 0 and hi >= lo:
        return max(floor, min(0.1, math.log(hi / lo) / 4))
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
    cfg = config or CandidateConfig(n_paths=800)
    ccy = holdings[0].currency.upper()
    keys = {h.stable_key: data_key(h) for h in holdings if h.asset_type != AssetType.CASH}
    series = {k: {(b["bar_date"] if isinstance(b["bar_date"], date) else date.fromisoformat(str(b["bar_date"])[:10])): b
                  for b in bars.get(k, [])} for k in keys.values()}
    all_dates = sorted(set().union(*[set(s) for s in series.values()])) if series else []
    future = [d for d in all_dates if d > origination]
    if len(future) < term_sessions:
        raise ValueError(f"only {len(future)} sessions after origination; term needs {term_sessions}")
    path = future[: term_sessions + workout_sessions]
    rng = np.random.default_rng(seed)
    cure_draws = rng.random(len(path))
    engine_seed = int(rng.integers(0, 2**40))
    cash_keys = {h.stable_key for h in holdings if h.asset_type == AssetType.CASH}
    profiles = {h.stable_key: profile_for_exchange(h.exchange) for h in holdings if h.asset_type != AssetType.CASH}
    settle = max([execution.settlement_delay_observations] + [p.settlement_days for p in profiles.values() if p])

    def market(d: date, last: dict[str, float]) -> dict[str, MarketData]:
        out = {}
        for h in holdings:
            if h.asset_type == AssetType.CASH:
                continue
            b = series[keys[h.stable_key]].get(d)
            px = float(b["close"]) if b else last.get(h.stable_key)
            if not px:
                continue
            last[h.stable_key] = px
            prof = profiles[h.stable_key]
            hs = _half_spread(b, prof.spread_floor, prof.spread_fallback) if (b and prof) else 0.01
            out[h.stable_key] = MarketData(h.asset_id, px, bid=px * (1 - hs), ask=px * (1 + hs),
                                           timestamp=datetime.combine(d, time(21), UTC))
        return out

    engine = CandidateEngine(cfg, priors)
    cache: dict[tuple[Any, ...], Any] = {}
    sorted_bars = {dk: sorted(series[dk].items()) for dk in keys.values()}
    vol20: dict[str, dict[date, float]] = {}
    for dk, rows in sorted_bars.items():
        closes = np.array([float(b["close"]) for _, b in rows])
        r = np.diff(np.log(closes)) if closes.size > 1 else np.zeros(0)
        vol20[dk] = {rows[j + 1][0]: float(r[max(0, j - 19): j + 1].std()) if j >= 4 else 0.02
                     for j in range(r.size)}

    def evaluate(qty: dict[str, float], d: date, md: dict[str, MarketData]) -> Any:
        """Thresholds depend on collateral and date, not on the balance."""
        k = (d, tuple(sorted((a, round(b, 6)) for a, b in qty.items())))
        if k not in cache:
            hs = [replace(h, quantity=qty.get(h.stable_key, 0.0)) for h in holdings if qty.get(h.stable_key, 0.0) > 0]
            visible = {dk: [b for bd, b in rows if bd <= d] for dk, rows in sorted_bars.items()}
            cache[k] = engine.evaluate(
                "lifecycle", hs, Loan(0.0, currency=ccy), policy, md, bars=visible, execution_policy=execution,
                interest_policy=interest, as_of=datetime.combine(d, time(21), UTC), seed=engine_seed, plan=False)
        return cache[k]

    md0 = market(origination, {})
    q0 = {h.stable_key: h.quantity for h in holdings}
    v0 = sum(q0[k] * md0[k].last_price for k in md0) + sum(h.quantity for h in holdings if h.asset_type == AssetType.CASH)
    base = evaluate(q0, origination, md0)
    p0 = float(principal if principal is not None else base.approved_credit_limit)
    if p0 <= 0:
        return {"origination": origination.isoformat(), "skipped": "no lending capacity at origination",
                "reasons": base.reasons[:6]}
    anchored = replace(interest, schedule_anchor=datetime.combine(origination, time(21), UTC))
    ltv0 = p0 / v0
    results = {}
    for v in variants:
        st = _State(Loan(p0, currency=ccy), dict(q0))
        out = Outcome(v.name, p0, ltv0)
        last_px: dict[str, float] = {k: md0[k].last_price for k in md0}
        prev_t = datetime.combine(origination, time(21), UTC)
        matured = False
        for i, d in enumerate(path):
            now = datetime.combine(d, time(21), UTC)
            before = st.loan.balance
            st.loan, _ = accrue_scheduled_periods(st.loan, anchored, prev_t, now)
            out.interest_accrued += st.loan.balance - before
            prev_t = now
            for due, amt in [x for x in st.pending if x[0] <= i]:
                st.loan, _ = apply_repayment(st.loan, amt)
            st.pending = [x for x in st.pending if x[0] > i]
            md = market(d, last_px)
            cash = sum(q for k, q in st.qty.items() if k in cash_keys)
            sec_value = sum(st.qty.get(k, 0.0) * m.last_price for k, m in md.items())
            value = sec_value + cash
            pending_total = sum(a for _, a in st.pending)
            bal = st.loan.balance
            net_bal = max(0.0, bal - pending_total)
            if i >= term_sessions and not matured:
                matured = True
                if st.episode is None and not st.selling:
                    out.repaid_at_maturity = True
                    st.loan = Loan(0.0, currency=ccy)
                    out.events.append({"session": i, "date": d.isoformat(), "event": "repaid_at_maturity",
                                       "balance": bal})
                    break
                st.episode = "workout"
            if bal <= 0.01 and not st.pending:
                break
            # Thresholds.
            if v.kind == "adaptive":
                th = evaluate(st.qty, d, md).thresholds
                b0, b_ew, b_mc, b_f = th["initial_capacity"], th["early_warning"], th["margin_call"], th["forced_recovery"]
                target = th["initial_capacity_uncapped"] if v.restoration == "initial_capacity" else th["early_warning"]
            else:
                b0 = ltv0 * value
                b_ew = b0
                b_mc = v.psi * ltv0 * value
                b_f = v.forced_psi * ltv0 * value if v.forced_psi else math.inf
                target = b0
            out.sessions += 1
            out.capacity_sum += b0
            if st.episode != "workout":
                if net_bal > b_f:
                    state = MarginState.LIQUIDATION
                elif net_bal > b_mc:
                    state = MarginState.MARGIN_CALL
                elif net_bal > b_ew:
                    state = MarginState.WATCH
                elif net_bal > b0 + 1e-9:
                    state = MarginState.RESTRICT_NEW_BORROWING
                else:
                    state = MarginState.SAFE
                out.sessions_restricted += state != MarginState.SAFE
                out.sessions_warning += state == MarginState.WATCH
                out.sessions_margin_call += state == MarginState.MARGIN_CALL
                out.sessions_forced += state == MarginState.LIQUIDATION
                if state == MarginState.LIQUIDATION and st.episode != "forced":
                    st.episode, st.selling = "forced", True
                    out.liquidation_episodes += 1
                    out.events.append({"session": i, "date": d.isoformat(), "event": "forced_recovery",
                                       "balance": net_bal, "threshold": b_f})
                elif state == MarginState.MARGIN_CALL and st.episode is None:
                    st.episode, st.grace_left = "margin_call", execution.margin_call_grace_observations
                    out.margin_calls += 1
                    out.events.append({"session": i, "date": d.isoformat(), "event": "margin_call",
                                       "balance": net_bal, "threshold": b_mc})
                elif st.episode == "margin_call" and not st.selling:
                    if net_bal <= b_mc:
                        st.episode = None   # recovered by price before grace expiry
                    elif st.grace_left > 0:
                        st.grace_left -= 1
                        if cure_draws[i] < cure_probability:
                            pay = max(0.0, net_bal - target)
                            st.loan, _ = apply_repayment(st.loan, pay)
                            out.cures += 1
                            out.cure_payments += pay
                            out.events.append({"session": i, "date": d.isoformat(), "event": "cure", "amount": pay})
                            st.episode = None
                            continue
                    else:
                        st.selling = True
                        out.liquidation_episodes += 1
                        out.events.append({"session": i, "date": d.isoformat(), "event": "liquidation_after_grace",
                                           "balance": net_bal})
                if st.selling and net_bal <= target:
                    st.selling, st.episode = False, None
                    out.events.append({"session": i, "date": d.isoformat(), "event": "restored", "balance": net_bal})
                    continue
            if not st.selling and st.episode != "workout":
                continue
            # Execute today's sale: sell the most liquid holdings first.
            full = st.episode == "workout"
            rho = 0.0 if full else (target / value if value > 0 else 0.0)
            order = sorted(md, key=lambda k: (md[k].bid or 0) / md[k].last_price, reverse=True)
            for k in order:
                q = st.qty.get(k, 0.0)
                b = series[keys[k]].get(d)
                if q <= 0 or b is None:
                    continue   # no observed trade today
                prof = profiles[k]
                vol_shares = float(b["volume"]) if b.get("volume_reported") and b.get("volume") else 0.0
                part = min(prof.max_participation_rate, execution.max_participation_rate) if prof else 0.0
                cap = part * vol_shares
                if cap <= 0:
                    continue
                hs = md[k].last_price - (md[k].bid or md[k].last_price)
                frac = hs / md[k].last_price
                cost = frac * cfg.spread_stress_multiplier + execution.execution_cost_rate
                impact = cfg.impact_coefficient * vol20[keys[k]].get(d, 0.02) * math.sqrt(part)
                net_px = md[k].last_price * max(0.0, 1 - cost - impact)
                if full:
                    sell = min(q, cap)
                else:
                    need = (net_bal - rho * value) / max(1e-9, (1 - cost - impact) - rho)
                    if need <= 0:
                        break
                    sell = min(q, cap, need / md[k].last_price)
                proceeds = sell * net_px
                st.qty[k] = q - sell
                st.pending.append((i + settle, proceeds))
                out.liquidated_value += sell * md[k].last_price
                out.execution_costs += sell * md[k].last_price - proceeds
                net_bal -= proceeds
                value -= sell * md[k].last_price
            if full and all(st.qty.get(k, 0.0) <= 1e-9 for k in md) and not st.pending:
                break
        # Remaining pending settlements at the end of the path.
        for _, amt in st.pending:
            st.loan, _ = apply_repayment(st.loan, amt)
        if not out.repaid_at_maturity:
            cash = sum(q for k, q in st.qty.items() if k in cash_keys)
            st.loan, _ = apply_repayment(st.loan, cash)
            remaining_sec = sum(q for k, q in st.qty.items() if k not in cash_keys and q > 1e-9)
            out.final_balance = st.loan.balance
            out.lender_loss = st.loan.balance if (matured and remaining_sec <= 1e-9) or not path else 0.0
            if matured and remaining_sec > 1e-9 and st.loan.balance > 0:
                # Workout window exhausted with collateral left: value it at the
                # last observed close less costs; any shortfall is loss.
                lastv = sum(st.qty[k] * last_px.get(k, 0.0) * 0.97 for k in st.qty if k not in cash_keys)
                out.lender_loss = max(0.0, st.loan.balance - lastv)
        results[v.name] = out.to_dict()
    return {"origination": origination.isoformat(), "principal": p0, "collateral_value": v0,
            "initial_ltv": ltv0, "term_sessions": term_sessions, "cure_probability": cure_probability,
            "seed": seed, "settlement_sessions": settle, "variants": results,
            "candidate_initial_reasons": base.reasons[:4]}


def aggregate(cohorts: list[dict[str, Any]]) -> dict[str, Any]:
    """Comparison metrics across matched cohorts, per variant."""
    done = [c for c in cohorts if "variants" in c]
    names = sorted({n for c in done for n in c["variants"]})
    out: dict[str, Any] = {"cohorts": len(done), "skipped": len(cohorts) - len(done), "variants": {}}
    for n in names:
        rows = [c["variants"][n] for c in done if n in c["variants"]]
        principal = sum(r["principal"] for r in rows) or 1.0
        loss = [r["lender_loss"] for r in rows]
        sess = sum(r["sessions"] for r in rows) or 1
        out["variants"][n] = {
            "loans": len(rows),
            "loss_rate": sum(loss) / principal,
            "loans_with_loss": sum(1 for x in loss if x > 0.01),
            "worst_loss_share_of_principal": max((r["lender_loss"] / r["principal"] for r in rows), default=0.0),
            "margin_calls_per_loan": sum(r["margin_calls"] for r in rows) / len(rows),
            "liquidation_frequency": sum(1 for r in rows if r["liquidation_episodes"] > 0) / len(rows),
            "liquidated_share_of_principal": sum(r["liquidated_value"] for r in rows) / principal,
            "borrower_execution_cost_share": sum(r["execution_costs"] for r in rows) / principal,
            "cure_payments_share": sum(r["cure_payments"] for r in rows) / principal,
            "time_restricted": sum(r["sessions_restricted"] for r in rows) / sess,
            "time_margin_call_or_forced": sum(r["sessions_margin_call"] + r["sessions_forced"] for r in rows) / sess,
            "mean_capacity_share_of_principal": sum((r["mean_capacity"] or 0) for r in rows) / principal,
            "repaid_at_maturity": sum(1 for r in rows if r["repaid_at_maturity"]) / len(rows),
        }
    return out


def simulated_bars(bars: list[dict[str, Any]], returns: np.ndarray, start: date) -> list[dict[str, Any]]:
    """Append a simulated return path after ``start`` (labelled provider 'simulated')."""
    hist = [b for b in bars if (b["bar_date"] if isinstance(b["bar_date"], date)
                                else date.fromisoformat(str(b["bar_date"])[:10])) <= start]
    px = float(hist[-1]["close"])
    vol = float(np.median([b["volume"] for b in hist[-20:] if b.get("volume")] or [0.0]))
    hl = np.median([math.log(b["high"] / b["low"]) for b in hist[-20:] if b.get("range_reported") and b.get("low")]
                   or [0.01])
    d, out = start, list(hist)
    for r in returns:
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        px *= math.exp(float(r))
        out.append({"bar_date": d, "open": px, "high": px * math.exp(hl / 2), "low": px * math.exp(-hl / 2),
                    "close": px, "volume": vol, "range_reported": True, "volume_reported": vol > 0,
                    "provider": "simulated"})
    return out
