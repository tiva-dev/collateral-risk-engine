"""Candidate risk and recovery engine: adaptive intervention thresholds.

All thresholds are balances in the loan currency, derived from one set of
simulated scenarios per decision so that they are mutually consistent:

    B*(q, d) = (C + P_q(d)) / g(d + T_exec + T_settle)

C        pledged cash in the loan currency (no haircut, never sold);
P_q(d)   mean net sale proceeds over the worst (1 - q) share of scenarios when
         selling starts d sessions from now, each instrument at its
         participation cap of reported volume, at the bid less the stressed
         half-spread, square-root impact and the execution fee;
g(T)     interest growth of the obligation over T sessions in calendar days.

Margin call      B_mc  = B*(q_mc, 1 + grace): waiting the grace period and
                 then selling still covers the obligation at q_mc.
Forced recovery  B_f   = B*(q_f, 1 + delay): above it even an immediate sale
                 is at risk at q_f; no grace is given.
Initial capacity B_0   = C/g + (B_mc - C/g) * m_h, m_h the (1 - q_h) quantile
                 of the securities value multiplier over h sessions: from
                 B_0 the probability of reaching B_mc within h sessions is
                 at most 1 - q_h. B_0 is then capped by the client's
                 configured LTV ceilings (ceilings only, never floors).
Early warning    B_ew  = C/g + (B_mc - C/g) * m_1 at q_ew.
Restoration      after a margin call or forced recovery the planner sells the
                 smallest quantity that brings the residual obligation to
                 B_0 (uncapped) of the remaining collateral.

Tail risk enters through filtered historical simulation of the instruments'
own standardised residuals (blended with a Student-t(4) tail for short
histories); the recovery horizon through grace, delay, execution and
settlement; execution liquidity through participation caps on reported
volume; settlement through the market profile; interest through g and the
accrued balance; parameter uncertainty through credible-bound volatilities.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
from scipy import stats

from app.candidate.config import VERSION, CandidateConfig
from app.candidate.estimation import InstrumentEstimate, estimate_instrument
from app.core.enums import AssetType, MarginState
from app.core.models import Holding, Loan, MarketData, Policy
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


def data_key(h: Holding) -> str:
    """Stable market-data identity (exchange, symbol, currency, type) of a holding.

    Accepts holdings whose asset_id is already ``EXCHANGE:SYMBOL:CCY``; never
    falls back to a bare symbol.
    """
    if len(h.asset_id.split(":")) == 3:
        return InstrumentIdentity.from_holding(h).stable_key
    return f"{h.exchange.upper()}:{h.asset_id.upper()}:{h.currency.upper()}:{h.asset_type.value.upper()}"


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
    restriction: str | None
    cure_amount: float
    asset_results: list[CandidateAsset]
    instruments: list[dict[str, Any]]
    recovery_plan: dict[str, Any] | None
    reasons: list[str]
    parameters: dict[str, Any]

    @property
    def stressed_liquidation_value(self) -> float | None:
        return self.thresholds.get("margin_call_proceeds")


@dataclass
class _Sim:
    keys: list[str]
    price: np.ndarray        # (n,) current bid (or last price)
    cap: np.ndarray          # (n,) shares per session at participation cap
    cost: np.ndarray         # (n,) stressed half-spread + fee
    cum: np.ndarray          # (M, D + 1, n) cumulative net price multipliers
    level: np.ndarray        # (M, D + 1, n) price multipliers S(t), S(0) = 1
    horizon: int
    joint: bool


class CandidateEngine:
    model_id = "candidate"

    def __init__(self, config: CandidateConfig | None = None, priors: dict[str, Prior] | None = None):
        self.cfg = config or CandidateConfig()
        self.priors = priors or {}

    # ---------------------------------------------------------- simulation

    def _simulate(self, est: list[InstrumentEstimate], price: np.ndarray, cap: np.ndarray, cost: np.ndarray,
                  participation: np.ndarray, horizon: int, rng: np.random.Generator) -> _Sim:
        cfg = self.cfg
        m, n = cfg.n_paths, len(est)
        res_dates = [e.dates[-e.residuals.size:] for e in est]
        common = sorted(set.intersection(*[set(d) for d in res_dates])) if n else []
        joint = len(common) >= 60
        z = np.empty((m, horizon, n))
        if joint:
            pos = [{d: i for i, d in enumerate(ds)} for ds in res_dates]
            table = np.array([[e.residuals[pos[j][d]] for j, e in enumerate(est)] for d in common])
            idx = rng.integers(0, len(common), size=(m, horizon))
            z[:] = table[idx]
        else:
            # Joint dependence cannot be estimated on a short common window:
            # comonotone draws (same rank for every instrument), a conservative
            # assumption for a long-only collateral portfolio.
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
        net = np.zeros((m, horizon + 1, n))
        impact_k = cfg.impact_coefficient * np.sqrt(participation)
        for t in range(horizon):
            s = np.sqrt(var)
            r = s * z[:, t]
            logp = logp + r
            level[:, t + 1] = np.exp(logp)
            # Sale during session t+1 at that session's price, net of costs.
            net[:, t + 1] = level[:, t + 1] * np.maximum(0.0, 1 - cost - impact_k * s)
            var = cfg.ewma_lambda * var + (1 - cfg.ewma_lambda) * r * r
        cum = np.cumsum(net, axis=1)
        return _Sim([e.key for e in est], price, cap, cost, cum, level, horizon, joint)

    @staticmethod
    def _exec_sessions(sim: _Sim, q: np.ndarray) -> np.ndarray:
        e = np.where(q > 0, np.ceil(q / np.maximum(sim.cap, 1e-12) - 1e-12), 0)
        return np.maximum(e, (q > 0).astype(float)).astype(int)

    def _proceeds_tail(self, sim: _Sim, q: np.ndarray, delay: int, conf: float) -> tuple[float, int]:
        """Mean net proceeds over the worst (1 - conf) scenarios, and sessions to finish."""
        if not np.any(q > 0):
            return 0.0, 0
        e = np.minimum(self._exec_sessions(sim, q), sim.horizon - delay)
        cols = np.arange(q.size)
        window = sim.cum[:, delay + e, cols] - sim.cum[:, delay, cols]
        per = np.where(e > 0, q * sim.price / np.maximum(e, 1), 0.0)
        p = window @ per
        k = max(1, math.ceil((1 - conf) * p.size))
        return float(np.sort(p)[:k].mean()), int(e.max())

    def _value_quantile(self, sim: _Sim, q: np.ndarray, sessions: int, conf: float) -> float:
        v0 = float(q @ sim.price)
        if v0 <= 0:
            return 1.0
        m = (sim.level[:, sessions] @ (q * sim.price)) / v0
        return float(np.quantile(m, 1 - conf))

    def _thresholds(self, sim: _Sim, q: np.ndarray, cash: float, growth: Any, grace: int, delay: int,
                    settle: int) -> dict[str, float]:
        cfg = self.cfg
        p_mc, e_mc = self._proceeds_tail(sim, q, 1 + grace, cfg.margin_call_confidence)
        p_f, e_f = self._proceeds_tail(sim, q, 1 + delay, cfg.forced_confidence)
        g_mc = growth(1 + grace + e_mc + settle)
        g_f = growth(1 + delay + e_f + settle)
        b_mc = (cash + p_mc) / g_mc
        b_f = max(b_mc, (cash + p_f) / g_f)
        c_part = cash / g_mc
        m_h = self._value_quantile(sim, q, min(cfg.headroom_sessions, sim.horizon), cfg.headroom_confidence)
        m_w = self._value_quantile(sim, q, min(cfg.warning_sessions, sim.horizon), cfg.warning_confidence)
        b0 = c_part + (b_mc - c_part) * min(1.0, m_h)
        b_ew = max(b0, c_part + (b_mc - c_part) * min(1.0, m_w))
        return {
            "initial_capacity_uncapped": b0, "early_warning": b_ew, "margin_call": b_mc, "forced_recovery": b_f,
            "margin_call_proceeds": cash + p_mc, "margin_call_horizon_sessions": float(1 + grace + e_mc + settle),
            "forced_horizon_sessions": float(1 + delay + e_f + settle),
            "headroom_value_multiplier": m_h, "warning_value_multiplier": m_w,
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
    ) -> CandidateEvaluation:
        cfg = self.cfg
        as_of = as_of or datetime.now(UTC)
        day: date = as_of.date()
        ex = execution_policy or LiquidationExecutionPolicy()
        holdings = aggregate_holdings(holdings)
        ccy = loan.currency.upper()
        market = resolve_market_data(holdings, market_data)
        reasons: list[str] = []
        restriction: str | None = None
        data_review = False
        cash = 0.0
        legs: list[tuple[Holding, MarketData, InstrumentEstimate, Any, float]] = []
        assets: list[CandidateAsset] = []
        ceiling_value = 0.0
        all_est: list[InstrumentEstimate] = []
        for h in holdings:
            if h.quantity <= 0:
                continue
            if h.currency.upper() != ccy:
                restriction = "cross_currency_collateral"
                reasons.append(f"{h.asset_id} is in {h.currency}; loan in {ccy}: cross-currency collateral is "
                               "not supported by the candidate engine and the account is restricted")
                continue
            if h.asset_type == AssetType.CASH:
                cash += h.quantity
                ceiling_value += h.quantity * policy.base_ltv.get(AssetType.CASH, 0.0)
                continue
            md = market.get(h.stable_key)
            dkey = data_key(h)
            age = (as_of - md.timestamp).days if md is not None and md.timestamp else 0
            if md is None or md.last_price <= 0 or age > ex.maximum_quote_age_days:
                data_review = True
                reasons.append(f"{h.asset_id}: no current price (missing or older than "
                               f"{ex.maximum_quote_age_days} days); data review, no automated sale")
                assets.append(CandidateAsset(h.stable_key, h.asset_id, 0.0, False, False, 0.0, 1.0, 0.0, dkey,
                                             ["no current price"]))
                continue
            prof = profile_for_exchange(h.exchange)
            if prof is None:
                reasons.append(f"{h.asset_id}: exchange {h.exchange} has no onboarded market profile; no lending value")
                assets.append(CandidateAsset(h.stable_key, h.asset_id, h.quantity * md.last_price, False, False, 0.0,
                                             1.0, 0.0, dkey, ["market not onboarded"]))
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
            participation = min(prof.max_participation_rate, ex.max_participation_rate)
            mv = h.quantity * md.last_price
            all_est.append(est)
            if not (md.bid and md.bid > 0):
                # Daily-bar quotes carry no observed bid: the reference bid is the
                # last price less the estimated half-spread, and it is flagged.
                md = replace(md, bid=md.last_price * (1 - est.spread / 2))
                market[h.stable_key] = md
                est.reasons.append("bid not observed: reference bid = last price less estimated half-spread")
            sellable = bool(est.adv_shares and not md.halted and est.residuals.size >= 10)
            if not sellable:
                why = ("halted" if md.halted else "no reported volume" if not est.adv_shares
                       else f"only {est.residuals.size} residuals")
                reasons.append(f"{h.asset_id}: not sellable in the model ({why}); no lending value")
            elif not est.eligible:
                reasons.append(f"{h.asset_id}: no lending value ({'; '.join(est.reasons[:2])})")
            lend_ok = est.eligible and sellable and not (md.halted and not policy.allow_lending_on_stale_or_halted_assets)
            leg_cost = est.spread * cfg.spread_stress_multiplier / 2 + ex.execution_cost_rate
            assets.append(CandidateAsset(
                h.stable_key, h.asset_id, mv, sellable, lend_ok, participation, min(1.0, leg_cost),
                min(1.0, est.sigma_used * math.sqrt(252)) if math.isfinite(est.sigma_used) else 1.0, dkey,
                est.reasons[:4]))
            if sellable:
                legs.append((h, md, est, prof, participation))
            if lend_ok:
                cap_q = min(h.quantity, participation * (est.adv_shares or 0.0) * cfg.max_exit_sessions)
                ceiling_value += cap_q * md.last_price * policy.base_ltv.get(h.asset_type, 0.0)
        ceiling_value *= policy.portfolio_ltv_cap

        securities_value = sum(a.market_value for a in assets)
        total_value = securities_value + cash
        balance = loan.balance
        rate = float(getattr(interest_policy, "annual_interest_rate", 0.0) or 0.0)
        basis = 360.0 if getattr(interest_policy, "day_count_convention", "") == "actual_360" else 365.0

        def growth(sessions: float) -> float:
            return 1.0 + rate * calendar_days(math.ceil(sessions)) / basis

        settle = max([ex.settlement_delay_observations] + [p.settlement_days for *_x, p, _y in legs])
        grace, delay = ex.margin_call_grace_observations, ex.liquidation_delay_observations
        est_list = [e for _h, _m, e, _p, _q in legs]
        price = np.array([float(m.bid or m.last_price) for _h, m, *_r in legs])
        cap = np.array([q * (e.adv_shares or 0.0) for _h, _m, e, _p, q in legs])
        cost = np.array([e.spread * cfg.spread_stress_multiplier / 2 + ex.execution_cost_rate for e in est_list])
        part = np.array([q for *_r, q in legs])
        lend_mask = np.array([next(a.lending_value_eligible for a in assets if a.instrument_key == h.stable_key)
                              for h, *_r in legs], dtype=bool)
        qty_full = np.array([h.quantity for h, *_r in legs])
        q_lend = np.where(lend_mask, np.minimum(qty_full, cap * cfg.max_exit_sessions), 0.0)
        horizon = 1 + max(grace, delay) + settle + cfg.max_exit_sessions
        horizon = max(horizon, cfg.headroom_sessions + 1)
        if seed is None:
            h_ = hashlib.sha256(f"{day.isoformat()}|{'|'.join(sorted(e.key for e in est_list))}".encode())
            seed = int(h_.hexdigest()[:12], 16)
        rng = np.random.default_rng(seed)
        sim = self._simulate(est_list, price, cap, cost, part, horizon, rng) if legs else None
        if sim is not None:
            th = self._thresholds(sim, q_lend, cash, growth, grace, delay, settle)
        else:
            th = {"initial_capacity_uncapped": cash / growth(1 + grace + settle), "early_warning": 0.0,
                  "margin_call": cash / growth(1 + grace + settle), "forced_recovery": cash / growth(1 + delay + settle),
                  "margin_call_proceeds": cash}
            th["early_warning"] = th["initial_capacity_uncapped"]
        b0 = min(th["initial_capacity_uncapped"], ceiling_value)
        th["client_ceiling"] = ceiling_value
        th["initial_capacity"] = b0
        th["restoration_target"] = (th["initial_capacity_uncapped"] if cfg.restoration == "initial_capacity"
                                    else th["early_warning"])
        if restriction:
            b0 = 0.0
            th["initial_capacity"] = 0.0

        if data_review:
            state = MarginState.RESTRICT_NEW_BORROWING
        elif balance <= b0 + 1e-9:
            state = MarginState.SAFE
        elif balance <= th["early_warning"]:
            state = MarginState.RESTRICT_NEW_BORROWING
        elif balance <= th["margin_call"]:
            state = MarginState.WATCH
        elif balance <= th["forced_recovery"]:
            state = MarginState.MARGIN_CALL
        else:
            state = MarginState.LIQUIDATION
        if restriction and state in {MarginState.SAFE}:
            state = MarginState.RESTRICT_NEW_BORROWING
        cure = max(0.0, balance - th["restoration_target"]) if state in {MarginState.MARGIN_CALL,
                                                                           MarginState.LIQUIDATION} else 0.0
        reasons[:0] = [
            (f"initial capacity {b0:,.2f} = min(risk capacity {th['initial_capacity_uncapped']:,.2f}, "
             f"client ceiling {ceiling_value:,.2f})"),
            (f"margin call above {th['margin_call']:,.2f}: grace {grace} + execution + settlement "
             f"{settle} sessions covered at {cfg.margin_call_confidence:.1%}"),
            f"forced recovery above {th['forced_recovery']:,.2f}: immediate sale covered at {cfg.forced_confidence:.1%}",
            f"balance {balance:,.2f} (principal, accrued interest and fees): state {state.value}",
        ]
        plan_dict = None
        if plan and sim is not None and not data_review and not restriction and state in {
                MarginState.MARGIN_CALL, MarginState.LIQUIDATION}:
            plan_dict = self._plan(account_ref, holdings, loan, policy, market, assets, state, ex,
                                   interest_policy, as_of, sim, cash, growth, grace, delay, settle)
            reasons.append(f"recovery: {plan_dict['action']} to the restoration target; expected net "
                           f"{plan_dict['expected_net_recovery']:,.2f} over "
                           f"{plan_dict['expected_execution_observations']} sessions")
        elif state in {MarginState.MARGIN_CALL, MarginState.LIQUIDATION} and (data_review or restriction):
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
            data_review_required=data_review, restriction=restriction, cure_amount=cure, asset_results=assets,
            instruments=[e.summary() for e in all_est], recovery_plan=plan_dict, reasons=reasons,
            parameters={"config": cfg.to_dict(), "grace_sessions": grace, "delay_sessions": delay,
                        "settlement_sessions": settle, "interest_rate": rate, "joint_scenarios": bool(sim and sim.joint),
                        "seed": seed, "priors": {k: vars(p) for k, p in self.priors.items()}},
        )

    # ---------------------------------------------------------- recovery

    def _plan(self, account_ref: str, holdings: list[Holding], loan: Loan, policy: Policy,
              market_data: dict[str, MarketData], assets: list[CandidateAsset], state: MarginState,
              ex: LiquidationExecutionPolicy, interest_policy: Any, as_of: datetime, sim: _Sim, cash: float,
              growth: Any, grace: int, delay: int, settle: int) -> dict[str, Any]:
        engine = self
        key_index = {}
        for a in assets:
            if a.data_key in sim.keys:
                key_index[a.instrument_key] = sim.keys.index(a.data_key)

        class _Restoration:
            """Planner adaptor: restored when the residual obligation is within
            the restoration target of the remaining collateral."""

            def evaluate(self, _ref: str, left: list[Holding], residual: Loan, *_a: Any, **_k: Any) -> Any:
                q = np.zeros(len(sim.keys))
                c = 0.0
                for h in left:
                    if h.asset_type == AssetType.CASH and h.currency.upper() == residual.currency.upper():
                        c += h.quantity
                    elif h.stable_key in key_index:
                        q[key_index[h.stable_key]] += h.quantity
                q = np.minimum(q, sim.cap * engine.cfg.max_exit_sessions)
                th = engine._thresholds(sim, q, c, growth, grace, delay, settle)
                target = (th["initial_capacity_uncapped"] if engine.cfg.restoration == "initial_capacity"
                          else th["early_warning"])
                ok = residual.balance <= target
                return type("R", (), {"margin_state": MarginState.SAFE if ok else MarginState.MARGIN_CALL,
                                      "data_review_required": False})()

        exec_policy = replace(ex, full_liquidation_on_forced_trigger=False,
                              settlement_delay_observations=settle,
                              max_execution_observations=max(ex.max_execution_observations, 1))
        # The planner sizes daily sales from ADV; give it the same reported-volume
        # ADV the candidate's thresholds assumed (stable keys only).
        resolved = resolve_market_data(holdings, market_data)
        planner_md: dict[str, MarketData] = dict(resolved)
        for h in holdings:
            j = key_index.get(h.stable_key)
            if j is not None and h.stable_key in resolved:
                adv = float(sim.cap[j] / max(next(a.participation_rate for a in assets
                                                   if a.instrument_key == h.stable_key), 1e-12))
                planner_md[h.stable_key] = replace(resolved[h.stable_key], average_daily_volume=adv)
        rp = plan_recovery(engine=_Restoration(), account_ref=account_ref, holdings=holdings, loan=loan,
                           balance=loan.balance, policy=policy, market_data=planner_md,
                           asset_results=assets, state=state, execution_policy=exec_policy,  # type: ignore[arg-type]
                           interest_policy=interest_policy, as_of=as_of)
        out = rp.to_dict()
        out["note"] = ("partial liquidation sized by the unified planner to the candidate restoration target; "
                       "advisory only, never executed")
        return out
