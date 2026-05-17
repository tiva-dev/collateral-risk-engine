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
    def _parse_holding_asset_id(asset_id: str, fallback_currency: str) -> tuple[str, str, str]:
        parts = asset_id.split(":")

        if len(parts) == 3:
            exchange, symbol, currency = parts
        elif len(parts) == 2:
            exchange, symbol = parts
            currency = fallback_currency
        elif len(parts) == 1:
            exchange = "UNKNOWN"
            symbol = parts[0]
            currency = fallback_currency
        else:
            raise ValueError(
                "holding.asset_id must be in 'EXCHANGE:SYMBOL[:CURRENCY]' format"
            )

        if not exchange and len(parts) > 1:
            raise ValueError("holding.asset_id exchange segment cannot be empty")
        if not symbol:
            raise ValueError("holding.asset_id symbol segment cannot be empty")
        if len(parts) == 3 and not currency:
            raise ValueError("holding.asset_id currency segment cannot be empty")

        return exchange, symbol, currency

    @staticmethod
    def from_holding(holding: Holding) -> "InstrumentIdentity":
        exchange, symbol, currency = InstrumentIdentity._parse_holding_asset_id(
            holding.asset_id, holding.currency
        )
        return InstrumentIdentity(
            asset_id=holding.asset_id,
            symbol=symbol,
            exchange=exchange,
            currency=currency,
            asset_type=holding.asset_type,
        )
