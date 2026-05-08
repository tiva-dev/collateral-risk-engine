from __future__ import annotations

import math
from statistics import NormalDist


TRADING_DAYS_PER_YEAR = 252.0


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0:
        return default
    return numerator / denominator


def normal_expected_shortfall_loss(
    annualized_volatility: float,
    horizon_days: float,
    confidence: float,
) -> float:
    """Parametric one-sided expected-shortfall loss under a normal approximation.

    This is not the only stress method used by the engine; it is a compact
    baseline used inside the dynamic buffer and LTV adjustments.
    """
    vol = max(0.0, annualized_volatility)
    horizon = max(1e-6, horizon_days)
    alpha = clamp(confidence, 0.50, 0.9999)
    sigma_h = vol * math.sqrt(horizon / TRADING_DAYS_PER_YEAR)
    if sigma_h == 0:
        return 0.0
    z = NormalDist().inv_cdf(alpha)
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    es_multiplier = pdf / max(1e-9, 1.0 - alpha)
    return max(0.0, es_multiplier * sigma_h)


def sqrt_impact(position_value: float, average_dollar_volume: float | None) -> float:
    """Square-root style market impact proxy.

    The impact estimate is deliberately conservative when ADV is missing.
    """
    if average_dollar_volume is None or average_dollar_volume <= 0:
        return 0.25
    participation = max(0.0, position_value) / max(1.0, average_dollar_volume)
    return clamp(0.08 * math.sqrt(participation), 0.0, 0.65)


def round_money(value: float) -> float:
    return round(float(value), 2)
