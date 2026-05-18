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
        return f"{self.exchange.upper()}:{self.symbol.upper()}:{self.currency.upper()}:{self.asset_type.value.upper()}"

    @staticmethod
    def from_holding(holding: Holding) -> "InstrumentIdentity":
        parts = holding.asset_id.split(":")
        if len(parts) == 3:
            exchange, symbol, currency = parts
        else:
            exchange, symbol, currency = "UNKNOWN", holding.asset_id, holding.currency
        return InstrumentIdentity(
            asset_id=holding.asset_id,
            symbol=symbol,
            exchange=exchange,
            currency=currency,
            asset_type=holding.asset_type,
        )
