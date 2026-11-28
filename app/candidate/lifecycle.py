"""Lifecycle validation of intervention policies on matched loans.

A cohort is one origination: collateral, principal, term, interest terms.
Every variant receives the same cohort (matched initial exposure), the same
market path and the same random numbers (cure draws, engine scenario seeds):
common random numbers. Only the intervention policy differs.

Timing (no same-session look-ahead): every decision uses the completed bar of
session i (close, 21:00 UTC). Orders decided at session i are submitted for
session i + 1 and later and execute at that session's typical price
((open + high + low + close) / 4) less that session's half-spread, square-root
impact and fee, limited by its volume. A session without a bar (suspension,
closure), without reported volume, or closing at the exchange's limit-down
band does not fill. Proceeds settle after the market's settlement lag and only
settled cash reduces the debt or counts as recovery.

Each session, per variant: contractual interest accrues (production engine,
capitalization anchored at origination); settled cash repays; thresholds are
computed for the actual collateral, loan composition and contract; the account
is classified; decisions are taken separately for

* new-borrowing restriction (capacity, early warning),
* monitoring and margin calls (optional hysteresis and confirmation),
* urgent lender protection (forced level; optional "urgent first": sell only
  what brings the account below the forced level, then continue as a margin call),
* liquidation quantity and timing (unified planner; optional staging),
* post-intervention restoration (the variant's target; remaining orders are
  cancelled once the account is restored).

Borrower behaviour is a cohort-level scenario: cure probability during grace,
and at maturity repay, default, partial repayment or refinancing (repays only
if the balance is within capacity). Unsold collateral at the end of the window
is never counted as recovered cash: the outcome is censored and reported with
the outstanding debt and an estimate of the residual collateral value.
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
    engine: tuple[tuple[str, Any], ...] = ()   # CandidateConfig overrides (research switches)
    hysteresis: bool = False                   # exit a margin call only below early warning
    confirm_sessions: int = 1                  # margin call only after this many breached sessions
    staged_fraction: float | None = None       # max share of the planned quantity sold per session
    urgent_first: bool = False                 # forced: sell only to below the forced level first


DEFAULT_VARIANTS = (
    Variant("fixed_psi_1.15", "fixed", psi=1.15),
    Variant("fixed_psi_1.30", "fixed", psi=1.30),
    Variant("adaptive", "adaptive", restoration="initial_capacity"),
    Variant("adaptive_restore_warning", "adaptive", restoration="early_warning"),
)

COMBINED_ENGINE = (("headroom_mode", "adaptive"), ("restoration_volatility", "long_run"),
                   ("concentration_control", True), ("suspension_value", "delayed"))

STRESS_VARIANTS = (
    Variant("adaptive_stress_grace", "adaptive", engine=(("stress_protection", True),)),
    Variant("adaptive_regime", "adaptive", engine=(("regime_conditioning", True),)),
    Variant("adaptive_stress_aware", "adaptive", engine=(("stress_protection", True), ("regime_conditioning", True))),
    Variant("adaptive_headroom_cal", "adaptive", engine=(("headroom_mode", "adaptive"),)),
    Variant("adaptive_concentration_cal", "adaptive", engine=(("concentration_control", True),)),
    Variant("adaptive_suspension_value", "adaptive", engine=(("suspension_value", "delayed"),)),
    Variant("rc_candidate", "adaptive", engine=(("suspension_value", "delayed"), ("stress_protection", True),
                                                ("regime_conditioning", True))),
)

RESEARCH_VARIANTS = (
    Variant("adaptive_headroom", "adaptive", engine=(("headroom_mode", "adaptive"),)),
    Variant("adaptive_longrun_restore", "adaptive", engine=(("restoration_volatility", "long_run"),)),
    Variant("adaptive_concentration", "adaptive", engine=(("concentration_control", True),)),
    Variant("adaptive_suspension_value", "adaptive", engine=(("suspension_value", "delayed"),)),
    Variant("adaptive_hysteresis_staged", "adaptive", hysteresis=True, confirm_sessions=2, staged_fraction=0.34,
            urgent_first=True),
    Variant("adaptive_combined", "adaptive", engine=COMBINED_ENGINE, hysteresis=True, confirm_sessions=2,
            staged_fraction=0.34, urgent_first=True),
)


def cure_sensitivity(probabilities: tuple[float, ...] = (0.0, 0.25, 0.5, 0.9)) -> tuple[Variant, ...]:
    out = []
    for p in probabilities:
        out += [Variant(f"fixed_psi_1.15@cure{p}", "fixed", psi=1.15, cure_probability=p),
                Variant(f"adaptive@cure{p}", "adaptive", cure_probability=p),
                Variant(f"adaptive_combined@cure{p}", "adaptive", cure_probability=p, engine=COMBINED_ENGINE,
                        hysteresis=True, confirm_sessions=2, staged_fraction=0.34, urgent_first=True)]
    return tuple(out)


@dataclass
class Outcome:
    variant: str
    principal: float
    initial_ltv: float
    lender_loss: float | None = 0.0        # None when censored
    loss_lower_bound: float = 0.0
    outstanding_debt: float = 0.0
    residual_collateral_estimate: float = 0.0
    censored: bool = False
    realized_recovery: float = 0.0          # settled sale proceeds applied to the debt
    pending_at_cutoff: float = 0.0          # sale proceeds not yet settled at the observation cutoff
    status: str = "open"                    # repaid | recovered | loss | censored_collateral | censored_settlement
    borrower_repayment: float = 0.0
    repaid_at_maturity: bool = False
    defaulted_at_maturity: bool = False
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
    completed_recoveries: int = 0
    unnecessary_liquidations: int = 0
    executed_gross: float = 0.0
    execution_costs: float = 0.0
    # Measurement only (27 Nov acceptance evaluation): cost attribution, maturity
    # outcome, recovery accuracy and coverage calibration. None of these feed back
    # into a decision.
    execution_costs_intervention: float = 0.0
    execution_costs_workout: float = 0.0
    executed_gross_intervention: float = 0.0
    executed_gross_workout: float = 0.0
    maturity_outcome: str = ""              # repaid | partial_then_default | refinanced | refinance_refused | default | in_recovery | ""
    episode_accuracy: list[dict[str, Any]] = field(default_factory=list)
    calibration: list[dict[str, Any]] = field(default_factory=list)
    no_fill_sessions: int = 0
    recovery_sessions: list[int] = field(default_factory=list)
    residual_after_recovery: list[float] = field(default_factory=list)
    interest_accrued: float = 0.0
    # Measurement only (Report 17 review): cash interest actually received (repayments are
    # allocated fees -> interest -> principal), funded principal summed over every session the
    # loan is open including the maturity workout, and those calendar sessions.
    interest_received: float = 0.0
    funded_principal_sessions: float = 0.0
    calendar_sessions: int = 0
    capacity_sum: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mean_capacity"] = self.capacity_sum / self.sessions if self.sessions else None
        d.pop("capacity_sum")
        d["events"] = self.events[:40]
        return d


def _date(x: Any) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def limit_locked(prev_close: float | None, close: float, limit: float | None) -> bool:
    """A daily bar that closes at the limit-down band cannot be assumed to fill sells."""
    return bool(limit and prev_close and close / prev_close - 1 <= -limit + 0.002)


def _half_spread(b: dict[str, Any], floor: float, fallback: float) -> float:
    hi, lo = float(b.get("high") or 0), float(b.get("low") or 0)
    if b.get("range_reported") and hi > 0 and lo > 0 and hi >= lo:
        return max(floor / 2, min(0.05, math.log(hi / lo) / 4))
    return fallback / 2


class _Feed:
    """Point-in-time market state for one pass (variant or counterfactual)."""

    def __init__(self, holdings: list[Holding], keys: dict[str, str], series: dict[str, dict[date, Any]]):
        self.h, self.keys, self.series = holdings, keys, series
        self.last_px: dict[str, float] = {}
        self.last_day: dict[str, date] = {}

    def market(self, d: date) -> dict[str, MarketData]:
        out = {}
        for h in self.h:
            if h.asset_type == AssetType.CASH:
                continue
            b = self.series[self.keys[h.stable_key]].get(d)
            if b:
                self.last_px[h.stable_key] = float(b["close"])
                self.last_day[h.stable_key] = d
            px = self.last_px.get(h.stable_key)
            if not px:
                continue
            ts = datetime.combine(self.last_day[h.stable_key], time(21), UTC)
            out[h.stable_key] = MarketData(h.asset_id, px, timestamp=ts)
        return out


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
    principal_scale: float = 1.0,
    maturity: str = "repay",
    workout_sessions: int = 60,
    origination_mode: str = "matched",
    origination_engine: tuple[tuple[str, Any], ...] = (),
    ceiling_cap: bool = False,
    max_principal: float | None = None,
    settlement: str = "fixed",
) -> dict[str, Any]:
    """Run all variants on one matched cohort with common random numbers.

    ``ceiling_cap``: the scaled principal never exceeds the contractual client ceiling
    x utilisation (independent of the risk capacity being scaled); ``max_principal``:
    an absolute institutional limit. Both default off (unchanged behaviour).
    ``settlement="point_in_time"`` (RC2 research) settles each sale on the cycle in force
    on its trade date instead of the profile's fixed cycle."""
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
    rng = np.random.default_rng(seed)
    cure_draws = rng.random(len(path))
    engine_seed = int(rng.integers(0, 2**40))
    profiles = {h.stable_key: profile_for_exchange(h.exchange) for h in holdings if h.asset_type != AssetType.CASH}
    settle = max([execution.settlement_delay_observations] + [p.settlement_days for p in profiles.values() if p])
    if settlement not in ("fixed", "point_in_time"):
        raise ValueError(f"settlement must be fixed or point_in_time, got {settlement}")

    def settle_at(d: date) -> int:
        if settlement == "fixed":
            return settle
        from app.rc2.settlement import settlement_days
        return max([execution.settlement_delay_observations]
                   + [settlement_days(h.exchange, d) for h in holdings if h.asset_type != AssetType.CASH])
    vol20: dict[str, dict[date, float]] = {}
    prev_close: dict[str, dict[date, float]] = {}
    for dk, rows in sorted_bars.items():
        closes = np.array([float(b["close"]) for _, b in rows])
        r = np.diff(np.log(closes)) if closes.size > 1 else np.zeros(0)
        vol20[dk] = {rows[j + 1][0]: float(r[max(0, j - 19): j + 1].std()) if j >= 4 else 0.02 for j in range(r.size)}
        prev_close[dk] = {rows[j + 1][0]: float(rows[j][1]["close"]) for j in range(len(rows) - 1)}
    anchored = replace(interest, schedule_anchor=datetime.combine(origination, time(21), UTC))
    engines: dict[tuple[Any, ...], CandidateEngine] = {}

    def engine_for(over: tuple[tuple[str, Any], ...], restoration: str = "initial_capacity") -> CandidateEngine:
        k = (over, restoration)
        if k not in engines:
            engines[k] = CandidateEngine(replace(cfg, restoration=restoration, **dict(over)), priors)
        return engines[k]

    cache: dict[tuple[Any, ...], Any] = {}

    def visible(d: date) -> dict[str, list[dict[str, Any]]]:
        return {dk: [b for bd, b in rows if bd <= d] for dk, rows in sorted_bars.items()}

    def evaluate(over: tuple[tuple[str, Any], ...], qty: dict[str, float], loan: Loan, d: date,
                 md: dict[str, MarketData]) -> Any:
        """Thresholds for the actual collateral and the actual loan composition
        (accrued-interest share, contract); the balance level does not change
        threshold levels, so it is normalised in the cache key."""
        rho = round(loan.accrued_interest / loan.balance, 2) if loan.balance > 0 else 0.0
        k = (over, d, tuple(sorted((a, round(b, 6)) for a, b in qty.items())), rho)
        if k not in cache:
            hs = [replace(h, quantity=qty.get(h.stable_key, 0.0)) for h in holdings if qty.get(h.stable_key, 0.0) > 0]
            norm = Loan(principal=1e6 * (1 - rho), accrued_interest=1e6 * rho, currency=ccy)
            cache[k] = engine_for(over).evaluate(
                "lifecycle", hs, norm, policy, md, bars=visible(d), execution_policy=execution,
                interest_policy=anchored, as_of=datetime.combine(d, time(21), UTC), seed=engine_seed, plan=False)
        return cache[k]

    def plan(v: Variant, qty: dict[str, float], loan: Loan, d: date, md: dict[str, MarketData],
             state: MarginState, ltv0: float, urgent: bool) -> tuple[dict[str, float], bool]:
        """The actual candidate recovery decision through the unified planner."""
        hs = [replace(h, quantity=qty.get(h.stable_key, 0.0)) for h in holdings if qty.get(h.stable_key, 0.0) > 0]
        eng = engine_for(v.engine, "early_warning" if v.restoration == "early_warning" else "initial_capacity")
        override = None
        if v.kind == "fixed":
            def override(q: np.ndarray, c: float, sim: Any) -> float:
                return ltv0 * (float(q @ sim.price) + c)
        elif urgent:
            g, dl = execution.margin_call_grace_observations, execution.liquidation_delay_observations

            def override(q: np.ndarray, c: float, sim: Any) -> float:
                return 0.97 * eng._thresholds(sim, q, c, g, dl)["forced_recovery"]
        ev = eng.evaluate("lifecycle", hs, loan, policy, md, bars=visible(d), execution_policy=execution,
                          interest_policy=anchored, as_of=datetime.combine(d, time(21), UTC), seed=engine_seed,
                          plan=True, restoration_override=override, force_plan_state=state)
        orders: dict[str, float] = {}
        expected = 0.0
        for o in (ev.recovery_plan or {}).get("orders", []):
            orders[o["instrument_key"]] = orders.get(o["instrument_key"], 0.0) + float(o["quantity"])
            expected += float(o.get("expected_net_proceeds") or 0.0)
        plan_expected[0] = expected
        return orders, bool(ev.data_review_required)

    plan_expected = [0.0]

    def realized_coverage(i0: int, start: int, qty_s: dict[str, float], loan_s: Loan,
                          counted: set[str]) -> tuple[float | None, int]:
        """Measurement: the balance level actually covered if the counted collateral had
        been sold starting at session ``start`` on the observed path, within participation
        limits, without fills on no-volume or limit-locked sessions, at the session's typical
        price less costs, with settlement and contractual interest. Returned in session-i0
        balance terms (comparable with the thresholds stated at i0). None if the sale or its
        settlement does not finish within the observed path (censored observation)."""
        rem = {k: q for k, q in qty_s.items() if k in counted and q > 1e-9}
        cash_s = sum(q for k, q in qty_s.items() if k in cash_keys)
        proceeds, last_settle = 0.0, start
        j = start
        while rem and j < len(path) and j - start < cfg.max_exit_sessions:
            d_j = path[j]
            for k in list(rem):
                b = fillable(k, d_j)
                if b is None:
                    continue
                prof = profiles[k]
                assert prof is not None
                part = min(prof.max_participation_rate, execution.max_participation_rate)
                sell = min(rem[k], part * float(b["volume"]))
                hs_ = _half_spread(b, prof.spread_floor, prof.spread_fallback)
                impact = cfg.impact_coefficient * vol20[keys[k]].get(d_j, 0.02) * math.sqrt(part)
                px = (float(b.get("open") or b["close"]) + float(b["high"]) + float(b["low"]) + float(b["close"])) / 4
                proceeds += sell * px * max(0.0, 1 - hs_ - impact - execution.execution_cost_rate)
                rem[k] -= sell
                if rem[k] <= 1e-9:
                    rem.pop(k)
                last_settle = j + settle_at(path[j])
            j += 1
        if rem and j - start < cfg.max_exit_sessions:
            return None, j            # path ended before the sale finished: censored
        if last_settle >= len(path):
            return None, last_settle  # settlement after the observed path: censored
        t0 = datetime.combine(path[i0], time(21), UTC)
        grown, _ = accrue_scheduled_periods(loan_s, anchored, t0, datetime.combine(path[last_settle], time(21), UTC))
        g = grown.balance / loan_s.balance if loan_s.balance > 0 else 1.0
        return (cash_s + proceeds) / g, last_settle

    feed0 = _Feed(holdings, keys, series)
    md0 = feed0.market(origination)
    q0 = {h.stable_key: h.quantity for h in holdings}
    cash0 = sum(h.quantity for h in holdings if h.asset_type == AssetType.CASH)
    v0 = sum(q0[k] * md0[k].last_price for k in md0) + cash0
    # Matched exposure: every policy lends the capacity of the origination engine (the
    # baseline candidate by default; RC1's own engine when RC1 is the reference).
    base = evaluate(origination_engine, q0, Loan(1.0, currency=ccy), origination, md0)
    if principal is None and base.data_status != "ok":
        # No new borrowing while material data is missing, stale or unreliable.
        return {"origination": origination.isoformat(), "skipped": f"data {base.data_status} at origination",
                "reasons": base.reasons[:6]}
    cap0 = min(base.thresholds["initial_capacity_uncapped"], base.thresholds["client_ceiling"])
    p0 = float(principal if principal is not None else cap0 * cfg.capacity_utilisation * principal_scale)
    binding = "scale"
    if principal is None and ceiling_cap and p0 > base.thresholds["client_ceiling"] * cfg.capacity_utilisation:
        p0, binding = float(base.thresholds["client_ceiling"] * cfg.capacity_utilisation), "contractual_ceiling"
    if principal is None and max_principal is not None and p0 > max_principal:
        p0, binding = float(max_principal), "institutional_limit"
    if p0 <= 0:
        return {"origination": origination.isoformat(), "skipped": "no lending capacity at origination",
                "reasons": base.reasons[:6]}
    ltv0 = p0 / v0

    # No-intervention counterfactual on the same path, with its own market state.
    feed_cf = _Feed(holdings, keys, series)
    feed_cf.market(origination)
    margin_cf = np.zeros(len(path))
    loan_cf, prev = Loan(p0, currency=ccy), datetime.combine(origination, time(21), UTC)
    for i, d in enumerate(path):
        now = datetime.combine(d, time(21), UTC)
        loan_cf, _ = accrue_scheduled_periods(loan_cf, anchored, prev, now)
        prev = now
        md = feed_cf.market(d)
        margin_cf[i] = cash0 + sum(q0[k] * m.last_price * 0.98 for k, m in md.items()) - loan_cf.balance

    def unnecessary(i: int) -> bool:
        return bool(margin_cf[i:term_sessions].min() > 0) if i < term_sessions else False  # through maturity

    def fillable(k: str, d: date) -> dict[str, Any] | None:
        b = series[keys[k]].get(d)
        prof = profiles[k]
        if not b or prof is None or not (b.get("volume_reported") and b.get("volume")):
            return None
        if limit_locked(prev_close[keys[k]].get(d), float(b["close"]), prof.daily_price_limit):
            return None   # closed at the limit-down band: sellers did not fill
        return b

    results = {}
    for v in variants:
        cure_p = cure_probability if v.cure_probability is None else v.cure_probability
        p_v = p0
        if origination_mode == "policy" and v.kind == "adaptive" and v.engine and principal is None:
            # Policy-originated: the variant lends its own capacity (fixed policies and the
            # baseline candidate lend the baseline candidate capacity).
            b_v = evaluate(v.engine, q0, Loan(1.0, currency=ccy), origination, md0)
            p_v = float(min(b_v.thresholds["initial_capacity_uncapped"], b_v.thresholds["client_ceiling"])
                        * cfg.capacity_utilisation * principal_scale)
            if p_v <= 0 or b_v.data_status != "ok":
                results[v.name] = {"skipped": "no capacity under this policy"}
                continue
        loan = Loan(p_v, currency=ccy)
        qty = dict(q0)
        out = Outcome(v.name, p_v, p_v / v0)
        ltv_v = p_v / v0
        feed = _Feed(holdings, keys, series)
        feed.market(origination)
        prev_t = datetime.combine(origination, time(21), UTC)
        episode: str | None = None       # None | margin_call | selling | workout
        grace_left = 0
        breach_run = 0
        orders: dict[str, float] = {}
        planned: dict[str, float] = {}
        submit_from = 10**9
        pending: list[tuple[int, float]] = []
        sale_start = -1
        matured = False
        target = 0.0
        for i, d in enumerate(path):
            now = datetime.combine(d, time(21), UTC)
            before = loan.balance
            loan, _ = accrue_scheduled_periods(loan, anchored, prev_t, now)
            out.interest_accrued += loan.balance - before
            prev_t = now
            for _, amt in [x for x in pending if x[0] <= i]:
                b4 = loan.balance
                loan, alloc = apply_repayment(loan, amt)
                out.interest_received += alloc["interest_paid"]
                out.realized_recovery += b4 - loan.balance
            pending = [x for x in pending if x[0] > i]
            out.calendar_sessions += 1
            out.funded_principal_sessions += loan.principal
            md = feed.market(d)
            # 1) Execute orders submitted at an earlier session against today's bar.
            if orders and i >= submit_from:
                for k in list(orders):
                    q = min(orders[k], qty.get(k, 0.0))
                    if q <= 1e-9:
                        orders.pop(k)
                        continue
                    b = fillable(k, d)
                    if b is None:
                        out.no_fill_sessions += 1
                        continue
                    prof = profiles[k]
                    assert prof is not None
                    part = min(prof.max_participation_rate, execution.max_participation_rate)
                    cap = part * float(b["volume"])
                    if v.staged_fraction and episode != "workout":
                        cap = min(cap, v.staged_fraction * planned.get(k, q))
                    sell = min(q, cap)
                    hs_ = _half_spread(b, prof.spread_floor, prof.spread_fallback)
                    impact = cfg.impact_coefficient * vol20[keys[k]].get(d, 0.02) * math.sqrt(part)
                    px = (float(b.get("open") or b["close"]) + float(b["high"]) + float(b["low"])
                          + float(b["close"])) / 4
                    proceeds = sell * px * max(0.0, 1 - hs_ - impact - execution.execution_cost_rate)
                    qty[k] = qty.get(k, 0.0) - sell
                    orders[k] = q - sell
                    if orders[k] <= 1e-9:
                        orders.pop(k)
                    if sale_start >= 0 and not any(e["event"] == "first_fill" and e.get("episode") == sale_start
                                                   for e in out.events):
                        out.events.append({"session": i, "event": "first_fill", "episode": sale_start})
                    pending.append((i + settle_at(d), proceeds))
                    out.executed_gross += sell * px
                    out.execution_costs += sell * px - proceeds
                    if episode == "workout":
                        out.execution_costs_workout += sell * px - proceeds
                        out.executed_gross_workout += sell * px
                    else:
                        out.execution_costs_intervention += sell * px - proceeds
                        out.executed_gross_intervention += sell * px
                        if out.episode_accuracy and out.episode_accuracy[-1]["start"] == sale_start:
                            ea = out.episode_accuracy[-1]
                            ea["realized_net"] += proceeds
                            ea["last_fill"] = i
            cash = sum(q for k, q in qty.items() if k in cash_keys)
            value = cash + sum(qty.get(k, 0.0) * m.last_price for k, m in md.items())
            bal = loan.balance
            net_bal = max(0.0, bal - sum(a for _, a in pending))
            if bal <= 0.01 and not pending:
                break
            # 2) Maturity: borrower behaviour scenario.
            if i >= term_sessions - 1 and not matured:   # path[term-1] is `term` sessions after origination
                matured = True
                pay = 0.0
                if episode is not None:
                    out.maturity_outcome = "in_recovery"
                if episode is None:
                    if maturity == "repay":
                        pay = bal
                    elif maturity.startswith("partial:"):
                        pay = bal * float(maturity.split(":")[1])
                    elif maturity == "refinance":
                        th_m = evaluate(v.engine, qty, loan, d, md).thresholds if v.kind == "adaptive" else None
                        cap_m = (min(th_m["initial_capacity_uncapped"], th_m["client_ceiling"]) if th_m
                                 else ltv0 * value)
                        pay = bal if net_bal <= cap_m else 0.0
                        out.maturity_outcome = "refinanced" if pay > 0 else "refinance_refused"
                    elif maturity == "default":
                        out.maturity_outcome = "default"
                    if maturity == "repay":
                        out.maturity_outcome = "repaid"
                    elif maturity.startswith("partial:"):
                        out.maturity_outcome = "partial_then_default"
                if pay > 0:
                    loan, alloc = apply_repayment(loan, pay)
                    out.interest_received += alloc["interest_paid"]
                    out.borrower_repayment += pay
                if loan.balance <= 0.01:
                    out.repaid_at_maturity = True
                    out.events.append({"session": i, "date": d.isoformat(), "event": "repaid_at_maturity"})
                    break
                out.defaulted_at_maturity = True
                episode = "workout"
                orders = {k: q for k, q in qty.items() if k not in cash_keys and q > 0}
                submit_from = i + 1
                out.events.append({"session": i, "date": d.isoformat(), "event": "maturity_default",
                                   "balance": loan.balance})
            if episode == "workout":
                if not orders and not pending:
                    break
                continue
            # 3) Thresholds and classification at the close of session i.
            if v.kind == "adaptive":
                ev = evaluate(v.engine, qty, loan, d, md)
                th = ev.thresholds
                # Capacity from the collateral, not from the normalised loan used for caching;
                # no additional borrowing while data is deteriorated.
                b0 = min(th["initial_capacity_uncapped"], th["client_ceiling"])
                if ev.data_status != "ok":
                    b0 = min(b0, net_bal)
                b_ew, b_mc, b_f = th["early_warning"], th["margin_call"], th["forced_recovery"]
                target = th["restoration_target"] if v.restoration == "initial_capacity" else b_ew
                te = ev.thresholds_economic
                data_hold = ev.data_status == "missing" or (ev.data_status == "degraded"
                                                            and net_bal <= te["margin_call"])
            else:
                b0 = b_ew = target = ltv_v * value
                b_mc, b_f, data_hold = v.psi * ltv_v * value, math.inf, False
            out.sessions += 1
            out.capacity_sum += b0
            state = (MarginState.LIQUIDATION if net_bal > b_f else MarginState.MARGIN_CALL if net_bal > b_mc
                     else MarginState.WATCH if net_bal > b_ew else MarginState.RESTRICT_NEW_BORROWING
                     if net_bal > b0 + 1e-9 else MarginState.SAFE)
            if (v.kind == "adaptive" and dict(v.engine).get("stress_protection") and state == MarginState.MARGIN_CALL
                    and ev.p_infeasible_after_grace(net_bal) > cfg.stress_infeasible_prob):
                state = MarginState.LIQUIDATION   # recovery after grace likely infeasible: urgent protection
            if data_hold and state in BAD:
                out.sessions_data_review += 1
                state = MarginState.RESTRICT_NEW_BORROWING
            out.sessions_restricted += state != MarginState.SAFE
            out.sessions_warning += state == MarginState.WATCH
            out.sessions_margin_call += state == MarginState.MARGIN_CALL
            out.sessions_forced += state == MarginState.LIQUIDATION
            breach_run = breach_run + 1 if state in BAD else 0
            # 4) Restoration: cancel remaining orders once restored.
            if episode == "selling":
                restored = net_bal <= target + 1e-6 and state not in BAD
                if restored or (not orders and not pending):
                    out.recovery_sessions.append(i - sale_start)
                    out.residual_after_recovery.append(max(0.0, net_bal - target))
                    if restored:
                        out.completed_recoveries += 1
                        orders, planned, episode, sale_start = {}, {}, None, -1
                        out.events.append({"session": i, "date": d.isoformat(), "event": "restored",
                                           "balance": net_bal})
                        continue
                    sale_start = -1   # still in breach: plan again below
            # 5) Monitoring, margin calls, urgent protection.
            urgent = False
            exit_level = b_ew if v.hysteresis else b_mc
            if (v.kind == "adaptive" and episode is None and state in BAD
                    and not (data_hold and state in BAD)):
                counted = {a.instrument_key for a in ev.asset_results if a.lending_value_eligible
                           or (a.data_status == "not_trading" and dict(v.engine).get("suspension_value") == "delayed"
                               and "impaired" not in a.notes)}
                delay_s = (execution.margin_call_grace_observations if state == MarginState.MARGIN_CALL
                           else execution.liquidation_delay_observations)
                cov, _end = realized_coverage(i, i + 1 + delay_s, qty, loan, counted)
                lvl = b_mc if state == MarginState.MARGIN_CALL else b_f
                out.calibration.append({"session": i, "kind": state.value, "balance": net_bal, "threshold": lvl,
                                        "realized_level": cov, "censored": cov is None,
                                        "shortfall_vs_threshold": None if cov is None else bool(cov < lvl),
                                        "shortfall_vs_balance": None if cov is None else bool(cov < net_bal)})
            if episode is None and state == MarginState.LIQUIDATION:
                out.forced_events += 1
                episode, urgent = "selling", v.urgent_first
            elif episode is None and state == MarginState.MARGIN_CALL and breach_run >= v.confirm_sessions:
                episode, grace_left = "margin_call", execution.margin_call_grace_observations
                out.margin_calls += 1
                out.events.append({"session": i, "date": d.isoformat(), "event": "margin_call",
                                   "balance": net_bal, "threshold": b_mc})
            elif episode == "margin_call":
                if net_bal <= exit_level and state not in BAD:
                    episode = None
                elif state == MarginState.LIQUIDATION:
                    out.forced_events += 1
                    episode, urgent = "selling", v.urgent_first
                elif grace_left > 0:
                    grace_left -= 1
                    if cure_draws[i] < cure_p:
                        pay = max(0.0, net_bal - target)
                        loan, alloc = apply_repayment(loan, pay)
                        out.interest_received += alloc["interest_paid"]
                        out.cures += 1
                        out.cure_payments += pay
                        out.events.append({"session": i, "date": d.isoformat(), "event": "cure", "amount": pay})
                        episode = None
                        continue
                else:
                    episode = "selling"
            # 6) Liquidation quantity and timing: decided now, executed from the next session.
            if episode == "selling" and sale_start < 0:
                plan_state = state if state in BAD else MarginState.MARGIN_CALL
                orders, review = plan(v, qty, loan, d, md, plan_state, ltv0, urgent)
                if not orders:
                    out.events.append({"session": i, "date": d.isoformat(),
                                       "event": "data_review_hold" if review else "no_executable_plan"})
                    episode, sale_start = None, -1
                    continue
                planned = dict(orders)
                submit_from = i + 1
                sale_start = i
                out.episode_accuracy.append({"start": i, "expected_net": plan_expected[0], "realized_net": 0.0,
                                             "last_fill": None, "kind": "urgent" if urgent else plan_state.value})
                out.liquidation_episodes += 1
                out.unnecessary_liquidations += unnecessary(i)
                out.events.append({"session": i, "date": d.isoformat(), "event": "liquidation_decided",
                                   "urgent": urgent, "balance": net_bal,
                                   "orders": {k: round(q, 4) for k, q in orders.items()}})
        # Proceeds not yet settled at the cutoff are NOT realized recovery: reported separately.
        out.pending_at_cutoff = sum(a for _, a in pending)
        if out.repaid_at_maturity:
            out.status = "repaid"
        else:
            cash = sum(q for k, q in qty.items() if k in cash_keys)
            loan, alloc = apply_repayment(loan, cash)
            out.interest_received += alloc["interest_paid"]
            out.outstanding_debt = loan.balance
            remaining = {k: q for k, q in qty.items() if k not in cash_keys and q > 1e-9}
            out.residual_collateral_estimate = sum(q * feed.last_px.get(k, 0.0) for k, q in remaining.items())
            if loan.balance <= 0.01:
                out.status, out.lender_loss, out.loss_lower_bound = "recovered", 0.0, 0.0
            elif remaining or out.pending_at_cutoff > 0:
                # Unsold collateral or unsettled proceeds: censored, loss not observed.
                out.status = "censored_collateral" if remaining else "censored_settlement"
                out.censored = True
                out.lender_loss = None
                out.loss_lower_bound = max(0.0, loan.balance - out.residual_collateral_estimate
                                           - out.pending_at_cutoff)
            else:
                out.status, out.lender_loss = "loss", loan.balance
                out.loss_lower_bound = loan.balance
        results[v.name] = out.to_dict()
    return {"origination": origination.isoformat(), "principal": p0, "collateral_value": v0,
            "initial_ltv": ltv0, "term_sessions": term_sessions, "cure_probability": cure_probability,
            "maturity": maturity, "principal_scale": principal_scale, "seed": seed,
            "principal_binding": binding, "risk_capacity": float(base.thresholds["initial_capacity_uncapped"]),
            "client_ceiling": float(base.thresholds["client_ceiling"]),
            "settlement_sessions": settle, "settlement": settlement, "variants": results, "candidate_initial_reasons": base.reasons[:4]}


