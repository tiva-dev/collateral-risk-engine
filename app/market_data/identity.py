from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import AssetType
from app.core.models import Holding


@dataclass(frozen=True)
class InstrumentIdentity:
    asset_id: str
    symbol: str
    exchange: str
    currency: str
    asset_type: AssetType
    isin: str | None = None
    figi: str | None = None
    provider_symbol: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def stable_key(self) -> str:
        return f"{self.exchange.upper()}:{self.symbol.upper()}:{self.currency.upper()}"

    @staticmethod
    def from_holding(holding: Holding) -> "InstrumentIdentity":
        symbol = holding.asset_id.split(":")[1] if ":" in holding.asset_id else holding.asset_id
        exchange = holding.asset_id.split(":")[0] if ":" in holding.asset_id else "UNKNOWN"
        return InstrumentIdentity(
            asset_id=holding.asset_id,
            symbol=symbol,
            exchange=exchange,
            currency=holding.currency,
            asset_type=holding.asset_type,
        )
