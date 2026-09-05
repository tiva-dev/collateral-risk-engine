from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from app.core.enums import DataMode, MarginState
from app.core.models import Holding, Loan, Policy
from app.credit.interest import InterestPolicy
from app.liquidation.policy import LiquidationExecutionPolicy
from app.market_data.policy import MarketDataPolicy
from app.market_data.providers import FXRate, RawQuote


def utc_now() -> datetime:
    return datetime.now(UTC)


class MonitoringStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class MonitoringEventType(str, Enum):
    MONITORING_TICK_COMPLETED = "monitoring_tick_completed"
    RISK_STATE_CHANGED = "risk_state_changed"
    AVAILABLE_CREDIT_CHANGED = "available_credit_changed"
    MARGIN_CALL_TRIGGERED = "margin_call_triggered"
    LIQUIDATION_TRIGGERED = "liquidation_triggered"
    DRAW_APPLIED = "draw_applied"
    REPAYMENT_APPLIED = "repayment_applied"
    MARKET_DATA_DEGRADED = "market_data_degraded"
    FX_MISSING = "fx_missing"
    LOAN_BALANCE_UPDATED = "loan_balance_updated"
    MONITORING_ERROR = "monitoring_error"


class MonitoringSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class MonitoringThresholds:
    available_credit_abs_change_threshold: float = 1.0
    available_credit_pct_change_threshold: float = 0.01
    data_quality_change_threshold: float = 0.15
    persist_unchanged_info_ticks: bool = False
    dedupe_ttl_seconds: int = 300


@dataclass
class MonitoredAccount:
    account_ref: str
    holdings: list[Holding]
    pledged_cash_balance: float
    loan: Loan
    loan_currency: str
    policy: Policy
    data_mode: DataMode
    market_data_policy: MarketDataPolicy
    interest_policy: InterestPolicy = field(default_factory=InterestPolicy)
    liquidation_execution_policy: LiquidationExecutionPolicy = field(
        default_factory=LiquidationExecutionPolicy
    )
    client_supplied_quotes: dict[str, RawQuote] = field(default_factory=dict)
    client_supplied_fx_rates: dict[tuple[str, str], FXRate] = field(
        default_factory=dict
    )
    monitoring_status: MonitoringStatus = MonitoringStatus.ACTIVE
    last_evaluation: Mapping[str, Any] | None = None
    last_margin_state: MarginState | None = None
    last_available_credit: float | None = None
    last_market_data_warnings: dict[str, list[str]] = field(default_factory=dict)
    last_missing_data: list[str] = field(default_factory=list)
    last_quality_scores: dict[str, float] = field(default_factory=dict)
    last_checked_at: datetime | None = None
    next_check_after: datetime | None = None
    last_interest_accrual_at: datetime | None = None
    processed_loan_event_references: list[str] = field(default_factory=list)
    processed_execution_references: set[str] = field(default_factory=set)
    processed_draw_references: set[str] = field(default_factory=set)
    processed_repayment_references: set[str] = field(default_factory=set)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class MonitoringEvent:
    event_id: str
    account_ref: str | None
    event_type: MonitoringEventType
    severity: MonitoringSeverity
    previous_margin_state: MarginState | None = None
    new_margin_state: MarginState | None = None
    previous_available_credit: float | None = None
    new_available_credit: float | None = None
    reason: str = ""
    evaluation_snapshot: Mapping[str, Any] | None = None
    market_data_warnings: dict[str, list[str]] = field(default_factory=dict)
    missing_data: list[str] = field(default_factory=list)
    liquidation_plan: Mapping[str, Any] | None = None
    model_versions: dict[str, str] = field(default_factory=dict)
    audit_id: str | None = None
    dedupe_key: str | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class MarketDataUpdate:
    instruments: list[str] = field(default_factory=list)
    quote_updates: dict[str, RawQuote] = field(default_factory=dict)
    fx_rate_updates: dict[tuple[str, str], FXRate] = field(default_factory=dict)
    source: str = "internal"
    received_at: datetime = field(default_factory=utc_now)
