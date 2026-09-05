from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.enums import (
    AssetType,
    MarginState,
    PortfolioActionType,
    RiskAppetite,
    RiskDecision,
    TransferDirection,
)


@dataclass(frozen=True)
class Holding:
    asset_id: str
    asset_type: AssetType
    quantity: float
    currency: str = "USD"
    exchange: str = "UNKNOWN"
    provider_id: str | None = None

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError("holding quantity must be greater than or equal to 0")
        if (
            not self.asset_id.strip()
            or not self.currency.strip()
            or not self.exchange.strip()
        ):
            raise ValueError("holding requires stable asset_id, currency, and exchange")

    @property
    def stable_identity(self) -> tuple[str, str, str, AssetType, str]:
        """Identity used for aggregation; never collapse cross-venue instruments."""
        return (
            self.asset_id.upper(),
            self.exchange.upper(),
            self.currency.upper(),
            self.asset_type,
            (self.provider_id or "").upper(),
        )

    @property
    def stable_key(self) -> str:
        """Serializable key for market-data maps and audit artifacts."""
        provider = (self.provider_id or "-").upper()
        return f"{self.exchange.upper()}:{self.asset_id.upper()}:{self.currency.upper()}:{self.asset_type.value.upper()}:{provider}"


@dataclass(frozen=True)
class Loan:
    principal: float
    accrued_interest: float = 0.0
    fees: float = 0.0
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.principal < 0:
            raise ValueError("loan principal must be greater than or equal to 0")
        if self.accrued_interest < 0:
            raise ValueError("loan accrued_interest must be greater than or equal to 0")
        if self.fees < 0:
            raise ValueError("loan fees must be greater than or equal to 0")

    @property
    def balance(self) -> float:
        return max(0.0, self.principal + self.accrued_interest + self.fees)


@dataclass(frozen=True)
class PortfolioAction:
    action_type: PortfolioActionType
    asset_id: str | None = None
    asset_type: AssetType | None = None
    quantity: float = 0.0
    amount: float = 0.0
    direction: TransferDirection = TransferDirection.OUT
    funding_source: str | None = None


@dataclass(frozen=True)
class AccountState:
    account_ref: str
    holdings: list[Holding]
    pledged_cash_balance: float
    loan: Loan
    approved_credit_limit: float
    available_credit: float
    last_margin_state: MarginState
    last_evaluation_time: datetime | None = None


@dataclass(frozen=True)
class PortfolioActionCheck:
    action_type: PortfolioActionType
    asset_id: str | None = None
    asset_type: AssetType | None = None
    quantity: float = 0.0
    amount: float = 0.0
    direction: TransferDirection = TransferDirection.OUT
    funding_source: str | None = None
    withdraw_proceeds: bool = False
    to_asset_id: str | None = None
    to_asset_type: AssetType | None = None
    to_quantity: float = 0.0
    to_amount: float = 0.0


@dataclass(frozen=True)
class PortfolioActionCheckResult:
    decision: RiskDecision
    reason: str
    current_outstanding_balance: float
    current_available_credit: float
    projected_outstanding_balance: float
    projected_loan_balance: float
    projected_approved_credit_limit: float
    projected_available_credit: float
    projected_margin_state: MarginState
    required_repayment_amount: float
    audit_id: str
    evaluation_result: PortfolioEvaluation
    projected_account_state: AccountState
    created_at: datetime


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    quantity: float

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("order book level price must be greater than 0")
        if self.quantity < 0:
            raise ValueError(
                "order book level quantity must be greater than or equal to 0"
            )


@dataclass(frozen=True)
class OrderBook:
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)


