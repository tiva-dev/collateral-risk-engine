from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.enums import AssetType, MarginState, RiskAppetite


@dataclass(frozen=True)
class Holding:
    asset_id: str
    asset_type: AssetType
    quantity: float
    currency: str = "USD"


@dataclass(frozen=True)
class Loan:
    principal: float
    accrued_interest: float = 0.0
    fees: float = 0.0
    currency: str = "USD"

    @property
    def balance(self) -> float:
        return max(0.0, self.principal + self.accrued_interest + self.fees)


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    quantity: float


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
    intraday_volatility: float | None = None  # annualized decimal when available
    recent_return_1d: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data_quality_score: float = 1.0  # 0 to 1
    halted: bool = False
    order_book: OrderBook | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    base_ltv: dict[AssetType, float]
    risk_appetite: RiskAppetite = RiskAppetite.BALANCED
    asset_ltv_caps: dict[AssetType, float] = field(default_factory=dict)
    max_participation_rate: float = 0.10
    min_data_quality_score: float = 0.35
    allow_lending_on_stale_or_halted_assets: bool = False

    @staticmethod
    def default() -> "Policy":
        return Policy(
            base_ltv={
                AssetType.CASH: 0.95,
                AssetType.BOND: 0.80,
                AssetType.BOND_FUND: 0.78,
                AssetType.ETF: 0.70,
                AssetType.LISTED_EQUITY: 0.65,
                AssetType.HIGH_VOLATILITY_EQUITY: 0.35,
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
                AssetType.HIGH_VOLATILITY_EQUITY: 0.50,
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


@dataclass(frozen=True)
class TriggerLevels:
    dynamic_liquidation_coverage: float
    dynamic_margin_call_coverage: float
    dynamic_restriction_coverage: float
    dynamic_warning_coverage: float
    required_cure_amount: float


@dataclass(frozen=True)
class LiquidationOrder:
    asset_id: str
    side: str
    quantity: float
    order_type: str
    estimated_cash_recovery: float
    reason: str


@dataclass(frozen=True)
class LiquidationPlan:
    action: str
    target_cash_recovery: float
    orders: list[LiquidationOrder]
    reason: str


@dataclass(frozen=True)
class PortfolioEvaluation:
    account_ref: str
    portfolio_market_value: float
    risk_adjusted_collateral_value: float
    approved_credit_limit: float
    stressed_liquidation_value: float
    loan_balance: float
    available_credit: float
    recovery_coverage_ratio: float | None
    portfolio_risk_score: float
    margin_state: MarginState
    trigger_levels: TriggerLevels
    asset_results: list[AssetRiskResult]
    liquidation_plan: LiquidationPlan | None
    audit_id: str
    model_version: str
    created_at: datetime
