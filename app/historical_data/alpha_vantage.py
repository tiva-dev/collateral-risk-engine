from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime

from .cache import HistoricalDataCache
from .config import load_config
from .models import (
    HistoricalBar,
    HistoricalFXRate,
    HistoricalFXSeries,
    HistoricalSeries,
    canonical_series_payload,
)
from .providers import HistoricalDataProvider, ProviderError, validate_provider_url


class AlphaVantageHistoricalProvider(HistoricalDataProvider):
    provider_name = "alpha_vantage"
    provider_capabilities = frozenset({"equity_daily_adjusted", "fx_daily"})

    def __init__(self, cache: HistoricalDataCache | None = None):
        super().__init__()
        self.config = load_config()
        validate_provider_url(
            self.config.alpha_vantage_base_url, {"www.alphavantage.co"}
        )
        self.cache = cache or HistoricalDataCache()
        self.cache_paths = []
        self.raw_response_paths = []
        self.provider_coverage_summary = {}
        self.last_request_call_count = 0
        self.total_api_call_count = 0

    def _request_json(self, params):
        self.last_request_call_count = 0
        q = dict(params)
        if self.config.alpha_vantage_api_key:
            q["apikey"] = self.config.alpha_vantage_api_key
        for attempt in range(3):
            self.last_request_call_count += 1
            self.total_api_call_count += 1
            try:
                url = (
                    self.config.alpha_vantage_base_url + "?" + urllib.parse.urlencode(q)
                )
                validate_provider_url(url, {"www.alphavantage.co"})
                # URL scheme and host are validated immediately above.
                with urllib.request.urlopen(url, timeout=30) as r:  # nosec B310
                    payload = json.loads(r.read().decode())
                if not any(payload.get(k) for k in ("Note", "Information")):
                    return payload
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                if attempt == 2:
                    raise ProviderError(
                        "Alpha Vantage request failed (credentials redacted)",
                        provider=self.provider_name,
                    ) from exc
            if attempt < 2:
                time.sleep(0.25 * (2**attempt))
        raise ProviderError(
            "Alpha Vantage quota/rate-limit response after bounded retries",
            provider=self.provider_name,
            code="rate_limited",
        )

    def _payload_warnings(self, payload, expected_key=None):
        warnings = []
        for k in ("Note", "Information", "Error Message"):
            if isinstance(payload, dict) and payload.get(k):
                warnings.append(str(payload[k]))
        if not payload:
            warnings.append("Alpha Vantage returned an empty payload")
        if (
            expected_key
            and isinstance(payload, dict)
            and expected_key not in payload
            and not warnings
        ):
            warnings.append(
                f"Alpha Vantage payload missing expected key: {expected_key}"
            )
        self.warnings.extend(w for w in warnings if w not in self.warnings)
        return warnings

    def parse_daily_adjusted(self, instrument, payload, start_date, end_date):
        warnings = self._payload_warnings(payload, "Time Series (Daily)")
        series = (
            payload.get("Time Series (Daily)", {}) if isinstance(payload, dict) else {}
        )
        bars = []
        for d, b in sorted(series.items()):
            try:
                dt = date.fromisoformat(d)
                if start_date <= dt <= end_date:
                    bars.append(
                        HistoricalBar(
                            instrument,
                            dt,
                            float(b.get("1. open", 0)),
                            float(b.get("2. high", 0)),
                            float(b.get("3. low", 0)),
                            float(b.get("4. close", 0)),
                            float(b.get("5. adjusted close", b.get("4. close", 0))),
                            float(b.get("6. volume", 0)),
                            provider_name=self.provider_name,
                            raw_metadata=b,
                        )
                    )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                warnings.append(
                    f"Malformed Alpha Vantage daily row skipped for {instrument}: {exc}"
                )
        if not bars and not any(
            "expected key" in w or "empty" in w or "Error" in w for w in warnings
        ):
            warnings.append(f"No Alpha Vantage daily adjusted data for {instrument}")
        return HistoricalSeries(
            instrument,
            bars,
            self.provider_name,
            datetime.now(UTC),
            start_date,
            end_date,
            "1d",
            warnings,
            {"bar_count": len(bars)},
        )

    def parse_fx_daily(self, fc, tc, payload, start_date, end_date):
        warnings = self._payload_warnings(payload, "Time Series FX (Daily)")
        series = (
            payload.get("Time Series FX (Daily)", {})
            if isinstance(payload, dict)
            else {}
        )
        rates = []
        for d, b in sorted(series.items()):
            try:
                dt = date.fromisoformat(d)
                if start_date <= dt <= end_date:
                    rates.append(
                        HistoricalFXRate(
                            fc,
                            tc,
                            float(b.get("4. close", 0)),
                            dt,
                            provider_name=self.provider_name,
                            raw_metadata=b,
                        )
                    )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                warnings.append(
                    f"Malformed Alpha Vantage FX row skipped for {fc}/{tc}: {exc}"
                )
        if not rates and not any("expected key" in w or "empty" in w for w in warnings):
            warnings.append(f"No Alpha Vantage FX_DAILY data for {fc}/{tc}")
        return HistoricalFXSeries(
            fc,
            tc,
            rates,
            self.provider_name,
            datetime.now(UTC),
            start_date,
            end_date,
            warnings,
            {"rate_count": len(rates)},
        )

    def parse_csv(self, text):
        return list(csv.DictReader(io.StringIO(text)))

    def fetch_equity_history(
        self, instrument, start_date, end_date, interval="1d", force_refresh=False
    ):
        key = {
            "provider": self.provider_name,
            "symbol": instrument,
            "start": str(start_date),
            "end": str(end_date),
            "interval": interval,
        }
        if not force_refresh and (c := self.cache.read("normalized", **key)):
            self.cache_paths.append(str(self.cache.read_path("normalized", **key)))
            rp = self.cache.read_path("raw", **key)
            if rp.exists():
                self.raw_response_paths.append(str(rp))
            self.provider_coverage_summary = {"requested": 1, "cached": 1, "fetched": 0}
            return (
                _equity_from_cache(c["data"])
                if c["data"].get("cache_schema") == "historical_series/v1"
                else self.parse_daily_adjusted(
                    instrument, c["data"], start_date, end_date
                )
            )
        p = self._request_json(
            {
                "function": "TIME_SERIES_DAILY_ADJUSTED",
                "symbol": instrument,
                "outputsize": "full",
            }
        )
        self.raw_response_paths.append(str(self.cache.write("raw", p, **key)))
        series = self.parse_daily_adjusted(instrument, p, start_date, end_date)
        if not series.bars:
            raise ProviderError(
                "Alpha Vantage adjusted equity history unavailable; this capability may require premium access",
                provider=self.provider_name,
                code="premium_capability_unavailable",
            )
        self.cache_paths.append(
            str(self.cache.write("normalized", canonical_series_payload(series), **key))
        )
        self.provider_coverage_summary = {
            "requested": 1,
            "cached": 0,
            "fetched": 1,
            "api_call_count": max(1, self.last_request_call_count),
        }
        return series

    def fetch_fx_history(
        self, from_currency, to_currency, start_date, end_date, force_refresh=False
    ):
        key = {
            "provider": self.provider_name,
            "pair": f"{from_currency}{to_currency}",
            "start": str(start_date),
            "end": str(end_date),
        }
        if not force_refresh and (c := self.cache.read("normalized", **key)):
            self.cache_paths.append(str(self.cache.read_path("normalized", **key)))
            rp = self.cache.read_path("raw", **key)
            if rp.exists():
                self.raw_response_paths.append(str(rp))
            self.provider_coverage_summary = {"requested": 1, "cached": 1, "fetched": 0}
            return (
                _fx_from_cache(c["data"])
                if c["data"].get("cache_schema") == "historical_fx_series/v1"
                else self.parse_fx_daily(
                    from_currency, to_currency, c["data"], start_date, end_date
                )
            )
        p = self._request_json(
            {
                "function": "FX_DAILY",
                "from_symbol": from_currency,
                "to_symbol": to_currency,
                "outputsize": "full",
            }
        )
        self.raw_response_paths.append(str(self.cache.write("raw", p, **key)))
        series = self.parse_fx_daily(
            from_currency, to_currency, p, start_date, end_date
        )
        if not series.rates:
            raise ProviderError(
                f"Alpha Vantage returned no FX coverage for {from_currency}/{to_currency}",
                provider=self.provider_name,
                code="empty_coverage",
            )
        self.cache_paths.append(
            str(self.cache.write("normalized", canonical_series_payload(series), **key))
        )
        self.provider_coverage_summary = {
            "requested": 1,
            "cached": 0,
            "fetched": 1,
            "api_call_count": max(1, self.last_request_call_count),
        }
        return series


def _equity_from_cache(d):
    bars = [
        HistoricalBar(
            **{
                **r,
                "timestamp": date.fromisoformat(str(r["timestamp"])[:10]),
                "instrument_identity": None,
            }
        )
        for r in d.get("bars", [])
    ]
    return HistoricalSeries(
        d["instrument"],
        bars,
        d["provider_name"],
        datetime.fromisoformat(d["retrieved_at"]),
        date.fromisoformat(d["start_date"]),
        date.fromisoformat(d["end_date"]),
        d.get("interval", "1d"),
        d.get("warnings", []),
        d.get("data_quality_summary", {}),
    )


def _fx_from_cache(d):
    rates = [
        HistoricalFXRate(
            **{**r, "timestamp": date.fromisoformat(str(r["timestamp"])[:10])}
        )
        for r in d.get("rates", [])
    ]
    return HistoricalFXSeries(
        d["from_currency"],
        d["to_currency"],
        rates,
        d["provider_name"],
        datetime.fromisoformat(d["retrieved_at"]),
        date.fromisoformat(d["start_date"]),
        date.fromisoformat(d["end_date"]),
        d.get("warnings", []),
        d.get("data_quality_summary", {}),
    )
