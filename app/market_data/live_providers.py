from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.models import OrderBook, OrderBookLevel
from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.config import load_config
from app.historical_data.ngnmarket import NGNMarketHistoricalProvider
from app.historical_data.providers import ProviderError, validate_provider_url
from app.market_data.identity import InstrumentIdentity
from app.risk.features import calculate_historical_risk_features

from .providers import (
    BaseProvider,
    FXRate,
    MarketStatus,
    RawQuote,
)


class ExchangeRoutingProvider(BaseProvider):
    """Route instruments to market-specific live providers."""

    provider_name = "exchange_router"

    def __init__(self, providers: dict[str, BaseProvider]) -> None:
        self.providers = {key.upper(): value for key, value in providers.items()}

    def _provider(self, exchange: str) -> BaseProvider | None:
        exchange = exchange.upper()
        if exchange in {"NASDAQ", "NYSE", "ARCA", "US"}:
            return self.providers.get("US")
        return self.providers.get(exchange)

    def get_quote(self, instrument: InstrumentIdentity) -> RawQuote | None:
        provider = self._provider(instrument.exchange)
        if provider is None and instrument.exchange.upper() == "UNKNOWN":
            provider = self.providers.get(
                "NGX" if instrument.currency.upper() == "NGN" else "US"
            )
        return provider.get_quote(instrument) if provider else None

    def get_fx_rate(self, from_currency: str, to_currency: str):
        for provider in self.providers.values():
            if rate := provider.get_fx_rate(from_currency, to_currency):
                return rate
        return None

    def get_market_status(self, exchange: str) -> MarketStatus:
        provider = self._provider(exchange)
        return provider.get_market_status(exchange) if provider else MarketStatus.UNKNOWN


class AlpacaLiveEquityProvider(BaseProvider):
    """Alpaca SIP snapshot plus CRI-calculated historical risk features."""

    provider_name = "alpaca_sip"

    def __init__(self, feed: str = "sip") -> None:
        self.config = load_config()
        self.feed = feed
        self.history = AlpacaTradingHistoricalProvider()

    def _headers(self) -> dict[str, str]:
        return self.history.auth_headers()

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = (
            self.config.alpaca_base_url.rstrip("/")
            + path
            + "?"
            + urllib.parse.urlencode(params)
        )
        validate_provider_url(url, {"data.alpaca.markets"})
        request = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
                return json.loads(response.read().decode())
        except Exception as exc:
            raise ProviderError(
                "Alpaca live market-data request failed",
                provider=self.provider_name,
            ) from exc

    def get_quote(self, instrument: InstrumentIdentity) -> RawQuote | None:
        symbol = instrument.provider_symbol or instrument.symbol
        snapshot = self._request(
            f"/v2/stocks/{urllib.parse.quote(symbol)}/snapshot",
            {"feed": self.feed},
        )
        latest_trade = snapshot.get("latestTrade") or {}
        latest_quote = snapshot.get("latestQuote") or {}
        daily_bar = snapshot.get("dailyBar") or snapshot.get("minuteBar") or {}
        price = (
            latest_trade.get("p")
            or daily_bar.get("c")
            or latest_quote.get("ap")
            or latest_quote.get("bp")
        )
        if price is None or float(price) <= 0:
            return None

        end = datetime.now(UTC).date()
        start = end - timedelta(days=370)
        series = self.history.fetch_equity_history(
            symbol,
            start,
            end,
            interval="1Day",
            feed=self.feed,
        )
        prices = [
            bar.adjusted_close if bar.adjusted_close is not None else bar.close
            for bar in series.bars
        ]
        volumes = [bar.volume for bar in series.bars]
        features = calculate_historical_risk_features(prices, volumes)
        timestamp_value = (
            latest_quote.get("t")
            or latest_trade.get("t")
            or daily_bar.get("t")
            or datetime.now(UTC).isoformat()
        )
        timestamp = datetime.fromisoformat(str(timestamp_value))
        bid = latest_quote.get("bp")
        ask = latest_quote.get("ap")
        bid_size = float(latest_quote.get("bs") or 0)
        ask_size = float(latest_quote.get("as") or 0)
        order_book = None
        if bid and ask:
            order_book = OrderBook(
                bids=[OrderBookLevel(float(bid), bid_size)],
                asks=[OrderBookLevel(float(ask), ask_size)],
            )
        return RawQuote(
            instrument=instrument,
            local_price=float(price),
            bid=float(bid) if bid else None,
            ask=float(ask) if ask else None,
            average_daily_volume=features.average_daily_volume_30d,
            average_dollar_volume=features.average_dollar_volume_30d,
            volatility_30d=features.volatility_30d,
            volatility_90d=features.volatility_90d,
            volatility_252d=features.volatility_252d,
            max_drawdown_252d=features.max_drawdown_252d,
            max_gap_252d=features.max_gap_252d,
            recent_return_1d=(
                prices[-1] / prices[-2] - 1.0 if len(prices) >= 2 else None
            ),
            order_book=order_book,
            timestamp=timestamp,
            provider_name=self.provider_name,
            metadata={
                "feed": self.feed,
                "depth": "top_of_book",
                "volume_coverage_30d": features.volume_coverage_30d,
                "inconsistent_zero_volume_count_30d": (
                    features.inconsistent_zero_volume_count_30d
                ),
            },
        )

    def get_market_status(self, exchange: str) -> MarketStatus:
        return MarketStatus.UNKNOWN