@dataclass(frozen=True)
class MarketData:
    asset_id: str
    last_price: float
    bid: float | None = None
    ask: float | None = None
    average_daily_volume: float | None = None
    average_dollar_volume: float | None = None
    volatility_30d: float | None = None  # annualized decimal, e.g. 0.32
    volatility_90d: float | None = None  # annualized decimal
    volatility_252d: float | None = None  # annualized decimal
    intraday_volatility: float | None = None  # annualized decimal when available
    max_drawdown_252d: float | None = None
    max_gap_252d: float | None = None
    recent_return_1d: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    data_quality_score: float = 1.0  # 0 to 1
    halted: bool = False
    order_book: OrderBook | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.last_price < 0:
            raise ValueError(
                "market data last_price must be greater than or equal to 0"
            )
        if self.bid is not None and self.bid <= 0:
            raise ValueError("market data bid must be greater than 0 when supplied")
        if self.ask is not None and self.ask <= 0:
            raise ValueError("market data ask must be greater than 0 when supplied")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("market data bid must be less than or equal to ask")
        if not 0 <= self.data_quality_score <= 1:
            raise ValueError("market data quality score must be between 0 and 1")
        if self.timestamp.tzinfo is None:
            raise ValueError("market data timestamp must be timezone-aware")


@dataclass(frozen=True)
class Policy:
    base_ltv: dict[AssetType, float]
    risk_appetite: RiskAppetite = RiskAppetite.BALANCED
    asset_ltv_caps: dict[AssetType, float] = field(default_factory=dict)
    portfolio_ltv_cap: float = 1.0
    # Retained for request compatibility. Liquidation participation is derived
    # by the CRI from observed liquidity and risk; this is only an absolute
    # safety ceiling and is not a client-selected trading assumption.
    max_participation_rate: float = 0.25
    min_data_quality_score: float = 0.35
    allow_lending_on_stale_or_halted_assets: bool = False
    allowed_asset_types: frozenset[AssetType] | None = None
    allowed_exchanges: frozenset[str] | None = None
    excluded_asset_ids: frozenset[str] = field(default_factory=frozenset)
    security_ltv_caps: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for asset_type, haircut in {**self.base_ltv, **self.asset_ltv_caps}.items():
            if not 0 <= haircut <= 1:
                raise ValueError(
                    f"haircut/ltv for {asset_type} must be between 0 and 1"
                )
        if not 0 <= self.max_participation_rate <= 1:
            raise ValueError("max_participation_rate must be between 0 and 1")
        if not 0 <= self.portfolio_ltv_cap <= 1:
            raise ValueError("portfolio_ltv_cap must be between 0 and 1")
        if not 0 <= self.min_data_quality_score <= 1:
            raise ValueError("min_data_quality_score must be between 0 and 1")
        for asset_id, cap in self.security_ltv_caps.items():
            if not asset_id or not 0 <= cap <= 1:
                raise ValueError("security_ltv_caps must contain valid ids and caps")

    @staticmethod
    def default() -> Policy:
        return Policy(
            base_ltv={
                AssetType.CASH: 0.95,
                AssetType.BOND: 0.80,
                AssetType.BOND_FUND: 0.78,
                AssetType.ETF: 0.70,
                AssetType.LISTED_EQUITY: 0.65,
                AssetType.HIGH_VOLATILITY_EQUITY: 0.65,
                AssetType.CRYPTO: 0.20,
                AssetType.OPTION: 0.05,
                AssetType.PRIVATE_ASSET: 0.0,
                AssetType.OTHER: 0.0,
            },
            risk_appetite=RiskAppetite.BALANCED,
            asset_ltv_caps={
                AssetType.CASH: 0.98,
                AssetType.BOND: 0.90,
                AssetType.BOND_FUND: 0.88,
                AssetType.ETF: 0.80,
                AssetType.LISTED_EQUITY: 0.75,
                AssetType.HIGH_VOLATILITY_EQUITY: 0.75,
                AssetType.CRYPTO: 0.35,
                AssetType.OPTION: 0.10,
                AssetType.PRIVATE_ASSET: 0.20,
                AssetType.OTHER: 0.0,
            },
        )


