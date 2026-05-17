from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.enums import LifecycleDecisionValue, MarginState, RiskDecision
from app.core.models import Holding, Loan, PortfolioEvaluation


@dataclass(frozen=True)
class LifecycleDecision:
    decision: LifecycleDecisionValue
    reason: str
    current_outstanding_balance: float
    current_available_credit: float
    projected_outstanding_balance: float | None
    projected_available_credit: float | None
    projected_margin_state: MarginState | None
    approved_credit_limit: float
    margin_state: MarginState
    required_cure_amount: float
    minimum_stressed_liquidation_value: float
    max_approved_draw_amount: float | None
    current_loan: Loan
    projected_loan: Loan | None
    liquidation_plan: object | None
    evaluation: PortfolioEvaluation
    audit_id: str
    created_at: datetime


@dataclass(frozen=True)
class OriginationResult:
    decision: LifecycleDecisionValue
    reason: str
    current_outstanding_balance: float
    current_available_credit: float
    projected_outstanding_balance: float | None
    projected_available_credit: float | None
    projected_margin_state: MarginState | None
    approved_credit_limit: float
    risk_adjusted_collateral_value: float
    stressed_liquidation_value: float
    minimum_stressed_liquidation_value: float
    asset_results: list
    margin_state: MarginState
    required_cure_amount: float
    max_approved_draw_amount: float | None
    current_loan: Loan
    projected_loan: Loan | None
    liquidation_plan: object | None
    evaluation: PortfolioEvaluation
    audit_id: str
    created_at: datetime


@dataclass(frozen=True)
class PreTradeCheckResult:
    decision: RiskDecision
    reason: str
    current_outstanding_balance: float
    current_available_credit: float
    projected_outstanding_balance: float | None
    projected_available_credit: float | None
    projected_margin_state: MarginState | None
    reduced_available_credit: float | None
    approved_credit_limit: float
    margin_state: MarginState
    required_cure_amount: float
    minimum_stressed_liquidation_value: float
    current_loan: Loan
    projected_loan: Loan | None
    current_holdings: list[Holding]
    projected_holdings: list[Holding]
    liquidation_plan: object | None
    evaluation: PortfolioEvaluation
    audit_id: str
    created_at: datetime


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
