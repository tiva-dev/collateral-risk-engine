from __future__ import annotations

from dataclasses import asdict
from typing import Mapping

from app.audit.logger import AuditLogger
from app.core.enums import LifecycleDecisionValue, MarginState, RiskDecision
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy
from app.lifecycle.models import LifecycleDecision, OriginationResult, PreTradeCheckResult, now_utc
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
        aggregated_holdings = aggregate_holdings(holdings)
        zero_loan = Loan(principal=0.0)
        evaluation = self.risk_engine.evaluate(
            account_ref=account_ref,
            holdings=aggregated_holdings,
            loan=zero_loan,
            policy=policy,
            market_data=market_data,
        )
        audit_id = self._write_lifecycle_audit(
            event_type="origination",
            account_ref=account_ref,
            payload={
                "decision": LifecycleDecisionValue.APPROVED.value,
                "current_outstanding_balance": 0.0,
                "current_available_credit": evaluation.available_credit,
                "projected_outstanding_balance": 0.0,
                "projected_available_credit": evaluation.available_credit,
                "projected_margin_state": MarginState.SAFE.value,
                "approved_credit_limit": evaluation.approved_credit_limit,
                "minimum_stressed_liquidation_value": evaluation.minimum_stressed_liquidation_value,
                "risk_evaluation_audit_id": evaluation.audit_id,
            },
        )
        return OriginationResult(
            decision=LifecycleDecisionValue.APPROVED,
            reason="zero outstanding balance credit line originated from current collateral limit",
            current_outstanding_balance=0.0,
            current_available_credit=evaluation.available_credit,
            projected_outstanding_balance=0.0,
            projected_available_credit=evaluation.available_credit,
            projected_margin_state=MarginState.SAFE,
            approved_credit_limit=evaluation.approved_credit_limit,
            risk_adjusted_collateral_value=evaluation.risk_adjusted_collateral_value,
            stressed_liquidation_value=evaluation.stressed_liquidation_value,
            minimum_stressed_liquidation_value=evaluation.minimum_stressed_liquidation_value,
            asset_results=evaluation.asset_results,
            margin_state=MarginState.SAFE,
            required_cure_amount=0.0,
            max_approved_draw_amount=None,
            current_loan=zero_loan,
            projected_loan=zero_loan,
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
        requested_repayment_amount: float = 0.0,
    ) -> LifecycleDecision:
        aggregated_holdings = aggregate_holdings(holdings)
        requested_draw_amount = round_money(max(0.0, requested_draw_amount))
        requested_repayment_amount = round_money(max(0.0, requested_repayment_amount))
        current_outstanding_balance = round_money(current_loan.balance)

        current_evaluation = self.risk_engine.evaluate(
            account_ref=account_ref,
            holdings=aggregated_holdings,
            loan=current_loan,
            policy=policy,
            market_data=market_data,
        )
        loan_after_repayment = apply_repayment(current_loan, requested_repayment_amount)
        projected_loan = Loan(
            principal=round_money(loan_after_repayment.principal + requested_draw_amount),
            accrued_interest=loan_after_repayment.accrued_interest,
            fees=loan_after_repayment.fees,
            currency=current_loan.currency,
        )
        projected_evaluation = self.risk_engine.evaluate(
            account_ref=account_ref,
            holdings=aggregated_holdings,
            loan=projected_loan,
            policy=policy,
            market_data=market_data,
        )

        safe_credit_limit = self._safe_credit_limit(current_evaluation)
        outstanding_after_repayment = round_money(loan_after_repayment.balance)
        max_approved_draw = round_money(max(0.0, safe_credit_limit - outstanding_after_repayment))
        projected_outstanding_balance = projected_evaluation.loan_balance
        projected_available_credit = projected_evaluation.available_credit

        if requested_draw_amount <= max_approved_draw and projected_evaluation.margin_state == MarginState.SAFE:
            decision = LifecycleDecisionValue.APPROVED
            reason = "requested draw keeps projected outstanding balance within dynamic safety requirement"
            max_approved_draw_amount = None
        elif max_approved_draw > 0:
            decision = LifecycleDecisionValue.PARTIALLY_APPROVED
            reason = "requested draw exceeds projected available credit"
            max_approved_draw_amount = max_approved_draw
        else:
            decision = LifecycleDecisionValue.REJECTED
            reason = "requested draw exceeds projected available credit"
            max_approved_draw_amount = 0.0

        audit_id = self._write_lifecycle_audit(
            event_type="draw_check",
            account_ref=account_ref,
            payload={
                "decision": decision.value,
                "reason": reason,
                "current_outstanding_balance": current_outstanding_balance,
                "current_available_credit": current_evaluation.available_credit,
                "projected_outstanding_balance": projected_outstanding_balance,
                "projected_available_credit": projected_available_credit,
                "projected_margin_state": projected_evaluation.margin_state.value,
                "requested_draw_amount": requested_draw_amount,
                "requested_repayment_amount": requested_repayment_amount,
                "current_loan": asdict(current_loan),
                "projected_loan": asdict(projected_loan),
                "approved_credit_limit": current_evaluation.approved_credit_limit,
                "safe_credit_limit": safe_credit_limit,
                "minimum_stressed_liquidation_value": projected_evaluation.minimum_stressed_liquidation_value,
                "max_approved_draw_amount": max_approved_draw_amount,
                "risk_evaluation_audit_id": current_evaluation.audit_id,
                "projected_risk_evaluation_audit_id": projected_evaluation.audit_id,
            },
        )
        return LifecycleDecision(
            decision=decision,
            reason=reason,
            current_outstanding_balance=current_outstanding_balance,
            current_available_credit=current_evaluation.available_credit,
            projected_outstanding_balance=projected_outstanding_balance,
            projected_available_credit=projected_available_credit,
            projected_margin_state=projected_evaluation.margin_state,
            approved_credit_limit=current_evaluation.approved_credit_limit,
            margin_state=current_evaluation.margin_state,
            required_cure_amount=projected_evaluation.trigger_levels.required_cure_amount,
            minimum_stressed_liquidation_value=projected_evaluation.minimum_stressed_liquidation_value,
            max_approved_draw_amount=max_approved_draw_amount,
            current_loan=current_loan,
            projected_loan=projected_loan,
            liquidation_plan=projected_evaluation.liquidation_plan,
            evaluation=projected_evaluation,
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
        aggregated_holdings = aggregate_holdings(holdings)
        evaluation = self.risk_engine.evaluate(
            account_ref=account_ref,
            holdings=aggregated_holdings,
            loan=loan,
            policy=policy,
            market_data=market_data,
        )
        decision = LifecycleDecisionValue(evaluation.margin_state.value)
        reason = self._monitor_reason(evaluation.margin_state)
        audit_id = self._write_lifecycle_audit(
            event_type="monitoring_run",
            account_ref=account_ref,
            payload={
                "decision": decision.value,
                "reason": reason,
                "current_outstanding_balance": evaluation.loan_balance,
                "current_available_credit": evaluation.available_credit,
                "projected_outstanding_balance": evaluation.loan_balance,
                "projected_available_credit": evaluation.available_credit,
                "projected_margin_state": evaluation.margin_state.value,
                "approved_credit_limit": evaluation.approved_credit_limit,
                "margin_state": evaluation.margin_state.value,
                "required_cure_amount": evaluation.trigger_levels.required_cure_amount,
                "minimum_stressed_liquidation_value": evaluation.minimum_stressed_liquidation_value,
                "liquidation_plan": asdict(evaluation.liquidation_plan) if evaluation.liquidation_plan else None,
                "risk_evaluation_audit_id": evaluation.audit_id,
            },
        )
        return LifecycleDecision(
            decision=decision,
            reason=reason,
            current_outstanding_balance=evaluation.loan_balance,
            current_available_credit=evaluation.available_credit,
            projected_outstanding_balance=evaluation.loan_balance,
            projected_available_credit=evaluation.available_credit,
            projected_margin_state=evaluation.margin_state,
            approved_credit_limit=evaluation.approved_credit_limit,
            margin_state=evaluation.margin_state,
            required_cure_amount=evaluation.trigger_levels.required_cure_amount,
            minimum_stressed_liquidation_value=evaluation.minimum_stressed_liquidation_value,
            max_approved_draw_amount=None,
            current_loan=loan,
            projected_loan=loan,
            liquidation_plan=evaluation.liquidation_plan,
            evaluation=evaluation,
            audit_id=audit_id,
            created_at=now_utc(),
        )

    def pre_trade_check(
        self,
        account_ref: str,
        loan: Loan,
        holdings: list[Holding],
        proposed_holding_changes: list[Holding],
        policy: Policy,
        market_data: Mapping[str, MarketData],
        requested_draw_amount: float = 0.0,
        requested_repayment_amount: float = 0.0,
    ) -> PreTradeCheckResult:
        current_holdings = aggregate_holdings(holdings)
        requested_draw_amount = round_money(max(0.0, requested_draw_amount))
        requested_repayment_amount = round_money(max(0.0, requested_repayment_amount))
        current_evaluation = self.risk_engine.evaluate(
            account_ref=account_ref,
            holdings=current_holdings,
            loan=loan,
            policy=policy,
            market_data=market_data,
        )
        projected_holdings, invalid_reason = project_holdings(current_holdings, proposed_holding_changes)
        loan_after_repayment = apply_repayment(loan, requested_repayment_amount)
        projected_loan = Loan(
            principal=round_money(loan_after_repayment.principal + requested_draw_amount),
            accrued_interest=loan_after_repayment.accrued_interest,
            fees=loan_after_repayment.fees,
            currency=loan.currency,
        )

        if invalid_reason:
            decision = RiskDecision.REJECT
            reason = invalid_reason
            projected_evaluation = current_evaluation
            projected_outstanding_balance = None
            projected_available_credit = None
            projected_margin_state = None
            reduced_available_credit = None
        else:
            projected_evaluation = self.risk_engine.evaluate(
                account_ref=account_ref,
                holdings=projected_holdings,
                loan=projected_loan,
                policy=policy,
                market_data=market_data,
            )
            projected_outstanding_balance = projected_evaluation.loan_balance
            projected_available_credit = projected_evaluation.available_credit
            projected_margin_state = projected_evaluation.margin_state
            safe_credit_limit = self._safe_credit_limit(projected_evaluation)
            if projected_evaluation.margin_state in {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}:
                decision = RiskDecision.REJECT
                reason = "projected margin state is unsafe; action must not proceed"
                reduced_available_credit = None
            elif projected_evaluation.loan_balance > safe_credit_limit:
                decision = RiskDecision.REJECT
                reason = "projected outstanding balance exceeds dynamic safety requirement"
                reduced_available_credit = None
            elif projected_evaluation.available_credit < current_evaluation.available_credit:
                decision = RiskDecision.REDUCE_AVAILABLE_CREDIT
                reason = "projected available credit is reduced but remains within dynamic safety requirement"
                reduced_available_credit = projected_evaluation.available_credit
            else:
                decision = RiskDecision.APPROVE
                reason = "projected state remains within dynamic safety requirement"
                reduced_available_credit = None

        audit_id = self._write_lifecycle_audit(
            event_type="pre_trade_check",
            account_ref=account_ref,
            payload={
                "decision": decision.value,
                "reason": reason,
                "current_outstanding_balance": current_evaluation.loan_balance,
                "current_available_credit": current_evaluation.available_credit,
                "projected_outstanding_balance": projected_outstanding_balance,
                "projected_available_credit": projected_available_credit,
                "projected_margin_state": projected_margin_state.value if projected_margin_state else None,
                "reduced_available_credit": reduced_available_credit,
                "requested_draw_amount": requested_draw_amount,
                "requested_repayment_amount": requested_repayment_amount,
                "approved_credit_limit": current_evaluation.approved_credit_limit,
                "minimum_stressed_liquidation_value": projected_evaluation.minimum_stressed_liquidation_value,
                "risk_evaluation_audit_id": current_evaluation.audit_id,
                "projected_risk_evaluation_audit_id": projected_evaluation.audit_id,
            },
        )
        return PreTradeCheckResult(
            decision=decision,
            reason=reason,
            current_outstanding_balance=current_evaluation.loan_balance,
            current_available_credit=current_evaluation.available_credit,
            projected_outstanding_balance=projected_outstanding_balance,
            projected_available_credit=projected_available_credit,
            projected_margin_state=projected_margin_state,
            reduced_available_credit=reduced_available_credit,
            approved_credit_limit=current_evaluation.approved_credit_limit,
            margin_state=current_evaluation.margin_state,
            required_cure_amount=projected_evaluation.trigger_levels.required_cure_amount,
            minimum_stressed_liquidation_value=projected_evaluation.minimum_stressed_liquidation_value,
            current_loan=loan,
            projected_loan=projected_loan,
            current_holdings=current_holdings,
            projected_holdings=projected_holdings,
            liquidation_plan=projected_evaluation.liquidation_plan,
            evaluation=projected_evaluation,
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
            MarginState.SAFE: "loan is within dynamic credit and recovery coverage limits",
            MarginState.WATCH: "outstanding balance is above dynamic warning threshold",
            MarginState.RESTRICT_NEW_BORROWING: "outstanding balance requires new borrowing restriction until coverage improves",
            MarginState.MARGIN_CALL: "outstanding balance requires margin cure or risk reducing liquidation",
            MarginState.LIQUIDATION: "outstanding balance breaches dynamic liquidation coverage threshold",
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


def apply_repayment(loan: Loan, repayment_amount: float) -> Loan:
    remaining_repayment = max(0.0, repayment_amount)

    fees_payment = min(max(0.0, loan.fees), remaining_repayment)
    remaining_repayment -= fees_payment
    fees = round_money(max(0.0, loan.fees - fees_payment))

    interest_payment = min(max(0.0, loan.accrued_interest), remaining_repayment)
    remaining_repayment -= interest_payment
    accrued_interest = round_money(max(0.0, loan.accrued_interest - interest_payment))

    principal_payment = min(max(0.0, loan.principal), remaining_repayment)
    principal = round_money(max(0.0, loan.principal - principal_payment))

    return Loan(
        principal=principal,
        accrued_interest=accrued_interest,
        fees=fees,
        currency=loan.currency,
    )


def aggregate_holdings(holdings: list[Holding]) -> list[Holding]:
    aggregated: dict[tuple[str, object, str], float] = {}
    order: list[tuple[str, object, str]] = []
    for holding in holdings:
        key = (holding.asset_id, holding.asset_type, holding.currency)
        if key not in aggregated:
            aggregated[key] = 0.0
            order.append(key)
        aggregated[key] += holding.quantity
    return [
        Holding(asset_id=asset_id, asset_type=asset_type, quantity=quantity, currency=currency)
        for asset_id, asset_type, currency in order
        for quantity in [round(aggregated[(asset_id, asset_type, currency)], 12)]
    ]


def project_holdings(
    current_holdings: list[Holding],
    proposed_holding_changes: list[Holding],
) -> tuple[list[Holding], str | None]:
    quantities: dict[tuple[str, object, str], float] = {}
    order: list[tuple[str, object, str]] = []
    identities: dict[tuple[str, object, str], tuple[str, object, str]] = {}

    for holding in [*current_holdings, *proposed_holding_changes]:
        key = (holding.asset_id, holding.asset_type, holding.currency)
        if key not in quantities:
            quantities[key] = 0.0
            identities[key] = key
            order.append(key)
        quantities[key] += holding.quantity

    projected: list[Holding] = []
    for key in order:
        quantity = round(quantities[key], 12)
        asset_id, asset_type, currency = identities[key]
        if quantity < 0:
            return [], "projected holding quantity would be negative; action must not proceed"
        if quantity > 0:
            projected.append(
                Holding(
                    asset_id=asset_id,
                    asset_type=asset_type,
                    quantity=quantity,
                    currency=currency,
                )
            )

    if not projected:
        return [], "projected holdings are empty; action must not proceed"
    return projected, None
