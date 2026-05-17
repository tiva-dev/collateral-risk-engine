from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from app.core.enums import DataMode
from app.core.models import Holding, MarketData
from app.market_data.fx import FXSelector
from app.market_data.identity import InstrumentIdentity
from app.market_data.normalizer import NormalizedMarketData, normalize_quote
from app.market_data.policy import MarketDataPolicy
from app.market_data.providers import (
    ClientSuppliedProvider,
    FXRate,
    MarketDataProvider,
    MarketStatus,
    MockEquityProvider,
    MockFXProvider,
    RawQuote,
)
from app.market_data.quality import age_minutes, clamp_score


@dataclass(frozen=True)
class MarketDataAggregationResult:
    normalized_market_data: dict[str, NormalizedMarketData]
    quality_report: dict[str, float]
    warnings_by_instrument: dict[str, list[str]]
    missing_data: list[str] = field(default_factory=list)

    def to_core_market_data(self) -> dict[str, MarketData]:
        return {
            key: normalized.to_market_data()
            for key, normalized in self.normalized_market_data.items()
        }


class ProviderRouter:
    def __init__(
        self,
        client_provider: MarketDataProvider | None = None,
        equity_provider: MarketDataProvider | None = None,
        fx_provider: MarketDataProvider | None = None,
    ) -> None:
        self.client_provider = client_provider
        self.equity_provider = equity_provider or MockEquityProvider()
        self.fx_provider = fx_provider or MockFXProvider()

    def choose_quote_provider(
        self,
        data_mode: DataMode,
        instrument: InstrumentIdentity,
        policy: MarketDataPolicy,
        now: datetime | None = None,
    ) -> tuple[RawQuote | None, MarketStatus, list[str]]:
        warnings: list[str] = []
        client_quote = self.client_provider.get_quote(instrument) if self.client_provider else None
        provider_quote = self.equity_provider.get_quote(instrument) if self.equity_provider else None

        if data_mode == DataMode.CLIENT_SUPPLIED:
            status = self._market_status(instrument.exchange)
            return client_quote, status, [] if client_quote else ["missing_client_supplied_quote"]
        if data_mode == DataMode.PROVIDED_BY_US:
            status = self._provider_market_status(instrument.exchange)
            return provider_quote, status, [] if provider_quote else ["missing_provider_quote"]

        if client_quote is not None:
            quality, quote_warnings = score_quote(client_quote, self._client_market_status(instrument.exchange), policy, now)
            if quality >= policy.minimum_quote_quality_score:
                status = self._client_market_status(instrument.exchange)
                return client_quote, status, quote_warnings
            warnings.extend(quote_warnings or ["client_quote_below_quality_threshold"])
        if policy.allow_fallback_provider and provider_quote is not None:
            status = self._provider_market_status(instrument.exchange)
            return provider_quote, status, [*warnings, "fallback_provider_quote_used"] if warnings else []
        status = self._market_status(instrument.exchange)
        return client_quote, status, warnings or ["missing_quote"]

    def _client_market_status(self, exchange: str) -> MarketStatus:
        if self.client_provider:
            status = self.client_provider.get_market_status(exchange)
            if status != MarketStatus.UNKNOWN:
                return status
        return self._provider_market_status(exchange)

    def _provider_market_status(self, exchange: str) -> MarketStatus:
        return self.equity_provider.get_market_status(exchange) if self.equity_provider else MarketStatus.UNKNOWN

    def _market_status(self, exchange: str) -> MarketStatus:
        status = self._client_market_status(exchange)
        if status != MarketStatus.UNKNOWN:
            return status
        return self._provider_market_status(exchange)


def score_quote(
    quote: RawQuote,
    market_status: MarketStatus,
    policy: MarketDataPolicy,
    now: datetime | None = None,
) -> tuple[float, list[str]]:
    warnings = list(quote.warnings)
    quality = clamp_score(quote.data_quality_score)
    max_age = policy.max_quote_age_minutes_by_exchange.get(
        quote.instrument.exchange.upper(),
        policy.max_quote_age_minutes_by_asset_type.get(quote.instrument.asset_type, 60),
    )
    if age_minutes(quote.timestamp, now) > max_age:
        quality = clamp_score(quality * (1.0 - policy.stale_quote_haircut))
        warnings.append("stale_quote")
    if market_status == MarketStatus.CLOSED:
        warnings.append("market_closed")
    elif market_status == MarketStatus.HALTED:
        warnings.append("halted")
        quality = clamp_score(min(quality, policy.minimum_quote_quality_score))
    elif market_status == MarketStatus.UNKNOWN:
        warnings.append("market_status_unknown")
    if quote.local_price <= 0:
        quality = 0.0
        warnings.append("invalid_price")
    if quality < policy.minimum_quote_quality_score:
        warnings.append("quote_quality_below_threshold")
    return quality, warnings


