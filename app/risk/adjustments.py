from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.enums import AssetType, RiskAppetite
from app.core.models import Holding, MarketData, Policy, RiskAdjustmentBreakdown
from app.risk.math_utils import clamp, normal_expected_shortfall_loss, safe_div, sqrt_impact


@dataclass(frozen=True)
class RawRiskInputs:
    annualized_volatility: float
    spread_rate: float
    position_to_adv: float
    liquidation_horizon_days: float
    concentration: float
    confidence: float


RISK_APPETITE_CONFIDENCE = {
    RiskAppetite.CONSERVATIVE: 0.995,
    RiskAppetite.BALANCED: 0.990,
    RiskAppetite.AGGRESSIVE: 0.975,
}

STRESS_SEVERITY_BY_APPETITE = {
    RiskAppetite.CONSERVATIVE: 1.00,
    RiskAppetite.BALANCED: 0.78,
    RiskAppetite.AGGRESSIVE: 0.58,
}

BASE_HORIZON_DAYS = {
    AssetType.CASH: 0.01,
    AssetType.BOND: 2.0,
    AssetType.BOND_FUND: 1.0,
    AssetType.ETF: 1.0,
    AssetType.LISTED_EQUITY: 1.0,
    AssetType.HIGH_VOLATILITY_EQUITY: 1.0,
    AssetType.CRYPTO: 0.5,
    AssetType.OPTION: 0.5,
    AssetType.PRIVATE_ASSET: 90.0,
    AssetType.OTHER: 10.0,
}

CONCENTRATION_TOLERANCE = {
    AssetType.CASH: 0.90,
    AssetType.BOND: 0.65,
    AssetType.BOND_FUND: 0.60,
    AssetType.ETF: 0.50,
    AssetType.LISTED_EQUITY: 0.35,
    AssetType.HIGH_VOLATILITY_EQUITY: 0.20,
    AssetType.CRYPTO: 0.15,
    AssetType.OPTION: 0.10,
    AssetType.PRIVATE_ASSET: 0.10,
    AssetType.OTHER: 0.10,
}

STRESS_FLOOR_BY_ASSET = {
    AssetType.CASH: 0.00,
    AssetType.BOND: 0.04,
    AssetType.BOND_FUND: 0.05,
    AssetType.ETF: 0.10,
    AssetType.LISTED_EQUITY: 0.18,
    AssetType.HIGH_VOLATILITY_EQUITY: 0.33,
    AssetType.CRYPTO: 0.50,
    AssetType.OPTION: 0.65,
    AssetType.PRIVATE_ASSET: 0.55,
    AssetType.OTHER: 0.35,
}

DEFAULT_VOL_BY_ASSET = {
    AssetType.CASH: 0.005,
    AssetType.BOND: 0.08,
    AssetType.BOND_FUND: 0.10,
    AssetType.ETF: 0.22,
    AssetType.LISTED_EQUITY: 0.35,
    AssetType.HIGH_VOLATILITY_EQUITY: 0.75,
    AssetType.CRYPTO: 1.20,
    AssetType.OPTION: 1.50,
    AssetType.PRIVATE_ASSET: 0.80,
    AssetType.OTHER: 0.60,
}


def annualized_volatility(asset_type: AssetType, market: MarketData) -> float:
    candidates = [
        market.volatility_30d,
        market.volatility_90d,
        market.intraday_volatility,
        DEFAULT_VOL_BY_ASSET.get(asset_type, 0.60),
    ]
    return max(float(v) for v in candidates if v is not None)


def spread_rate(market: MarketData) -> float:
    if market.bid is None or market.ask is None or market.bid <= 0 or market.ask <= 0:
        return 0.02
    mid = (market.bid + market.ask) / 2.0
    return clamp((market.ask - market.bid) / max(mid, 1e-9), 0.0, 1.0)


def liquidation_horizon_days(
    holding: Holding,
    market: MarketData,
    policy: Policy,
    market_value: float,
) -> float:
    base = BASE_HORIZON_DAYS.get(holding.asset_type, 2.0)
    if holding.asset_type == AssetType.CASH:
        return base
    adv = market.average_dollar_volume
    if adv is None and market.average_daily_volume is not None:
        adv = market.average_daily_volume * market.last_price
    if adv is None or adv <= 0:
        return max(base, 10.0)
    daily_exit_capacity = max(1.0, adv * clamp(policy.max_participation_rate, 0.01, 0.25))
    required_days = market_value / daily_exit_capacity
    return clamp(max(base, required_days), base, 90.0)


def build_raw_inputs(
    holding: Holding,
    market: MarketData,
    policy: Policy,
    market_value: float,
    portfolio_market_value: float,
) -> RawRiskInputs:
    vol = annualized_volatility(holding.asset_type, market)
    spread = spread_rate(market)
    adv = market.average_dollar_volume
    if adv is None and market.average_daily_volume is not None:
        adv = market.average_daily_volume * market.last_price
    position_to_adv = market_value / max(1.0, adv or 1.0)
    horizon = liquidation_horizon_days(holding, market, policy, market_value)
    concentration = safe_div(market_value, portfolio_market_value, default=0.0)
    confidence = RISK_APPETITE_CONFIDENCE[policy.risk_appetite]
    return RawRiskInputs(
        annualized_volatility=vol,
        spread_rate=spread,
        position_to_adv=position_to_adv,
        liquidation_horizon_days=horizon,
        concentration=concentration,
        confidence=confidence,
    )


