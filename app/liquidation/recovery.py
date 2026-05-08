from __future__ import annotations

from dataclasses import dataclass

from app.core.models import Holding, MarketData, Policy
from app.liquidation.order_book import estimate_market_sell_from_order_book
from app.risk.adjustments import RawRiskInputs, spread_rate
from app.risk.math_utils import clamp, normal_expected_shortfall_loss, sqrt_impact


@dataclass(frozen=True)
class RecoveryEstimate:
    stressed_liquidation_value: float
    estimated_slippage_rate: float
    per_unit_stressed_recovery: float
    method: str


def estimate_stressed_recovery(
    holding: Holding,
    market: MarketData,
    policy: Policy,
    raw: RawRiskInputs,
    market_value: float,
) -> RecoveryEstimate:
    if market.halted and not policy.allow_lending_on_stale_or_halted_assets:
        return RecoveryEstimate(
            stressed_liquidation_value=0.0,
            estimated_slippage_rate=1.0,
            per_unit_stressed_recovery=0.0,
            method="halted_asset_zero_recovery",
        )

    quantity = max(0.0, holding.quantity)
    last_price = max(0.0, market.last_price)
    if quantity == 0 or last_price == 0:
        return RecoveryEstimate(0.0, 1.0, 0.0, "zero_quantity_or_price")

    spread = spread_rate(market)
    stress_loss = normal_expected_shortfall_loss(
        raw.annualized_volatility,
        max(raw.liquidation_horizon_days, 1.0),
        raw.confidence,
    )

    order_book_estimate = estimate_market_sell_from_order_book(
        quantity=quantity,
        last_price=last_price,
        order_book=market.order_book,
    )

    if order_book_estimate is not None:
        residual_penalty = 0.0
        if order_book_estimate.residual_quantity > 0:
            residual_ratio = order_book_estimate.residual_quantity / max(quantity, 1e-9)
            residual_penalty = clamp(0.15 + residual_ratio * 0.45, 0.0, 0.75)
        stress_overlay = clamp(0.45 * stress_loss + 0.50 * spread + residual_penalty, 0.0, 0.90)
        stressed_value = order_book_estimate.proceeds * (1.0 - stress_overlay)
        slippage = clamp(order_book_estimate.order_book_slippage_rate + stress_overlay, 0.0, 1.0)
        return RecoveryEstimate(
            stressed_liquidation_value=max(0.0, stressed_value),
            estimated_slippage_rate=slippage,
            per_unit_stressed_recovery=max(0.0, stressed_value / quantity),
            method="order_book_depth_with_stress_overlay",
        )

    half_spread_cost = spread / 2.0
    market_impact = sqrt_impact(market_value, market.average_dollar_volume)
    data_penalty = clamp((1.0 - market.data_quality_score) * 0.25, 0.0, 0.25)
    total_slippage = clamp(half_spread_cost + market_impact + stress_loss + data_penalty, 0.0, 0.95)
    per_unit = last_price * (1.0 - total_slippage)
    return RecoveryEstimate(
        stressed_liquidation_value=max(0.0, quantity * per_unit),
        estimated_slippage_rate=total_slippage,
        per_unit_stressed_recovery=max(0.0, per_unit),
        method="proxy_spread_volume_volatility_stress",
    )