class MarketDataAggregator:
    def __init__(
        self,
        provider_router: ProviderRouter | None = None,
        client_provider: MarketDataProvider | None = None,
        equity_provider: MarketDataProvider | None = None,
        fx_provider: MarketDataProvider | None = None,
    ) -> None:
        self.router = provider_router or ProviderRouter(client_provider, equity_provider, fx_provider)

    def normalize(
        self,
        instruments: list[InstrumentIdentity] | None = None,
        *,
        holdings: list[Holding] | None = None,
        loan_currency: str = "USD",
        market_data_policy: MarketDataPolicy | None = None,
        data_mode: DataMode = DataMode.HYBRID,
        client_supplied_quotes: Mapping[str, RawQuote] | None = None,
        client_supplied_fx_rates: Mapping[tuple[str, str], FXRate] | None = None,
        provider_registry: dict[str, MarketDataProvider] | None = None,
        now: datetime | None = None,
    ) -> MarketDataAggregationResult:
        policy = market_data_policy or MarketDataPolicy()
        identities = instruments or [InstrumentIdentity.from_holding(holding) for holding in (holdings or [])]
        client_provider = ClientSuppliedProvider(dict(client_supplied_quotes or {}), dict(client_supplied_fx_rates or {}))
        equity_provider = (provider_registry or {}).get("equity") or self.router.equity_provider
        fx_provider = (provider_registry or {}).get("fx") or self.router.fx_provider
        router = ProviderRouter(client_provider, equity_provider, fx_provider)
        fx_selector = FXSelector(client_provider, fx_provider)

        normalized: dict[str, NormalizedMarketData] = {}
        quality_report: dict[str, float] = {}
        warnings_by_instrument: dict[str, list[str]] = {}
        missing_data: list[str] = []

        for instrument in identities:
            quote, status, router_warnings = router.choose_quote_provider(data_mode, instrument, policy, now)
            key = instrument.asset_id or instrument.stable_key
            if quote is None:
                warnings_by_instrument[key] = router_warnings
                missing_data.append(key)
                quality_report[key] = 0.0
                continue

            quote_quality, warnings = score_quote(quote, status, policy, now)
            warnings = [*router_warnings, *warnings]
            fx_rate = None
            fx_quality = None
            if instrument.currency.upper() != loan_currency.upper():
                allow_client_fx = data_mode in {DataMode.CLIENT_SUPPLIED, DataMode.HYBRID}
                allow_provider_fx = data_mode == DataMode.PROVIDED_BY_US or (
                    data_mode == DataMode.HYBRID and policy.fx.allow_fallback_provider
                )
                fx_decision = fx_selector.select_rate(
                    instrument.currency,
                    loan_currency,
                    policy.fx,
                    allow_client=allow_client_fx,
                    allow_provider=allow_provider_fx,
                    now=now,
                )
                warnings.extend(fx_decision.warnings)
                fx_rate = fx_decision.rate
                fx_quality = fx_decision.quality_score
                if fx_decision.missing_required_fx or fx_rate is None:
                    quote_quality = min(quote_quality, 0.05)
                else:
                    quote_quality = clamp_score(quote_quality * fx_quality)
                    if fx_quality < policy.fx.minimum_fx_quality_score:
                        quote_quality = clamp_score(quote_quality * (1.0 - policy.fx.stale_fx_haircut))
            normalized_result = normalize_quote(
                quote,
                loan_currency,
                status,
                quote_quality,
                warnings,
                fx_rate,
                fx_quality,
            )
            normalized[key] = normalized_result
            quality_report[key] = normalized_result.data_quality_score
            warnings_by_instrument[key] = normalized_result.warnings

        return MarketDataAggregationResult(normalized, quality_report, warnings_by_instrument, missing_data)
