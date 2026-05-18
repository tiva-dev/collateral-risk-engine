from __future__ import annotations

"""Legacy snapshot-provider compatibility layer.

The canonical v0.3.1 market-data provider contract lives in
``app.market_data.providers`` and returns ``RawQuote`` objects keyed by
``InstrumentIdentity.stable_key``.  This module is retained only for older
examples/tests that request already-normalized ``MarketData`` snapshots by
client/legacy ``asset_id``.
"""

from abc import ABC, abstractmethod

from app.core.models import MarketData
from app.market_data.providers import MarketDataProvider as RawQuoteProvider


class LegacySnapshotProvider(ABC):
    legacy_contract = True

    @abstractmethod
    def get_snapshot(self, asset_ids: list[str]) -> dict[str, MarketData]:
        raise NotImplementedError


class MarketDataProvider(LegacySnapshotProvider):
    """Deprecated alias for legacy snapshot providers.

    New integrations should implement ``app.market_data.providers.MarketDataProvider``.
    """


__all__ = ["LegacySnapshotProvider", "MarketDataProvider", "RawQuoteProvider"]