class NGNMarketDailyEquityProvider(BaseProvider):
    """Latest NGX close and CRI-calculated one-year risk/liquidity features."""

    provider_name = "ngnmarket_daily"

    def __init__(self) -> None:
        self.history = NGNMarketHistoricalProvider()

    def get_quote(self, instrument: InstrumentIdentity) -> RawQuote | None:
        symbol = instrument.provider_symbol or instrument.symbol
        end = datetime.now(UTC).date()
        start = end - timedelta(days=370)
        series = self.history.fetch_equity_history(symbol, start, end, interval="1d")
        if not series.bars:
            return None
        latest = series.bars[-1]
        prices = [
            bar.adjusted_close if bar.adjusted_close is not None else bar.close
            for bar in series.bars
        ]
        volumes = [bar.volume for bar in series.bars]
        features = calculate_historical_risk_features(prices, volumes)
        timestamp = (
            latest.timestamp
            if isinstance(latest.timestamp, datetime)
            else datetime.combine(latest.timestamp, datetime.min.time(), tzinfo=UTC)
        )
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return RawQuote(
            instrument=instrument,
            local_price=float(prices[-1]),
            average_daily_volume=features.average_daily_volume_30d,
            average_dollar_volume=features.average_dollar_volume_30d,
            volatility_30d=features.volatility_30d,
            volatility_90d=features.volatility_90d,
            volatility_252d=features.volatility_252d,
            max_drawdown_252d=features.max_drawdown_252d,
            max_gap_252d=features.max_gap_252d,
            recent_return_1d=(
                prices[-1] / prices[-2] - 1.0 if len(prices) >= 2 else None
            ),
            timestamp=timestamp,
            provider_name=self.provider_name,
            metadata={
                "monitoring_cadence": "daily",
                "depth": "not_available",
                "volume_coverage_30d": features.volume_coverage_30d,
                "inconsistent_zero_volume_count_30d": (
                    features.inconsistent_zero_volume_count_30d
                ),
            },
        )

    def get_market_status(self, exchange: str) -> MarketStatus:
        return MarketStatus.UNKNOWN


class AlphaVantageDailyFXProvider(BaseProvider):
    """Latest provider FX close with an explicit daily-data warning."""

    provider_name = "alpha_vantage_daily_fx"

    def __init__(self) -> None:
        self.history = AlphaVantageHistoricalProvider()

    def get_fx_rate(self, from_currency: str, to_currency: str) -> FXRate | None:
        source = from_currency.upper()
        target = to_currency.upper()
        if source == target:
            return FXRate(
                source,
                target,
                1.0,
                timestamp=datetime.now(UTC),
                provider_name=self.provider_name,
            )
        end = datetime.now(UTC).date()
        start = end - timedelta(days=10)
        series = self.history.fetch_fx_history(source, target, start, end)
        if not series.rates:
            return None
        latest = max(series.rates, key=lambda item: item.timestamp)
        timestamp = (
            latest.timestamp
            if isinstance(latest.timestamp, datetime)
            else datetime.combine(latest.timestamp, datetime.min.time(), tzinfo=UTC)
        )
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return FXRate(
            source,
            target,
            latest.rate,
            timestamp=timestamp,
            source="provided_by_us",
            provider_name=self.provider_name,
            quality_score=latest.quality_score,
            warnings=[
                *latest.warnings,
                "daily_fx_close_not_intraday",
            ],
        )


def configured_live_equity_provider() -> BaseProvider | None:
    config = load_config()
    providers: dict[str, BaseProvider] = {}
    if config.alpaca_api_key and config.alpaca_secret_key:
        feed = os.getenv("ALPACA_DATA_FEED", "iex").strip().lower()
        if feed not in {"iex", "sip", "boats", "otc"}:
            raise ValueError(
                "ALPACA_DATA_FEED must be one of: iex, sip, boats, otc"
            )
        providers["US"] = AlpacaLiveEquityProvider(feed=feed)
    if config.ngnmarket_api_key:
        providers["NGX"] = NGNMarketDailyEquityProvider()
    return ExchangeRoutingProvider(providers) if providers else None


def configured_live_fx_provider() -> BaseProvider | None:
    config = load_config()
    if config.alpha_vantage_api_key:
        return AlphaVantageDailyFXProvider()
    return None
