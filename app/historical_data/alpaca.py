from __future__ import annotations

import json, urllib.parse, urllib.request
from datetime import date, datetime, timezone
from typing import Any

from app.core.enums import AssetType
from app.market_data.identity import InstrumentIdentity

from .cache import HistoricalDataCache
from .config import load_config
from .models import HistoricalBar, HistoricalSeries, canonical_series_payload
from .providers import HistoricalDataProvider, ProviderError, validate_provider_url


class AlpacaTradingHistoricalProvider(HistoricalDataProvider):
    provider_name = "alpaca"
    provider_capabilities = frozenset({"us_equity_bars", "etf_bars", "daily_bars"})

    def __init__(self, cache: HistoricalDataCache | None = None):
        super().__init__(); self.config = load_config(); validate_provider_url(self.config.alpaca_base_url,{"data.alpaca.markets"}); self.cache = cache or HistoricalDataCache()
        self.cache_paths: list[str] = []; self.raw_response_paths: list[str] = []; self.provider_coverage_summary: dict[str, Any] = {}

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
        except Exception as exc: raise ProviderError(f"Alpaca request failed: {exc}", provider=self.provider_name) from exc
        if isinstance(payload, dict) and (payload.get("code") or payload.get("message")) and "bars" not in payload:
            msg = str(payload.get("message") or payload)
            self.warnings.append(msg); raise ProviderError(f"Alpaca API error: {msg}", provider=self.provider_name, metadata=payload)
        return payload

    def _identity(self, symbol: str) -> InstrumentIdentity:
        asset_type = AssetType.ETF if symbol.upper() in {"SPY","QQQ","IWM","EFA","VTI"} else AssetType.LISTED_EQUITY
        return InstrumentIdentity(f"US:{symbol}:USD", symbol, "US", "USD", asset_type, provider_symbol=symbol)

    def _extract_symbol_bars(self, instrument: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        bars_payload = payload.get("bars", {}) if isinstance(payload, dict) else {}
        if isinstance(bars_payload, dict):
            return list(bars_payload.get(instrument) or bars_payload.get(instrument.upper()) or [])
        if isinstance(bars_payload, list):
            return list(bars_payload)
        return []

    def parse_bars(self, instrument: str, payload: dict[str, Any], start_date: date, end_date: date, interval: str) -> HistoricalSeries:
        warnings = list(dict.fromkeys(self.warnings))
        raw_bars = self._extract_symbol_bars(instrument, payload)
        if not raw_bars: warnings.append(f"No Alpaca bars returned for {instrument}")
        identity = self._identity(instrument)
        bars=[]
        for b in raw_bars:
            try:
                ts=datetime.fromisoformat(str(b.get("t")).replace("Z", "+00:00"))
                bars.append(HistoricalBar(instrument=instrument, timestamp=ts, open=float(b.get("o", 0)), high=float(b.get("h", 0)), low=float(b.get("l", 0)), close=float(b.get("c", 0)), volume=float(b.get("v", 0)), currency="USD", provider_name=self.provider_name, instrument_identity=identity, raw_metadata=b))
            except Exception as exc:
                warnings.append(f"Malformed Alpaca bar skipped for {instrument}: {exc}")
        return HistoricalSeries(instrument, bars, self.provider_name, datetime.now(timezone.utc), start_date, end_date, interval, warnings=warnings, data_quality_summary={"bar_count": len(bars), "page_count": self.provider_coverage_summary.get("page_count", 0), "quota_metadata": self.quota_metadata}, instrument_identity=identity)

    def _merge_payload(self, pages: list[dict[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {"bars": {}}
        for p in pages:
            bp=p.get("bars", {}) if isinstance(p, dict) else {}
            if isinstance(bp, dict):
                for sym, rows in bp.items(): merged["bars"].setdefault(sym, []).extend(rows or [])
            elif isinstance(bp, list):
                merged["bars"].setdefault("__single__", []).extend(bp)
        return merged

    def fetch_equity_history(self, instrument: str, start_date: date, end_date: date, interval: str = "1d", force_refresh: bool = False, adjustment: str = "all", feed: str | None = None, currency: str = "USD", limit: int = 10000) -> HistoricalSeries:
        key = dict(provider=self.provider_name, symbol=instrument, start=str(start_date), end=str(end_date), interval=interval, adjustment=adjustment, feed=feed or "", currency=currency, limit=limit)
        if not force_refresh and (cached := self.cache.read("normalized", **key)):
            self.cache_paths.append(str(self.cache.read_path("normalized", **key)))
            raw_path=self.cache.read_path("raw", **key)
            if raw_path.exists(): self.raw_response_paths.append(str(raw_path))
            data=cached["data"]
            self.provider_coverage_summary.update({"requested":1,"available":1,"missing":0,"cached":1,"fetched":0,"page_count":data.get("data_quality_summary",{}).get("page_count",0)})
            if data.get("cache_schema") == "historical_series/v1":
                return _series_from_cache(data)
            return self.parse_bars(instrument, data, start_date, end_date, interval)
        token=None; pages=[]; page_count=0
        try:
            while True:
                q={"symbols": instrument, "timeframe": "1Day" if interval in {"1d", "daily"} else interval, "start": start_date.isoformat(), "end": end_date.isoformat(), "adjustment": adjustment, "currency": currency, "limit": limit}
                if feed: q["feed"]=feed
                if token: q["page_token"]=token
                payload=self._request_json(self.config.alpaca_base_url.rstrip("/")+"/v2/stocks/bars?"+urllib.parse.urlencode(q))
                pages.append(payload); page_count += 1; token=payload.get("next_page_token") if isinstance(payload,dict) else None
                if not token: break
        except ProviderError as exc:
            self.warnings.append(f"Alpaca pagination failed after {page_count} pages: {exc}")
            if not pages: raise
        merged=self._merge_payload(pages)
        if "__single__" in merged["bars"]: merged["bars"]={instrument: merged["bars"].pop("__single__")}
        merged["metadata"]={"page_count":page_count,"adjustment":adjustment,"feed":feed,"currency":currency,"quota_metadata":self.quota_metadata}
        self.provider_coverage_summary.update({"requested":1,"available":1 if self._extract_symbol_bars(instrument, merged) else 0,"missing":0 if self._extract_symbol_bars(instrument, merged) else 1,"cached":0,"fetched":1,"page_count":page_count,"api_call_count":page_count,"adjustment":adjustment})
        self.raw_response_paths.append(str(self.cache.write("raw", merged, **key)))
        series=self.parse_bars(instrument, merged, start_date, end_date, interval)
        if not series.bars: raise ProviderError(f"Alpaca returned no covered bars for {instrument}",provider=self.provider_name,code="empty_coverage")
        self.cache_paths.append(str(self.cache.write("normalized", canonical_series_payload(series), **key)))
        return series

def _series_from_cache(data):
    identity = InstrumentIdentity(**data["instrument_identity"]) if data.get("instrument_identity") else None
    bars=[]
    for row in data.get("bars",[]):
        ri=InstrumentIdentity(**row["instrument_identity"]) if row.get("instrument_identity") else identity
        bars.append(HistoricalBar(**{**row,"timestamp":datetime.fromisoformat(row["timestamp"].replace("Z","+00:00")),"instrument_identity":ri}))
    return HistoricalSeries(data["instrument"],bars,data["provider_name"],datetime.fromisoformat(data["retrieved_at"].replace("Z","+00:00")),date.fromisoformat(data["start_date"]),date.fromisoformat(data["end_date"]),data.get("interval","1d"),data.get("warnings",[]),data.get("data_quality_summary",{}),identity)
