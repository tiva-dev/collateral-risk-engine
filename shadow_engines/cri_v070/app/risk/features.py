from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from statistics import fmean

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class HistoricalRiskFeatures:
    observation_count: int
    volatility_30d: float | None
    volatility_90d: float | None
    volatility_252d: float | None
    max_drawdown_252d: float | None
    max_gap_252d: float | None
    average_daily_volume_30d: float | None
    average_dollar_volume_30d: float | None
    volume_observation_count_30d: int
    volume_coverage_30d: float
    inconsistent_zero_volume_count_30d: int

    @property
    def data_driven_high_risk(self) -> bool:
        return any(
            (
                (self.volatility_30d or 0.0) >= 0.60,
                (self.volatility_90d or 0.0) >= 0.55,
                (self.max_drawdown_252d or 0.0) >= 0.35,
                (self.max_gap_252d or 0.0) >= 0.12,
            )
        )


def _realized_volatility(prices: list[float], window: int) -> float | None:
    usable = [float(price) for price in prices if price is not None and price > 0]
    usable = usable[-(window + 1) :]
    if len(usable) < 3:
        return None
    returns = [
        math.log(current / previous)
        for previous, current in pairwise(usable)
        if previous > 0 and current > 0
    ]
    if len(returns) < 2:
        return None
    mean = fmean(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _max_drawdown(prices: list[float], window: int = 252) -> float | None:
    usable = [float(price) for price in prices if price is not None and price > 0][-window:]
    if len(usable) < 2:
        return None
    peak = usable[0]
    worst = 0.0
    for price in usable:
        peak = max(peak, price)
        worst = max(worst, 1.0 - price / peak)
    return worst


def _max_gap(prices: list[float], window: int = 252) -> float | None:
    usable = [float(price) for price in prices if price is not None and price > 0][
        -(window + 1) :
    ]
    if len(usable) < 2:
        return None
    return max(
        abs(current / previous - 1.0)
        for previous, current in pairwise(usable)
    )


def calculate_historical_risk_features(
    prices: list[float],
    volumes: list[float | None] | None = None,
) -> HistoricalRiskFeatures:
    volumes = list(volumes or [])
    recent_volumes = volumes[-30:]
    recent_prices = [float(value) for value in prices[-len(recent_volumes) :]]
    observed_volumes: list[float] = []
    paired_values: list[float] = []
    inconsistent_zero_count = 0
    for index, (volume, price) in enumerate(
        zip(recent_volumes, recent_prices, strict=False)
    ):
        if volume is None or volume < 0:
            continue
        price_changed = index > 0 and price != recent_prices[index - 1]
        if volume == 0 and price_changed:
            inconsistent_zero_count += 1
            continue
        observed_volumes.append(float(volume))
        if price > 0:
            paired_values.append(float(volume) * price)
    if inconsistent_zero_count and not any(value > 0 for value in observed_volumes):
        observed_volumes = []
        paired_values = []
    denominator = len(recent_volumes)
    return HistoricalRiskFeatures(
        observation_count=len([price for price in prices if price is not None]),
        volatility_30d=_realized_volatility(prices, 30),
        volatility_90d=_realized_volatility(prices, 90),
        volatility_252d=_realized_volatility(prices, 252),
        max_drawdown_252d=_max_drawdown(prices),
        max_gap_252d=_max_gap(prices),
        average_daily_volume_30d=(
            fmean(observed_volumes) if observed_volumes else None
        ),
        average_dollar_volume_30d=(
            fmean(paired_values) if paired_values else None
        ),
        volume_observation_count_30d=len(observed_volumes),
        volume_coverage_30d=(
            len(observed_volumes) / denominator if denominator else 0.0
        ),
        inconsistent_zero_volume_count_30d=inconsistent_zero_count,
    )
