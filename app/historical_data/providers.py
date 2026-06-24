from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any


class HistoricalDataProvider(ABC):
    provider_name: str = "base"
    provider_capabilities: set[str] = set()
    quota_metadata: dict[str, Any] = {}

    @abstractmethod
    def fetch_equity_history(self, instrument: str, start_date: date, end_date: date, interval: str = "1d", force_refresh: bool = False): ...
    def fetch_fx_history(self, from_currency: str, to_currency: str, start_date: date, end_date: date, force_refresh: bool = False):
        raise NotImplementedError
    def fetch_company_list(self, exchange: str, force_refresh: bool = False):
        raise NotImplementedError
    def fetch_index_history(self, index_symbol: str, start_date: date, end_date: date, force_refresh: bool = False):
        raise NotImplementedError
