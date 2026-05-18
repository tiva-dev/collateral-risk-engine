from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.core.enums import (
    AssetType,
    LifecycleDecisionValue,
    MarginState,
    PortfolioActionType,
    RiskAppetite,
    RiskDecision,
    TransferDirection,
    DataMode,
)
from app.core.models import (
    Holding,
    Loan,
    MarketData,
    OrderBook,
    OrderBookLevel,
    Policy,
    AccountState,
    PortfolioAction,
    PortfolioActionCheck,
)


class HoldingIn(BaseModel):
    asset_id: str
    asset_type: AssetType
    quantity: float = Field(ge=0)
    currency: str = "USD"

    def to_domain(self) -> Holding:
        return Holding(
            asset_id=self.asset_id,
            asset_type=self.asset_type,
            quantity=self.quantity,
            currency=self.currency,
        )


class LoanIn(BaseModel):
    principal: float = Field(ge=0)
    accrued_interest: float = Field(default=0.0, ge=0)
    fees: float = Field(default=0.0, ge=0)
    currency: str = "USD"

    def to_domain(self) -> Loan:
        return Loan(
            principal=self.principal,
            accrued_interest=self.accrued_interest,
            fees=self.fees,
            currency=self.currency,
        )


class PortfolioActionIn(BaseModel):
    action_type: PortfolioActionType
    asset_id: str | None = None
    asset_type: AssetType | None = None
    quantity: float = 0.0
    amount: float = 0.0
    direction: TransferDirection = TransferDirection.OUT
    funding_source: str | None = None

    def to_domain(self) -> PortfolioAction:
        return PortfolioAction(
            action_type=self.action_type,
            asset_id=self.asset_id,
            asset_type=self.asset_type,
            quantity=self.quantity,
            amount=self.amount,
            direction=self.direction,
            funding_source=self.funding_source,
        )


class AccountStateIn(BaseModel):
    account_ref: str
    holdings: list[HoldingIn]
    pledged_cash_balance: float = 0.0
    loan_principal: float = Field(
        ge=0, validation_alias=AliasChoices("loan_principal", "principal")
    )
    accrued_interest: float = Field(default=0.0, ge=0)
    fees: float = Field(default=0.0, ge=0)
    loan_currency: str = "USD"
    approved_credit_limit: float = 0.0
    available_credit: float = 0.0
    last_margin_state: MarginState = MarginState.SAFE
    last_evaluation_time: datetime | None = None

    def to_domain(self) -> AccountState:
        return AccountState(
            account_ref=self.account_ref,
            holdings=[holding.to_domain() for holding in self.holdings],
            pledged_cash_balance=self.pledged_cash_balance,
            loan=Loan(
                principal=self.loan_principal,
                accrued_interest=self.accrued_interest,
                fees=self.fees,
                currency=self.loan_currency,
            ),
            approved_credit_limit=self.approved_credit_limit,
            available_credit=self.available_credit,
            last_margin_state=self.last_margin_state,
            last_evaluation_time=self.last_evaluation_time,
        )


class PortfolioActionCheckIn(BaseModel):
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

    def to_domain(self) -> PortfolioActionCheck:
        return PortfolioActionCheck(
            action_type=self.action_type,
            asset_id=self.asset_id,
            asset_type=self.asset_type,
            quantity=self.quantity,
            amount=self.amount,
            direction=self.direction,
            funding_source=self.funding_source,
            withdraw_proceeds=self.withdraw_proceeds,
            to_asset_id=self.to_asset_id,
            to_asset_type=self.to_asset_type,
            to_quantity=self.to_quantity,
            to_amount=self.to_amount,
        )


class OrderBookLevelIn(BaseModel):
    price: float = Field(gt=0)
    quantity: float = Field(ge=0)

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
    last_price: float = Field(gt=0)
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)
    average_daily_volume: float | None = None
    average_dollar_volume: float | None = None
    volatility_30d: float | None = None
    volatility_90d: float | None = None
    intraday_volatility: float | None = None
    recent_return_1d: float | None = None
    data_quality_score: float = Field(default=1.0, ge=0, le=1)
    halted: bool = False
    order_book: OrderBookIn | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_bid_ask(self):
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid must be less than or equal to ask")
        return self

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
    max_participation_rate: float = Field(default=0.10, ge=0, le=1)
    min_data_quality_score: float = Field(default=0.35, ge=0, le=1)
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


class InstrumentIdentityIn(BaseModel):
    asset_id: str
    symbol: str
    exchange: str
    currency: str
    asset_type: AssetType
    isin: str | None = None
    figi: str | None = None
    provider_symbol: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self):
        from app.market_data.identity import InstrumentIdentity

        return InstrumentIdentity(
            asset_id=self.asset_id,
            symbol=self.symbol,
            exchange=self.exchange,
            currency=self.currency,
            asset_type=self.asset_type,
            isin=self.isin,
            figi=self.figi,
            provider_symbol=self.provider_symbol,
            metadata=self.metadata,
        )


