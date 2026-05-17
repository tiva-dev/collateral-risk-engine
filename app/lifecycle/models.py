from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.enums import MarginState
from app.core.models import PortfolioEvaluation


@dataclass(frozen=True)
class LifecycleDecision:
    decision: str
    reason: str
    current_loan_balance: float
    projected_loan_balance: float | None
    approved_credit_limit: float
    available_credit: float
    margin_state: MarginState
    required_cure_amount: float
    max_approved_draw_amount: float | None
    liquidation_plan: object | None
    evaluation: PortfolioEvaluation
    audit_id: str
    created_at: datetime


@dataclass(frozen=True)
class OriginationResult:
    decision: str
    reason: str
    current_loan_balance: float
    projected_loan_balance: float | None
    approved_credit_limit: float
    available_credit: float
    risk_adjusted_collateral_value: float
    stressed_liquidation_value: float
    asset_results: list
    margin_state: MarginState
    required_cure_amount: float
    max_approved_draw_amount: float | None
    liquidation_plan: object | None
    evaluation: PortfolioEvaluation
    audit_id: str
    created_at: datetime


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
