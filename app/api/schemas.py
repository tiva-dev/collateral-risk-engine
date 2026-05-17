from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import AssetType, RiskAppetite
from app.core.models import (
    Holding,
    Loan,
    MarketData,
    OrderBook,
    OrderBookLevel,
    Policy,
)


class HoldingIn(BaseModel):
    asset_id: str
    asset_type: AssetType
    quantity: float
    currency: str = "USD"

    def to_domain(self) -> Holding:
        return Holding(
            asset_id=self.asset_id,
            asset_type=self.asset_type,
            quantity=self.quantity,
            currency=self.currency,
        )


class LoanIn(BaseModel):
    principal: float
    accrued_interest: float = 0.0
    fees: float = 0.0
    currency: str = "USD"

    def to_domain(self) -> Loan:
        return Loan(
            principal=self.principal,
            accrued_interest=self.accrued_interest,
            fees=self.fees,
            currency=self.currency,
        )


class OrderBookLevelIn(BaseModel):
    price: float
    quantity: float

    def to_domain(self) -> OrderBookLevel:
        return OrderBookLevel(price=self.price, quantity=self.quantity)


class OrderBookIn(BaseModel):
    bids: list[OrderBookLevelIn] = Field(default_factory=list)
    asks: list[OrderBookLevelIn] = Field(default_factory=list)

    def to_domain(self) -> OrderBook:
        return OrderBook(
            bids=[level.to_domain() for level in self.bids],
            asks=[level.to_domain() for level in self.asks],
        )


class MarketDataIn(BaseModel):
    asset_id: str
    last_price: float
    bid: float | None = None
    ask: float | None = None
    average_daily_volume: float | None = None
    average_dollar_volume: float | None = None
    volatility_30d: float | None = None
    volatility_90d: float | None = None
    intraday_volatility: float | None = None
    recent_return_1d: float | None = None
    data_quality_score: float = 1.0
    halted: bool = False
    order_book: OrderBookIn | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self) -> MarketData:
        return MarketData(
            asset_id=self.asset_id,
            last_price=self.last_price,
            bid=self.bid,
            ask=self.ask,
            average_daily_volume=self.average_daily_volume,
            average_dollar_volume=self.average_dollar_volume,
            volatility_30d=self.volatility_30d,
            volatility_90d=self.volatility_90d,
            intraday_volatility=self.intraday_volatility,
            recent_return_1d=self.recent_return_1d,
            data_quality_score=self.data_quality_score,
            halted=self.halted,
            order_book=self.order_book.to_domain() if self.order_book else None,
            metadata=self.metadata,
        )


class PolicyIn(BaseModel):
    base_ltv: dict[AssetType, float]
    risk_appetite: RiskAppetite = RiskAppetite.BALANCED
    asset_ltv_caps: dict[AssetType, float] = Field(default_factory=dict)
    max_participation_rate: float = 0.10
    min_data_quality_score: float = 0.35
    allow_lending_on_stale_or_halted_assets: bool = False

    def to_domain(self) -> Policy:
        default_policy = Policy.default()
        base_ltv = {**default_policy.base_ltv, **self.base_ltv}
        caps = {**default_policy.asset_ltv_caps, **self.asset_ltv_caps}
        return Policy(
            base_ltv=base_ltv,
            risk_appetite=self.risk_appetite,
            asset_ltv_caps=caps,
            max_participation_rate=self.max_participation_rate,
            min_data_quality_score=self.min_data_quality_score,
            allow_lending_on_stale_or_halted_assets=self.allow_lending_on_stale_or_halted_assets,
        )


class EvaluateRequest(BaseModel):
    account_ref: str
    loan: LoanIn
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]


class EvaluateResponse(BaseModel):
    result: dict[str, Any]


class OriginateRequest(BaseModel):
    account_ref: str
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]


class DrawCheckRequest(BaseModel):
    account_ref: str
    current_loan: LoanIn
    requested_draw_amount: float
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]


class MonitorRequest(BaseModel):
    account_ref: str
    loan: LoanIn
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]


class LifecycleResponse(BaseModel):
    result: dict[str, Any]
