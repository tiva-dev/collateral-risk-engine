from __future__ import annotations

from dataclasses import asdict
from typing import Mapping

from app.audit.logger import AuditLogger
from app.core.enums import MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy
from app.lifecycle.models import LifecycleDecision, OriginationResult, now_utc
from app.risk.math_utils import round_money

LIFECYCLE_MODEL_VERSION = "cre-v0.2.0"


class CreditLifecycleEngine:
    """Credit lifecycle orchestration that reuses the v0.1 risk evaluator.

    The lifecycle engine deliberately delegates collateral valuation, stressed
    recovery, dynamic margin states, cure math, and liquidation planning to
    ``CollateralRiskEngine`` so v0.2 extends rather than replaces v0.1 logic.
    """

    def __init__(self, risk_engine: CollateralRiskEngine, audit_logger: AuditLogger | None = None) -> None:
        self.risk_engine = risk_engine
        self.audit_logger = audit_logger

    def originate(
        self,
        account_ref: str,
        holdings: list[Holding],
        policy: Policy,
        market_data: Mapping[str, MarketData],
    ) -> OriginationResult:
        evaluation = self.risk_engine.evaluate(
            account_ref=account_ref,
            holdings=holdings,
            loan=Loan(principal=0.0),
            policy=policy,
            market_data=market_data,
        )
        audit_id = self._write_lifecycle_audit(
            event_type="origination",
            account_ref=account_ref,
            payload={
                "decision": "approved",
                "current_loan_balance": 0.0,
                "projected_loan_balance": None,
                "approved_credit_limit": evaluation.approved_credit_limit,
                "available_credit": evaluation.available_credit,
                "risk_evaluation_audit_id": evaluation.audit_id,
            },
        )
        return OriginationResult(
            decision="approved",
            reason="zero_balance_credit_line_originated_from_current_collateral_limit",
            current_loan_balance=0.0,
            projected_loan_balance=None,
            approved_credit_limit=evaluation.approved_credit_limit,
            available_credit=evaluation.available_credit,
            risk_adjusted_collateral_value=evaluation.risk_adjusted_collateral_value,
            stressed_liquidation_value=evaluation.stressed_liquidation_value,
            asset_results=evaluation.asset_results,
            margin_state=MarginState.SAFE,
            required_cure_amount=0.0,
            max_approved_draw_amount=None,
            liquidation_plan=None,
            evaluation=evaluation,
            audit_id=audit_id,
            created_at=now_utc(),
        )

    def check_draw(
        self,
        account_ref: str,
        current_loan: Loan,
        requested_draw_amount: float,
        holdings: list[Holding],
        policy: Policy,
        market_data: Mapping[str, MarketData],
    ) -> LifecycleDecision:
        current_balance = round_money(current_loan.balance)
        requested_draw_amount = max(0.0, requested_draw_amount)
        projected_balance = round_money(current_balance + requested_draw_amount)
        current_evaluation = self.risk_engine.evaluate(
            account_ref=account_ref,
            holdings=holdings,
            loan=current_loan,
            policy=policy,
            market_data=market_data,
        )
        safe_credit_limit = self._safe_credit_limit(current_evaluation)
        max_approved_draw = round_money(max(0.0, safe_credit_limit - current_balance))

        if requested_draw_amount <= max_approved_draw:
            decision = "approved"
            reason = "requested_draw_keeps_projected_balance_within_safe_credit_and_recovery_coverage_limits"
            max_approved_draw_amount = None
        elif max_approved_draw > 0:
            decision = "partially_approved"
            reason = "requested_draw_exceeds_safe_projected_balance_but_partial_draw_is_available"
            max_approved_draw_amount = max_approved_draw
        else:
            decision = "rejected"
            reason = "no_additional_draw_available_under_safe_credit_or_recovery_coverage_limits"
            max_approved_draw_amount = 0.0

        audit_id = self._write_lifecycle_audit(
            event_type="draw_check",
            account_ref=account_ref,
            payload={
                "decision": decision,
                "reason": reason,
                "current_loan_balance": current_balance,
                "projected_loan_balance": projected_balance,
                "requested_draw_amount": round_money(requested_draw_amount),
                "approved_credit_limit": current_evaluation.approved_credit_limit,
                "safe_credit_limit": safe_credit_limit,
                "max_approved_draw_amount": max_approved_draw_amount,
                "risk_evaluation_audit_id": current_evaluation.audit_id,
            },
        )
        return LifecycleDecision(
            decision=decision,
            reason=reason,
            current_loan_balance=current_balance,
            projected_loan_balance=projected_balance,
            approved_credit_limit=current_evaluation.approved_credit_limit,
            available_credit=current_evaluation.available_credit,
            margin_state=current_evaluation.margin_state,
            required_cure_amount=current_evaluation.trigger_levels.required_cure_amount,
            max_approved_draw_amount=max_approved_draw_amount,
            liquidation_plan=current_evaluation.liquidation_plan,
            evaluation=current_evaluation,
            audit_id=audit_id,
            created_at=now_utc(),
        )

    def monitor(
        self,
        account_ref: str,
        loan: Loan,
        holdings: list[Holding],
        policy: Policy,
        market_data: Mapping[str, MarketData],
    ) -> LifecycleDecision:
        evaluation = self.risk_engine.evaluate(
            account_ref=account_ref,
            holdings=holdings,
            loan=loan,
            policy=policy,
            market_data=market_data,
        )
        decision = evaluation.margin_state.value
        reason = self._monitor_reason(evaluation.margin_state)
        audit_id = self._write_lifecycle_audit(
            event_type="monitoring_run",
            account_ref=account_ref,
            payload={
                "decision": decision,
                "reason": reason,
                "current_loan_balance": evaluation.loan_balance,
                "approved_credit_limit": evaluation.approved_credit_limit,
                "available_credit": evaluation.available_credit,
                "margin_state": evaluation.margin_state.value,
                "required_cure_amount": evaluation.trigger_levels.required_cure_amount,
                "liquidation_plan": asdict(evaluation.liquidation_plan) if evaluation.liquidation_plan else None,
                "risk_evaluation_audit_id": evaluation.audit_id,
            },
        )
        return LifecycleDecision(
            decision=decision,
            reason=reason,
            current_loan_balance=evaluation.loan_balance,
            projected_loan_balance=None,
            approved_credit_limit=evaluation.approved_credit_limit,
            available_credit=evaluation.available_credit,
            margin_state=evaluation.margin_state,
            required_cure_amount=evaluation.trigger_levels.required_cure_amount,
            max_approved_draw_amount=None,
            liquidation_plan=evaluation.liquidation_plan,
            evaluation=evaluation,
            audit_id=audit_id,
            created_at=now_utc(),
        )

    def _safe_credit_limit(self, evaluation) -> float:
        recovery_limited_balance = evaluation.stressed_liquidation_value / max(
            evaluation.trigger_levels.dynamic_warning_coverage,
            1e-9,
        )
        return round_money(max(0.0, min(evaluation.approved_credit_limit, recovery_limited_balance)))

    def _monitor_reason(self, margin_state: MarginState) -> str:
        return {
            MarginState.SAFE: "loan_is_within_dynamic_credit_and_recovery_coverage_limits",
            MarginState.WATCH: "loan_is_above_dynamic_warning_threshold",
            MarginState.RESTRICT_NEW_BORROWING: "loan_requires_new_borrowing_restriction_until_coverage_improves",
            MarginState.MARGIN_CALL: "loan_requires_margin_cure_or_risk_reducing_liquidation",
            MarginState.LIQUIDATION: "loan_breaches_dynamic_liquidation_coverage_threshold",
        }[margin_state]

    def _write_lifecycle_audit(self, event_type: str, account_ref: str, payload: dict) -> str:
        if not self.audit_logger:
            return "audit_disabled"
        audit_payload = {
            "event_type": event_type,
            "account_ref": account_ref,
            "lifecycle_model_version": LIFECYCLE_MODEL_VERSION,
            **payload,
        }
        return self.audit_logger.write(audit_payload)