def variant_rows(cohorts: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [c["variants"][name] | {"origination": c["origination"]} for c in cohorts
            if "variants" in c and name in c["variants"] and "skipped" not in c["variants"][name]]


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Point metrics for one variant over a set of loans."""
    if not rows:
        return {"loans": 0}
    principal = np.array([r["principal"] for r in rows])
    unc = [r for r in rows if not r["censored"]]
    if not unc:
        return {"loans": len(rows), "completed": 0, "censored": len(rows),
                "note": "no completed outcomes: loss statistics not estimable"}
    loss = np.array([(r["lender_loss"] or 0.0) / r["principal"] for r in unc])
    # Conservative bound: censored loans lose their entire outstanding debt.
    upper = np.array([((r["lender_loss"] or 0.0) if not r["censored"] else r["outstanding_debt"]) / r["principal"]
                      for r in rows])
    lb = np.array([r["loss_lower_bound"] / r["principal"] for r in rows])
    episodes = float(sum(r["liquidation_episodes"] for r in rows))
    sess = np.array([max(r["sessions"], 1) for r in rows], dtype=float)
    rec = [x for r in rows for x in r["recovery_sessions"]]

    def es(x: np.ndarray, q: float) -> float:
        k = max(1, math.ceil((1 - q) * x.size))
        return float(np.sort(x)[-k:].mean())

    return {
        "loans": len(rows), "completed": len(unc), "censored": len(rows) - len(unc),
        "expected_loss_conservative_upper": float(upper.mean()),
        "loss_probability_conservative_upper": float(np.mean(upper > 1e-6)),
        "pending_at_cutoff_share": float(sum(r.get("pending_at_cutoff", 0.0) for r in rows) / principal.sum()),
        "initial_ltv_mean": float(np.mean([r["initial_ltv"] for r in rows])),
        "mean_capacity_share_of_principal": float(np.mean([(r["mean_capacity"] or 0) / r["principal"] for r in rows])),
        "loss_probability": float((loss > 1e-6).mean()), "expected_loss": float(loss.mean()),
        "es95_loss": es(loss, 0.95), "es99_loss": es(loss, 0.99), "worst_loss": float(loss.max()),
        "censored_loss_lower_bound_mean": float(lb.mean()),
        "censored_outstanding_share": float(sum(r["outstanding_debt"] for r in rows if r["censored"])
                                            / principal.sum()),
        "margin_calls_per_loan": float(np.mean([r["margin_calls"] for r in rows])),
        "repeated_margin_call_share": float(np.mean([r["margin_calls"] >= 2 for r in rows])),
        "liquidation_frequency": float(np.mean([r["liquidation_episodes"] > 0 for r in rows])),
        "recovery_completion": (float(sum(r["completed_recoveries"] for r in rows) / episodes) if episodes else None),
        "unnecessary_liquidation_share": (sum(r["unnecessary_liquidations"] for r in rows) / episodes
                                          if episodes else 0.0),
        "borrower_execution_cost_share": float(sum(r["execution_costs"] for r in rows) / principal.sum()),
        "executed_share_of_principal": float(sum(r["executed_gross"] for r in rows) / principal.sum()),
        "realized_recovery_share": float(sum(r["realized_recovery"] for r in rows) / principal.sum()),
        "cure_payments_share": float(sum(r["cure_payments"] for r in rows) / principal.sum()),
        "recovery_sessions_median": float(np.median(rec)) if rec else None,
        "recovery_sessions_p95": float(np.percentile(rec, 95)) if rec else None,
        "residual_after_recovery_share": float(sum(x for r in rows for x in r["residual_after_recovery"])
                                               / principal.sum()),
        "time_restricted": float(np.mean(np.array([r["sessions_restricted"] for r in rows]) / sess)),
        "time_margin_call_or_forced": float(np.mean(
            np.array([r["sessions_margin_call"] + r["sessions_forced"] for r in rows]) / sess)),
        "data_review_sessions": int(sum(r["sessions_data_review"] for r in rows)),
        "no_fill_sessions": int(sum(r["no_fill_sessions"] for r in rows)),
        "repaid_at_maturity": float(np.mean([r["repaid_at_maturity"] for r in rows])),
        **_measurements(rows, principal),
    }


def _measurements(rows: list[dict[str, Any]], principal: np.ndarray) -> dict[str, Any]:
    """Acceptance-evaluation measurements (27 Nov): cost attribution, maturity outcomes,
    recovery accuracy and coverage calibration. Absent fields (older results) are skipped."""
    if not rows or "execution_costs_intervention" not in rows[0]:
        return {}
    tot = principal.sum()
    out: dict[str, Any] = {
        "cost_intervention_share": float(sum(r["execution_costs_intervention"] for r in rows) / tot),
        "cost_maturity_workout_share": float(sum(r["execution_costs_workout"] for r in rows) / tot),
        "executed_intervention_share": float(sum(r["executed_gross_intervention"] for r in rows) / tot),
        "executed_workout_share": float(sum(r["executed_gross_workout"] for r in rows) / tot),
    }
    outcomes = [r.get("maturity_outcome") or "none" for r in rows]
    for o in sorted(set(outcomes)):
        out[f"maturity_{o}"] = float(np.mean([x == o for x in outcomes]))
    refused = [r for r in rows if r.get("maturity_outcome") == "refinance_refused"]
    out["refinance_refused_cost_share"] = float(sum(r["execution_costs_workout"] for r in refused) / tot)
    eps = [e for r in rows for e in r.get("episode_accuracy", []) if e.get("expected_net")]
    if eps:
        ratio = np.array([e["realized_net"] / e["expected_net"] for e in eps])
        out.update({"episodes_measured": len(eps), "realized_over_expected_median": float(np.median(ratio)),
                    "realized_over_expected_p05": float(np.percentile(ratio, 5)),
                    "realized_below_90pct_of_expected": float(np.mean(ratio < 0.9))})
    cal = [c for r in rows for c in r.get("calibration", [])]
    if cal:
        for kind in ("margin_call", "liquidation"):
            ck = [c for c in cal if c["kind"] == kind]
            done = [c for c in ck if not c["censored"]]
            out[f"calibration_{kind}_events"] = len(ck)
            out[f"calibration_{kind}_censored"] = len(ck) - len(done)
            if done:
                k = sum(c["shortfall_vs_threshold"] for c in done)
                from scipy.stats import beta

                out[f"calibration_{kind}_shortfall_rate"] = k / len(done)
                out[f"calibration_{kind}_shortfall_upper95"] = float(beta.ppf(0.95, k + 1, len(done) - k))
                out[f"calibration_{kind}_below_balance_rate"] = float(np.mean([c["shortfall_vs_balance"]
                                                                                for c in done]))
    return out


PAIRED_KEYS = ("loss", "cost", "cost_intervention", "margin_calls", "restricted", "liquidation")


def _per_loan(r: dict[str, Any], key: str) -> float | None:
    p = r["principal"]
    if key == "loss":
        return None if r["censored"] else (r["lender_loss"] or 0.0) / p
    if key == "cost":
        return r["execution_costs"] / p
    if key == "cost_intervention":
        return r.get("execution_costs_intervention", r["execution_costs"]) / p
    if key == "margin_calls":
        return float(r["margin_calls"])
    if key == "restricted":
        return r["sessions_restricted"] / max(r["sessions"], 1)
    if key == "liquidation":
        return float(r["liquidation_episodes"] > 0)
    raise KeyError(key)


def paired_difference(cohorts: list[dict[str, Any]], a: str, b: str, key: str, cluster_days: int = 91,
                      reps: int = 2000, seed: int = 11) -> dict[str, Any]:
    """Mean of per-cohort differences a - b on the SAME cohorts (same origination, path,
    principal and random numbers), with a 95% interval resampling origination-quarter
    clusters in moving blocks. Overlapping marginal intervals are not used to claim
    equivalence; this interval is the uncertainty of the difference itself."""
    pts: list[tuple[date, float]] = []
    for c in cohorts:
        va, vb = c.get("variants", {}).get(a), c.get("variants", {}).get(b)
        if not va or not vb or "skipped" in va or "skipped" in vb:
            continue
        xa, xb = _per_loan(va, key), _per_loan(vb, key)
        if xa is None or xb is None:
            continue
        pts.append((_date(c["origination"]), xa - xb))
    if not pts:
        return {"pairs": 0}
    t0 = min(d for d, _ in pts)
    groups: dict[int, list[float]] = {}
    for d, x in pts:
        groups.setdefault((d - t0).days // cluster_days, []).append(x)
    ids = sorted(groups)
    sums = np.array([sum(groups[g]) for g in ids])
    cnts = np.array([len(groups[g]) for g in ids])
    rng = np.random.default_rng(seed)
    n, block = len(ids), min(2, len(ids))
    nb = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(reps, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(reps, -1)[:, :n]
    est = sums[idx].sum(axis=1) / np.maximum(cnts[idx].sum(axis=1), 1)
    diffs = np.array([x for _, x in pts])
    return {"pairs": len(pts), "clusters": n, "mean_difference": float(diffs.mean()),
            "ci95": [float(np.percentile(est, 2.5)), float(np.percentile(est, 97.5))],
            "share_a_worse": float(np.mean(diffs > 1e-12)), "share_a_better": float(np.mean(diffs < -1e-12))}


def clustered_interval(rows: list[dict[str, Any]], key: str, cluster_days: int = 91, reps: int = 2000,
                       seed: int = 7) -> list[float]:
    """95% interval for a per-loan metric resampling CLUSTERS: all loans
    originated within the same ~quarter (shared market shocks, overlapping
    terms, repeated instruments, retail and institutional on the same paths)
    are drawn together, adjacent clusters in moving blocks of two."""
    groups: dict[int, list[float]] = {}
    if not rows:
        return [0.0, 0.0]
    t0 = min(_date(r["origination"]) for r in rows)
    for r in rows:
        if key in ("loss", "loss_event") and r["censored"]:
            continue
        loss = (r["lender_loss"] or 0.0) / r["principal"]
        x = (loss if key == "loss" else float(loss > 1e-6) if key == "loss_event"
             else float(r["liquidation_episodes"] > 0) if key == "liquidation" else float(r[key]))
        groups.setdefault((_date(r["origination"]) - t0).days // cluster_days, []).append(x)
    ids = sorted(groups)
    if not ids:
        return [0.0, 0.0]
    rng = np.random.default_rng(seed)
    sums = np.array([sum(groups[g]) for g in ids])
    cnts = np.array([len(groups[g]) for g in ids])
    n, block = len(ids), min(2, len(ids))
    nb = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(reps, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(reps, -1)[:, :n]
    est = sums[idx].sum(axis=1) / np.maximum(cnts[idx].sum(axis=1), 1)
    return [float(np.percentile(est, 2.5)), float(np.percentile(est, 97.5))]


def zero_event_bound(rows: list[dict[str, Any]], cluster_days: int = 91, conf: float = 0.95) -> dict[str, Any]:
    """Dependence-aware inference when few or no losses occur. Loans originated in the same
    quarter share market shocks, so the unit of inference is the quarter cluster: with k of n
    clusters containing a completed loss, the exact (Clopper-Pearson) one-sided upper bound on
    the probability that a quarter's cohort contains a loss. Per-loan bounds are not claimed."""
    from scipy.stats import beta

    if not rows:
        return {"clusters": 0}
    t0 = min(_date(r["origination"]) for r in rows)
    cl: dict[int, bool] = {}
    for r in rows:
        if r["censored"]:
            continue
        g = (_date(r["origination"]) - t0).days // cluster_days
        cl[g] = cl.get(g, False) or bool((r["lender_loss"] or 0.0) > 1e-6 * r["principal"])
    n, k = len(cl), sum(cl.values())
    ub = float(beta.ppf(conf, k + 1, n - k)) if n else 1.0
    return {"clusters": n, "clusters_with_loss": k, "cluster_loss_probability_upper95": ub}


def aggregate(cohorts: list[dict[str, Any]], spacing_sessions: int | None = None,
              term_sessions: int | None = None, seed: int = 7) -> dict[str, Any]:
    """Metrics per variant with clustered 95% intervals (see clustered_interval)."""
    done = [c for c in cohorts if "variants" in c]
    names = sorted({n for c in done for n in c["variants"]})
    block = max(1, math.ceil((term_sessions or 1) / max(spacing_sessions or term_sessions or 1, 1)))
    out: dict[str, Any] = {"cohorts": len(done), "skipped": len(cohorts) - len(done), "block": block,
                           "effective_independent_cohorts": len(done) / block, "variants": {}}
    for n in names:
        rows = variant_rows(done, n)
        m = metrics(rows)
        m["loss_probability_ci95"] = clustered_interval(rows, "loss_event", seed=seed)
        m["expected_loss_ci95"] = clustered_interval(rows, "loss", seed=seed)
        m["liquidation_frequency_ci95"] = clustered_interval(rows, "liquidation", seed=seed)
        m["margin_calls_ci95"] = clustered_interval(rows, "margin_calls", seed=seed)
        m.update(zero_event_bound(rows))
        out["variants"][n] = m
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
        open_px = px
        px *= math.exp(float(r))
        vm = float(volume_multiplier[i]) if volume_multiplier is not None else 1.0
        hi, lo = max(open_px, px) * math.exp(hl / 2), min(open_px, px) * math.exp(-hl / 2)
        out.append({"bar_date": d, "open": open_px, "high": hi, "low": lo, "close": px, "volume": vol * vm,
                    "range_reported": True, "volume_reported": vol * vm > 0, "provider": provider})
    return out
