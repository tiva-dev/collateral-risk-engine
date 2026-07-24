from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace

from app.audit.logger import AuditLogger
from app.core.enums import (
    AssetType,
    LifecycleDecisionValue,
    MarginState,
    PortfolioActionType,
    RiskDecision,
    TransferDirection,
)
from app.core.evaluator import CollateralRiskEngine, RiskEvaluationError
from app.core.models import (
    AccountState,
    Holding,
    Loan,
    MarketData,
    Policy,
    PortfolioActionCheck,
    PortfolioActionCheckResult,
)
from app.lifecycle.models import (
    LifecycleDecision,
    OriginationResult,
    PreTradeCheckResult,
    now_utc,
)
from app.risk.math_utils import round_money
from app.version import LIFECYCLE_MODEL_VERSION as APP_LIFECYCLE_MODEL_VERSION
from app.version import (
    PORTFOLIO_ACTION_MODEL_VERSION as APP_PORTFOLIO_ACTION_MODEL_VERSION,
)

LIFECYCLE_MODEL_VERSION = APP_LIFECYCLE_MODEL_VERSION
PORTFOLIO_ACTION_MODEL_VERSION = APP_PORTFOLIO_ACTION_MODEL_VERSION
PLEDGED_CASH_ASSET_ID_PREFIX = "PLEDGED_CASH"


def pledged_cash_asset_id(currency: str) -> str:
    normalized_currency = (currency or "USD").upper()
    return f"{PLEDGED_CASH_ASSET_ID_PREFIX}_{normalized_currency}"


def holding_identity(holding: Holding) -> tuple[str, str, str, AssetType, str]:
    return holding.stable_identity


