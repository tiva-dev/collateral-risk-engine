from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from app.core.enums import AssetType
from app.core.models import OrderBook
from app.market_data.identity import InstrumentIdentity


class MarketStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    HALTED = "halted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RawQuote:
    instrument: InstrumentIdentity
    local_price: float
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
    source: str = "provided_by_us"
    provider_name: str = "unknown"
    data_quality_score: float = 1.0
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.local_price <= 0:
            raise ValueError("raw quote local_price must be greater than 0")
        if self.bid is not None and self.bid <= 0:
            raise ValueError("raw quote bid must be greater than 0 when supplied")
        if self.ask is not None and self.ask <= 0:
            raise ValueError("raw quote ask must be greater than 0 when supplied")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("raw quote bid must be less than or equal to ask")
        if not 0 <= self.data_quality_score <= 1:
            raise ValueError("raw quote data_quality_score must be between 0 and 1")


@dataclass(frozen=True)
class FXRate:
    from_currency: str
    to_currency: str
    rate: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "provided_by_us"
    provider_name: str = "unknown"
    quality_score: float = 1.0
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("FX rate must be greater than 0")
        if not 0 <= self.quality_score <= 1:
            raise ValueError("FX quality_score must be between 0 and 1")


class MarketDataProvider(Protocol):
    provider_name: str

    def get_quote(self, instrument: InstrumentIdentity) -> RawQuote | None: ...

    def get_quotes(self, instruments: list[InstrumentIdentity]) -> dict[str, RawQuote]: ...

    def get_fx_rate(self, from_currency: str, to_currency: str) -> FXRate | None: ...

    def get_market_status(self, exchange: str) -> MarketStatus: ...


class BaseProvider:
    provider_name = "base"

    def get_quotes(self, instruments: list[InstrumentIdentity]) -> dict[str, RawQuote]:
        return {
            instrument.stable_key: quote
            for instrument in instruments
            if (quote := self.get_quote(instrument)) is not None
        }

    def get_quote(self, instrument: InstrumentIdentity) -> RawQuote | None:
        return None

    def get_fx_rate(self, from_currency: str, to_currency: str) -> FXRate | None:
        return None

    def get_market_status(self, exchange: str) -> MarketStatus:
        return MarketStatus.UNKNOWN


class MissingProvider(BaseProvider):
    """Fail-closed runtime provider used when no live client was injected."""

    provider_name = "missing_provider"


class ClientSuppliedProvider(BaseProvider):
    provider_name = "client_supplied"

    def __init__(
        self,
        quotes: dict[str, RawQuote] | None = None,
        fx_rates: dict[tuple[str, str], FXRate] | None = None,
        market_statuses: dict[str, MarketStatus] | None = None,
    ) -> None:
        self.quotes = quotes or {}
        self.fx_rates = {
            (src.upper(), dst.upper()): rate for (src, dst), rate in (fx_rates or {}).items()
        }
        self.market_statuses = {k.upper(): v for k, v in (market_statuses or {}).items()}

    def _quote_keys(self, instrument: InstrumentIdentity) -> list[str]:
        return [instrument.asset_id, instrument.stable_key, instrument.symbol]

    def get_quote(self, instrument: InstrumentIdentity) -> RawQuote | None:
        for key in self._quote_keys(instrument):
            if key in self.quotes:
                return replace(
                    self.quotes[key],
                    instrument=instrument,
                    source="client_supplied",
                    provider_name=self.provider_name,
                )
        return None

    def get_fx_rate(self, from_currency: str, to_currency: str) -> FXRate | None:
        pair = (from_currency.upper(), to_currency.upper())
        if pair in self.fx_rates:
            rate = self.fx_rates[pair]
            return replace(rate, source="client_supplied", provider_name=self.provider_name)
        inverse = (to_currency.upper(), from_currency.upper())
        if inverse in self.fx_rates and self.fx_rates[inverse].rate > 0:
            source_rate = self.fx_rates[inverse]
            return FXRate(
                from_currency=from_currency.upper(),
                to_currency=to_currency.upper(),
                rate=1.0 / source_rate.rate,
                timestamp=source_rate.timestamp,
                source="client_supplied",
                provider_name=self.provider_name,
                quality_score=source_rate.quality_score,
                warnings=[*source_rate.warnings, "fx_rate_inverted"],
            )
        return None

    def get_market_status(self, exchange: str) -> MarketStatus:
        return self.market_statuses.get(exchange.upper(), MarketStatus.UNKNOWN)


