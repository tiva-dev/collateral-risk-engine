"""Exchange and execution characteristics as data, not code.

Every field records its provenance: ``observed`` (measured from data),
``configured`` (taken from an exchange rulebook or operator setting, with the
source cited) or ``assumption`` (a placeholder awaiting confirmation). Fields
marked ``not_in_approved_methodology`` are recorded for research and execution
planning only; in particular NGX daily price-limit modelling has not been
approved as part of the risk methodology and is not used by the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MarketProfile:
    code: str
    name: str
    exchanges: tuple[str, ...]
    currency: str
    timezone: str
    session_local: tuple[str, str]
    settlement_days: int
    daily_price_limit: float | None
    max_participation_rate: float
    # Used only when a spread cannot be estimated from observed high/low data.
    spread_floor: float
    spread_fallback: float
    currency_convertibility: str  # free | managed | restricted
    volume_reported_from: str | None = None
    provenance: dict[str, str] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


US = MarketProfile(
    code="US",
    name="United States listed equities and ETFs",
    exchanges=("NASDAQ", "NYSE", "ARCA", "NYSEARCA", "BATS", "AMEX", "IEX", "US"),
    currency="USD",
    timezone="America/New_York",
    session_local=("09:30", "16:00"),
    settlement_days=1,
    daily_price_limit=None,
    max_participation_rate=0.10,
    spread_floor=0.0002,
    spread_fallback=0.0005,
    currency_convertibility="free",
    volume_reported_from=None,
    provenance={
        "settlement_days": "configured: SEC T+1 settlement effective 28 May 2024",
        "daily_price_limit": "configured: no daily limit; LULD bands are intraday pauses",
        "spread_floor": "assumption: research default (methodology_lab real_data.py)",
        "max_participation_rate": "assumption: CRI default 10% of ADV",
        "volume": "observed: Alpaca IEX feed only (partial consolidated volume)",
    },
)

NGX = MarketProfile(
    code="NGX",
    name="Nigerian Exchange equities",
    exchanges=("NGX", "NGSE", "NSE"),
    currency="NGN",
    timezone="Africa/Lagos",
    session_local=("10:00", "14:30"),
    settlement_days=2,
    daily_price_limit=0.10,
    max_participation_rate=0.10,
    spread_floor=0.005,
    spread_fallback=0.02,
    currency_convertibility="managed",
    volume_reported_from="2025-08-01",
    provenance={
        "settlement_days": "assumption: T+2; confirm against current NGX/CSCS rule before use",
        "daily_price_limit": "observed: one-day moves cluster at exactly +/-10% in NGNMarket data",
        "spread_floor": "assumption: research default (methodology_lab real_data.py)",
        "max_participation_rate": "assumption: CRI default 10% of ADV",
        "volume": "observed: NGNMarket reports volume only from 2025-08-01",
        "currency_convertibility": "configured: managed float; no executable USD/NGN source available",
    },
    status={"daily_price_limit": "not_in_approved_methodology"},
)

PROFILES: dict[str, MarketProfile] = {p.code: p for p in (US, NGX)}


def profile_for_exchange(exchange: str) -> MarketProfile | None:
    ex = (exchange or "").upper()
    for profile in PROFILES.values():
        if ex in profile.exchanges:
            return profile
    return None
