"""Executable liquidation advisories and recovery settlement primitives."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import date
from typing import Any

from app.core.models import Holding, Loan, MarketData
from app.liquidation.policy import LiquidationExecutionPolicy
from app.risk.math_utils import round_money


def projected_interest_buffer(
    loan: Loan,
    annual_interest_rate: float,
    observations_until_settlement: int,
) -> float:
    """Conservative simple-interest buffer through execution and settlement."""

    days = max(0, observations_until_settlement)
    return round_money(loan.principal * max(0.0, annual_interest_rate) * days / 365)


def build_recovery_advisory(
    *,
    holdings: list[Holding],
    market_data: Mapping[str, MarketData],
    target_net_recovery: float,
    policy: LiquidationExecutionPolicy,
    trigger_state: str,
    issued_date: date,
) -> dict[str, Any]:
    """Build an auditable stock, quantity, and executable-limit-price advisory."""

    remaining = max(0.0, target_net_recovery)
    orders: list[dict[str, Any]] = []
    def liquidity_value(holding: Holding) -> float:
        data = market_data.get(holding.asset_id)
        return float(data.average_dollar_volume or 0.0) if data is not None else 0.0

    candidates = sorted(holdings, key=liquidity_value, reverse=True)
    for holding in candidates:
        if remaining <= 0:
            break
        data = market_data.get(holding.asset_id)
        if data is None or data.bid is None or data.bid <= 0 or data.halted:
            continue
        gross_unit_price = data.bid
        net_unit_price = gross_unit_price * (1 - policy.execution_cost_rate)
        if net_unit_price <= 0:
            continue
        requested_quantity = min(holding.quantity, remaining / net_unit_price)
        estimated_net = requested_quantity * net_unit_price
        orders.append(
            {
                "asset_id": holding.asset_id,
                "side": "sell",
                "requested_quantity": round(requested_quantity, 8),
                "order_type": "marketable_limit",
                "reference_bid": round_money(gross_unit_price),
                "minimum_limit_price": round_money(
                    gross_unit_price * (1 - policy.maximum_price_slippage)
                ),
                "estimated_gross_proceeds": round_money(
                    requested_quantity * gross_unit_price
                ),
                "estimated_execution_costs": round_money(
                    requested_quantity
                    * gross_unit_price
                    * policy.execution_cost_rate
                ),
                "estimated_net_proceeds": round_money(estimated_net),
            }
        )
        remaining -= estimated_net
    return {
        "issued_date": issued_date.isoformat(),
        "trigger_state": trigger_state,
        "target_net_recovery": round_money(target_net_recovery),
        "orders": orders,
        "estimated_net_recovery": round_money(
            sum(float(order["estimated_net_proceeds"]) for order in orders)
        ),
        "uncovered_target": round_money(max(0.0, remaining)),
        "plan_complete": remaining <= 0.01,
        "execution_policy": asdict(policy),
    }


def execute_recovery_advisory(
    *,
    holdings: list[Holding],
    market_data: Mapping[str, MarketData],
    advisory: Mapping[str, Any],
    remaining_target: float,
    policy: LiquidationExecutionPolicy,
    observation_date: date,
) -> tuple[list[Holding], dict[str, Any]]:
    """Execute against current bids with participation, freshness, and cost controls."""

    by_asset = {holding.asset_id: holding for holding in holdings}
    fills: list[dict[str, Any]] = []
    unfilled_reasons: list[str] = []
    target = max(0.0, remaining_target)

    for requested in advisory.get("orders", []):
        if target <= 0:
            break
        asset_id = str(requested["asset_id"])
        holding = by_asset.get(asset_id)
        data = market_data.get(asset_id)
        if holding is None or holding.quantity <= 0:
            unfilled_reasons.append(f"{asset_id}:holding_unavailable")
            continue
        if data is None or data.bid is None or data.bid <= 0:
            unfilled_reasons.append(f"{asset_id}:executable_bid_unavailable")
            continue
        if data.halted:
            unfilled_reasons.append(f"{asset_id}:market_halted")
            continue
        quote_date = data.timestamp.date()
        if (observation_date - quote_date).days > policy.maximum_quote_age_days:
            unfilled_reasons.append(f"{asset_id}:quote_too_stale")
            continue
        minimum_price = float(requested.get("minimum_limit_price", 0.0))
        if data.bid < minimum_price:
            unfilled_reasons.append(f"{asset_id}:bid_below_limit")
            continue
        average_volume = max(0.0, float(data.average_daily_volume or 0.0))
        max_daily_quantity = average_volume * policy.max_participation_rate
        if max_daily_quantity <= 0:
            unfilled_reasons.append(f"{asset_id}:volume_unavailable")
            continue

        net_unit_price = data.bid * (1 - policy.execution_cost_rate)
        requested_quantity = float(requested.get("requested_quantity", 0.0))
        target_quantity = target / max(net_unit_price, 1e-9)
        filled_quantity = min(
            holding.quantity,
            requested_quantity,
            max_daily_quantity,
            target_quantity,
        )
        if filled_quantity <= 0:
            continue

        gross = filled_quantity * data.bid
        costs = gross * policy.execution_cost_rate
        net = gross - costs
        target -= net
        remaining_quantity = max(0.0, holding.quantity - filled_quantity)
        by_asset[asset_id] = replace(holding, quantity=remaining_quantity)
        fills.append(
            {
                "asset_id": asset_id,
                "side": "sell",
                "requested_quantity": round(requested_quantity, 8),
                "filled_quantity": round(filled_quantity, 8),
                "unfilled_quantity": round(
                    max(0.0, requested_quantity - filled_quantity), 8
                ),
                "execution_price": round_money(data.bid),
                "minimum_limit_price": round_money(minimum_price),
                "gross_proceeds": round_money(gross),
                "execution_costs": round_money(costs),
                "net_proceeds": round_money(net),
                "participation_quantity_cap": round(max_daily_quantity, 8),
            }
        )

    updated_holdings = [
        by_asset[holding.asset_id]
        for holding in holdings
        if by_asset[holding.asset_id].quantity > 0
    ]
    gross = round_money(sum(float(fill["gross_proceeds"]) for fill in fills))
    costs = round_money(sum(float(fill["execution_costs"]) for fill in fills))
    net = round_money(sum(float(fill["net_proceeds"]) for fill in fills))
    return updated_holdings, {
        "date": observation_date.isoformat(),
        "fills": fills,
        "unfilled_reasons": sorted(set(unfilled_reasons)),
        "gross_proceeds": gross,
        "execution_costs": costs,
        "net_proceeds": net,
        "remaining_target": round_money(max(0.0, remaining_target - net)),
        "status": (
            "filled"
            if remaining_target - net <= 0.01
            else "partial"
            if fills
            else "unfilled"
        ),
    }
