from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Mapping

from app.audit.logger import AuditLogger
from app.core.enums import AssetType, MarginState, PortfolioActionType, RiskDecision, TransferDirection
from app.core.models import (
    AssetRiskResult,
    Holding,
    Loan,
    MarketData,
    Policy,
    PortfolioAction,
    PortfolioEvaluation,
    PreTradeRiskCheckResult,
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
        requested_draw_amount: float = 0.0,
    ) -> PortfolioEvaluation:
        if not account_ref:
            raise RiskEvaluationError("account_ref is required")
        if not holdings:
            raise RiskEvaluationError("at least one holding is required")
        if requested_draw_amount < 0:
            raise RiskEvaluationError("requested_draw_amount cannot be negative")

        outstanding_balance = loan.balance
        requested_draw_amount = max(0.0, requested_draw_amount)
        projected_loan_balance = outstanding_balance + requested_draw_amount

        portfolio_market_value = 0.0
        for holding in holdings:
            market = market_data.get(holding.asset_id)
            if market is None:
                continue
            portfolio_market_value += max(0.0, holding.quantity) * max(0.0, market.last_price)

        if portfolio_market_value <= 0 and projected_loan_balance <= 0:
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
        available_credit = max(0.0, approved_credit_limit - outstanding_balance)
        projected_available_credit = max(0.0, approved_credit_limit - projected_loan_balance)
        recovery_coverage_ratio = None if projected_loan_balance <= 0 else stressed_liquidation_value / projected_loan_balance
        portfolio_risk_score = self._portfolio_risk_score(asset_results)
        trigger_levels = self._trigger_levels(stressed_liquidation_value, projected_loan_balance, portfolio_risk_score)
        dynamic_safety_requirement = max(
            0.0,
            projected_loan_balance * (trigger_levels.dynamic_liquidation_coverage - 1.0),
        )
        minimum_stressed_liquidation_value = projected_loan_balance + dynamic_safety_requirement
        margin_state = self._margin_state(
            approved_credit_limit=approved_credit_limit,
            loan_balance=projected_loan_balance,
            recovery_coverage_ratio=recovery_coverage_ratio,
            triggers=trigger_levels,
        )
        target_cash = max(
            trigger_levels.required_cure_amount,
            max(0.0, projected_loan_balance - approved_credit_limit),
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
            "outstanding_balance": round_money(outstanding_balance),
            "available_credit": round_money(available_credit),
            "requested_draw_amount": round_money(requested_draw_amount),
            "projected_loan_balance": round_money(projected_loan_balance),
            "projected_available_credit": round_money(projected_available_credit),
            "recovery_coverage_ratio": recovery_coverage_ratio,
            "dynamic_safety_requirement": round_money(dynamic_safety_requirement),
            "minimum_stressed_liquidation_value": round_money(minimum_stressed_liquidation_value),
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
            loan_balance=round_money(projected_loan_balance),
            outstanding_balance=round_money(outstanding_balance),
            available_credit=round_money(available_credit),
            requested_draw_amount=round_money(requested_draw_amount),
            projected_loan_balance=round_money(projected_loan_balance),
            projected_available_credit=round_money(projected_available_credit),
            recovery_coverage_ratio=None if recovery_coverage_ratio is None else round(recovery_coverage_ratio, 4),
            dynamic_safety_requirement=round_money(dynamic_safety_requirement),
            minimum_stressed_liquidation_value=round_money(minimum_stressed_liquidation_value),
            portfolio_risk_score=round(portfolio_risk_score, 4),
            margin_state=margin_state,
            trigger_levels=trigger_levels,
            asset_results=asset_results,
            liquidation_plan=liquidation_plan,
            audit_id=audit_id,
            model_version=MODEL_VERSION,
            created_at=datetime.now(timezone.utc),
        )

    def pre_trade_check(
        self,
        account_ref: str,
        holdings: list[Holding],
        loan: Loan,
        policy: Policy,
        market_data: Mapping[str, MarketData],
        actions: list[PortfolioAction],
    ) -> PreTradeRiskCheckResult:
        if not actions:
            raise RiskEvaluationError("at least one portfolio action is required")

        projected_holdings, requested_draw_amount, repayment_amount = self._project_actions(
            holdings,
            market_data,
            actions,
        )
        outstanding_balance = loan.balance
        post_repayment_outstanding = max(0.0, outstanding_balance - repayment_amount)
        projected_evaluation = self.evaluate(
            account_ref=account_ref,
            holdings=projected_holdings,
            loan=Loan(principal=post_repayment_outstanding, currency=loan.currency),
            policy=policy,
            market_data=market_data,
            requested_draw_amount=requested_draw_amount,
        )
        decision, reason, required_repayment = self._pre_trade_decision(
            projected_evaluation,
            requested_draw_amount=requested_draw_amount,
        )

        return PreTradeRiskCheckResult(
            account_ref=account_ref,
            decision=decision,
            approved=decision == RiskDecision.APPROVE,
            reason=reason,
            outstanding_balance=round_money(outstanding_balance),
            available_credit=projected_evaluation.available_credit,
            requested_draw_amount=projected_evaluation.requested_draw_amount,
            projected_loan_balance=projected_evaluation.projected_loan_balance,
            projected_available_credit=projected_evaluation.projected_available_credit,
            projected_stressed_liquidation_value=projected_evaluation.stressed_liquidation_value,
            dynamic_safety_requirement=projected_evaluation.dynamic_safety_requirement,
            minimum_stressed_liquidation_value=projected_evaluation.minimum_stressed_liquidation_value,
            required_repayment_amount=required_repayment,
            reduced_available_credit=projected_evaluation.available_credit,
            projected_margin_state=projected_evaluation.margin_state,
            projected_holdings=projected_holdings,
            projected_evaluation=projected_evaluation,
            created_at=datetime.now(timezone.utc),
        )

    def _project_actions(
        self,
        holdings: list[Holding],
        market_data: Mapping[str, MarketData],
        actions: list[PortfolioAction],
    ) -> tuple[list[Holding], float, float]:
        projected = {holding.asset_id: holding for holding in holdings}
        requested_draw_amount = 0.0
        repayment_amount = 0.0

        for action in actions:
            if action.action_type == PortfolioActionType.CREDIT_DRAW:
                requested_draw_amount += self._cash_amount(action, "credit draw")
                continue
            if action.action_type == PortfolioActionType.REPAYMENT:
                repayment_amount += self._cash_amount(action, "repayment")
                continue

            asset_id = action.asset_id
            if not asset_id:
                raise RiskEvaluationError(f"{action.action_type.value} action requires asset_id")

            quantity = self._asset_quantity(action, market_data)
            direction = action.direction
            if action.action_type in {PortfolioActionType.SELL, PortfolioActionType.WITHDRAWAL}:
                delta = -quantity
            elif action.action_type == PortfolioActionType.BUY:
                delta = quantity
            elif action.action_type == PortfolioActionType.TRANSFER:
                delta = quantity if direction == TransferDirection.IN else -quantity
            else:
                raise RiskEvaluationError(f"unsupported action_type: {action.action_type.value}")

            current = projected.get(asset_id)
            if current is None:
                if delta < 0:
                    raise RiskEvaluationError(f"cannot remove {asset_id}; no current holding exists")
                if action.asset_type is None:
                    raise RiskEvaluationError(f"{action.action_type.value} action for new asset requires asset_type")
                projected[asset_id] = Holding(
                    asset_id=asset_id,
                    asset_type=action.asset_type,
                    quantity=delta,
                )
                continue

            new_quantity = current.quantity + delta
            if new_quantity < -1e-9:
                raise RiskEvaluationError(f"{action.action_type.value} exceeds available {asset_id} quantity")
            projected[asset_id] = replace(current, quantity=max(0.0, new_quantity))

        return list(projected.values()), requested_draw_amount, repayment_amount

    def _asset_quantity(
        self,
        action: PortfolioAction,
        market_data: Mapping[str, MarketData],
    ) -> float:
        if action.quantity > 0:
            return action.quantity
        if action.amount <= 0:
            raise RiskEvaluationError(f"{action.action_type.value} action requires positive quantity or amount")

        market = market_data.get(action.asset_id or "")
        if market is None or market.last_price <= 0:
            raise RiskEvaluationError(
                f"{action.action_type.value} action with amount requires positive market price"
            )
        return action.amount / market.last_price

    def _cash_amount(self, action: PortfolioAction, label: str) -> float:
        amount = action.amount if action.amount > 0 else action.quantity
        if amount <= 0:
            raise RiskEvaluationError(f"{label} action requires positive amount")
        return amount

    def _pre_trade_decision(
        self,
        projected_evaluation: PortfolioEvaluation,
        requested_draw_amount: float,
    ) -> tuple[RiskDecision, str, float]:
        projected_balance = projected_evaluation.projected_loan_balance
        if projected_balance <= 0:
            return RiskDecision.APPROVE, "projected loan balance is zero after the action", 0.0

        required_repayment = self._required_repayment_to_restore_safety(projected_evaluation)
        if projected_evaluation.stressed_liquidation_value < projected_balance:
            return (
                RiskDecision.LIQUIDATION,
                "projected stressed liquidation value no longer covers projected loan balance",
                required_repayment,
            )
        if projected_evaluation.stressed_liquidation_value < projected_evaluation.minimum_stressed_liquidation_value:
            return (
                RiskDecision.REQUIRE_REPAYMENT,
                "projected stressed liquidation value does not cover projected loan balance plus dynamic safety requirement",
                required_repayment,
            )
        if requested_draw_amount > projected_evaluation.available_credit:
            return (
                RiskDecision.REDUCE_AVAILABLE_CREDIT,
                "requested draw exceeds projected available credit",
                0.0,
            )
        if projected_balance > projected_evaluation.approved_credit_limit:
            return (
                RiskDecision.MARGIN_CALL,
                "projected loan balance exceeds approved credit limit",
                required_repayment,
            )
        if projected_evaluation.margin_state == MarginState.MARGIN_CALL:
            return (
                RiskDecision.MARGIN_CALL,
                "projected portfolio is in margin call",
                required_repayment,
            )
        if projected_evaluation.margin_state == MarginState.RESTRICT_NEW_BORROWING:
            return (
                RiskDecision.REDUCE_AVAILABLE_CREDIT,
                "projected portfolio requires new borrowing restrictions",
                0.0,
            )
        if projected_evaluation.margin_state == MarginState.LIQUIDATION:
            return (
                RiskDecision.LIQUIDATION,
                "projected portfolio is in liquidation",
                required_repayment,
            )
        return RiskDecision.APPROVE, "projected portfolio remains inside dynamic risk limits", 0.0

    def _required_repayment_to_restore_safety(self, evaluation: PortfolioEvaluation) -> float:
        projected_balance = evaluation.projected_loan_balance
        if projected_balance <= 0:
            return 0.0
        coverage = max(evaluation.trigger_levels.dynamic_liquidation_coverage, 1e-9)
        max_safe_balance = evaluation.stressed_liquidation_value / coverage
        return round_money(max(0.0, projected_balance - max_safe_balance))

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
