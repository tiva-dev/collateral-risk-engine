from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from app.market_data.policy import FXPolicy
from app.market_data.providers import FXRate, MarketDataProvider
from app.market_data.quality import age_minutes, clamp_score, utc_now


@dataclass(frozen=True)
class FXDecision:
    rate: FXRate | None
    quality_score: float
    warnings: list[str] = field(default_factory=list)
    missing_required_fx: bool = False


def score_fx_rate(rate: FXRate, policy: FXPolicy, now: datetime | None = None) -> tuple[float, list[str]]:
    warnings = list(rate.warnings)
    quality = clamp_score(rate.quality_score)
    if age_minutes(rate.timestamp, now) > policy.max_fx_age_minutes:
        quality = clamp_score(quality * (1.0 - policy.stale_fx_haircut))
        warnings.append("stale_fx")
    if quality < policy.minimum_fx_quality_score:
        warnings.append("fx_quality_below_threshold")
    return quality, warnings


class FXSelector:
    def __init__(self, client_provider: MarketDataProvider | None, provider: MarketDataProvider | None) -> None:
        self.client_provider = client_provider
        self.provider = provider

    def select_rate(
        self,
        from_currency: str,
        to_currency: str,
        policy: FXPolicy,
        *,
        allow_client: bool = True,
        allow_provider: bool = True,
        now: datetime | None = None,
    ) -> FXDecision:
        src, dst = from_currency.upper(), to_currency.upper()
        if src == dst:
            return FXDecision(FXRate(src, dst, 1.0, now or utc_now(), "not_required", "not_required", 1.0), 1.0)

        candidates: list[tuple[str, FXRate, float, list[str]]] = []
        if allow_client and self.client_provider:
            client_rate = self.client_provider.get_fx_rate(src, dst)
            if client_rate:
                quality, warnings = score_fx_rate(client_rate, policy, now)
                candidates.append(("client", replace(client_rate, warnings=warnings), quality, warnings))
        if allow_provider and self.provider:
            provider_rate = self.provider.get_fx_rate(src, dst)
            if provider_rate:
                quality, warnings = score_fx_rate(provider_rate, policy, now)
                candidates.append(("provider", replace(provider_rate, warnings=warnings), quality, warnings))

        if not candidates:
            return FXDecision(None, 0.05, ["missing_required_fx"], True)

        usable = [c for c in candidates if c[2] >= policy.minimum_fx_quality_score]
        if policy.use_conservative_rate_when_sources_disagree and len(candidates) > 1:
            # For long-only collateral, the lower direct conversion rate produces lower collateral value.
            selected = min(candidates, key=lambda candidate: candidate[1].rate)
            warnings = [*selected[3], "conservative_fx_rate_selected"]
            return FXDecision(replace(selected[1], warnings=warnings), selected[2], warnings)

        preferred_client = policy.preferred_source == "client"
        if preferred_client and policy.allow_fallback_provider:
            client_stale = any(candidate[0] == "client" and "stale_fx" in candidate[3] for candidate in candidates)
            if client_stale:
                for candidate in usable:
                    if candidate[0] == "provider":
                        warnings = [*candidate[3], "fallback_provider_fx_used"]
                        return FXDecision(replace(candidate[1], warnings=warnings), candidate[2], warnings)
        ordered_sources = ["client", "provider"] if preferred_client else ["provider", "client"]
        for source in ordered_sources:
            for candidate in usable:
                if candidate[0] == source:
                    return FXDecision(candidate[1], candidate[2], candidate[3])
        if policy.allow_fallback_provider and usable:
            selected = usable[0]
            return FXDecision(selected[1], selected[2], selected[3])

        selected = candidates[0]
        return FXDecision(selected[1], selected[2], selected[3])
