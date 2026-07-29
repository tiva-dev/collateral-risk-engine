from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime

from app.core.enums import AssetType
from app.market_data.identity import InstrumentIdentity

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


class NGNMarketHistoricalProvider(HistoricalDataProvider):
    provider_name = "ngnmarket"
    provider_capabilities = frozenset(
        {"ngx_companies", "ngx_bars", "ngx_fx", "ngx_indices"}
    )

    def __init__(self, cache: HistoricalDataCache | None = None):
        super().__init__()
        self.config = load_config()
        validate_provider_url(self.config.ngnmarket_base_url, {"api.ngnmarket.com"})
        self.cache = cache or HistoricalDataCache()
        self.missing_symbols = []
        self.missing_symbol_reasons = {}
        self.cache_paths = []
        self.raw_response_paths = []
        self.provider_coverage_summary = {}
        self.total_api_call_count = 0

    def _api_key(self) -> str:
        key = (self.config.ngnmarket_api_key or "").strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            raise ProviderError(
                "NGNMarket API key is missing",
                provider=self.provider_name,
                code="missing_api_key",
            )
        if not key.startswith("ngm_"):
            raise ProviderError(
                "NGNMarket API key has an invalid format; expected an ngm_ key",
                provider=self.provider_name,
                code="invalid_api_key_format",
            )
        return key

    def auth_headers(self):
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Accept": "application/json",
            "User-Agent": "collateral-risk-engine/0.6.2",
        }

    def parse_envelope(self, payload):
        if isinstance(payload, dict) and "meta" in payload:
            self.quota_metadata.update(payload.get("meta") or {})
        if isinstance(payload, dict) and payload.get("success") is False:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            msg = (
                error.get("message")
                if isinstance(error, dict)
                else error or payload.get("message") or "NGNMarket error"
            )
            msg = str(msg)
            self.warnings.append(msg)
            raise ProviderError(
                msg, provider=self.provider_name, code=code, metadata=payload
            )
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def _request_json(self, path, params=None):
        self.total_api_call_count += 1
        url = self.config.ngnmarket_base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        validate_provider_url(url, {"api.ngnmarket.com"})
        try:
            req = urllib.request.Request(url, headers=self.auth_headers())
            # URL scheme and host are validated immediately above.
            with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            raw_body = ""
            try:
                raw_body = exc.read().decode(errors="replace")
                payload = json.loads(raw_body)
            except (AttributeError, json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            error = payload.get("error") if isinstance(payload, dict) else None
            provider_code = (
                str(error.get("code"))
                if isinstance(error, dict) and error.get("code")
                else str(payload.get("code"))
                if isinstance(payload, dict) and payload.get("code")
                else f"http_{exc.code}"
            )
            provider_message = (
                str(error.get("message"))
                if isinstance(error, dict) and error.get("message")
                else str(payload.get("message"))
                if isinstance(payload, dict) and payload.get("message")
                else ""
            )
            if not provider_message and raw_body:
                body_text = re.sub(r"<[^>]+>", " ", raw_body)
                body_text = " ".join(body_text.split())
                provider_message = f"request rejected; response={body_text[:240]}"
            provider_message = provider_message or "request rejected"
            provider_message = provider_message.replace(self._api_key(), "[redacted]")
            response_headers = {
                name: value
                for name, value in {
                    "content_type": exc.headers.get("Content-Type")
                    if exc.headers
                    else None,
                    "server": exc.headers.get("Server") if exc.headers else None,
                    "request_id": (
                        exc.headers.get("CF-Ray")
                        or exc.headers.get("X-Request-ID")
                        or exc.headers.get("X-Correlation-ID")
                    )
                    if exc.headers
                    else None,
                }.items()
                if value
            }
            raise ProviderError(
                f"NGNMarket HTTP {exc.code}: {provider_code}: {provider_message}",
                provider=self.provider_name,
                code=provider_code,
                metadata={
                    "http_status": exc.code,
                    "response_headers": response_headers,
                },
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"NGNMarket transport error: {exc.reason}",
                provider=self.provider_name,
                code="transport_error",
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(
                "NGNMarket returned an invalid JSON response",
                provider=self.provider_name,
                code="invalid_json",
            ) from exc
        except Exception as exc:
            raise ProviderError(
                "NGNMarket request failed (credentials and URL redacted)",
                provider=self.provider_name,
            ) from exc

    def _identity(self, symbol, asset_type=AssetType.LISTED_EQUITY):
        return InstrumentIdentity(
            f"NGX:{symbol}:NGN",
            symbol,
            "NGX",
            "NGN",
            asset_type,
            provider_symbol=symbol,
        )

    def _rows(self, data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return (
                data.get("data")
                or data.get("prices")
                or data.get("chart")
                or data.get("history")
                or []
            )
        return []

    def parse_company_chart(
        self, symbol, payload, start_date, end_date, asset_type=AssetType.LISTED_EQUITY
    ):
        data = self.parse_envelope(payload)
        rows = self._rows(data)
        warnings = list(self.warnings)
        bars = []
        identity = self._identity(symbol, asset_type)
        null_ohlc_fallbacks = 0
        if not rows:
            warnings.append(f"No NGNMarket chart data for {symbol}")
            self.missing_symbols.append(symbol)
            self.missing_symbol_reasons[symbol] = "empty chart data"
        for b in rows:
            if not isinstance(b, dict):
                warnings.append(
                    f"Malformed NGNMarket chart row skipped for {symbol}: not an object"
                )
                continue
            d = b.get("date") or b.get("timestamp") or b.get("time")
            if not d:
                warnings.append(
                    f"NGNMarket chart row skipped for {symbol}: missing date"
                )
                continue
            close = b.get("close", b.get("c", b.get("price")))
            if close in (None, ""):
                warnings.append(
                    f"NGNMarket chart row skipped for {symbol}: missing price"
                )
                continue
            try:
                dt = date.fromisoformat(str(d)[:10])
                if start_date <= dt <= end_date:
                    c = float(close)
                    open_value = b.get("open", b.get("o"))
                    high_value = b.get("high", b.get("h"))
                    low_value = b.get("low", b.get("l"))
                    null_ohlc_fallbacks += sum(
                        value in (None, "")
                        for value in (open_value, high_value, low_value)
                    )
                    bars.append(
                        HistoricalBar(
                            symbol,
                            dt,
                            float(c if open_value in (None, "") else open_value),
                            float(c if high_value in (None, "") else high_value),
                            float(c if low_value in (None, "") else low_value),
                            c,
                            b.get("adjusted_close"),
                            float(b.get("volume", b.get("v", 0)) or 0),
                            b.get("value_traded"),
                            "NGN",
                            provider_name=self.provider_name,
                            instrument_identity=identity,
                            raw_metadata=b,
                        )
                    )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                warnings.append(
                    f"Malformed NGNMarket chart row skipped for {symbol}: {exc}"
                )
        if null_ohlc_fallbacks:
            warnings.append(
                f"NGNMarket filled {null_ohlc_fallbacks} missing OHLC field(s) "
                f"from close for {symbol}"
            )
        return HistoricalSeries(
            symbol,
            bars,
            self.provider_name,
            datetime.now(UTC),
            start_date,
            end_date,
            "1d",
            warnings,
            {"bar_count": len(bars), "quota_metadata": self.quota_metadata},
            identity,
        )

    def parse_fx_history(self, fc, tc, payload, start_date, end_date):
        data = self.parse_envelope(payload)
        rows = (
            data.get("rates", data if isinstance(data, list) else [])
            if isinstance(data, (dict, list))
            else []
        )
        warnings = list(self.warnings)
        rates = []
        if not rows:
            warnings.append(f"No NGNMarket FX data for {fc}/{tc}")
        for r in rows:
            if not isinstance(r, dict):
                warnings.append(
                    f"Malformed NGNMarket FX row skipped for {fc}/{tc}: not an object"
                )
                continue
            returned_source = str(
                r.get("source") or r.get("from_currency") or fc
            ).upper()
            returned_target = str(r.get("target") or r.get("to_currency") or tc).upper()
            if (returned_source, returned_target) != (fc.upper(), tc.upper()):
                raise ProviderError(
                    f"NGNMarket FX orientation mismatch: requested {fc}/{tc}, "
                    f"received {returned_source}/{returned_target}",
                    provider=self.provider_name,
                    code="fx_orientation_mismatch",
                )
            d = r.get("date") or r.get("timestamp")
            val = r.get("rate", r.get("close"))
            if not d or val in (None, ""):
                warnings.append(
                    f"NGNMarket FX row skipped for {fc}/{tc}: missing date or rate"
                )
                continue
            try:
                dt = date.fromisoformat(str(d)[:10])
                if start_date <= dt <= end_date:
                    rates.append(
                        HistoricalFXRate(
                            fc,
                            tc,
                            float(val),
                            dt,
                            provider_name=self.provider_name,
                            raw_metadata=r,
                        )
                    )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                warnings.append(
                    f"Malformed NGNMarket FX row skipped for {fc}/{tc}: {exc}"
                )
        return HistoricalFXSeries(
            fc,
            tc,
            rates,
            self.provider_name,
            datetime.now(UTC),
            start_date,
            end_date,
            warnings,
            {"rate_count": len(rates), "quota_metadata": self.quota_metadata},
        )

    def fetch_company_list(self, exchange="NGX", force_refresh=False):
        key = {"provider": self.provider_name, "exchange": exchange}
        if not force_refresh and (c := self.cache.read("raw", **key)):
            self.raw_response_paths.append(str(self.cache.read_path("raw", **key)))
            return self.parse_envelope(c["data"])
        p = self._request_json("/companies")
        self.raw_response_paths.append(str(self.cache.write("raw", p, **key)))
        data = self.parse_envelope(p)
        return data.get("data", data) if isinstance(data, dict) else data

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
                else self.parse_company_chart(
                    instrument, c["data"], start_date, end_date
                )
            )
        p = self._request_json(
            f"/companies/{instrument}/chart",
            {
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "format": "detailed",
            },
        )
        self.raw_response_paths.append(str(self.cache.write("raw", p, **key)))
        series = self.parse_company_chart(instrument, p, start_date, end_date)
        if not series.bars:
            raise ProviderError(
                f"NGNMarket returned no covered chart rows for {instrument}",
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
            "api_call_count": 1,
            "actual_start": str(series.bars[0].timestamp),
            "actual_end": str(series.bars[-1].timestamp),
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
                else self.parse_fx_history(
                    from_currency, to_currency, c["data"], start_date, end_date
                )
            )
        p = self._request_json(
            "/forex/history",
            {
                "source": from_currency,
                "target": to_currency,
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
            },
        )
        self.raw_response_paths.append(str(self.cache.write("raw", p, **key)))
        series = self.parse_fx_history(
            from_currency, to_currency, p, start_date, end_date
        )
        if not series.rates:
            raise ProviderError(
                f"NGNMarket returned no covered FX rows for {from_currency}/{to_currency}",
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
            "api_call_count": 1,
            "actual_start": str(series.rates[0].timestamp),
            "actual_end": str(series.rates[-1].timestamp),
        }
        return series

    def fetch_index_history(
        self, index_symbol, start_date, end_date, force_refresh=False
    ):
        key = {
            "provider": self.provider_name,
            "index": index_symbol,
            "start": str(start_date),
            "end": str(end_date),
        }
        if not force_refresh and (c := self.cache.read("normalized", **key)):
            self.cache_paths.append(str(self.cache.read_path("normalized", **key)))
            return self.parse_company_chart(
                index_symbol, c["data"], start_date, end_date, AssetType.OTHER
            )
        p = self._request_json(
            f"/indices/{index_symbol}/chart",
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )
        self.raw_response_paths.append(str(self.cache.write("raw", p, **key)))
        self.cache_paths.append(str(self.cache.write("normalized", p, **key)))
        return self.parse_company_chart(
            index_symbol, p, start_date, end_date, AssetType.OTHER
        )


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