def volatility_adjustment(raw: RawRiskInputs, market: MarketData, asset_type: AssetType) -> float:
    es_loss = normal_expected_shortfall_loss(
        raw.annualized_volatility,
        raw.liquidation_horizon_days,
        raw.confidence,
    )
    recent_move = abs(market.recent_return_1d or 0.0)
    jump_penalty = 0.0
    if asset_type in {AssetType.HIGH_VOLATILITY_EQUITY, AssetType.CRYPTO, AssetType.OPTION}:
        jump_penalty += clamp(0.10 * raw.annualized_volatility, 0.03, 0.20)
    jump_penalty += clamp(max(0.0, recent_move - 0.05) * 0.45, 0.0, 0.18)
    haircut = clamp(es_loss + jump_penalty, 0.0, 0.92)
    return clamp(1.0 - haircut, 0.02, 1.0)


def liquidity_adjustment(raw: RawRiskInputs, market_value: float, market: MarketData) -> float:
    adv = market.average_dollar_volume
    if adv is None and market.average_daily_volume is not None:
        adv = market.average_daily_volume * market.last_price
    if adv is None or adv <= 0:
        return 0.25
    horizon_penalty = 0.035 * math.sqrt(max(0.0, raw.liquidation_horizon_days))
    impact_penalty = sqrt_impact(market_value, adv)
    haircut = clamp(horizon_penalty + impact_penalty, 0.0, 0.85)
    return clamp(1.0 - haircut, 0.05, 1.0)


def bid_ask_spread_adjustment(raw: RawRiskInputs) -> float:
    haircut = raw.spread_rate * 1.75
    if raw.spread_rate > 0.01:
        haircut += (raw.spread_rate - 0.01) * 2.5
    return clamp(1.0 - clamp(haircut, 0.0, 0.80), 0.05, 1.0)


def concentration_adjustment(raw: RawRiskInputs, asset_type: AssetType) -> float:
    tolerance = CONCENTRATION_TOLERANCE.get(asset_type, 0.20)
    excess = max(0.0, raw.concentration - tolerance)
    slope = 0.70 if asset_type not in {AssetType.HIGH_VOLATILITY_EQUITY, AssetType.CRYPTO, AssetType.OPTION} else 1.20
    haircut = clamp(excess * slope, 0.0, 0.90)
    return clamp(1.0 - haircut, 0.05, 1.0)


def stress_adjustment(raw: RawRiskInputs, asset_type: AssetType, policy: Policy) -> float:
    stress_floor = STRESS_FLOOR_BY_ASSET.get(asset_type, 0.30)
    stress_es = normal_expected_shortfall_loss(
        raw.annualized_volatility,
        max(raw.liquidation_horizon_days * 2.0, 1.0),
        max(raw.confidence, 0.995),
    )
    stress_loss = max(stress_floor, stress_es)
    severity = STRESS_SEVERITY_BY_APPETITE[policy.risk_appetite]
    return clamp(1.0 - clamp(stress_loss * severity, 0.0, 0.95), 0.02, 1.0)


def data_quality_adjustment(market: MarketData, policy: Policy) -> float:
    score = clamp(market.data_quality_score, 0.0, 1.0)
    if market.halted and not policy.allow_lending_on_stale_or_halted_assets:
        return 0.0
    if score < policy.min_data_quality_score:
        return clamp(score * 0.50, 0.0, 0.20)
    return clamp(0.25 + 0.75 * score, 0.0, 1.0)


def all_adjustments(
    holding: Holding,
    market: MarketData,
    policy: Policy,
    market_value: float,
    portfolio_market_value: float,
) -> tuple[RiskAdjustmentBreakdown, RawRiskInputs]:
    raw = build_raw_inputs(holding, market, policy, market_value, portfolio_market_value)
    breakdown = RiskAdjustmentBreakdown(
        volatility=volatility_adjustment(raw, market, holding.asset_type),
        liquidity=liquidity_adjustment(raw, market_value, market),
        spread=bid_ask_spread_adjustment(raw),
        concentration=concentration_adjustment(raw, holding.asset_type),
        stress=stress_adjustment(raw, holding.asset_type, policy),
        data_quality=data_quality_adjustment(market, policy),
    )
    return breakdown, raw


def risk_drivers_from_breakdown(breakdown: RiskAdjustmentBreakdown) -> list[str]:
    drivers: list[str] = []
    driver_thresholds = {
        "volatility": 0.86,
        "liquidity": 0.82,
        "spread": 0.90,
        "concentration": 0.88,
        "stress": 0.82,
        "data_quality": 0.90,
    }
    for name, value in breakdown.as_dict().items():
        if value < driver_thresholds.get(name, 0.80):
            drivers.append(name)
    return drivers
