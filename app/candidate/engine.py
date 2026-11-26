"""Candidate risk and recovery engine: adaptive intervention thresholds.

All thresholds are balances in the loan currency, derived from one simulated
scenario set per decision so that they are mutually consistent.

Coverage of a balance B if recovery starts d sessions from now, per path:

    cover(d) = (C + P(d)) / G(d + T_exec(path) + T_settle)

C        pledged cash in the loan currency (at par, never sold);
P(d)     net sale proceeds on that path: each instrument is sold each session
         at its participation cap of that session's simulated volume (which
         varies over time and falls with the portfolio's drawdown), only on
         sessions that fill (no-fill, suspension and limit-down sessions do
         not), at the simulated bid less the stressed extra spread,
         square-root impact on the simulated volatility and the fee; any
         quantity unsold within the horizon contributes nothing;
G(T)     contractual growth of the obligation over T sessions, projected
         with the production interest engine (capitalization schedule,
         day count, existing accrued interest), T path-dependent.

Margin call      B_mc = ES_{q_mc} of cover(1 + grace): the mean coverage over
                 the worst (1 - q_mc) paths.
Forced recovery  B_f  = ES_{q_f} of cover(1 + delay); never below B_mc.
Initial capacity B_0: the largest balance whose probability of crossing the
                 margin-call level at ANY session within h sessions (first
                 passage, path-wise, with the obligation growing under the
                 contract) is at most 1 - q_h; then capped by the client's LTV
                 ceilings (institutional policy, applied independently).
Early warning    B_ew: the same first-passage rule over the warning horizon at
                 q_ew; never below the uncapped B_0 and never above B_mc.
Restoration      the uncapped B_0 of the remaining LENDABLE collateral after a
                 sale (assets without lending value never gain it).

Recovery sizing uses the unified recovery planner with the same reference
bid, costs, participation and settlement as the thresholds. Data
deterioration (missing, stale or unreliable material data) is separated from
economic deterioration: it blocks new borrowing and is escalated, but never
by itself triggers a sale.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from scipy import stats

from app.candidate.config import VERSION, CandidateConfig
from app.candidate.estimation import InstrumentEstimate, estimate_instrument
from app.core.enums import AssetType, MarginState
from app.core.models import Holding, Loan, MarketData, Policy
from app.credit.interest import InterestPolicy, accrue_scheduled_periods
from app.liquidation.planning import (
    aggregate_holdings,
    calendar_days,
    plan_recovery,
    resolve_market_data,
)
from app.liquidation.policy import LiquidationExecutionPolicy
from app.market_data.identity import InstrumentIdentity
from app.markets.hierarchical import Prior
from app.markets.profiles import profile_for_exchange

T4_SCALE = math.sqrt(2.0 / 4.0)  # unit-variance Student-t(4)
BAD = {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}


def data_key(h: Holding) -> str:
    """Stable market-data identity (exchange, symbol, currency, type) of a holding.

    Accepts holdings whose asset_id is already ``EXCHANGE:SYMBOL:CCY``; never
    falls back to a bare symbol.
    """
    if len(h.asset_id.split(":")) == 3:
        return InstrumentIdentity.from_holding(h).stable_key
    return f"{h.exchange.upper()}:{h.asset_id.upper()}:{h.currency.upper()}:{h.asset_type.value.upper()}"


def obligation_growth(loan: Loan, interest_policy: Any, as_of: datetime, max_sessions: int) -> np.ndarray:
    """G[t]: projected obligation / current obligation after t sessions, from the
    contractual interest engine (capitalization boundaries, day count, accrued
    interest and fees of the actual loan). Client-supplied interest is projected
    with the same contractual terms, engine-calculated."""
    g = np.ones(max_sessions + 1)
    if interest_policy is None:
        return g
    terms = interest_policy
    if getattr(terms, "interest_accrual_mode", "engine_calculated") != "engine_calculated":
        terms = replace(terms, interest_accrual_mode="engine_calculated")
    base = loan if loan.balance > 0 else Loan(1.0, currency=loan.currency)
    b0 = base.balance
    cur, t_prev = base, as_of
    for t in range(1, max_sessions + 1):
        t_now = as_of + timedelta(days=calendar_days(t))
        cur, _ = accrue_scheduled_periods(cur, terms, t_prev, t_now)
        t_prev = t_now
        g[t] = cur.balance / b0
    return g


@dataclass
class CandidateAsset:
    """Per-holding result, shaped for the unified recovery planner."""

    instrument_key: str
    asset_id: str
    market_value: float
    eligible: bool                 # sellable now (planner)
    lending_value_eligible: bool   # contributes to capacity
    participation_rate: float
    estimated_slippage_rate: float
    risk_score: float
    data_key: str
    data_status: str = "ok"        # ok | degraded | missing
    notes: list[str] = field(default_factory=list)


@dataclass
class CandidateEvaluation:
    model_version: str
    account_ref: str
    as_of: str
    loan_currency: str
    portfolio_market_value: float
    securities_value: float
    cash_value: float
    current_outstanding_balance: float
    thresholds: dict[str, float]
    threshold_ltv: dict[str, float | None]
    approved_credit_limit: float
    current_available_credit: float
    margin_state: MarginState
    data_review_required: bool
    data_status: str
    deterioration: str | None
    restriction: str | None
    cure_amount: float
    asset_results: list[CandidateAsset]
    instruments: list[dict[str, Any]]
    recovery_plan: dict[str, Any] | None
    reasons: list[str]
    threshold_reasoning: dict[str, str]
    parameters: dict[str, Any]

    @property
    def stressed_liquidation_value(self) -> float | None:
        return self.thresholds.get("margin_call_proceeds")


@dataclass
class _Sim:
    keys: list[str]
    price: np.ndarray        # (n,) reference bid now
    cap: np.ndarray          # (n,) shares per session at participation cap of normal volume
    extra_cost: np.ndarray   # (n,) stressed extra half-spread + fee (beyond the bid)
    impact_k: np.ndarray     # (n,) impact coefficient x sqrt(participation)
    level: np.ndarray        # (M, D + 1, n) price multipliers, level[:, 0] = 1
    sigma: np.ndarray        # (M, D + 1, n) simulated daily vol in force during each session
    volume: np.ndarray       # (M, D + 1, n) volume multiplier x fill indicator
    growth: np.ndarray       # (D + settle + 2,) contractual obligation growth
    horizon: int
    settle: int
    joint: bool
    stats: dict[str, Any] = field(default_factory=dict)


class CandidateEngine:
    model_id = "candidate"

    def __init__(self, config: CandidateConfig | None = None, priors: dict[str, Prior] | None = None):
        self.cfg = config or CandidateConfig()
        self.priors = priors or {}

    # ---------------------------------------------------------- simulation

    def _simulate(self, est: list[InstrumentEstimate], limits: list[float | None], weights: np.ndarray,
                  horizon: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray, bool,
                                                                   dict[str, Any]]:
        cfg = self.cfg
        m, n = cfg.n_paths, len(est)
        res_dates = [e.dates[-e.residuals.size:] for e in est]
        common = sorted(set.intersection(*[set(d) for d in res_dates])) if n else []
        joint = len(common) >= 60
        z = np.empty((m, horizon, n))
        if joint:
            pos = [{d: i for i, d in enumerate(ds)} for ds in res_dates]
            table = np.array([[e.residuals[pos[j][d]] for j, e in enumerate(est)] for d in common])
            z[:] = table[rng.integers(0, len(common), size=(m, horizon))]
        else:
            # Joint dependence is not estimable on a short common window:
            # comonotone draws, conservative for long-only collateral.
            u = rng.random((m, horizon))
            for j, e in enumerate(est):
                z[:, :, j] = np.quantile(e.residuals, u)
        for j, e in enumerate(est):
            if e.tail_weight < 1.0:
                pool = np.sort(e.residuals)
                ranks = (np.searchsorted(pool, z[:, :, j], side="left") + 0.5) / pool.size
                swap = rng.random((m, horizon)) >= e.tail_weight
                t = stats.t.ppf(np.clip(ranks, 1e-6, 1 - 1e-6), 4) * T4_SCALE
                z[:, :, j] = np.where(swap, t, z[:, :, j])
        sig0 = np.array([e.sigma_used for e in est])
        var = np.broadcast_to(sig0**2, (m, n)).copy()
        logp = np.zeros((m, n))
        level = np.ones((m, horizon + 1, n))
        sigma = np.empty((m, horizon + 1, n))
        sigma[:, 0] = sig0
        volume = np.ones((m, horizon + 1, n))
        # Liquidity state: AR(1) log volume, falling with portfolio drawdown.
        a = np.zeros((m, n))
        p_nf = np.array([cfg.no_fill_share_of_stale * e.stale_share for e in est])
        p_susp = np.array([max(cfg.suspension_floor, e.missing_share / max(cfg.suspension_mean_sessions, 1.0))
                           for e in est])
        suspended = np.zeros((m, n), dtype=bool)
        carry = np.zeros((m, n))
        lim = np.array([math.log(1 - x) if (x and cfg.price_limit_scenarios) else -np.inf for x in limits])
        counts = {"limit_down_sessions": 0, "suspended_sessions": 0, "no_fill_sessions": 0}
        for t in range(horizon):
            s = np.sqrt(var)
            r = s * z[:, t] + carry
            carry = np.zeros((m, n))
            # Suspensions: price frozen, move accumulates, released on resumption.
            start = rng.random((m, n)) < p_susp
            stay = suspended & (rng.random((m, n)) > 1.0 / max(cfg.suspension_mean_sessions, 1.0))
            suspended = stay | start
            carry = np.where(suspended, r, carry)
            r = np.where(suspended, 0.0, r)
            # Price limits: the session closes at the band; the excess carries.
            locked = r < lim
            carry = carry + np.where(locked, r - lim, 0.0)
            r = np.where(locked, lim, r)
            logp = logp + r
            level[:, t + 1] = np.exp(logp)
            sigma[:, t + 1] = s
            port = np.exp(logp) @ weights
            dd = np.clip(1.0 - port, 0.0, 1.0)[:, None]
            a = cfg.liquidity_persistence * a + cfg.liquidity_noise_sd * math.sqrt(
                1 - cfg.liquidity_persistence**2) * rng.standard_normal((m, n))
            nu = np.clip(np.exp(a - 0.5 * cfg.liquidity_noise_sd**2) * (1 - cfg.volume_drawdown_elasticity * dd),
                         cfg.min_volume_multiplier, 3.0)
            no_fill = rng.random((m, n)) < p_nf
            fill = ~(suspended | locked | no_fill)
            volume[:, t + 1] = nu * fill
            counts["limit_down_sessions"] += int(locked.sum())
            counts["suspended_sessions"] += int(suspended.sum())
            counts["no_fill_sessions"] += int(no_fill.sum())
            var = cfg.ewma_lambda * var + (1 - cfg.ewma_lambda) * (s * z[:, t]) ** 2
        tot = m * horizon * max(n, 1)
        st = {k: v / tot for k, v in counts.items()}
        return level, sigma, volume, joint, st

    def _proceeds(self, sim: _Sim, q: np.ndarray, delay: int) -> tuple[np.ndarray, np.ndarray]:
        """Per-path net proceeds and sessions to complete, selling from session delay+1."""
        m = sim.level.shape[0]
        if not np.any(q > 0):
            return np.zeros(m), np.zeros(m, dtype=int)
        sl = slice(delay + 1, sim.horizon + 1)
        cap = sim.cap[None, None, :] * sim.volume[:, sl]                 # (M, T, n)
        cum = np.cumsum(cap, axis=1)
        sold_cum = np.minimum(cum, q[None, None, :])
        sold = np.diff(sold_cum, axis=1, prepend=0.0)
        net_px = sim.price * sim.level[:, sl] * np.maximum(
            0.0, 1 - sim.extra_cost - sim.impact_k * sim.sigma[:, sl])
        proceeds = (sold * net_px).sum(axis=(1, 2))
        done = sold_cum >= q[None, None, :] - 1e-9                       # (M, T, n)
        all_done = done.all(axis=2)
        first = np.where(all_done.any(axis=1), all_done.argmax(axis=1) + 1, sl.stop - sl.start)
        return proceeds, first.astype(int)

    def _coverage_es(self, sim: _Sim, q: np.ndarray, cash: float, delay: int, conf: float) -> tuple[float, float, float]:
        p, t_exec = self._proceeds(sim, q, delay)
        idx = np.minimum(delay + t_exec + sim.settle, sim.growth.size - 1)
        cover = (cash + p) / sim.growth[idx]
        k = max(1, math.ceil((1 - conf) * cover.size))
        worst = np.sort(cover)[:k]
        return float(worst.mean()), float(np.percentile(t_exec, 50)), float(np.percentile(t_exec, 95))

    def _first_passage_level(self, sim: _Sim, q: np.ndarray, cash: float, b_mc: float, sessions: int,
                             conf: float) -> float:
        """Largest B with P(B * G(t) > B_mc(t) for some t <= sessions) <= 1 - conf, where the
        margin-call level moves with the lendable collateral value along each path."""
        v0 = float(q @ sim.price)
        c_part = cash / sim.growth[min(sim.growth.size - 1, 1 + sim.settle)]
        if v0 <= 0 or b_mc <= c_part:
            return max(0.0, min(b_mc, c_part))
        mult = (sim.level[:, 1:sessions + 1] @ (q * sim.price)) / v0              # (M, h)
        level_t = c_part + (b_mc - c_part) * mult
        admissible = (level_t / sim.growth[1:sessions + 1][None, :]).min(axis=1)  # per path
        return float(min(b_mc, np.quantile(admissible, 1 - conf)))

    def _thresholds(self, sim: _Sim, q: np.ndarray, cash: float, grace: int, delay: int) -> dict[str, float]:
        cfg = self.cfg
        b_mc, mc_med, mc_p95 = self._coverage_es(sim, q, cash, 1 + grace, cfg.margin_call_confidence)
        b_f_raw, f_med, f_p95 = self._coverage_es(sim, q, cash, 1 + delay, cfg.forced_confidence)
        b_f = max(b_mc, b_f_raw)
        b0 = self._first_passage_level(sim, q, cash, b_mc, min(cfg.headroom_sessions, sim.horizon),
                                       cfg.headroom_confidence)
        b_ew_raw = self._first_passage_level(sim, q, cash, b_mc, min(cfg.warning_sessions, sim.horizon),
                                             cfg.warning_confidence)
        b_ew = min(b_mc, max(b0, b_ew_raw))
        return {
            "initial_capacity_uncapped": b0, "early_warning": b_ew, "margin_call": b_mc, "forced_recovery": b_f,
            "margin_call_proceeds": b_mc, "execution_sessions_median_mc": mc_med,
            "execution_sessions_p95_mc": mc_p95, "execution_sessions_median_forced": f_med,
            "execution_sessions_p95_forced": f_p95,
        }

    # ---------------------------------------------------------- evaluate

    def evaluate(
        self,
        account_ref: str,
        holdings: list[Holding],
        loan: Loan,
        policy: Policy,
        market_data: dict[str, MarketData],
        *,
        bars: dict[str, list[dict[str, Any]]],
        execution_policy: LiquidationExecutionPolicy | None = None,
        interest_policy: Any = None,
        as_of: datetime | None = None,
        seed: int | None = None,
        plan: bool = True,
        restoration_override: Any = None,
        force_plan_state: MarginState | None = None,
    ) -> CandidateEvaluation:
        cfg = self.cfg
        as_of = as_of or datetime.now(UTC)
        day: date = as_of.date()
        ex = execution_policy or LiquidationExecutionPolicy()
        interest_policy = interest_policy or InterestPolicy(0.0)
        holdings = aggregate_holdings(holdings)
        ccy = loan.currency.upper()
        market = resolve_market_data(holdings, market_data)
        reasons: list[str] = []
        restriction: str | None = None
        missing = False
        cash = 0.0
        legs: list[dict[str, Any]] = []
        assets: list[CandidateAsset] = []
        all_est: list[InstrumentEstimate] = []
        cash_ceiling = 0.0
        for h in holdings:
            if h.quantity <= 0:
                continue
            if h.currency.upper() != ccy:
                restriction = "cross_currency_collateral"
                reasons.append(f"{h.asset_id} is in {h.currency}; loan in {ccy}: cross-currency collateral is "
                               "not supported until FX valuation, conversion and recovery are implemented")
                continue
            if h.asset_type == AssetType.CASH:
                cash += h.quantity
                cash_ceiling += h.quantity * policy.base_ltv.get(AssetType.CASH, 0.0)
                continue
            md = market.get(h.stable_key)
            dkey = data_key(h)
            if md is None or md.last_price <= 0 or (as_of - md.timestamp).days > ex.maximum_quote_age_days:
                missing = True
                reasons.append(f"{h.asset_id}: no current price (missing or older than "
                               f"{ex.maximum_quote_age_days} days); data review, no automated sale")
                assets.append(CandidateAsset(h.stable_key, h.asset_id, 0.0, False, False, 0.0, 1.0, 0.0, dkey,
                                             "missing", ["no current price"]))
                continue
            age_bd = int(np.busday_count(md.timestamp.date(), day)) if md.timestamp else 0
            prof = profile_for_exchange(h.exchange)
            if prof is None:
                reasons.append(f"{h.asset_id}: exchange {h.exchange} has no onboarded market profile; no lending value")
                assets.append(CandidateAsset(h.stable_key, h.asset_id, h.quantity * md.last_price, False, False, 0.0,
                                             1.0, 0.0, dkey, "degraded", ["market not onboarded"]))
                continue
            quote_spread = None
            if md.bid and md.ask and md.ask > md.bid:
                quote_spread = (md.ask - md.bid) / ((md.ask + md.bid) / 2)
            est = estimate_instrument(dkey, bars.get(dkey, []), day, self.priors.get(prof.code),
                                      prof.spread_floor, prof.spread_fallback, cfg, quote_spread)
            if est.adv_shares is None and md.average_daily_volume:
                est.adv_shares = float(md.average_daily_volume)
                est.reasons.append("ADV from current market data (stored bars lack volume)")
                est.eligible = est.sessions >= cfg.min_sessions and est.data_quality >= cfg.min_data_quality
            all_est.append(est)
            if not (md.bid and md.bid > 0):
                md = replace(md, bid=md.last_price * (1 - est.spread / 2))
                market[h.stable_key] = md
                est.reasons.append("bid not observed: reference bid = last price less estimated half-spread")
            participation = min(prof.max_participation_rate, ex.max_participation_rate)
            mv = h.quantity * md.last_price
            # Data deterioration (distinct from economic deterioration).
            bar_lag = int(np.busday_count(est.dates[-1], day)) if est.dates else 99
            degraded = []
            if age_bd > cfg.max_quote_lag_business_days:
                degraded.append(f"quote {age_bd} business days old")
            if bar_lag > cfg.max_bar_lag_business_days:
                degraded.append(f"last stored bar {bar_lag} business days old")
            if est.data_quality < cfg.min_data_quality:
                degraded.append(f"data quality {est.data_quality:.2f}")
            if md.halted:
                degraded.append("trading halted")
            sellable = bool(est.adv_shares and not md.halted and est.residuals.size >= 10)
            lend_ok = est.eligible and sellable and not degraded
            # Economic view: the collateral on its latest reliable information,
            # used only to tell data deterioration from economic deterioration.
            econ_ok = sellable and est.sessions >= cfg.min_sessions
            extra = est.spread / 2 * (cfg.spread_stress_multiplier - 1) + ex.execution_cost_rate
            status = "degraded" if degraded else "ok"
            assets.append(CandidateAsset(
                h.stable_key, h.asset_id, mv, sellable, lend_ok, participation, min(1.0, extra + est.spread / 2),
                min(1.0, est.sigma_used * math.sqrt(252)) if math.isfinite(est.sigma_used) else 1.0, dkey, status,
                (degraded + est.reasons)[:5]))
            if not sellable:
                why = ("halted" if md.halted else "no reported volume" if not est.adv_shares
                       else f"only {est.residuals.size} residuals")
                reasons.append(f"{h.asset_id}: not sellable in the model ({why}); no lending value")
            elif degraded:
                reasons.append(f"{h.asset_id}: data deterioration ({'; '.join(degraded)}); no lending value")
            elif not est.eligible:
                reasons.append(f"{h.asset_id}: no lending value ({'; '.join(est.reasons[:2])})")
            if sellable:
                legs.append({"h": h, "md": md, "est": est, "prof": prof, "part": participation, "lend": lend_ok,
                             "econ": econ_ok, "extra": extra, "degraded": bool(degraded)})

        securities_value = sum(a.market_value for a in assets)
        total_value = securities_value + cash
        material_degraded = [a for a in assets if a.data_status != "ok"
                             and a.market_value >= cfg.material_share * max(securities_value, 1e-9)]
        data_status = "missing" if missing else ("degraded" if material_degraded else "ok")
        balance = loan.balance
        settle = max([ex.settlement_delay_observations] + [lg["prof"].settlement_days for lg in legs])
        grace, delay = ex.margin_call_grace_observations, ex.liquidation_delay_observations
        horizon = max(1 + max(grace, delay) + cfg.max_exit_sessions, cfg.headroom_sessions + 1)
        growth = obligation_growth(loan, interest_policy, as_of, horizon + settle + 2)
        est_list = [lg["est"] for lg in legs]
        price = np.array([float(lg["md"].bid) for lg in legs])
        cap = np.array([lg["part"] * (lg["est"].adv_shares or 0.0) for lg in legs])
        qty = np.array([lg["h"].quantity for lg in legs])
        lend_mask = np.array([lg["lend"] for lg in legs], dtype=bool)
        econ_mask = np.array([lg["econ"] for lg in legs], dtype=bool)
        q_lend = np.where(lend_mask, np.minimum(qty, cap * cfg.max_exit_sessions), 0.0)
        q_econ = np.where(econ_mask, np.minimum(qty, cap * cfg.max_exit_sessions), 0.0)
        if seed is None:
            h_ = hashlib.sha256(f"{day.isoformat()}|{'|'.join(sorted(e.key for e in est_list))}".encode())
            seed = int(h_.hexdigest()[:12], 16)
        rng = np.random.default_rng(seed)
        sim = None
        if legs:
            w = qty * price / max(float(qty @ price), 1e-12)
            level, sigma, volume, joint, sstats = self._simulate(
                est_list, [lg["prof"].daily_price_limit for lg in legs], w, horizon, rng)
            sim = _Sim([e.key for e in est_list], price, cap, np.array([lg["extra"] for lg in legs]),
                       self.cfg.impact_coefficient * np.sqrt(np.array([lg["part"] for lg in legs])),
                       level, sigma, volume, growth, horizon, settle, joint, sstats)
            th = self._thresholds(sim, q_lend, cash, grace, delay)
            th_econ = self._thresholds(sim, q_econ, cash, grace, delay) if data_status == "degraded" else th
        else:
            c = cash / growth[min(growth.size - 1, 1 + grace + settle)]
            th = {"initial_capacity_uncapped": c, "early_warning": c, "margin_call": c,
                  "forced_recovery": cash / growth[min(growth.size - 1, 1 + delay + settle)], "margin_call_proceeds": c}
            th_econ = th
        # Client LTV ceilings: independent institutional policy constraints.
        ceiling_value = cash_ceiling + sum(
            float(q_lend[j]) * lg["md"].last_price * policy.base_ltv.get(lg["h"].asset_type, 0.0)
            for j, lg in enumerate(legs))
        ceiling_value *= policy.portfolio_ltv_cap
        b0 = min(th["initial_capacity_uncapped"], ceiling_value)
        th["client_ceiling"] = ceiling_value
        th["initial_capacity"] = b0
        th["restoration_target"] = (th["initial_capacity_uncapped"] if cfg.restoration == "initial_capacity"
                                    else th["early_warning"])
        if restriction:
            b0 = th["initial_capacity"] = 0.0

        def classify(t: dict[str, float], cap0: float) -> MarginState:
            if balance <= cap0 + 1e-9:
                return MarginState.SAFE
            if balance <= t["early_warning"]:
                return MarginState.RESTRICT_NEW_BORROWING
            if balance <= t["margin_call"]:
                return MarginState.WATCH
            if balance <= t["forced_recovery"]:
                return MarginState.MARGIN_CALL
            return MarginState.LIQUIDATION

        state = classify(th, b0)
        deterioration: str | None = None
        data_review = missing
        if data_status != "ok":
            # No additional borrowing while material data is missing, stale or unreliable.
            b0 = min(b0, balance)
            th["initial_capacity"] = b0
            econ_state = classify(th_econ, min(th_econ["initial_capacity_uncapped"], ceiling_value))
            if state in BAD and econ_state not in BAD:
                deterioration = "data"
                data_review = True
                reasons.append("the breach disappears on the latest reliable data for the affected holdings: "
                               "treated as data deterioration (no sale, escalated for review)")
                state = MarginState.RESTRICT_NEW_BORROWING
            elif state in BAD:
                deterioration = "economic"
            if state == MarginState.SAFE:
                state = MarginState.RESTRICT_NEW_BORROWING
            if missing:
                # A holding that cannot be valued: the breach cannot be attributed,
                # so the account is held in data review (no sale) and escalated.
                data_review = True
                if state in BAD:
                    deterioration = "data"
                state = MarginState.RESTRICT_NEW_BORROWING
        elif state in BAD:
            deterioration = "economic"
        if restriction and state == MarginState.SAFE:
            state = MarginState.RESTRICT_NEW_BORROWING
        cure = max(0.0, balance - th["restoration_target"]) if state in BAD else 0.0
        reasoning = {
            "initial_capacity": (f"{b0:,.2f}: largest balance whose chance of crossing the margin-call level at any "
                                 f"session within {cfg.headroom_sessions} sessions is at most "
                                 f"{1 - cfg.headroom_confidence:.0%} (first passage, contractual interest), "
                                 f"capped by the client ceiling {ceiling_value:,.2f}"
                                 + ("; no additional borrowing while data is deteriorated" if data_status != "ok"
                                    else "")),
            "early_warning": (f"{th['early_warning']:,.2f}: a {cfg.warning_sessions}-session path reaches the "
                              f"margin-call level with probability {1 - cfg.warning_confidence:.0%}"),
            "margin_call": (f"{th['margin_call']:,.2f}: mean coverage of the worst {1 - cfg.margin_call_confidence:.0%}"
                            f" of paths if the borrower is given {grace} session(s) of grace, then the collateral is "
                            f"sold at the participation cap of simulated volume and settles in {settle} session(s); "
                            f"median sale {th.get('execution_sessions_median_mc', 0):.0f} sessions, "
                            f"95th percentile {th.get('execution_sessions_p95_mc', 0):.0f}"),
            "forced_recovery": (f"{th['forced_recovery']:,.2f}: mean coverage of the worst "
                                f"{1 - cfg.forced_confidence:.0%} of paths for an immediate sale "
                                f"(delay {delay}); above it no grace is given"),
            "restoration_target": (f"{th['restoration_target']:,.2f}: initial risk capacity of the remaining "
                                   "lendable collateral (excluded assets never gain lending value)"),
        }
        reasons[:0] = [f"balance {balance:,.2f} (principal, accrued interest and fees): state {state.value}"
                       + (f"; deterioration: {deterioration}" if deterioration else "")]
        plan_dict = None
        plan_state = force_plan_state if force_plan_state in BAD else state
        if plan and sim is not None and not data_review and not restriction and plan_state in BAD:
            plan_dict = self._plan(account_ref, holdings, loan, policy, market, assets, legs, plan_state, ex,
                                   interest_policy, as_of, sim, grace, delay, restoration_override)
            reasons.append(f"recovery: {plan_dict['action']} to the restoration target; expected net "
                           f"{plan_dict['expected_net_recovery']:,.2f} over "
                           f"{plan_dict['expected_execution_observations']} sessions")
        elif state in BAD and (data_review or restriction):
            reasons.append("no automated recovery plan: data review or unsupported currency; escalate to a person")

        def ratio(x: float) -> float | None:
            return x / total_value if total_value > 0 else None

        return CandidateEvaluation(
            model_version=VERSION, account_ref=account_ref, as_of=as_of.isoformat(), loan_currency=ccy,
            portfolio_market_value=total_value, securities_value=securities_value, cash_value=cash,
            current_outstanding_balance=balance, thresholds=th,
            threshold_ltv={k: ratio(th[k]) for k in ("initial_capacity", "early_warning", "margin_call",
                                                     "forced_recovery", "restoration_target")}
            | {"current": ratio(balance)},
            approved_credit_limit=b0, current_available_credit=max(0.0, b0 - balance), margin_state=state,
            data_review_required=data_review, data_status=data_status, deterioration=deterioration,
            restriction=restriction, cure_amount=cure, asset_results=assets,
            instruments=[e.summary() for e in all_est], recovery_plan=plan_dict, reasons=reasons,
            threshold_reasoning=reasoning,
            parameters={"config": cfg.to_dict(), "grace_sessions": grace, "delay_sessions": delay,
                        "settlement_sessions": settle,
                        "interest": {"annual_rate": float(getattr(interest_policy, "annual_interest_rate", 0.0)),
                                     "compounding": getattr(interest_policy, "compounding", None),
                                     "day_count": getattr(interest_policy, "day_count_convention", None),
                                     "growth_over_mc_horizon": float(growth[min(growth.size - 1, 1 + grace + settle)])},
                        "joint_scenarios": bool(sim and sim.joint), "scenario_liquidity": sim.stats if sim else {},
                        "seed": seed, "priors": {k: vars(p) for k, p in self.priors.items()},
                        "provenance": {"observed": "bars, quotes, volumes", "estimated": "volatility, tails, spread, ADV",
                                       "assumed": "confidence levels, liquidity dynamics, impact coefficient",
                                       "synthetic": "simulated scenarios"}},
        )

    # ---------------------------------------------------------- recovery

    def _plan(self, account_ref: str, holdings: list[Holding], loan: Loan, policy: Policy,
              market: dict[str, MarketData], assets: list[CandidateAsset], legs: list[dict[str, Any]],
              state: MarginState, ex: LiquidationExecutionPolicy, interest_policy: Any, as_of: datetime,
              sim: _Sim, grace: int, delay: int, restoration_override: Any) -> dict[str, Any]:
        engine = self
        index = {lg["h"].stable_key: j for j, lg in enumerate(legs)}
        lendable = np.array([lg["lend"] for lg in legs], dtype=bool)

        class _Restoration:
            """Restored when the residual obligation is within the restoration
            target of the remaining collateral. Only assets that had lending value
            before the sale count; nothing gains lending value by being left over."""

            def evaluate(self, _ref: str, left: list[Holding], residual: Loan, *_a: Any, **_k: Any) -> Any:
                q = np.zeros(len(legs))
                c = 0.0
                for h in left:
                    if h.asset_type == AssetType.CASH and h.currency.upper() == residual.currency.upper():
                        c += h.quantity
                    elif h.stable_key in index:
                        q[index[h.stable_key]] += h.quantity
                q = np.where(lendable, np.minimum(q, sim.cap * engine.cfg.max_exit_sessions), 0.0)
                if restoration_override is not None:
                    target = restoration_override(q, c, sim)
                else:
                    th = engine._thresholds(sim, q, c, grace, delay)
                    target = (th["initial_capacity_uncapped"] if engine.cfg.restoration == "initial_capacity"
                              else th["early_warning"])
                ok = residual.balance <= target
                return type("R", (), {"margin_state": MarginState.SAFE if ok else MarginState.MARGIN_CALL,
                                      "data_review_required": False})()

        # Same execution model as the thresholds: the reference bid less the
        # stressed extra spread and expected impact; the planner applies the fee.
        planner_md: dict[str, MarketData] = dict(market)
        for h in holdings:
            j = index.get(h.stable_key)
            if j is None:
                continue
            lg = legs[j]
            impact = float(sim.impact_k[j] * lg["est"].sigma_used)
            extra_spread = float(lg["extra"]) - ex.execution_cost_rate
            planner_md[h.stable_key] = replace(lg["md"], bid=float(lg["md"].bid) * max(0.0, 1 - extra_spread - impact),
                                               average_daily_volume=float(lg["est"].adv_shares or 0.0))
        exec_policy = replace(ex, full_liquidation_on_forced_trigger=False, settlement_delay_observations=sim.settle,
                              max_execution_observations=max(ex.max_execution_observations, 1))
        rp = plan_recovery(engine=_Restoration(), account_ref=account_ref, holdings=holdings, loan=loan,
                           balance=loan.balance, policy=policy, market_data=planner_md,
                           asset_results=assets, state=state, execution_policy=exec_policy,  # type: ignore[arg-type]
                           interest_policy=interest_policy, as_of=as_of)
        out = rp.to_dict()
        out["note"] = ("partial liquidation sized by the unified planner to the candidate restoration target, "
                       "with the candidate's execution model; advisory only, never executed")
        return out
