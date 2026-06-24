from __future__ import annotations

import json, urllib.parse, urllib.request
from datetime import date, datetime, timezone
from typing import Any

from app.core.enums import AssetType
from app.market_data.identity import InstrumentIdentity

from .cache import HistoricalDataCache
from .config import load_config
from .models import HistoricalBar, HistoricalSeries
from .providers import HistoricalDataProvider


class ProviderError(RuntimeError): pass


class AlpacaTradingHistoricalProvider(HistoricalDataProvider):
    provider_name = "alpaca"
    provider_capabilities = frozenset({"us_equity_bars", "etf_bars", "daily_bars"})

    def __init__(self, cache: HistoricalDataCache | None = None):
        super().__init__(); self.config = load_config(); self.cache = cache or HistoricalDataCache()
        self.cache_paths: list[str] = []; self.raw_response_paths: list[str] = []

    def auth_headers(self) -> dict[str, str]:
        h = {}
        if self.config.alpaca_api_key: h["APCA-API-KEY-ID"] = self.config.alpaca_api_key
        if self.config.alpaca_secret_key: h["APCA-API-SECRET-KEY"] = self.config.alpaca_secret_key
        return h

    def _request_json(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=self.auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.quota_metadata.update({k: v for k, v in resp.headers.items() if "limit" in k.lower() or "remaining" in k.lower()})
                payload = json.loads(resp.read().decode())
        except Exception as exc: raise ProviderError(f"Alpaca request failed: {exc}") from exc
        if isinstance(payload, dict) and (payload.get("code") or payload.get("message")) and "bars" not in payload:
            msg = str(payload.get("message") or payload)
            self.warnings.append(msg); raise ProviderError(f"Alpaca API error: {msg}")
        return payload

    def _identity(self, symbol: str) -> InstrumentIdentity:
        return InstrumentIdentity(f"US:{symbol}:USD", symbol, "US", "USD", AssetType.LISTED_EQUITY, provider_symbol=symbol)

    def parse_bars(self, instrument: str, payload: dict[str, Any], start_date: date, end_date: date, interval: str) -> HistoricalSeries:
        warnings = list(self.warnings)
        bars_payload = payload.get("bars", {}) if isinstance(payload, dict) else {}
        raw_bars = bars_payload.get(instrument, []) if isinstance(bars_payload, dict) else bars_payload
        if not raw_bars: warnings.append(f"No Alpaca bars returned for {instrument}")
        identity = self._identity(instrument)
        bars = [HistoricalBar(instrument=instrument, timestamp=datetime.fromisoformat(str(b.get("t")).replace("Z", "+00:00")), open=float(b.get("o", 0)), high=float(b.get("h", 0)), low=float(b.get("l", 0)), close=float(b.get("c", 0)), volume=float(b.get("v", 0)), currency="USD", provider_name=self.provider_name, instrument_identity=identity, raw_metadata=b) for b in raw_bars]
        return HistoricalSeries(instrument, bars, self.provider_name, datetime.now(timezone.utc), start_date, end_date, interval, warnings=warnings, data_quality_summary={"bar_count": len(bars)}, instrument_identity=identity)

    def fetch_equity_history(self, instrument: str, start_date: date, end_date: date, interval: str = "1d", force_refresh: bool = False) -> HistoricalSeries:
        key = dict(provider=self.provider_name, symbol=instrument, start=str(start_date), end=str(end_date), interval=interval)
        if not force_refresh and (cached := self.cache.read("normalized", **key)):
            return self.parse_bars(instrument, cached["data"], start_date, end_date, interval)
        params = urllib.parse.urlencode({"symbols": instrument, "timeframe": "1Day" if interval in {"1d", "daily"} else interval, "start": start_date.isoformat(), "end": end_date.isoformat()})
        payload = self._request_json(self.config.alpaca_base_url.rstrip("/") + "/v2/stocks/bars?" + params)
        self.raw_response_paths.append(str(self.cache.write("raw", payload, **key)))
        self.cache_paths.append(str(self.cache.write("normalized", payload, **key)))
        return self.parse_bars(instrument, payload, start_date, end_date, interval)
