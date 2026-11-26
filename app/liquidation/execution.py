"""Execution of liquidation advisories.

Advisories are produced only by app.liquidation.planning (plan_recovery ->
advisory_from_plan); this module executes them. Orders are matched to
holdings and quotes by stable instrument key. The daily participation cap is
the order's own rate (the rate the risk model assumed for that instrument),
never more than the execution policy's maximum.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from typing import Any

from app.core.models import Holding, MarketData
from app.liquidation.policy import LiquidationExecutionPolicy
from app.risk.math_utils import round_money


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

    from app.liquidation.planning import resolve_market_data

    by_key = {holding.stable_key: holding for holding in holdings}
    asset_counts: dict[str, int] = {}
    for held in holdings:
        asset_counts[held.asset_id.upper()] = asset_counts.get(held.asset_id.upper(), 0) + 1
    quotes = resolve_market_data(holdings, market_data)
    fills: list[dict[str, Any]] = []
    unfilled_reasons: list[str] = []
    target = max(0.0, remaining_target)

    for requested in advisory.get("orders", []):
        if target <= 0:
            break
        asset_id = str(requested["asset_id"])
        key = str(requested.get("instrument_key") or "")
        if not key:
            # Legacy advisory without an instrument key: accept the bare
            # asset_id only when it identifies exactly one holding.
            matches = [h.stable_key for h in holdings if h.asset_id == asset_id]
            key = matches[0] if len(matches) == 1 and asset_counts.get(asset_id.upper(), 0) == 1 else ""
        holding = by_key.get(key)
        data = quotes.get(key)
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
        participation = min(
            policy.max_participation_rate,
            float(requested.get("participation_rate", policy.max_participation_rate)),
        )
        max_daily_quantity = average_volume * participation
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
        by_key[key] = replace(holding, quantity=remaining_quantity)
        fills.append(
            {
                "asset_id": asset_id,
                "instrument_key": key,
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
        by_key[held.stable_key] for held in holdings if by_key[held.stable_key].quantity > 0
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
