from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.models import MarketData


class MarketDataProvider(ABC):
    @abstractmethod
    def get_snapshot(self, asset_ids: list[str]) -> dict[str, MarketData]:
        raise NotImplementedError
