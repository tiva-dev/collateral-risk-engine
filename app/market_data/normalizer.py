from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.models import MarketData, OrderBook
from app.market_data.identity import InstrumentIdentity
from app.market_data.providers import FXRate, MarketStatus, RawQuote
from app.market_data.quality import clamp_score


@dataclass(frozen=True)
class NormalizedMarketData:
    instrument: InstrumentIdentity
    local_price: float
    local_currency: str
    loan_currency: str
    converted_price: float
    bid: float | None = None
    ask: float | None = None
    average_daily_volume: float | None = None
    average_dollar_volume: float | None = None
    volatility_30d: float | None = None
    volatility_90d: float | None = None
    intraday_volatility: float | None = None
    recent_return_1d: float | None = None
    order_book: OrderBook | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "unknown"
    provider_name: str = "unknown"
    exchange: str = "UNKNOWN"
    market_status: MarketStatus = MarketStatus.UNKNOWN
    data_quality_score: float = 1.0
    warnings: list[str] = field(default_factory=list)
    fx_rate_used: float | None = None
    fx_source: str | None = None
    fx_timestamp: datetime | None = None
    fx_quality_score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def asset_id(self) -> str:
        return self.instrument.asset_id

    @property
    def stable_key(self) -> str:
        return self.instrument.stable_key

    def to_market_data(self) -> MarketData:
        return MarketData(
            asset_id=self.instrument.asset_id,
            last_price=self.converted_price,
            bid=None if self.bid is None else self._convert_value(self.bid),
            ask=None if self.ask is None else self._convert_value(self.ask),
            average_daily_volume=self.average_daily_volume,
            average_dollar_volume=self.average_dollar_volume,
            volatility_30d=self.volatility_30d,
            volatility_90d=self.volatility_90d,
            intraday_volatility=self.intraday_volatility,
            recent_return_1d=self.recent_return_1d,
            timestamp=self.timestamp,
            data_quality_score=clamp_score(self.data_quality_score),
            halted=self.market_status == MarketStatus.HALTED or "halted" in self.warnings,
            order_book=self.order_book,
            metadata={
                **self.metadata,
                "instrument": {
                    "stable_key": self.stable_key,
                    "symbol": self.instrument.symbol,
                    "exchange": self.exchange,
                    "currency": self.local_currency,
                    "isin": self.instrument.isin,
                    "figi": self.instrument.figi,
                    "provider_symbol": self.instrument.provider_symbol,
                },
                "market_data_source": self.source,
                "provider_name": self.provider_name,
                "market_status": self.market_status.value,
                "warnings": self.warnings,
                "loan_currency": self.loan_currency,
                "local_price": self.local_price,
                "fx_rate_used": self.fx_rate_used,
                "fx_source": self.fx_source,
                "fx_timestamp": self.fx_timestamp.isoformat() if self.fx_timestamp else None,
                "fx_quality_score": self.fx_quality_score,
            },
        )

    def _convert_value(self, value: float) -> float:
        if self.fx_rate_used is None:
            return value
        return value * self.fx_rate_used


def normalize_quote(
    quote: RawQuote,
    loan_currency: str,
    market_status: MarketStatus,
    quote_quality_score: float,
    warnings: list[str],
    fx_rate: FXRate | None = None,
    fx_quality_score: float | None = None,
) -> NormalizedMarketData:
    fx_rate_value = None if fx_rate is None or fx_rate.rate == 1.0 and quote.instrument.currency.upper() == loan_currency.upper() else fx_rate.rate
    converted_price = quote.local_price if fx_rate_value is None else quote.local_price * fx_rate_value
    average_dollar_volume = quote.average_dollar_volume
    if average_dollar_volume is not None and fx_rate_value is not None:
        average_dollar_volume *= fx_rate_value
    return NormalizedMarketData(
        instrument=quote.instrument,
        local_price=quote.local_price,
        local_currency=quote.instrument.currency.upper(),
        loan_currency=loan_currency.upper(),
        converted_price=converted_price,
        bid=quote.bid,
        ask=quote.ask,
        average_daily_volume=quote.average_daily_volume,
        average_dollar_volume=average_dollar_volume,
        volatility_30d=quote.volatility_30d,
        volatility_90d=quote.volatility_90d,
        intraday_volatility=quote.intraday_volatility,
        recent_return_1d=quote.recent_return_1d,
        order_book=quote.order_book,
        timestamp=quote.timestamp,
        source=quote.source,
        provider_name=quote.provider_name,
        exchange=quote.instrument.exchange.upper(),
        market_status=market_status,
        data_quality_score=clamp_score(quote_quality_score),
        warnings=warnings,
        fx_rate_used=fx_rate_value,
        fx_source=None if fx_rate_value is None else fx_rate.source,
        fx_timestamp=None if fx_rate_value is None else fx_rate.timestamp,
        fx_quality_score=None if fx_rate_value is None else fx_quality_score,
        metadata=quote.metadata,
    )