class ClientQuoteIn(BaseModel):
    asset_id: str
    symbol: str | None = None
    exchange: str | None = None
    currency: str | None = None
    asset_type: AssetType = AssetType.LISTED_EQUITY
    local_price: float = Field(gt=0)
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)
    average_daily_volume: float | None = None
    average_dollar_volume: float | None = None
    volatility_30d: float | None = None
    volatility_90d: float | None = None
    intraday_volatility: float | None = None
    recent_return_1d: float | None = None
    order_book: OrderBookIn | None = None
    timestamp: datetime | None = None
    data_quality_score: float = Field(default=1.0, ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)
    provider_name: str = "client_supplied"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_bid_ask(self):
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid must be less than or equal to ask")
        return self

    def to_raw_quote(self):
        from app.market_data.identity import InstrumentIdentity
        from app.market_data.providers import RawQuote

        instrument = InstrumentIdentity(
            asset_id=self.asset_id,
            symbol=self.symbol or self.asset_id,
            exchange=self.exchange or "UNKNOWN",
            currency=self.currency or "USD",
            asset_type=self.asset_type,
        )
        return RawQuote(
            instrument=instrument,
            local_price=self.local_price,
            bid=self.bid,
            ask=self.ask,
            average_daily_volume=self.average_daily_volume,
            average_dollar_volume=self.average_dollar_volume,
            volatility_30d=self.volatility_30d,
            volatility_90d=self.volatility_90d,
            intraday_volatility=self.intraday_volatility,
            recent_return_1d=self.recent_return_1d,
            order_book=self.order_book.to_domain() if self.order_book else None,
            timestamp=self.timestamp or datetime.now().astimezone(),
            source="client_supplied",
            provider_name=self.provider_name,
            data_quality_score=self.data_quality_score,
            warnings=self.warnings,
            metadata=self.metadata,
        )


class ClientFXRateIn(BaseModel):
    from_currency: str
    to_currency: str
    rate: float = Field(gt=0)
    timestamp: datetime | None = None
    quality_score: float = Field(default=1.0, ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)
    provider_name: str = "client_supplied"

    def to_fx_rate(self):
        from app.market_data.providers import FXRate

        return FXRate(
            from_currency=self.from_currency,
            to_currency=self.to_currency,
            rate=self.rate,
            timestamp=self.timestamp or datetime.now().astimezone(),
            source="client_supplied",
            provider_name=self.provider_name,
            quality_score=self.quality_score,
            warnings=self.warnings,
        )


class FXPolicyIn(BaseModel):
    preferred_source: str = "client"
    allow_fallback_provider: bool = True
    max_fx_age_minutes: int = Field(default=24 * 60, gt=0)
    stale_fx_haircut: float = Field(default=0.35, ge=0, le=1)
    use_conservative_rate_when_sources_disagree: bool = False
    minimum_fx_quality_score: float = Field(default=0.50, ge=0, le=1)

    def to_domain(self):
        from app.market_data.policy import FXPolicy

        return FXPolicy(**self.model_dump())


class MarketDataPolicyIn(BaseModel):
    fx: FXPolicyIn = Field(default_factory=FXPolicyIn)
    max_quote_age_minutes_by_asset_type: dict[AssetType, int] = Field(default_factory=dict)
    max_quote_age_minutes_by_exchange: dict[str, int] = Field(default_factory=dict)
    stale_quote_haircut: float = Field(default=0.35, ge=0, le=1)
    minimum_quote_quality_score: float = Field(default=0.50, ge=0, le=1)
    allow_fallback_provider: bool = True

    def to_domain(self):
        from app.market_data.policy import MarketDataPolicy

        default_policy = MarketDataPolicy()
        return MarketDataPolicy(
            fx=self.fx.to_domain(),
            max_quote_age_minutes_by_asset_type={
                **default_policy.max_quote_age_minutes_by_asset_type,
                **self.max_quote_age_minutes_by_asset_type,
            },
            max_quote_age_minutes_by_exchange={
                key.upper(): value
                for key, value in self.max_quote_age_minutes_by_exchange.items()
            },
            stale_quote_haircut=self.stale_quote_haircut,
            minimum_quote_quality_score=self.minimum_quote_quality_score,
            allow_fallback_provider=self.allow_fallback_provider,
        )


class MarketDataNormalizeRequest(BaseModel):
    instruments: list[InstrumentIdentityIn] = Field(default_factory=list)
    holdings: list[HoldingIn] = Field(default_factory=list)
    loan_currency: str = "USD"
    data_mode: DataMode = DataMode.HYBRID
    market_data_policy: MarketDataPolicyIn = Field(default_factory=MarketDataPolicyIn)
    client_supplied_quotes: dict[str, ClientQuoteIn] = Field(default_factory=dict)
    client_supplied_fx_rates: list[ClientFXRateIn] = Field(default_factory=list)