class MockEquityProvider(BaseProvider):
    provider_name = "mock_equity_provider"

    def __init__(
        self,
        quotes: dict[str, RawQuote] | None = None,
        market_statuses: dict[str, MarketStatus] | None = None,
    ) -> None:
        self.quotes = quotes or default_mock_quotes()
        self.market_statuses = {
            "NASDAQ": MarketStatus.OPEN,
            "NYSE": MarketStatus.OPEN,
            "NGX": MarketStatus.OPEN,
            "XPAR": MarketStatus.CLOSED,
            "XLON": MarketStatus.OPEN,
            "XTKS": MarketStatus.CLOSED,
            **{k.upper(): v for k, v in (market_statuses or {}).items()},
        }

    def get_quote(self, instrument: InstrumentIdentity) -> RawQuote | None:
        for key in (instrument.stable_key, f"{instrument.exchange.upper()}:{instrument.symbol.upper()}:{instrument.currency.upper()}", instrument.asset_id, instrument.symbol):
            if key in self.quotes:
                return replace(
                    self.quotes[key],
                    instrument=instrument,
                    source="provided_by_us",
                    provider_name=self.provider_name,
                )
        return None

    def get_market_status(self, exchange: str) -> MarketStatus:
        return self.market_statuses.get(exchange.upper(), MarketStatus.UNKNOWN)


class MockFXProvider(BaseProvider):
    provider_name = "mock_fx_provider"

    def __init__(self, rates: dict[tuple[str, str], FXRate] | None = None) -> None:
        now = datetime.now(timezone.utc)
        defaults = {
            ("NGN", "USD"): FXRate("NGN", "USD", 0.00067, now, "provided_by_us", self.provider_name, 0.95),
            ("EUR", "USD"): FXRate("EUR", "USD", 1.08, now, "provided_by_us", self.provider_name, 0.97),
            ("JPY", "USD"): FXRate("JPY", "USD", 0.0065, now, "provided_by_us", self.provider_name, 0.96),
            ("GBP", "USD"): FXRate("GBP", "USD", 1.25, now, "provided_by_us", self.provider_name, 0.97),
            ("USD", "NGN"): FXRate("USD", "NGN", 1492.0, now, "provided_by_us", self.provider_name, 0.94),
        }
        self.rates = {**defaults, **(rates or {})}
        self.rates = {(src.upper(), dst.upper()): rate for (src, dst), rate in self.rates.items()}

    def get_fx_rate(self, from_currency: str, to_currency: str) -> FXRate | None:
        src, dst = from_currency.upper(), to_currency.upper()
        if src == dst:
            return FXRate(src, dst, 1.0, provider_name=self.provider_name)
        if (src, dst) in self.rates:
            return replace(self.rates[(src, dst)], source="provided_by_us", provider_name=self.provider_name)
        if (dst, src) in self.rates and self.rates[(dst, src)].rate > 0:
            rate = self.rates[(dst, src)]
            return FXRate(
                src,
                dst,
                1.0 / rate.rate,
                rate.timestamp,
                "provided_by_us",
                self.provider_name,
                rate.quality_score,
                [*rate.warnings, "fx_rate_inverted"],
            )
        return None


def default_mock_quotes() -> dict[str, RawQuote]:
    now = datetime.now(timezone.utc)
    def inst(asset_id: str, symbol: str, exchange: str, currency: str, asset_type: AssetType = AssetType.LISTED_EQUITY) -> InstrumentIdentity:
        return InstrumentIdentity(asset_id, symbol, exchange, currency, asset_type)

    return {
        "NASDAQ:AAPL:USD": RawQuote(inst("AAPL", "AAPL", "NASDAQ", "USD"), 190.0, 189.98, 190.02, 60_000_000, 11_400_000_000, 0.28, 0.31, recent_return_1d=-0.012, timestamp=now, provider_name="mock_equity_provider", data_quality_score=0.99),
        "NASDAQ:NVDA:USD": RawQuote(inst("NVDA", "NVDA", "NASDAQ", "USD", AssetType.HIGH_VOLATILITY_EQUITY), 900.0, 899.5, 900.5, 45_000_000, 40_500_000_000, 0.72, 0.68, 0.85, -0.055, timestamp=now, provider_name="mock_equity_provider", data_quality_score=0.98),
        "NYSE:SPY:USD": RawQuote(inst("SPY", "SPY", "NYSE", "USD", AssetType.ETF), 520.0, 519.99, 520.01, 75_000_000, 39_000_000_000, 0.18, 0.20, recent_return_1d=-0.004, timestamp=now, provider_name="mock_equity_provider", data_quality_score=0.99),
        "NGX:MTNN:NGN": RawQuote(inst("MTNN", "MTNN", "NGX", "NGN"), 275.0, 274.0, 276.0, 5_000_000, 1_375_000_000, 0.34, 0.38, recent_return_1d=0.006, timestamp=now, provider_name="mock_equity_provider", data_quality_score=0.93),
        "XPAR:AIR:EUR": RawQuote(inst("AIR", "AIR", "XPAR", "EUR"), 155.0, 154.9, 155.1, 2_300_000, 356_500_000, 0.24, 0.27, recent_return_1d=-0.003, timestamp=now, provider_name="mock_equity_provider", data_quality_score=0.94),
        "XTKS:7203:JPY": RawQuote(inst("7203", "7203", "XTKS", "JPY"), 3300.0, 3298.0, 3302.0, 20_000_000, 66_000_000_000, 0.26, 0.30, recent_return_1d=0.002, timestamp=now, provider_name="mock_equity_provider", data_quality_score=0.94),
    }