@dataclass(frozen=True)
class RiskAdjustmentBreakdown:
    volatility: float
    liquidity: float
    spread: float
    concentration: float
    stress: float
    data_quality: float

    @property
    def product(self) -> float:
        value = 1.0
        for adjustment in [
            self.volatility,
            self.liquidity,
            self.spread,
            self.concentration,
            self.stress,
            self.data_quality,
        ]:
            value *= adjustment
        return value

    def as_dict(self) -> dict[str, float]:
        return {
            "volatility": self.volatility,
            "liquidity": self.liquidity,
            "spread": self.spread,
            "concentration": self.concentration,
            "stress": self.stress,
            "data_quality": self.data_quality,
        }


@dataclass(frozen=True)
class AssetRiskResult:
    asset_id: str
    asset_type: AssetType
    quantity: float
    market_value: float
    base_ltv: float
    effective_ltv: float
    lendable_value: float
    stressed_liquidation_value: float
    estimated_slippage_rate: float
    liquidation_horizon_days: float
    risk_score: float
    risk_drivers: list[str]
    eligible: bool
    adjustments: RiskAdjustmentBreakdown
    notes: list[str] = field(default_factory=list)
    safe_participation_rate: float | None = None
    liquidity_observed: bool = False
    stable_key: str | None = None


@dataclass(frozen=True)
class TriggerLevels:
    dynamic_liquidation_coverage: float
    dynamic_margin_call_coverage: float
    dynamic_restriction_coverage: float
    dynamic_warning_coverage: float
    required_cure_amount: float
    repayment_only_cure: float = 0.0
    collateral_injection_only_cure: float = 0.0


@dataclass(frozen=True)
class LiquidationOrder:
    asset_id: str
    side: str
    quantity: float
    order_type: str
    estimated_cash_recovery: float
    reason: str
    minimum_execution_price: float | None = None
    estimated_slippage_rate: float = 0.0
    sequence: int = 0
    status: str = "advisory"
    stable_key: str | None = None


@dataclass(frozen=True)
class LiquidationPlan:
    action: str
    target_cash_recovery: float
    orders: list[LiquidationOrder]
    reason: str
    estimated_total_recovery: float = 0.0
    unrecovered_target_amount: float = 0.0
    plan_complete: bool = True
    remaining_debt_after_plan: float = 0.0
    execution_status: str = "awaiting_client_execution"
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class PortfolioEvaluation:
    account_ref: str
    portfolio_market_value: float
    risk_adjusted_collateral_value: float
    approved_credit_limit: float
    stressed_liquidation_value: float
    current_outstanding_balance: float
    current_available_credit: float
    loan_balance: float
    outstanding_balance: float
    available_credit: float
    requested_draw_amount: float
    projected_loan_balance: float
    projected_available_credit: float
    recovery_coverage_ratio: float | None
    dynamic_safety_requirement: float
    minimum_stressed_liquidation_value: float
    portfolio_risk_score: float
    margin_state: MarginState
    trigger_levels: TriggerLevels
    asset_results: list[AssetRiskResult]
    liquidation_plan: LiquidationPlan | None
    audit_id: str
    model_version: str
    created_at: datetime


@dataclass(frozen=True)
class PreTradeRiskCheckResult:
    account_ref: str
    decision: RiskDecision
    approved: bool
    reason: str
    current_outstanding_balance: float
    current_available_credit: float
    outstanding_balance: float
    available_credit: float
    requested_draw_amount: float
    projected_loan_balance: float
    projected_available_credit: float
    projected_stressed_liquidation_value: float
    dynamic_safety_requirement: float
    minimum_stressed_liquidation_value: float
    required_repayment_amount: float
    reduced_available_credit: float | None
    projected_margin_state: MarginState
    projected_holdings: list[Holding]
    projected_evaluation: PortfolioEvaluation
    created_at: datetime
