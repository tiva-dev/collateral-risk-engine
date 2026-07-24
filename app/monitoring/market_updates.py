from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from threading import RLock

from app.market_data.identity import InstrumentIdentity
from app.market_data.providers import BaseProvider, FXRate, MarketStatus, RawQuote


@dataclass
class CachedMarketData:
    quotes: dict[str, RawQuote] = field(default_factory=dict)
    fx_rates: dict[tuple[str, str], FXRate] = field(default_factory=dict)
    latest_update_time: datetime | None = None
    source: str = "internal"


class MarketDataCache(ABC):
    """Cache contract for latest internal market data updates.

    The in-memory adapter is only for development/test deployments. Production
    systems should replace it with a durable/cache store appropriate to their
    consistency and replay needs.
    """

    @abstractmethod
    def merge(
        self,
        quotes: dict[str, RawQuote],
        fx_rates: dict[tuple[str, str], FXRate],
        source: str,
        received_at: datetime | None = None,
    ) -> CachedMarketData: ...

    @abstractmethod
    def is_symbol_ambiguous(self, symbol: str) -> bool: ...

    @abstractmethod
    def provider(self) -> BaseProvider: ...

    @abstractmethod
    def snapshot(self) -> CachedMarketData: ...


class CachedMarketDataProvider(BaseProvider):
    provider_name = "monitoring_market_data_cache"

    def __init__(self, cache: InMemoryMarketDataCache) -> None:
        self.cache = cache

    def get_quote(self, instrument: InstrumentIdentity) -> RawQuote | None:
        data = self.cache.snapshot()
        keys = [instrument.stable_key, instrument.asset_id, instrument.asset_id.upper()]
        if not self.cache.is_symbol_ambiguous(instrument.symbol):
            keys.extend([instrument.symbol, instrument.symbol.upper()])
        for key in keys:
            if key in data.quotes:
                quote = data.quotes[key]
                return replace(
                    quote, instrument=instrument, provider_name=self.provider_name
                )
        return None

    def get_fx_rate(self, from_currency: str, to_currency: str) -> FXRate | None:
        data = self.cache.snapshot()
        pair = (from_currency.upper(), to_currency.upper())
        if pair in data.fx_rates:
            return data.fx_rates[pair]
        inverse = (to_currency.upper(), from_currency.upper())
        if inverse in data.fx_rates and data.fx_rates[inverse].rate > 0:
            source = data.fx_rates[inverse]
            return FXRate(
                pair[0],
                pair[1],
                1.0 / source.rate,
                source.timestamp,
                source.source,
                self.provider_name,
                source.quality_score,
                [*source.warnings, "fx_rate_inverted"],
            )
        return None

    def get_market_status(self, exchange: str) -> MarketStatus:
        return MarketStatus.OPEN


class InMemoryMarketDataCache(MarketDataCache):
    def __init__(self) -> None:
        self._data = CachedMarketData()
        self._lock = RLock()
        self._provider = CachedMarketDataProvider(self)
        self._symbol_to_stable_keys: dict[str, set[str]] = {}

    def merge(
        self,
        quotes: dict[str, RawQuote],
        fx_rates: dict[tuple[str, str], FXRate],
        source: str,
        received_at: datetime | None = None,
    ) -> CachedMarketData:
        with self._lock:
            for key, quote in quotes.items():
                symbol = quote.instrument.symbol.upper()
                self._symbol_to_stable_keys.setdefault(symbol, set()).add(
                    quote.instrument.stable_key.upper()
                )
                ambiguous = len(self._symbol_to_stable_keys[symbol]) > 1
                keys = {
                    quote.instrument.asset_id,
                    quote.instrument.asset_id.upper(),
                    quote.instrument.stable_key,
                }
                if not ambiguous or key.upper() != symbol:
                    keys.add(key)
                if not ambiguous:
                    keys.update(
                        {quote.instrument.symbol, quote.instrument.symbol.upper()}
                    )
                else:
                    self._data.quotes.pop(quote.instrument.symbol, None)
                    self._data.quotes.pop(quote.instrument.symbol.upper(), None)
                for quote_key in keys:
                    self._data.quotes[quote_key] = quote
            for (src, dst), rate in fx_rates.items():
                self._data.fx_rates[(src.upper(), dst.upper())] = rate
            self._data.source = source
            self._data.latest_update_time = received_at or datetime.now(UTC)
            return self.snapshot()

    def is_symbol_ambiguous(self, symbol: str) -> bool:
        with self._lock:
            return len(self._symbol_to_stable_keys.get(symbol.upper(), set())) > 1

    def provider(self) -> BaseProvider:
        return self._provider

    def snapshot(self) -> CachedMarketData:
        with self._lock:
            return CachedMarketData(
                dict(self._data.quotes),
                dict(self._data.fx_rates),
                self._data.latest_update_time,
                self._data.source,
            )
