from __future__ import annotations

from typing import Mapping

from app.core.enums import DataMode
from app.core.models import Holding, MarketData
from app.market_data.aggregator import MarketDataAggregator, MarketDataAggregationResult
from app.market_data.identity import InstrumentIdentity
from app.market_data.policy import MarketDataPolicy
from app.market_data.providers import FXRate, RawQuote


def normalize_market_data_for_evaluation(
    *,
    holdings: list[Holding] | None = None,
    instruments: list[InstrumentIdentity] | None = None,
    loan_currency: str = "USD",
    data_mode: DataMode = DataMode.HYBRID,
    market_data_policy: MarketDataPolicy | None = None,
    client_supplied_quotes: Mapping[str, RawQuote] | None = None,
    client_supplied_fx_rates: Mapping[tuple[str, str], FXRate] | None = None,
    aggregator: MarketDataAggregator | None = None,
) -> tuple[dict[str, MarketData], MarketDataAggregationResult]:
    engine = aggregator or MarketDataAggregator()
    result = engine.normalize(
        instruments=instruments,
        holdings=holdings,
        loan_currency=loan_currency,
        data_mode=data_mode,
        market_data_policy=market_data_policy,
        client_supplied_quotes=client_supplied_quotes,
        client_supplied_fx_rates=client_supplied_fx_rates,
    )
    return result.to_core_market_data(), result
