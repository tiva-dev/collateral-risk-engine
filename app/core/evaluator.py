from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Mapping

from app.audit.logger import AuditLogger
from app.core.enums import AssetType, MarginState
from app.core.models import (
    AssetRiskResult,
    Holding,
    Loan,
    MarketData,
    Policy,
    PortfolioEvaluation,
    TriggerLevels,
)
from app.liquidation.plan import build_liquidation_plan, cure_amount_to_restore_coverage
from app.liquidation.recovery import estimate_stressed_recovery
from app.risk.adjustments import all_adjustments, risk_drivers_from_breakdown
from app.risk.math_utils import clamp, round_money, safe_div

MODEL_VERSION = "cre-v0.1.0"


class RiskEvaluationError(Exception):
    pass


class CollateralRiskEngine:
    def __init__(self, audit_logger: AuditLogger | None = None) -> None:
        self.audit_logger = audit_logger

    def evaluate(
        self,
        account_ref: str,
        holdings: list[Holding],
        loan: Loan,
        policy: Policy,
        market_data: Mapping[str, MarketData],
    ) -> PortfolioEvaluation:
        if not account_ref:
            raise RiskEvaluationError("account_ref is required")
        if not holdings:
            raise RiskEvaluationError("at least one holding is required")

        portfolio_market_value = 0.0
        for holding in holdings:
            market = market_data.get(holding.asset_id)
            if market is None:
                continue
            portfolio_market_value += max(0.0, holding.quantity) * max(0.0, market.last_price)

        if portfolio_market_value <= 0:
            raise RiskEvaluationError("portfolio_market_value is zero; market data may be missing or invalid")

        asset_results: list[AssetRiskResult] = []
        for holding in holdings:
            market = market_data.get(holding.asset_id)
            if market is None:
                asset_results.append(self._missing_market_result(holding))
                continue
            asset_results.append(
                self._evaluate_asset(
                    holding=holding,
                    market=market,
                    policy=policy,
                    portfolio_market_value=portfolio_market_value,
                )
            )

        risk_adjusted_collateral_value = sum(a.lendable_value for a in asset_results)
        stressed_liquidation_value = sum(a.stressed_liquidation_value for a in asset_results)
        approved_credit_limit = max(0.0, risk_adjusted_collateral_value)
        loan_balance = loan.balance
        available_credit = max(0.0, approved_credit_limit - loan_balance)
        recovery_coverage_ratio = None if loan_balance <= 0 else stressed_liquidation_value / loan_balance
        portfolio_risk_score = self._portfolio_risk_score(asset_results)
        trigger_levels = self._trigger_levels(stressed_liquidation_value, loan_balance, portfolio_risk_score)
        minimum_stressed_liquidation_value = loan_balance * trigger_levels.dynamic_warning_coverage
        margin_state = self._margin_state(
            approved_credit_limit=approved_credit_limit,
            loan_balance=loan_balance,
            recovery_coverage_ratio=recovery_coverage_ratio,
            triggers=trigger_levels,
        )
        target_cash = max(
            trigger_levels.required_cure_amount,
            max(0.0, loan_balance - approved_credit_limit),
        )
        liquidation_plan = build_liquidation_plan(
            account_ref=account_ref,
            asset_results=asset_results,
            margin_state=margin_state,
            target_cash_recovery=target_cash,
        )

        audit_payload = {
            "account_ref": account_ref,
            "model_version": MODEL_VERSION,
            "portfolio_market_value": round_money(portfolio_market_value),
            "risk_adjusted_collateral_value": round_money(risk_adjusted_collateral_value),
            "approved_credit_limit": round_money(approved_credit_limit),
            "stressed_liquidation_value": round_money(stressed_liquidation_value),
            "minimum_stressed_liquidation_value": round_money(minimum_stressed_liquidation_value),
            "loan_balance": round_money(loan_balance),
            "recovery_coverage_ratio": recovery_coverage_ratio,
            "portfolio_risk_score": portfolio_risk_score,
            "margin_state": margin_state.value,
            "trigger_levels": asdict(trigger_levels),
            "asset_results": [self._asset_result_audit(a) for a in asset_results],
            "liquidation_plan": asdict(liquidation_plan) if liquidation_plan else None,
            "policy": {
                "risk_appetite": policy.risk_appetite.value,
                "base_ltv": {k.value: v for k, v in policy.base_ltv.items()},
                "asset_ltv_caps": {k.value: v for k, v in policy.asset_ltv_caps.items()},
                "max_participation_rate": policy.max_participation_rate,
            },
        }
        audit_id = self.audit_logger.write(audit_payload) if self.audit_logger else "audit_disabled"

        return PortfolioEvaluation(
            account_ref=account_ref,
            portfolio_market_value=round_money(portfolio_market_value),
            risk_adjusted_collateral_value=round_money(risk_adjusted_collateral_value),
            approved_credit_limit=round_money(approved_credit_limit),
            stressed_liquidation_value=round_money(stressed_liquidation_value),
            minimum_stressed_liquidation_value=round_money(minimum_stressed_liquidation_value),
            loan_balance=round_money(loan_balance),
            available_credit=round_money(available_credit),
            recovery_coverage_ratio=None if recovery_coverage_ratio is None else round(recovery_coverage_ratio, 4),
            portfolio_risk_score=round(portfolio_risk_score, 4),
            margin_state=margin_state,
            trigger_levels=trigger_levels,
            asset_results=asset_results,
            liquidation_plan=liquidation_plan,
            audit_id=audit_id,
            model_version=MODEL_VERSION,
            created_at=datetime.now(timezone.utc),
        )

    def _evaluate_asset(
        self,
        holding: Holding,
        market: MarketData,
        policy: Policy,
        portfolio_market_value: float,
    ) -> AssetRiskResult:
        market_value = max(0.0, holding.quantity) * max(0.0, market.last_price)
        base_ltv = clamp(policy.base_ltv.get(holding.asset_type, 0.0), 0.0, 1.0)
        cap = clamp(policy.asset_ltv_caps.get(holding.asset_type, 1.0), 0.0, 1.0)

        breakdown, raw = all_adjustments(
            holding=holding,
            market=market,
            policy=policy,
            market_value=market_value,
            portfolio_market_value=portfolio_market_value,
        )
        eligible = not (
            market.halted and not policy.allow_lending_on_stale_or_halted_assets
        ) and market.data_quality_score >= policy.min_data_quality_score

        effective_ltv = clamp(base_ltv * breakdown.product, 0.0, cap)
        if not eligible:
            effective_ltv = 0.0
        lendable_value = market_value * effective_ltv
        recovery = estimate_stressed_recovery(holding, market, policy, raw, market_value)
        drivers = risk_drivers_from_breakdown(breakdown)
        notes: list[str] = []
        if not eligible:
            notes.append("asset_ineligible_due_to_halt_or_data_quality")
        if recovery.method.startswith("order_book"):
            notes.append("order_book_used_for_recovery_estimate")
        else:
            notes.append("proxy_liquidity_model_used_for_recovery_estimate")

        product_adj = breakdown.product
        risk_score = clamp(1.0 - product_adj, 0.0, 1.0)
        return AssetRiskResult(
            asset_id=holding.asset_id,
            asset_type=holding.asset_type,
            quantity=holding.quantity,
            market_value=round_money(market_value),
            base_ltv=round(base_ltv, 4),
            effective_ltv=round(effective_ltv, 4),
            lendable_value=round_money(lendable_value),
            stressed_liquidation_value=round_money(recovery.stressed_liquidation_value),
            estimated_slippage_rate=round(recovery.estimated_slippage_rate, 4),
            liquidation_horizon_days=round(raw.liquidation_horizon_days, 4),
            risk_score=round(risk_score, 4),
            risk_drivers=drivers,
            eligible=eligible,
            adjustments=breakdown,
            notes=notes,
        )

    def _missing_market_result(self, holding: Holding) -> AssetRiskResult:
        return AssetRiskResult(
            asset_id=holding.asset_id,
            asset_type=holding.asset_type,
            quantity=holding.quantity,
            market_value=0.0,
            base_ltv=0.0,
            effective_ltv=0.0,
            lendable_value=0.0,
            stressed_liquidation_value=0.0,
            estimated_slippage_rate=1.0,
            liquidation_horizon_days=90.0,
            risk_score=1.0,
            risk_drivers=["missing_market_data"],
            eligible=False,
            adjustments=self._zero_adjustments(),
            notes=["missing_market_data_zero_lendable_value"],
        )

    def _zero_adjustments(self):
        from app.core.models import RiskAdjustmentBreakdown

        return RiskAdjustmentBreakdown(
            volatility=0.0,
            liquidity=0.0,
            spread=0.0,
            concentration=0.0,
            stress=0.0,
            data_quality=0.0,
        )

    def _portfolio_risk_score(self, asset_results: list[AssetRiskResult]) -> float:
        total = sum(a.market_value for a in asset_results)
        if total <= 0:
            return 1.0
        weighted_risk = sum(a.risk_score * a.market_value for a in asset_results) / total
        concentration_penalty = sum((a.market_value / total) ** 2 for a in asset_results)
        ineligible_penalty = sum(a.market_value for a in asset_results if not a.eligible) / total
        return clamp(0.75 * weighted_risk + 0.20 * concentration_penalty + 0.35 * ineligible_penalty, 0.0, 1.0)

    def _trigger_levels(
        self,
        stressed_liquidation_value: float,
        loan_balance: float,
        portfolio_risk_score: float,
    ) -> TriggerLevels:
        dynamic_liquidation_coverage = clamp(1.01 + 0.34 * portfolio_risk_score, 1.02, 1.60)
        dynamic_margin_call_coverage = clamp(
            dynamic_liquidation_coverage + 0.04 + 0.12 * portfolio_risk_score,
            1.05,
            1.85,
        )
        dynamic_restriction_coverage = clamp(
            dynamic_margin_call_coverage + 0.04 + 0.08 * portfolio_risk_score,
            1.08,
            2.00,
        )
        dynamic_warning_coverage = clamp(
            dynamic_restriction_coverage + 0.05 + 0.10 * portfolio_risk_score,
            1.12,
            2.20,
        )
        cure_amount = cure_amount_to_restore_coverage(
            stressed_liquidation_value=stressed_liquidation_value,
            loan_balance=loan_balance,
            target_coverage=dynamic_margin_call_coverage,
        )
        return TriggerLevels(
            dynamic_liquidation_coverage=round(dynamic_liquidation_coverage, 4),
            dynamic_margin_call_coverage=round(dynamic_margin_call_coverage, 4),
            dynamic_restriction_coverage=round(dynamic_restriction_coverage, 4),
            dynamic_warning_coverage=round(dynamic_warning_coverage, 4),
            required_cure_amount=round_money(cure_amount),
        )

    def _margin_state(
        self,
        approved_credit_limit: float,
        loan_balance: float,
        recovery_coverage_ratio: float | None,
        triggers: TriggerLevels,
    ) -> MarginState:
        if loan_balance <= 0:
            return MarginState.SAFE
        if approved_credit_limit <= 0:
            return MarginState.LIQUIDATION
        if recovery_coverage_ratio is None:
            return MarginState.SAFE
        if recovery_coverage_ratio < triggers.dynamic_liquidation_coverage:
            return MarginState.LIQUIDATION
        if loan_balance > approved_credit_limit:
            return MarginState.MARGIN_CALL
        if recovery_coverage_ratio < triggers.dynamic_margin_call_coverage:
            return MarginState.MARGIN_CALL
        if recovery_coverage_ratio < triggers.dynamic_restriction_coverage:
            return MarginState.RESTRICT_NEW_BORROWING
        if recovery_coverage_ratio < triggers.dynamic_warning_coverage:
            return MarginState.WATCH
        return MarginState.SAFE

    def _asset_result_audit(self, asset: AssetRiskResult) -> dict:
        return {
            "asset_id": asset.asset_id,
            "asset_type": asset.asset_type.value,
            "market_value": asset.market_value,
            "base_ltv": asset.base_ltv,
            "effective_ltv": asset.effective_ltv,
            "lendable_value": asset.lendable_value,
            "stressed_liquidation_value": asset.stressed_liquidation_value,
            "estimated_slippage_rate": asset.estimated_slippage_rate,
            "liquidation_horizon_days": asset.liquidation_horizon_days,
            "risk_score": asset.risk_score,
            "risk_drivers": asset.risk_drivers,
            "eligible": asset.eligible,
            "notes": asset.notes,
        }
