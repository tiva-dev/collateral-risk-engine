from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, ClassVar

from .models import HistoricalFXSeries, HistoricalSeries


class HistoricalDataProvider(ABC):
    provider_name: ClassVar[str] = "base"
    provider_capabilities: ClassVar[frozenset[str]] = frozenset()

    def __init__(self) -> None:
        self.quota_metadata: dict[str, Any] = {}
        self.warnings: list[str] = []

    @abstractmethod
    def fetch_equity_history(self, instrument: str, start_date: date, end_date: date, interval: str = "1d", force_refresh: bool = False) -> HistoricalSeries: ...
    def fetch_fx_history(self, from_currency: str, to_currency: str, start_date: date, end_date: date, force_refresh: bool = False) -> HistoricalFXSeries:
        raise NotImplementedError
    def fetch_company_list(self, exchange: str, force_refresh: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
        raise NotImplementedError
    def fetch_index_history(self, index_symbol: str, start_date: date, end_date: date, force_refresh: bool = False) -> HistoricalSeries:
        raise NotImplementedError
