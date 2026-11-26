"""The single liquidation planner used by the evaluator, monitoring and replay.

Before this module, three places sized sales differently: the evaluator sold
the repayment cure measured at stressed value, monitoring and replay sold the
repayment cure at the bid plus a simple-interest buffer. None of them checked
that the sale restores the account, and all keyed orders by bare asset_id.

``plan_recovery`` sizes a sale with the same execution model that
``execute_recovery_advisory`` applies (bid less execution cost, a per-instrument
participation cap, the policy's execution window), accrues interest on the
obligation through grace, execution and settlement, removes the sold
quantities, and re-evaluates the residual account with the risk engine. The
smallest sale that leaves the account outside MARGIN_CALL and LIQUIDATION is
chosen. Two sale orderings are searched (priority and pro-rata) because
selling the most liquid holding first can concentrate the residual portfolio
and make restoration non-monotonic in the sale size; the search records how
often the restored/not-restored verdict changes along its grid.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any, Protocol

from shadow_engines.cri_v070.app.core.enums import MarginState
from shadow_engines.cri_v070.app.core.models import (
    AssetRiskResult,
    Holding,
    LiquidationOrder,
    LiquidationPlan,
    Loan,
    MarketData,
    Policy,
)
from shadow_engines.cri_v070.app.liquidation.policy import LiquidationExecutionPolicy
from shadow_engines.cri_v070.app.risk.math_utils import round_money

BAD_STATES = {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}
GRID_POINTS = 32
BISECTION_STEPS = 40
ROUNDING_MARGIN = 0.02


class _Engine(Protocol):
    def evaluate(self, *args: Any, **kwargs: Any) -> Any: ...


def resolve_market_data(
    holdings: list[Holding], market_data: Mapping[str, MarketData]
) -> dict[str, MarketData]:
    """Market data per holding stable_key.

    Looks up the stable key first. A bare asset_id key is accepted only when
    exactly one holding uses that asset_id, so two listings of the same symbol
    on different venues or currencies are never conflated.
    """
    counts: dict[str, int] = {}
    for h in holdings:
        counts[h.asset_id.upper()] = counts.get(h.asset_id.upper(), 0) + 1
    out: dict[str, MarketData] = {}
    for h in holdings:
        md = market_data.get(h.stable_key)
        if md is None and counts[h.asset_id.upper()] == 1:
            md = market_data.get(h.asset_id)
        if md is not None:
            out[h.stable_key] = md
    return out


def aggregate_holdings(holdings: list[Holding]) -> list[Holding]:
    """Merge holdings that share a stable identity (same rule as the evaluator)."""
    merged: dict[str, Holding] = {}
    for h in holdings:
        prior = merged.get(h.stable_key)
        merged[h.stable_key] = (
            h if prior is None else replace(prior, quantity=prior.quantity + h.quantity)
        )
    return list(merged.values())


@dataclass(frozen=True)
class PlannedOrder:
    instrument_key: str
    asset_id: str
    quantity: float
    reference_bid: float
    minimum_limit_price: float
    participation_rate: float
    daily_quantity_cap: float
    execution_observations: int
    expected_gross_proceeds: float
    expected_execution_costs: float
    expected_net_proceeds: float


@dataclass(frozen=True)
class RecoveryPlan:
    trigger_state: str
    action: str  # restore_coverage | full_recovery | data_review | none
    orders: list[PlannedOrder]
    target_net_recovery: float
    expected_net_recovery: float
    expected_execution_observations: int
    observations_to_settlement: int
    projected_interest_to_settlement: float
    projected_residual_obligation: float
    projected_post_settlement_state: str | None
    restores_safety_standard: bool
    plan_complete: bool
    ordering: str
    search: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Sellable:
    holding: Holding
    result: AssetRiskResult
    bid: float
    net_price: float
    participation: float
    daily_cap: float
    max_quantity: float
    priority: float


def _priority(asset: AssetRiskResult, total_mv: float) -> float:
    liquidity_quality = 1.0 - asset.estimated_slippage_rate
    concentration = asset.market_value / max(total_mv, 1e-9)
    return 0.45 * liquidity_quality + 0.35 * asset.risk_score + 0.20 * concentration


def _sellables(
    holdings: list[Holding],
    asset_results: list[AssetRiskResult],
    market: dict[str, MarketData],
    execution_policy: LiquidationExecutionPolicy,
) -> list[_Sellable]:
    results = {a.instrument_key: a for a in asset_results if a.instrument_key}
    total_mv = sum(a.market_value for a in asset_results)
    window = max(1, execution_policy.max_execution_observations)
    out = []
    for h in holdings:
        r = results.get(h.stable_key)
        md = market.get(h.stable_key)
        if r is None or md is None or not r.eligible or h.quantity <= 0 or md.halted:
            continue
        if md.bid is None or md.bid <= 0:
            continue
        # C4: execution uses the participation rate the risk model assumed for
        # this instrument, capped by the client's execution policy.
        participation = min(execution_policy.max_participation_rate, r.participation_rate)
        adv_shares = float(md.average_daily_volume or 0.0)
        if adv_shares <= 0 and md.average_dollar_volume and md.last_price > 0:
            adv_shares = float(md.average_dollar_volume) / md.last_price
        daily_cap = participation * adv_shares
        if daily_cap <= 0:
            continue
        out.append(
            _Sellable(
                holding=h,
                result=r,
                bid=float(md.bid),
                net_price=float(md.bid) * (1 - execution_policy.execution_cost_rate),
                participation=participation,
                daily_cap=daily_cap,
                max_quantity=min(h.quantity, daily_cap * window),
                priority=_priority(r, total_mv),
            )
        )
    out.sort(key=lambda s: s.priority, reverse=True)
    return out


def _quantities(sellables: list[_Sellable], target: float, ordering: str) -> list[float]:
    if ordering == "priority":
        qty = []
        remaining = target
        for s in sellables:
            q = min(s.max_quantity, max(0.0, remaining) / s.net_price)
            qty.append(q)
            remaining -= q * s.net_price
        return qty
    # pro_rata: the same fraction f of each holding's quantity, each capped by
    # what the execution window allows; f found by bisection.
    def net(f: float) -> float:
        return sum(min(s.max_quantity, f * s.holding.quantity) * s.net_price for s in sellables)

    lo, hi = 0.0, 1.0
    if net(hi) <= target:
        lo = hi
    else:
        for _ in range(60):
            mid = (lo + hi) / 2
            if net(mid) < target:
                lo = mid
            else:
                hi = mid
        lo = hi
    return [min(s.max_quantity, lo * s.holding.quantity) for s in sellables]


def _interest(
    balance_loan: Loan, interest_policy: Any, as_of: datetime, observations: int
) -> float:
    if interest_policy is None or observations <= 0:
        return 0.0
    from shadow_engines.cri_v070.app.credit.interest import accrue_scheduled_periods

    _, result = accrue_scheduled_periods(
        balance_loan, interest_policy, as_of, as_of + timedelta(days=observations)
    )
    return float(result.interest_accrued)


def plan_recovery(
    *,
    engine: _Engine,
    account_ref: str,
    holdings: list[Holding],
    loan: Loan,
    balance: float,
    policy: Policy,
    market_data: Mapping[str, MarketData],
    asset_results: list[AssetRiskResult],
    state: MarginState,
    execution_policy: LiquidationExecutionPolicy | None = None,
    interest_policy: Any = None,
    as_of: datetime | None = None,
    data_review_required: bool = False,
) -> RecoveryPlan:
    execution_policy = execution_policy or LiquidationExecutionPolicy()
    as_of = as_of or datetime(2000, 1, 1, tzinfo=UTC)
    holdings = aggregate_holdings(holdings)
    if data_review_required:
        return _empty(state, "data_review", balance)
    if state not in BAD_STATES or balance <= 0:
        return _empty(state, "none", balance)
    market = resolve_market_data(holdings, market_data)
    sellables = _sellables(holdings, asset_results, market, execution_policy)
    forced = state == MarginState.LIQUIDATION
    delay = (
        execution_policy.liquidation_delay_observations
        if forced
        else execution_policy.margin_call_grace_observations
    )
    settle = execution_policy.settlement_delay_observations

    def outcome(target: float, ordering: str) -> tuple[bool, dict[str, Any]]:
        qty = _quantities(sellables, target, ordering)
        net = sum(q * s.net_price for q, s in zip(qty, sellables, strict=True))
        exec_obs = max(
            [math.ceil(q / s.daily_cap - 1e-12) for q, s in zip(qty, sellables, strict=True) if q > 0],
            default=0,
        )
        obs = delay + exec_obs + settle
        interest = _interest(loan, interest_policy, as_of, obs)
        residual = balance + interest - net
        info: dict[str, Any] = {
            "qty": qty, "net": net, "exec_obs": exec_obs, "obs": obs,
            "interest": interest, "residual": residual, "state": None,
        }
        if residual <= 0.005:
            info["state"] = MarginState.SAFE.value
            return True, info
        sold = {s.holding.stable_key: q for q, s in zip(qty, sellables, strict=True)}
        left = [
            replace(h, quantity=h.quantity - sold.get(h.stable_key, 0.0))
            for h in holdings
            if h.quantity - sold.get(h.stable_key, 0.0) > 1e-9
        ]
        if not left:
            return False, info
        after = engine.evaluate(
            account_ref,
            left,
            Loan(principal=residual + ROUNDING_MARGIN, currency=loan.currency),
            policy,
            market_data,
            _size_plan=False,
        )
        info["state"] = after.margin_state.value
        ok = after.margin_state not in BAD_STATES and not getattr(
            after, "data_review_required", False
        )
        return ok, info

    max_net = sum(s.max_quantity * s.net_price for s in sellables)
    if forced and execution_policy.full_liquidation_on_forced_trigger:
        target = balance + _interest(loan, interest_policy, as_of, delay + settle)
        ok, info = outcome(min(target, max_net), "priority")
        return _build(state, "full_recovery", sellables, info, target, ok, "priority",
                      execution_policy, {"max_net": max_net})

    search: dict[str, Any] = {"max_net": round_money(max_net), "orderings": {}}
    best: tuple[float, str, dict[str, Any]] | None = None
    for ordering in ("priority", "pro_rata"):
        grid = [max_net * k / GRID_POINTS for k in range(GRID_POINTS + 1)]
        verdicts = []
        first_ok = None
        for x in grid:
            ok, _ = outcome(x, ordering)
            verdicts.append(ok)
            if ok and first_ok is None:
                first_ok = x
        flips = sum(1 for a, b in pairwise(verdicts) if a != b)
        search["orderings"][ordering] = {
            "grid_points": len(grid),
            "verdict_changes": flips,
            "monotonic_on_grid": flips <= 1 and (not verdicts or verdicts[-1] or not any(verdicts)),
        }
        if first_ok is None:
            continue
        hi = first_ok
        lo = grid[max(0, grid.index(first_ok) - 1)] if first_ok > 0 else 0.0
        if first_ok > 0:
            for _ in range(BISECTION_STEPS):
                if hi - lo <= 0.01:
                    break
                mid = (lo + hi) / 2
                if outcome(mid, ordering)[0]:
                    hi = mid
                else:
                    lo = mid
        ok, info = outcome(hi, ordering)
        if ok and (best is None or info["net"] < best[0] - 0.01):
            best = (info["net"], ordering, info)
        search["orderings"][ordering]["minimal_restoring_net"] = round_money(info["net"])
    if best is None:
        _, info = outcome(max_net, "priority")
        return _build(state, "restore_coverage", sellables, info, max_net, False,
                      "priority", execution_policy, search)
    _, ordering, info = best
    return _build(state, "restore_coverage", sellables, info, info["net"], True,
                  ordering, execution_policy, search)


def _empty(state: MarginState, action: str, balance: float) -> RecoveryPlan:
    return RecoveryPlan(
        trigger_state=state.value, action=action, orders=[], target_net_recovery=0.0,
        expected_net_recovery=0.0, expected_execution_observations=0,
        observations_to_settlement=0, projected_interest_to_settlement=0.0,
        projected_residual_obligation=round_money(balance),
        projected_post_settlement_state=None,
        restores_safety_standard=action == "none", plan_complete=action == "none",
        ordering="none",
    )


def _build(
    state: MarginState,
    action: str,
    sellables: list[_Sellable],
    info: dict[str, Any],
    target: float,
    restores: bool,
    ordering: str,
    execution_policy: LiquidationExecutionPolicy,
    search: dict[str, Any],
) -> RecoveryPlan:
    orders = []
    for q, s in zip(info["qty"], sellables, strict=True):
        if q <= 1e-12:
            continue
        gross = q * s.bid
        costs = gross * execution_policy.execution_cost_rate
        orders.append(
            PlannedOrder(
                instrument_key=s.holding.stable_key,
                asset_id=s.holding.asset_id,
                quantity=round(q, 8),
                reference_bid=round_money(s.bid),
                minimum_limit_price=round_money(s.bid * (1 - execution_policy.maximum_price_slippage)),
                participation_rate=round(s.participation, 6),
                daily_quantity_cap=round(s.daily_cap, 8),
                execution_observations=math.ceil(q / s.daily_cap - 1e-12),
                expected_gross_proceeds=round_money(gross),
                expected_execution_costs=round_money(costs),
                expected_net_proceeds=round_money(gross - costs),
            )
        )
    return RecoveryPlan(
        trigger_state=state.value,
        action=action,
        orders=orders,
        target_net_recovery=round_money(target),
        expected_net_recovery=round_money(info["net"]),
        expected_execution_observations=int(info["exec_obs"]),
        observations_to_settlement=int(info["obs"]),
        projected_interest_to_settlement=round_money(info["interest"]),
        projected_residual_obligation=round_money(max(0.0, info["residual"])),
        projected_post_settlement_state=info["state"],
        restores_safety_standard=restores,
        plan_complete=restores and abs(info["net"] - target) <= 0.05,
        ordering=ordering,
        search=search,
    )


def advisory_from_plan(
    plan: RecoveryPlan,
    *,
    execution_policy: LiquidationExecutionPolicy,
    issued_date: date,
) -> dict[str, Any]:
    """The executable advisory consumed by execute_recovery_advisory."""
    return {
        "issued_date": issued_date.isoformat(),
        "trigger_state": plan.trigger_state,
        "action": plan.action,
        "target_net_recovery": plan.target_net_recovery,
        "orders": [
            {
                "instrument_key": o.instrument_key,
                "asset_id": o.asset_id,
                "side": "sell",
                "requested_quantity": o.quantity,
                "order_type": "marketable_limit",
                "reference_bid": o.reference_bid,
                "minimum_limit_price": o.minimum_limit_price,
                "participation_rate": o.participation_rate,
                "estimated_gross_proceeds": o.expected_gross_proceeds,
                "estimated_execution_costs": o.expected_execution_costs,
                "estimated_net_proceeds": o.expected_net_proceeds,
            }
            for o in plan.orders
        ],
        "estimated_net_recovery": plan.expected_net_recovery,
        "uncovered_target": round_money(max(0.0, plan.target_net_recovery - plan.expected_net_recovery)),
        "plan_complete": plan.plan_complete,
        "restores_safety_standard": plan.restores_safety_standard,
        "projected_post_settlement_state": plan.projected_post_settlement_state,
        "projected_interest_to_settlement": plan.projected_interest_to_settlement,
        "observations_to_settlement": plan.observations_to_settlement,
        "ordering": plan.ordering,
        "search": plan.search,
        "execution_policy": asdict(execution_policy),
    }


def liquidation_plan_from_recovery(plan: RecoveryPlan) -> LiquidationPlan | None:
    """The evaluator's LiquidationPlan view of a RecoveryPlan."""
    if plan.action in {"none", "data_review"}:
        return None
    forced = plan.trigger_state == MarginState.LIQUIDATION.value
    reason = (
        "forced_liquidation_recovery_breach" if forced else "margin_call_dynamic_coverage_breach"
    )
    if not plan.orders:
        reason = "insufficient_liquid_collateral_to_meet_target_recovery"
    elif not plan.restores_safety_standard:
        reason = f"{reason}; plan_does_not_restore_safety_standard"
    return LiquidationPlan(
        action="liquidate" if forced else "recommend_liquidation_or_cure",
        target_cash_recovery=plan.target_net_recovery,
        orders=[
            LiquidationOrder(
                asset_id=o.asset_id,
                side="sell",
                quantity=o.quantity,
                order_type="marketable_limit",
                estimated_cash_recovery=o.expected_net_proceeds,
                reason="restore_dynamic_recovery_coverage"
                if plan.action == "restore_coverage"
                else "full_recovery",
                instrument_key=o.instrument_key,
            )
            for o in plan.orders
        ],
        reason=reason,
        estimated_total_recovery=plan.expected_net_recovery,
        unrecovered_target_amount=round_money(
            max(0.0, plan.target_net_recovery - plan.expected_net_recovery)
        ),
        plan_complete=plan.plan_complete,
    )
