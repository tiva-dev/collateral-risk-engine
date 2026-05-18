from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import AssetType


@dataclass(frozen=True)
class FXPolicy:
    preferred_source: str = "client"
    allow_fallback_provider: bool = True
    max_fx_age_minutes: int = 24 * 60
    stale_fx_haircut: float = 0.35
    use_conservative_rate_when_sources_disagree: bool = False
    minimum_fx_quality_score: float = 0.50

    def __post_init__(self) -> None:
        if self.max_fx_age_minutes <= 0:
            raise ValueError("max_fx_age_minutes must be greater than 0")
        if not 0 <= self.stale_fx_haircut <= 1:
            raise ValueError("stale_fx_haircut must be between 0 and 1")
        if not 0 <= self.minimum_fx_quality_score <= 1:
            raise ValueError("minimum_fx_quality_score must be between 0 and 1")


@dataclass(frozen=True)
class MarketDataPolicy:
    fx: FXPolicy = field(default_factory=FXPolicy)
    max_quote_age_minutes_by_asset_type: dict[AssetType, int] = field(
        default_factory=lambda: {
            AssetType.LISTED_EQUITY: 20,
            AssetType.HIGH_VOLATILITY_EQUITY: 10,
            AssetType.ETF: 20,
            AssetType.BOND: 24 * 60,
            AssetType.BOND_FUND: 24 * 60,
            AssetType.CASH: 24 * 60,
            AssetType.CRYPTO: 5,
            AssetType.OPTION: 5,
            AssetType.PRIVATE_ASSET: 7 * 24 * 60,
            AssetType.OTHER: 24 * 60,
        }
    )
    max_quote_age_minutes_by_exchange: dict[str, int] = field(default_factory=dict)
    stale_quote_haircut: float = 0.35
    minimum_quote_quality_score: float = 0.50
    allow_fallback_provider: bool = True

    def __post_init__(self) -> None:
        for exchange, minutes in self.max_quote_age_minutes_by_exchange.items():
            if minutes <= 0:
                raise ValueError(
                    f"max quote age minutes for {exchange} must be greater than 0"
                )
        for asset_type, minutes in self.max_quote_age_minutes_by_asset_type.items():
            if minutes <= 0:
                raise ValueError(
                    f"max quote age minutes for {asset_type} must be greater than 0"
                )
        if not 0 <= self.stale_quote_haircut <= 1:
            raise ValueError("stale_quote_haircut must be between 0 and 1")
        if not 0 <= self.minimum_quote_quality_score <= 1:
            raise ValueError("minimum_quote_quality_score must be between 0 and 1")
