from __future__ import annotations

from app.core.enums import MarginState
from app.core.models import AssetRiskResult, LiquidationOrder, LiquidationPlan
from app.risk.math_utils import clamp


def cure_amount_to_restore_coverage(
    stressed_liquidation_value: float,
    loan_balance: float,
    target_coverage: float,
) -> float:
    if loan_balance <= 0:
        return 0.0
    if target_coverage <= 1.0:
        return max(0.0, loan_balance - stressed_liquidation_value)
    numerator = target_coverage * loan_balance - stressed_liquidation_value
    if numerator <= 0:
        return 0.0
    return clamp(numerator / (target_coverage - 1.0), 0.0, loan_balance)


def build_liquidation_plan(
    account_ref: str,
    asset_results: list[AssetRiskResult],
    margin_state: MarginState,
    target_cash_recovery: float,
) -> LiquidationPlan | None:
    if target_cash_recovery <= 0:
        return None
    if margin_state not in {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}:
        return None

    remaining = target_cash_recovery
    orders: list[LiquidationOrder] = []

    def priority(asset: AssetRiskResult) -> float:
        liquidity_quality = 1.0 - asset.estimated_slippage_rate
        risk_contribution = asset.risk_score
        concentration_proxy = asset.market_value / max(sum(a.market_value for a in asset_results), 1e-9)
        return 0.45 * liquidity_quality + 0.35 * risk_contribution + 0.20 * concentration_proxy

    candidates = [a for a in asset_results if a.eligible and a.quantity > 0 and a.stressed_liquidation_value > 0]
    for asset in sorted(candidates, key=priority, reverse=True):
        if remaining <= 0:
            break
        per_unit = asset.stressed_liquidation_value / max(asset.quantity, 1e-9)
        if per_unit <= 0:
            continue
        qty = min(asset.quantity, remaining / per_unit)
        estimated_cash = qty * per_unit
        remaining -= estimated_cash
        orders.append(
            LiquidationOrder(
                asset_id=asset.asset_id,
                side="sell",
                quantity=round(qty, 8),
                order_type="marketable_limit",
                estimated_cash_recovery=round(estimated_cash, 2),
                reason="restore_dynamic_recovery_coverage",
            )
        )

    if not orders:
        return None

    reason = (
        "forced_liquidation_recovery_breach"
        if margin_state == MarginState.LIQUIDATION
        else "margin_call_dynamic_coverage_breach"
    )
    return LiquidationPlan(
        action="liquidate" if margin_state == MarginState.LIQUIDATION else "recommend_liquidation_or_cure",
        target_cash_recovery=round(target_cash_recovery, 2),
        orders=orders,
        reason=reason,
    )
