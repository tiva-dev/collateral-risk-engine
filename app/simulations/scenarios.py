from __future__ import annotations

from dataclasses import replace

from app.core.models import MarketData
from app.risk.math_utils import clamp


def apply_market_shock(
    market: MarketData,
    price_shock: float = 0.0,
    volatility_multiplier: float = 1.0,
    spread_multiplier: float = 1.0,
    volume_multiplier: float = 1.0,
    data_quality_delta: float = 0.0,
) -> MarketData:
    new_price = max(0.0, market.last_price * (1.0 + price_shock))
    bid = market.bid
    ask = market.ask
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        spread = (ask - bid) * spread_multiplier
        bid = max(0.0, mid * (1.0 + price_shock) - spread / 2.0)
        ask = max(0.0, mid * (1.0 + price_shock) + spread / 2.0)
    return replace(
        market,
        last_price=new_price,
        bid=bid,
        ask=ask,
        average_daily_volume=None if market.average_daily_volume is None else market.average_daily_volume * volume_multiplier,
        average_dollar_volume=None if market.average_dollar_volume is None else market.average_dollar_volume * volume_multiplier * max(0.0, 1.0 + price_shock),
        volatility_30d=None if market.volatility_30d is None else market.volatility_30d * volatility_multiplier,
        volatility_90d=None if market.volatility_90d is None else market.volatility_90d * volatility_multiplier,
        intraday_volatility=None if market.intraday_volatility is None else market.intraday_volatility * volatility_multiplier,
        recent_return_1d=price_shock,
        data_quality_score=clamp(market.data_quality_score + data_quality_delta, 0.0, 1.0),
    )


SCENARIOS = {
    "normal": {},
    "broad_selloff": {
        "price_shock": -0.12,
        "volatility_multiplier": 1.6,
        "spread_multiplier": 2.0,
        "volume_multiplier": 0.80,
    },
    "liquidity_collapse": {
        "price_shock": -0.08,
        "volatility_multiplier": 2.0,
        "spread_multiplier": 5.0,
        "volume_multiplier": 0.20,
        "data_quality_delta": -0.10,
    },
    "overnight_gap": {
        "price_shock": -0.25,
        "volatility_multiplier": 1.8,
        "spread_multiplier": 3.0,
        "volume_multiplier": 0.60,
    },
}