class CreditLifecycleEngine:
    """Credit lifecycle orchestration that reuses the core risk evaluator.

    The lifecycle engine deliberately delegates collateral valuation, stressed
    recovery, dynamic margin states, cure math, and liquidation planning to
    ``CollateralRiskEngine`` so lifecycle controls extend rather than replace core risk logic.
    """

    def __init__(
        self, risk_engine: CollateralRiskEngine, audit_logger: AuditLogger | None = None
    ) -> None:
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
        safe_credit_limit = self._safe_credit_limit(evaluation)
        safe_available_credit = round_money(max(0.0, safe_credit_limit))
        if safe_available_credit <= 0:
            raise RiskEvaluationError(
                "origination rejected: approved available credit is zero"
            )
        audit_id = self._write_lifecycle_audit(
            event_type="origination",
            account_ref=account_ref,
            payload={
                "decision": LifecycleDecisionValue.APPROVED.value,
                "current_outstanding_balance": 0.0,
                "current_available_credit": safe_available_credit,
                "projected_outstanding_balance": 0.0,
                "projected_available_credit": safe_available_credit,
                "projected_margin_state": MarginState.SAFE.value,
                "approved_credit_limit": evaluation.approved_credit_limit,
                "safe_credit_limit": safe_credit_limit,
                "minimum_stressed_liquidation_value": evaluation.minimum_stressed_liquidation_value,
                "risk_evaluation_audit_id": evaluation.audit_id,
            },
        )
        return OriginationResult(
            decision=LifecycleDecisionValue.APPROVED,
            reason="zero outstanding balance credit line originated from current collateral limit",
            current_outstanding_balance=0.0,
            current_available_credit=safe_available_credit,
            projected_outstanding_balance=0.0,
            projected_available_credit=safe_available_credit,
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
            principal=round_money(
                loan_after_repayment.principal + requested_draw_amount
            ),
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
        projected_safe_credit_limit = self._safe_credit_limit(projected_evaluation)
        outstanding_after_repayment = round_money(loan_after_repayment.balance)
        max_approved_draw = round_money(
            max(0.0, safe_credit_limit - outstanding_after_repayment)
        )
        projected_outstanding_balance = projected_evaluation.loan_balance
        current_available_credit = round_money(
            max(0.0, safe_credit_limit - current_outstanding_balance)
        )
        projected_available_credit = round_money(
            max(0.0, projected_safe_credit_limit - projected_outstanding_balance)
        )

        if (
            requested_draw_amount <= max_approved_draw
            and projected_evaluation.margin_state == MarginState.SAFE
        ):
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
                "current_available_credit": current_available_credit,
                "projected_outstanding_balance": projected_outstanding_balance,
                "projected_available_credit": projected_available_credit,
                "projected_margin_state": projected_evaluation.margin_state.value,
                "requested_draw_amount": requested_draw_amount,
                "requested_repayment_amount": requested_repayment_amount,
                "current_loan": asdict(current_loan),
                "projected_loan": asdict(projected_loan),
                "approved_credit_limit": current_evaluation.approved_credit_limit,
                "safe_credit_limit": safe_credit_limit,
                "projected_safe_credit_limit": projected_safe_credit_limit,
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
            current_available_credit=current_available_credit,
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

    def check_portfolio_action(
        self,
        account_state: AccountState,
        proposed_action: PortfolioActionCheck,
        policy: Policy,
        market_data: Mapping[str, MarketData],
    ) -> PortfolioActionCheckResult:
        if account_state.account_ref == "":
            raise ValueError("account_ref is required")

        normalized_market_data = self._market_data_with_pledged_cash(
            market_data, account_state.loan.currency
        )
        pre_holdings = self._holdings_with_pledged_cash(
            aggregate_holdings(account_state.holdings),
            account_state.pledged_cash_balance,
            account_state.loan.currency,
        )
        pre_evaluation = self.risk_engine.evaluate(
            account_ref=account_state.account_ref,
            holdings=pre_holdings,
            loan=account_state.loan,
            policy=policy,
            market_data=normalized_market_data,
        )

        if proposed_action.action_type == PortfolioActionType.DRAW:
            draw_amount = self._cash_amount(proposed_action, "draw")
            draw_result = self.check_draw(
                account_ref=account_state.account_ref,
                current_loan=account_state.loan,
                requested_draw_amount=draw_amount,
                requested_repayment_amount=0.0,
                holdings=pre_holdings,
                policy=policy,
                market_data=normalized_market_data,
            )
            decision = self._risk_decision_from_lifecycle(draw_result.decision)
            reason = f"draw action delegated to credit draw check: {draw_result.reason}"
            projected_evaluation = draw_result.evaluation
            projected_state = AccountState(
                account_ref=account_state.account_ref,
                holdings=self._strip_pledged_cash_holding(pre_holdings),
                pledged_cash_balance=round_money(account_state.pledged_cash_balance),
                loan=draw_result.projected_loan or account_state.loan,
                approved_credit_limit=projected_evaluation.approved_credit_limit,
                available_credit=draw_result.projected_available_credit or 0.0,
                last_margin_state=projected_evaluation.margin_state,
                last_evaluation_time=projected_evaluation.created_at,
            )
            required_repayment = draw_result.required_cure_amount
        else:
            try:
                projected_holdings, projected_cash, projected_loan = (
                    self._project_portfolio_action(
                        account_ref=account_state.account_ref,
                        holdings=aggregate_holdings(account_state.holdings),
                        pledged_cash_balance=account_state.pledged_cash_balance,
                        loan=account_state.loan,
                        action=proposed_action,
                        market_data=normalized_market_data,
                        policy=policy,
                    )
                )
            except ValueError as exc:
                self._write_lifecycle_audit(
                    event_type="portfolio_action_check_rejected",
                    account_ref=account_state.account_ref,
                    payload={
                        "action_type": proposed_action.action_type.value,
                        "pre_action_state": self._account_state_audit(account_state),
                        "proposed_action": asdict(proposed_action),
                        "decision": RiskDecision.REJECT.value,
                        "reason": str(exc),
                        "model_version": PORTFOLIO_ACTION_MODEL_VERSION,
                        "policy_snapshot": self._policy_snapshot(policy),
                        "risk_evaluation_audit_id": pre_evaluation.audit_id,
                    },
                )
                raise
            projected_evaluation_holdings = self._holdings_with_pledged_cash(
                projected_holdings,
                projected_cash,
                projected_loan.currency,
            )
            projected_evaluation = self.risk_engine.evaluate(
                account_ref=account_state.account_ref,
                holdings=projected_evaluation_holdings,
                loan=projected_loan,
                policy=policy,
                market_data=normalized_market_data,
            )
            safe_credit_limit = self._safe_credit_limit(projected_evaluation)
            projected_available_credit = round_money(
                max(0.0, safe_credit_limit - projected_evaluation.loan_balance)
            )
            decision, reason, required_repayment = self._portfolio_action_decision(
                pre_evaluation=pre_evaluation,
                projected_evaluation=projected_evaluation,
                projected_available_credit=projected_available_credit,
            )
            projected_state = AccountState(
                account_ref=account_state.account_ref,
                holdings=projected_holdings,
                pledged_cash_balance=projected_cash,
                loan=projected_loan,
                approved_credit_limit=projected_evaluation.approved_credit_limit,
                available_credit=projected_available_credit,
                last_margin_state=projected_evaluation.margin_state,
                last_evaluation_time=projected_evaluation.created_at,
            )

        audit_id = self._write_lifecycle_audit(
            event_type="portfolio_action_check",
            account_ref=account_state.account_ref,
            payload={
                "action_type": proposed_action.action_type.value,
                "pre_action_state": self._account_state_audit(account_state),
                "proposed_action": asdict(proposed_action),
                "projected_post_action_state": self._account_state_audit(
                    projected_state
                ),
                "decision": decision.value,
                "reason": reason,
                "model_version": PORTFOLIO_ACTION_MODEL_VERSION,
                "policy_snapshot": self._policy_snapshot(policy),
                "risk_evaluation_audit_id": pre_evaluation.audit_id,
                "projected_risk_evaluation_audit_id": projected_evaluation.audit_id,
            },
        )

        return PortfolioActionCheckResult(
            decision=decision,
            reason=reason,
            current_outstanding_balance=pre_evaluation.current_outstanding_balance,
            current_available_credit=pre_evaluation.current_available_credit,
            projected_outstanding_balance=projected_evaluation.current_outstanding_balance,
            projected_loan_balance=projected_evaluation.loan_balance,
            projected_approved_credit_limit=projected_evaluation.approved_credit_limit,
            projected_available_credit=projected_state.available_credit,
            projected_margin_state=projected_evaluation.margin_state,
            required_repayment_amount=required_repayment,
            audit_id=audit_id,
            evaluation_result=projected_evaluation,
            projected_account_state=projected_state,
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
                "liquidation_plan": (
                    asdict(evaluation.liquidation_plan)
                    if evaluation.liquidation_plan
                    else None
                ),
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
        projected_holdings, invalid_reason = project_holdings(
            current_holdings, proposed_holding_changes
        )
        loan_after_repayment = apply_repayment(loan, requested_repayment_amount)
        projected_loan = Loan(
            principal=round_money(
                loan_after_repayment.principal + requested_draw_amount
            ),
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
            if projected_evaluation.margin_state in {
                MarginState.MARGIN_CALL,
                MarginState.LIQUIDATION,
            }:
                decision = RiskDecision.REJECT
                reason = "projected margin state is unsafe; action must not proceed"
                reduced_available_credit = None
            elif projected_evaluation.loan_balance > safe_credit_limit:
                decision = RiskDecision.REJECT
                reason = (
                    "projected outstanding balance exceeds dynamic safety requirement"
                )
                reduced_available_credit = None
            elif (
                projected_evaluation.available_credit
                < current_evaluation.available_credit
            ):
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
                "projected_margin_state": (
                    projected_margin_state.value if projected_margin_state else None
                ),
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

    def _project_portfolio_action(
        self,
        account_ref: str,
        holdings: list[Holding],
        pledged_cash_balance: float,
        loan: Loan,
        action: PortfolioActionCheck,
        market_data: Mapping[str, MarketData],
        policy: Policy,
    ) -> tuple[list[Holding], float, Loan]:
        quantities = {holding_identity(holding): holding for holding in holdings}
        pledged_cash = round_money(max(0.0, pledged_cash_balance))
        projected_loan = loan

        if action.action_type == PortfolioActionType.SELL:
            quantity = self._asset_quantity(action, market_data)
            self._add_holding_delta(
                quantities,
                action.asset_id,
                action.asset_type,
                -quantity,
                projected_loan.currency,
            )
            proceeds = self._market_amount(
                action.asset_id, quantity, market_data, side="sell"
            )
            if not action.withdraw_proceeds:
                pledged_cash = round_money(pledged_cash + proceeds)
        elif action.action_type == PortfolioActionType.BUY:
            quantity = self._asset_quantity(action, market_data)
            cost = (
                self._cash_amount(action, "buy")
                if action.amount > 0
                else self._market_amount(
                    action.asset_id, quantity, market_data, side="buy"
                )
            )
            if cost > pledged_cash + 1e-9:
                funding_source = (action.funding_source or "").lower()
                shortfall = round_money(cost - pledged_cash)
                if funding_source in {"draw", "credit_draw"}:
                    draw_result = self.check_draw(
                        account_ref=account_ref,
                        current_loan=projected_loan,
                        requested_draw_amount=shortfall,
                        requested_repayment_amount=0.0,
                        holdings=list(quantities.values()),
                        policy=policy,
                        market_data=market_data,
                    )
                    if draw_result.decision != LifecycleDecisionValue.APPROVED:
                        raise ValueError(
                            "buy action draw funding exceeds safe credit draw amount"
                        )
                    projected_loan = Loan(
                        principal=round_money(projected_loan.principal + shortfall),
                        accrued_interest=projected_loan.accrued_interest,
                        fees=projected_loan.fees,
                        currency=projected_loan.currency,
                    )
                    pledged_cash = 0.0
                elif funding_source in {
                    "transfer_in",
                    "external_cash",
                    "external_cash_source",
                }:
                    pledged_cash = 0.0
                else:
                    raise ValueError(
                        "buy action requires sufficient pledged cash or an explicit draw, transfer_in, or external_cash funding_source"
                    )
            else:
                pledged_cash = round_money(pledged_cash - cost)
            self._add_holding_delta(
                quantities,
                action.asset_id,
                action.asset_type,
                quantity,
                self._currency_for_new_asset(action.asset_id, market_data),
            )
        elif action.action_type == PortfolioActionType.WITHDRAW_CASH:
            amount = self._cash_amount(action, "withdraw_cash")
            if amount > pledged_cash + 1e-9:
                raise ValueError("withdraw_cash action exceeds pledged cash balance")
            pledged_cash = round_money(pledged_cash - amount)
        elif action.action_type in {
            PortfolioActionType.WITHDRAW_SECURITY,
            PortfolioActionType.WITHDRAWAL,
        }:
            quantity = self._asset_quantity(action, market_data)
            self._add_holding_delta(
                quantities,
                action.asset_id,
                action.asset_type,
                -quantity,
                projected_loan.currency,
            )
        elif action.action_type in {
            PortfolioActionType.TRANSFER_SECURITY,
            PortfolioActionType.TRANSFER,
        }:
            quantity = self._asset_quantity(action, market_data)
            delta = quantity if action.direction == TransferDirection.IN else -quantity
            self._add_holding_delta(
                quantities,
                action.asset_id,
                action.asset_type,
                delta,
                self._currency_for_new_asset(action.asset_id, market_data)
                if delta > 0
                else projected_loan.currency,
            )
        elif action.action_type in {
            PortfolioActionType.REPAY,
            PortfolioActionType.REPAYMENT,
        }:
            projected_loan = apply_repayment(loan, self._cash_amount(action, "repay"))
        elif action.action_type == PortfolioActionType.REBALANCE:
            sell_quantity = self._asset_quantity(action, market_data)
            self._add_holding_delta(
                quantities,
                action.asset_id,
                action.asset_type,
                -sell_quantity,
                projected_loan.currency,
            )
            proceeds = self._market_amount(
                action.asset_id, sell_quantity, market_data, side="sell"
            )
            buy_amount = action.to_amount if action.to_amount > 0 else proceeds
            if buy_amount > proceeds + pledged_cash + 1e-9:
                raise ValueError(
                    "rebalance buy leg exceeds sale proceeds plus pledged cash"
                )
            if buy_amount > proceeds:
                pledged_cash = round_money(pledged_cash - (buy_amount - proceeds))
            else:
                pledged_cash = round_money(pledged_cash + (proceeds - buy_amount))
            to_quantity = (
                action.to_quantity
                if action.to_quantity > 0
                else self._quantity_from_amount(
                    action.to_asset_id,
                    buy_amount,
                    market_data,
                    "rebalance",
                    side="buy",
                )
            )
            self._add_holding_delta(
                quantities,
                action.to_asset_id,
                action.to_asset_type,
                to_quantity,
                self._currency_for_new_asset(action.to_asset_id, market_data),
            )
        else:
            raise ValueError(
                f"unsupported portfolio action_type: {action.action_type.value}"
            )

        projected_holdings = [
            holding for holding in quantities.values() if holding.quantity > 1e-9
        ]
        if not projected_holdings and pledged_cash <= 0 and projected_loan.balance <= 0:
            raise ValueError("projected account has no pledged collateral")
        return aggregate_holdings(projected_holdings), pledged_cash, projected_loan

    def _portfolio_action_decision(
        self,
        pre_evaluation,
        projected_evaluation,
        projected_available_credit: float,
    ) -> tuple[RiskDecision, str, float]:
        if projected_evaluation.loan_balance <= 0:
            return (
                RiskDecision.APPROVE,
                "projected loan balance is zero after the action",
                0.0,
            )
        required_repayment = self._required_repayment_to_restore_safety(
            projected_evaluation
        )
        if projected_evaluation.margin_state == MarginState.LIQUIDATION:
            return (
                RiskDecision.LIQUIDATION,
                "projected portfolio is in liquidation",
                required_repayment,
            )
        if projected_evaluation.margin_state == MarginState.MARGIN_CALL:
            return (
                RiskDecision.MARGIN_CALL,
                "projected portfolio is in margin call",
                required_repayment,
            )
        if projected_evaluation.margin_state != MarginState.SAFE:
            return (
                RiskDecision.REJECT,
                "projected account is not safe after the action",
                required_repayment,
            )
        if projected_evaluation.loan_balance > self._safe_credit_limit(
            projected_evaluation
        ):
            return (
                RiskDecision.REJECT,
                "projected outstanding balance exceeds dynamic safety requirement",
                required_repayment,
            )
        if (
            projected_available_credit
            < self._safe_credit_limit(pre_evaluation) - pre_evaluation.loan_balance
        ):
            return (
                RiskDecision.REDUCE_AVAILABLE_CREDIT,
                "projected account remains safe with reduced available credit",
                0.0,
            )
        return (
            RiskDecision.APPROVE,
            "projected account remains safe after the action",
            0.0,
        )

    def _add_holding_delta(
        self,
        holdings: dict[tuple[str, str, str, AssetType, str], Holding],
        asset_id: str | None,
        asset_type: AssetType | None,
        delta: float,
        default_currency: str | None = None,
    ) -> None:
        if not asset_id:
            raise ValueError("security action requires asset_id")
        candidates = [
            key
            for key in holdings
            if key[0] == asset_id.upper()
            and (asset_type is None or key[3] == asset_type)
        ]
        if len(candidates) > 1:
            raise ValueError(
                f"{asset_id} action is ambiguous across asset_type or currency; provide a unique asset identity"
            )
        key = candidates[0] if candidates else None
        current = holdings.get(key) if key else None
        if current is None:
            if delta < 0:
                raise ValueError(f"cannot remove {asset_id}; no current holding exists")
            if asset_type is None:
                raise ValueError("new security action requires asset_type")
            new_holding = Holding(
                asset_id=asset_id,
                asset_type=asset_type,
                quantity=round(delta, 12),
                currency=self._validated_new_asset_currency(asset_id, default_currency),
            )
            holdings[holding_identity(new_holding)] = new_holding
            return
        new_quantity = round(current.quantity + delta, 12)
        if new_quantity < -1e-9:
            raise ValueError(f"action exceeds available {asset_id} quantity")
        holdings[holding_identity(current)] = replace(
            current, quantity=max(0.0, new_quantity)
        )

    def _validated_new_asset_currency(self, asset_id: str, currency: str | None) -> str:
        if not currency:
            raise ValueError(
                f"new security action for {asset_id} requires explicit currency or market data/instrument identity currency"
            )
        return currency.upper()

    def _currency_for_new_asset(
        self, asset_id: str | None, market_data: Mapping[str, MarketData]
    ) -> str | None:
        if not asset_id:
            return None
        market = market_data.get(asset_id)
        instrument = market.metadata.get("instrument", {}) if market else {}
        currency = instrument.get("currency") or (
            market.metadata.get("currency") if market else None
        )
        if currency:
            return str(currency).upper()
        if market is not None and not market.metadata:
            return "USD"
        parts = asset_id.split(":")
        if len(parts) >= 3 and len(parts[2]) == 3:
            return parts[2].upper()
        return None

    def _asset_quantity(
        self, action: PortfolioActionCheck, market_data: Mapping[str, MarketData]
    ) -> float:
        if action.quantity > 0:
            return action.quantity
        if action.amount <= 0:
            raise ValueError(
                f"{action.action_type.value} action requires positive quantity or amount"
            )
        return self._quantity_from_amount(
            action.asset_id,
            action.amount,
            market_data,
            action.action_type.value,
            side="buy" if action.action_type == PortfolioActionType.BUY else "sell",
        )

    def _quantity_from_amount(
        self,
        asset_id: str | None,
        amount: float,
        market_data: Mapping[str, MarketData],
        action_label: str,
        side: str,
    ) -> float:
        market = market_data.get(asset_id or "")
        if market is None:
            raise ValueError(
                f"{action_label} action with amount requires executable market data"
            )
        return amount / self._execution_price(market, side)

    def _market_amount(
        self,
        asset_id: str | None,
        quantity: float,
        market_data: Mapping[str, MarketData],
        side: str,
    ) -> float:
        market = market_data.get(asset_id or "")
        if market is None:
            raise ValueError("security action requires executable market data")
        return round_money(quantity * self._execution_price(market, side))

    @staticmethod
    def _execution_price(market: MarketData, side: str) -> float:
        price = (
            market.ask or market.last_price * 1.02
            if side == "buy"
            else market.bid or market.last_price * 0.98
        )
        if price <= 0:
            raise ValueError("security action requires a positive executable price")
        return price

    def _cash_amount(self, action: PortfolioActionCheck, label: str) -> float:
        amount = action.amount if action.amount > 0 else action.quantity
        if amount <= 0:
            raise ValueError(f"{label} action requires positive amount")
        return round_money(amount)

    def _holdings_with_pledged_cash(
        self, holdings: list[Holding], pledged_cash_balance: float, currency: str
    ) -> list[Holding]:
        combined = list(holdings)
        if pledged_cash_balance > 0:
            combined.append(
                Holding(
                    pledged_cash_asset_id(currency),
                    AssetType.CASH,
                    round_money(pledged_cash_balance),
                    currency,
                )
            )
        return aggregate_holdings(combined)

    def _strip_pledged_cash_holding(self, holdings: list[Holding]) -> list[Holding]:
        return [
            holding
            for holding in holdings
            if not holding.asset_id.startswith(f"{PLEDGED_CASH_ASSET_ID_PREFIX}_")
        ]

    def _market_data_with_pledged_cash(
        self,
        market_data: Mapping[str, MarketData],
        currency: str,
    ) -> dict[str, MarketData]:
        normalized = dict(market_data)
        asset_id = pledged_cash_asset_id(currency)
        normalized.setdefault(
            asset_id,
            MarketData(
                asset_id=asset_id,
                last_price=1.0,
                bid=1.0,
                ask=1.0,
                average_daily_volume=1_000_000_000,
                average_dollar_volume=1_000_000_000,
                volatility_30d=0.0,
                volatility_90d=0.0,
                data_quality_score=1.0,
                metadata={"currency": currency, "source": "pledged_cash_balance"},
            ),
        )
        return normalized

    def _risk_decision_from_lifecycle(
        self, decision: LifecycleDecisionValue
    ) -> RiskDecision:
        if decision == LifecycleDecisionValue.APPROVED:
            return RiskDecision.APPROVE
        if decision == LifecycleDecisionValue.PARTIALLY_APPROVED:
            return RiskDecision.REDUCE_AVAILABLE_CREDIT
        if decision == LifecycleDecisionValue.LIQUIDATION:
            return RiskDecision.LIQUIDATION
        if decision == LifecycleDecisionValue.MARGIN_CALL:
            return RiskDecision.MARGIN_CALL
        return RiskDecision.REJECT

    def _required_repayment_to_restore_safety(self, evaluation) -> float:
        if evaluation.loan_balance <= 0:
            return 0.0
        max_safe_balance = evaluation.stressed_liquidation_value / max(
            evaluation.trigger_levels.dynamic_liquidation_coverage,
            1e-9,
        )
        return round_money(max(0.0, evaluation.loan_balance - max_safe_balance))

    def _account_state_audit(self, state: AccountState) -> dict:
        return {
            "account_ref": state.account_ref,
            "holdings": [asdict(holding) for holding in state.holdings],
            "pledged_cash_balance": state.pledged_cash_balance,
            "loan_principal": state.loan.principal,
            "accrued_interest": state.loan.accrued_interest,
            "fees": state.loan.fees,
            "loan_currency": state.loan.currency,
            "approved_credit_limit": state.approved_credit_limit,
            "available_credit": state.available_credit,
            "last_margin_state": state.last_margin_state.value,
            "last_evaluation_time": (
                state.last_evaluation_time.isoformat()
                if state.last_evaluation_time
                else None
            ),
        }

    def _policy_snapshot(self, policy: Policy) -> dict:
        return {
            "risk_appetite": policy.risk_appetite.value,
            "base_ltv": {key.value: value for key, value in policy.base_ltv.items()},
            "asset_ltv_caps": {
                key.value: value for key, value in policy.asset_ltv_caps.items()
            },
            "max_participation_rate": policy.max_participation_rate,
            "min_data_quality_score": policy.min_data_quality_score,
            "allow_lending_on_stale_or_halted_assets": policy.allow_lending_on_stale_or_halted_assets,
        }

    def _safe_credit_limit(self, evaluation) -> float:
        recovery_limited_balance = evaluation.stressed_liquidation_value / max(
            evaluation.trigger_levels.dynamic_warning_coverage,
            1e-9,
        )
        return round_money(
            max(0.0, min(evaluation.approved_credit_limit, recovery_limited_balance))
        )

    def _monitor_reason(self, margin_state: MarginState) -> str:
        return {
            MarginState.SAFE: "loan is within dynamic credit and recovery coverage limits",
            MarginState.WATCH: "outstanding balance is above dynamic warning threshold",
            MarginState.RESTRICT_NEW_BORROWING: "outstanding balance requires new borrowing restriction until coverage improves",
            MarginState.MARGIN_CALL: "outstanding balance requires margin cure or risk reducing liquidation",
            MarginState.LIQUIDATION: "outstanding balance breaches dynamic liquidation coverage threshold",
        }[margin_state]

    def _write_lifecycle_audit(
        self, event_type: str, account_ref: str, payload: dict
    ) -> str:
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
    aggregated: dict[tuple[str, str, str, AssetType, str], float] = {}
    originals: dict[tuple[str, str, str, AssetType, str], Holding] = {}
    order: list[tuple[str, str, str, AssetType, str]] = []
    for holding in holdings:
        key = holding.stable_identity
        if key not in aggregated:
            aggregated[key] = 0.0
            originals[key] = holding
            order.append(key)
        aggregated[key] += holding.quantity
    return [
        replace(originals[key], quantity=round(aggregated[key], 12)) for key in order
    ]


def project_holdings(
    current_holdings: list[Holding],
    proposed_holding_changes: list[Holding],
) -> tuple[list[Holding], str | None]:
    quantities: dict[tuple[str, str, str, AssetType, str], float] = {}
    order: list[tuple[str, str, str, AssetType, str]] = []
    originals: dict[tuple[str, str, str, AssetType, str], Holding] = {}

    for holding in [*current_holdings, *proposed_holding_changes]:
        key = holding.stable_identity
        if key not in quantities:
            quantities[key] = 0.0
            originals[key] = holding
            order.append(key)
        quantities[key] += holding.quantity

    projected: list[Holding] = []
    for key in order:
        quantity = round(quantities[key], 12)
        if quantity < 0:
            return (
                [],
                "projected holding quantity would be negative; action must not proceed",
            )
        if quantity > 0:
            projected.append(replace(originals[key], quantity=quantity))

    if not projected:
        return [], "projected holdings are empty; action must not proceed"
    return projected, None