class MarketDataNormalizeResponse(BaseModel):
    market_data_model_version: str
    normalized_market_data: dict[str, Any]
    warnings: dict[str, list[str]]
    quality_scores: dict[str, float]
    fx_decisions: dict[str, Any]
    missing_data: list[str]
    evaluator_market_data: dict[str, Any] = Field(default_factory=dict)
    evaluator_key_to_stable_key: dict[str, str] = Field(default_factory=dict)


class EvaluateRequest(BaseModel):
    account_ref: str
    loan: LoanIn
    requested_draw_amount: float = Field(default=0.0, ge=0)
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]


class EvaluateResponse(BaseModel):
    result: dict[str, Any]


class PreTradeRiskCheckRequest(BaseModel):
    account_ref: str
    loan: LoanIn
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]
    actions: list[PortfolioActionIn]


class PreTradeRiskCheckResponse(BaseModel):
    result: LegacyPreTradeRiskCheckResult


class PortfolioActionCheckRequest(BaseModel):
    account_state: AccountStateIn
    policy: PolicyIn
    market_data: dict[str, MarketDataIn]
    proposed_action: PortfolioActionCheckIn = Field(
        validation_alias=AliasChoices("proposed_action", "action")
    )


class PortfolioActionCheckResponse(BaseModel):
    result: PortfolioActionCheckResultOut


class OriginateRequest(BaseModel):
    account_ref: str
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]


class DrawCheckRequest(BaseModel):
    account_ref: str
    current_loan: LoanIn
    requested_draw_amount: float = Field(ge=0)
    requested_repayment_amount: float = Field(default=0.0, ge=0)
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]


class MonitorRequest(BaseModel):
    account_ref: str
    loan: LoanIn
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]


class PreTradeCheckRequest(BaseModel):
    account_ref: str
    loan: LoanIn
    policy: PolicyIn
    holdings: list[HoldingIn]
    market_data: dict[str, MarketDataIn]
    proposed_holding_changes: list[HoldingIn] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "proposed_holding_changes", "holding_changes", "proposed_trades", "trades"
        ),
    )
    requested_draw_amount: float = 0.0
    requested_repayment_amount: float = 0.0


class LoanOut(BaseModel):
    principal: float = Field(ge=0)
    accrued_interest: float = Field(default=0.0, ge=0)
    fees: float = Field(default=0.0, ge=0)
    currency: str = "USD"


class AccountStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_ref: str
    holdings: list[HoldingIn]
    pledged_cash_balance: float
    loan: LoanOut
    approved_credit_limit: float
    available_credit: float
    last_margin_state: MarginState
    last_evaluation_time: datetime | None = None


class LegacyPreTradeRiskCheckResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    reduced_available_credit: float | None = None
    projected_margin_state: MarginState
    projected_holdings: list[HoldingIn]
    projected_evaluation: dict[str, Any]
    created_at: datetime


class PortfolioActionCheckResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    evaluation_result: dict[str, Any]
    projected_account_state: AccountStateOut
    created_at: datetime


class LifecycleResult(BaseModel):
    decision: LifecycleDecisionValue
    reason: str
    current_outstanding_balance: float
    current_available_credit: float
    projected_outstanding_balance: float | None = None
    projected_available_credit: float | None = None
    projected_margin_state: MarginState | None = None
    approved_credit_limit: float
    margin_state: MarginState
    required_cure_amount: float
    minimum_stressed_liquidation_value: float
    max_approved_draw_amount: float | None = None
    current_loan: LoanOut | None = None
    projected_loan: LoanOut | None = None
    risk_adjusted_collateral_value: float | None = None
    stressed_liquidation_value: float | None = None
    asset_results: list[Any] | None = None
    liquidation_plan: dict[str, Any] | None = None
    evaluation: dict[str, Any]
    audit_id: str
    created_at: datetime


class LifecycleResponse(BaseModel):
    result: LifecycleResult


class PreTradeResult(BaseModel):
    decision: RiskDecision
    reason: str
    current_outstanding_balance: float
    current_available_credit: float
    projected_outstanding_balance: float | None = None
    projected_available_credit: float | None = None
    projected_margin_state: MarginState | None = None
    reduced_available_credit: float | None = None
    approved_credit_limit: float
    margin_state: MarginState
    required_cure_amount: float
    minimum_stressed_liquidation_value: float
    current_loan: LoanOut
    projected_loan: LoanOut | None = None
    current_holdings: list[HoldingIn]
    projected_holdings: list[HoldingIn]
    liquidation_plan: dict[str, Any] | None = None
    evaluation: dict[str, Any]
    audit_id: str
    created_at: datetime


class PreTradeResponse(BaseModel):
    result: PreTradeResult
