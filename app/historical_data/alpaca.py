from __future__ import annotations

import json, urllib.parse, urllib.request
from datetime import date, datetime, timezone
from typing import Any

from .cache import HistoricalDataCache
from .config import load_config
from .models import HistoricalBar, HistoricalSeries
from .providers import HistoricalDataProvider


class ProviderError(RuntimeError): pass


class AlpacaTradingHistoricalProvider(HistoricalDataProvider):
    provider_name = "alpaca"
    provider_capabilities = {"us_equity_bars", "etf_bars", "daily_bars"}

    def __init__(self, cache: HistoricalDataCache | None = None):
        self.config = load_config(); self.cache = cache or HistoricalDataCache(); self.quota_metadata = {}

    def auth_headers(self) -> dict[str, str]:
        h = {}
        if self.config.alpaca_api_key: h["APCA-API-KEY-ID"] = self.config.alpaca_api_key
        if self.config.alpaca_secret_key: h["APCA-API-SECRET-KEY"] = self.config.alpaca_secret_key
        return h

    def _request_json(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=self.auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.quota_metadata = {k: v for k, v in resp.headers.items() if "limit" in k.lower() or "remaining" in k.lower()}
                return json.loads(resp.read().decode())
        except Exception as exc: raise ProviderError(f"Alpaca request failed: {exc}") from exc

    def parse_bars(self, instrument: str, payload: dict[str, Any], start_date: date, end_date: date, interval: str) -> HistoricalSeries:
        raw_bars = payload.get("bars", {}).get(instrument, payload.get("bars", []))
        bars = [HistoricalBar(instrument=instrument, timestamp=datetime.fromisoformat(str(b.get("t")).replace("Z", "+00:00")), open=float(b.get("o", 0)), high=float(b.get("h", 0)), low=float(b.get("l", 0)), close=float(b.get("c", 0)), volume=float(b.get("v", 0)), currency="USD", provider_name=self.provider_name, raw_metadata=b) for b in raw_bars]
        return HistoricalSeries(instrument, bars, self.provider_name, datetime.now(timezone.utc), start_date, end_date, interval, data_quality_summary={"bar_count": len(bars)})

    def fetch_equity_history(self, instrument: str, start_date: date, end_date: date, interval: str = "1d", force_refresh: bool = False) -> HistoricalSeries:
        key = dict(provider=self.provider_name, symbol=instrument, start=str(start_date), end=str(end_date), interval=interval)
        if not force_refresh and (cached := self.cache.read("normalized", **key)): return self.parse_bars(instrument, cached["data"], start_date, end_date, interval)
        params = urllib.parse.urlencode({"symbols": instrument, "timeframe": "1Day" if interval in {"1d", "daily"} else interval, "start": start_date.isoformat(), "end": end_date.isoformat()})
        url = self.config.alpaca_base_url.rstrip("/") + "/v2/stocks/bars?" + params
        payload = self._request_json(url); self.cache.write("raw", payload, **key); self.cache.write("normalized", payload, **key)
        return self.parse_bars(instrument, payload, start_date, end_date, interval)
