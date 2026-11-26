"""Loan lifecycle simulator: monitoring, margin call, liquidation, settlement.

One vectorised implementation is used both as the "truth" mechanics applied to
the realised synthetic path (M = 1) and as Approach C's inner Monte Carlo over
model-generated scenarios (M = many). Using the same mechanics in both places
is what makes origination consistent with recovery.

Conventions: initial collateral value V0 = 1 in loan currency; prices are
indices starting at 1; ADV is in the same value units; time step is one
trading day; interest accrues at rate/252 per day on the full obligation.

Policy pi (all parameters explicit, see ``Policy``):
  * Monitoring at each daily close on observed (possibly stale) prices.
  * Margin call when O_t > psi * C_t, where C_t = ell_t * V_t is the method's
    current safe capacity and psi >= 1 separates the initial standard from the
    maintenance standard.
  * The borrower does not cure (worst case for the lender) unless cure_p > 0.
  * After ``grace`` days, if the breach persists, the lender sells collateral
    until O_t - pending proceeds <= C_t (restore the initial standard), using
    the collateral-funded cure size (corrected) or the cash-cure size (the
    defect under study), or sells everything (``full``).
  * Each day at most ``participation`` x ADV of value is sold per asset, none
    while halted, at the true price less half-spread, square-root impact
    Y * sigma_d * sqrt(sold / ADV) and a fee.
  * Proceeds settle ``settle_days`` later; FX moves during settlement apply.
  * At maturity the borrower repays if the net liquidation value covers the
    obligation; otherwise the lender liquidates and loss = shortfall.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DAILY = 252.0


@dataclass(frozen=True)
class Policy:
    psi: float = 1.15
    grace: int = 2
    participation: float = 0.10
    impact_y: float = 0.7
    fee: float = 0.0010
    settle_days: int = 2
    rate: float = 0.10
    sizing: str = "corrected"  # corrected | cash_cure | full | none
    cure_p: float = 0.0


def simulate_loans(
    principal: float | np.ndarray,
    weights: np.ndarray,
    r_true: np.ndarray,          # (M, D, n) loan-currency log returns
    halted: np.ndarray,          # (M, D, n)
    spread: np.ndarray,          # (M, D, n) relative spread
    adv: np.ndarray,             # (M, D, n) ADV value units
    sigma_d: np.ndarray,         # (M, D, n) daily vol used for impact
    ell: np.ndarray,             # (M, D) capacity ratio C_t / V_t of the method
    fx_ret: np.ndarray | None,   # (M, D) depreciation of collateral currency
    policy: Policy,
    rng: np.random.Generator | None = None,
) -> dict[str, np.ndarray]:
    m, d_total, n = r_true.shape
    prices = np.exp(np.cumsum(r_true, axis=1))                # true prices
    obs = prices.copy()
    for t in range(1, d_total):                                # stale while halted
        obs[:, t] = np.where(halted[:, t], obs[:, t - 1], prices[:, t])
    holdings = np.broadcast_to(weights, (m, n)).astype(float).copy()
    obligation = np.broadcast_to(np.asarray(principal, float), (m,)).astype(float).copy()
    pending = np.zeros((m, d_total + policy.settle_days + 1))
    in_call = np.zeros(m, bool)
    call_day = np.full(m, -1)
    liquidating = np.zeros(m, bool)
    calls = np.zeros(m, int)
    cancelled = np.zeros(m, int)
    episodes = np.zeros(m, int)
    first_call = np.full(m, -1)
    first_sale = np.full(m, -1)
    last_sale = np.full(m, -1)
    first_underwater = np.full(m, -1)
    sold_value = np.zeros(m)
    cost_paid = np.zeros(m)
    exhausted = np.zeros(m, bool)
    min_cushion = np.full(m, np.inf)
    cushion_path = np.zeros((m, d_total))
    rng = rng or np.random.default_rng(0)
    rows = np.arange(m)
    for t in range(d_total):
        obligation *= 1 + policy.rate / DAILY
        due = pending[:, t]  # FX during settlement is applied when cash is queued
        obligation = obligation - due
        overpaid = obligation < 0
        obligation[overpaid] = 0.0
        value_obs = (holdings * obs[:, t]).sum(axis=1)
        cost_rate = spread[:, t] / 2 + policy.impact_y * sigma_d[:, t] * np.sqrt(
            policy.participation
        ) + policy.fee
        nlv = (holdings * prices[:, t] * (1 - cost_rate)).sum(axis=1)
        pend_total = pending[:, t + 1 :].sum(axis=1)
        cushion = nlv + pend_total - obligation
        cushion_path[:, t] = cushion
        newly_under = (cushion < 0) & (first_underwater < 0) & (obligation > 0)
        first_underwater[newly_under] = t
        min_cushion = np.minimum(min_cushion, np.where(obligation > 0, cushion, np.inf))
        if policy.sizing == "none":
            continue
        capacity = ell[:, t] * value_obs
        net_obl = obligation - pend_total
        breach = (net_obl > policy.psi * capacity) & (net_obl > 1e-9)
        start_call = breach & ~in_call & ~liquidating
        if policy.cure_p > 0:
            cured = start_call & (rng.random(m) < policy.cure_p)
            obligation[cured] = np.minimum(obligation[cured], capacity[cured])
            start_call &= ~cured
        in_call |= start_call
        call_day[start_call] = t
        calls += start_call
        first_call[start_call & (first_call < 0)] = t
        decide = in_call & ~liquidating & (t >= call_day + policy.grace + 1)
        recovered = decide & ~breach
        in_call[recovered] = False
        cancelled += recovered
        begin = decide & breach
        liquidating |= begin
        in_call[begin] = False
        episodes += begin
        first_sale[begin & (first_sale < 0)] = t
        if not liquidating.any():
            continue
        # Required sale value (observed-price units) to restore net_obl <= ell * V.
        mean_cost = (cost_rate * holdings * obs[:, t]).sum(axis=1) / np.maximum(value_obs, 1e-12)
        ell_t = ell[:, t]
        denom = (1 - mean_cost) - ell_t
        excess = net_obl - ell_t * value_obs
        if policy.sizing == "corrected":
            target = np.where(denom > 1e-6, excess / np.maximum(denom, 1e-6), np.inf)
        elif policy.sizing == "cash_cure":
            target = net_obl - capacity
        else:
            target = np.full(m, np.inf)
        exhausted_equity = net_obl >= value_obs * (1 - mean_cost)
        target = np.where(exhausted_equity, np.inf, target)
        target = np.minimum(target, value_obs)
        done = liquidating & (excess <= 1e-9)
        liquidating[done] = False
        act = liquidating & (target > 1e-9)
        if act.any():
            share = holdings * obs[:, t] / np.maximum(value_obs, 1e-12)[:, None]
            want = np.where(share > 0, share * np.where(np.isfinite(target), target, value_obs)[:, None], 0.0)
            cap = np.where(halted[:, t], 0.0, policy.participation * adv[:, t])
            sell_val_obs = np.clip(
                np.minimum(np.minimum(want, cap), holdings * obs[:, t]), 0.0, None
            )
            sell_val_obs[~act] = 0.0
            qty = np.where(obs[:, t] > 0, sell_val_obs / obs[:, t], 0.0)
            gross = qty * prices[:, t]
            impact = policy.impact_y * sigma_d[:, t] * np.sqrt(
                np.where(adv[:, t] > 0, gross / adv[:, t], 0.0)
            )
            costs = gross * (spread[:, t] / 2 + impact + policy.fee)
            net = np.maximum(gross - costs, 0.0).sum(axis=1)
            if fx_ret is not None and policy.settle_days:
                end = min(t + policy.settle_days, d_total - 1)
                fx_move = fx_ret[:, t + 1 : end + 1].sum(axis=1) if end > t else 0.0
                net = net * np.exp(-fx_move)
            pending[rows, t + policy.settle_days] += net
            holdings = np.maximum(holdings - qty, 0.0)
            sold_value += gross.sum(axis=1)
            cost_paid += costs.sum(axis=1)
            sold_any = gross.sum(axis=1) > 0
            last_sale[sold_any] = t
        empty = holdings.sum(axis=1) <= 1e-12
        exhausted |= empty & (obligation - pending[:, t + 1 :].sum(axis=1) > 1e-9)
    final_t = d_total - 1
    obligation_final = obligation - pending[:, final_t + 1 :].sum(axis=1)
    cost_rate = spread[:, final_t] / 2 + policy.impact_y * sigma_d[:, final_t] * np.sqrt(
        policy.participation
    ) + policy.fee
    nlv_final = (holdings * prices[:, final_t] * (1 - cost_rate)).sum(axis=1)
    loss = np.maximum(0.0, obligation_final - nlv_final)
    return {
        "loss": loss,
        "calls": calls,
        "cancelled_calls": cancelled,
        "liquidation_episodes": episodes,
        "first_call": first_call,
        "first_sale": first_sale,
        "last_sale": last_sale,
        "first_underwater": first_underwater,
        "sold_value": sold_value,
        "execution_cost": cost_paid,
        "exhausted": exhausted,
        "min_cushion": min_cushion,
        "cushion_path": cushion_path,
        "final_obligation": obligation_final,
        # State at the end of the term, for terminal_workout.
        "holdings_final": holdings,
        "obligation_gross_final": obligation,
        "pending_after_final": pending[:, final_t + 1 :],
        "price_index_final": prices[:, final_t],
        "obs_price_final": obs[:, final_t],
    }


def terminal_workout(
    out: dict[str, np.ndarray],
    r_tail: np.ndarray,       # (M, T, n) loan-currency log returns after maturity
    halted: np.ndarray,       # (M, T, n)
    spread: np.ndarray,       # (M, T, n)
    adv: np.ndarray,          # (M, T, n)
    sigma_d: np.ndarray,      # (M, T, n)
    fx_ret: np.ndarray | None,  # (M, T)
    policy: Policy,
) -> dict[str, np.ndarray]:
    """Maturity: repayment or a lender workout through real execution.

    At maturity the borrower repays when the account (net liquidation value
    plus pending proceeds) covers the obligation. Otherwise the lender sells
    the remaining collateral day by day, at most ``participation`` x ADV per
    asset, nothing while halted, paying half-spread, impact and fees; proceeds
    settle ``settle_days`` later (FX moves in between apply) and interest keeps
    accruing until the obligation is covered. Loss is the obligation left
    after every settlement. If collateral is still unsold when the path ends,
    the loss marks the remainder at net liquidation value and the loan is
    flagged ``censored``.
    """
    m, t_len, _ = r_tail.shape
    holdings = out["holdings_final"].astype(float).copy()
    obligation = out["obligation_gross_final"].astype(float).copy()
    carry = out["pending_after_final"]
    pending = np.zeros((m, t_len + policy.settle_days + carry.shape[1] + 2))
    pending[:, : carry.shape[1]] = carry
    prices = out["price_index_final"][:, None, :] * np.exp(np.cumsum(r_tail, axis=1))
    rows = np.arange(m)
    cost0 = spread[:, 0] / 2 + policy.impact_y * sigma_d[:, 0] * np.sqrt(policy.participation) + policy.fee
    nlv0 = (holdings * out["price_index_final"] * (1 - cost0)).sum(axis=1)
    cushion0 = nlv0 + carry.sum(axis=1) - obligation
    repaid = (cushion0 >= 0) | (obligation <= 1e-9)
    defaulted = ~repaid
    obligation[repaid] = 0.0
    holdings[repaid] = 0.0
    workout_days = np.zeros(m, int)
    sold_cost = np.zeros(m)
    for t in range(t_len):
        obligation *= 1 + policy.rate / DAILY
        obligation -= pending[:, t]
        obligation = np.maximum(obligation, 0.0)
        net_owed = obligation - pending[:, t + 1 :].sum(axis=1)
        active = defaulted & (net_owed > 1e-9) & (holdings.sum(axis=1) > 1e-12)
        if not active.any():
            continue
        workout_days[active] += 1
        cap_val = np.where(halted[:, t], 0.0, policy.participation * adv[:, t])
        held_val = holdings * prices[:, t]
        cost_rate = spread[:, t] / 2 + policy.impact_y * sigma_d[:, t] * np.sqrt(policy.participation) + policy.fee
        need_gross = net_owed / np.maximum(1 - cost_rate.mean(axis=1), 1e-6)
        share = held_val / np.maximum(held_val.sum(axis=1, keepdims=True), 1e-12)
        sell_val = np.minimum(np.minimum(share * need_gross[:, None], cap_val), held_val)
        sell_val[~active] = 0.0
        qty = np.where(prices[:, t] > 0, sell_val / prices[:, t], 0.0)
        impact = policy.impact_y * sigma_d[:, t] * np.sqrt(np.where(adv[:, t] > 0, sell_val / adv[:, t], 0.0))
        costs = sell_val * (spread[:, t] / 2 + impact + policy.fee)
        net = np.maximum(sell_val - costs, 0.0).sum(axis=1)
        if fx_ret is not None and policy.settle_days:
            end = min(t + policy.settle_days, t_len - 1)
            net = net * np.exp(-(fx_ret[:, t + 1 : end + 1].sum(axis=1) if end > t else 0.0))
        pending[rows, t + policy.settle_days] += net
        holdings = np.maximum(holdings - qty, 0.0)
        sold_cost += costs.sum(axis=1)
    for t in range(t_len, pending.shape[1]):
        obligation -= pending[:, t]
    obligation = np.maximum(obligation, 0.0)
    last = t_len - 1
    cost_last = spread[:, last] / 2 + policy.impact_y * sigma_d[:, last] * np.sqrt(policy.participation) + policy.fee
    residual_nlv = (holdings * prices[:, last] * (1 - cost_last)).sum(axis=1)
    censored = defaulted & (obligation > 1e-9) & (holdings.sum(axis=1) > 1e-12)
    loss = np.maximum(0.0, obligation - residual_nlv)
    return {
        "loss": loss,
        "defaulted_at_maturity": defaulted,
        "workout_days": workout_days,
        "workout_cost": sold_cost,
        "censored": censored,
    }
