"""Central Bank of Nigeria official exchange rates (reference rates).

Source: ``https://www.cbn.gov.ng/api/GetAllExchangeRates``, the JSON feed
behind the CBN exchange-rate page. One record per currency per CBN business
day with ``buyingrate``, ``centralrate`` and ``sellingrate`` (NGN per unit of
the currency); US dollar history runs from December 2001. No key is needed.

What the rate is, and is not:

* It is the **official reference rate** published by the CBN. Since the June
  2023 FX-market unification the CBN describes its NFEM rate as a volume-
  weighted average that "stands as the official exchange rate for the day".
* Before unification (in this dataset, 2018 to June 2023) the official rate
  was an administered rate that differed widely from the rates at which
  most market participants could actually convert (I&E window / NAFEX,
  parallel market). It is not an executable conversion rate for those years.
* It is published after the trading day, so a valuation that uses day t's
  CBN rate cannot be made before that publication.

These series are therefore labelled ``rate_kind = "official_reference"``.
Market-indicative series (NGNMarket, Alpha Vantage) are labelled
``"market_indicative"``. No provider in this repository supplies executable
dealer quotes; see docs/governance/FX_SOURCES.md.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, date, datetime

from .cache import HistoricalDataCache
from .models import HistoricalFXRate, HistoricalFXSeries
from .providers import ProviderError, validate_provider_url

CBN_URL = "https://www.cbn.gov.ng/api/GetAllExchangeRates"
RATE_KIND = "official_reference"
# CBN currency labels are inconsistent (trailing spaces/tabs, synonyms).
_CURRENCY_NAMES = {
    "US DOLLAR": "USD",
    "EURO": "EUR",
    "POUND STERLING": "GBP",
    "POUNDS STERLING": "GBP",
    "SWISS FRANC": "CHF",
    "YEN": "JPY",
    "JAPANESE YEN": "JPY",
    "YUAN/RENMINBI": "CNY",
    "SOUTH AFRICAN RAND": "ZAR",
    "RIYAL": "SAR",
    "UAE DIRHAM": "AED",
    "DANISH KRONA": "DKK",
    "DANISH KRONER": "DKK",
}
RATE_FIELDS = {"central": "centralrate", "buying": "buyingrate", "selling": "sellingrate"}


def normalise_currency(name: str) -> str | None:
    return _CURRENCY_NAMES.get(" ".join(str(name).split()).upper())


def parse_cbn_rates(
    rows: list[dict],
    currency: str,
    start_date: date,
    end_date: date,
    rate_field: str = "central",
) -> HistoricalFXSeries:
    """CBN rows -> currency/NGN series (NGN per unit of currency)."""
    key = RATE_FIELDS[rate_field]
    warnings: list[str] = []
    by_date: dict[date, HistoricalFXRate] = {}
    for r in rows:
        if normalise_currency(r.get("currency", "")) != currency.upper():
            continue
        try:
            d = date.fromisoformat(str(r.get("ratedate"))[:10])
            rate = float(str(r.get(key)))
        except (TypeError, ValueError):
            warnings.append(f"CBN row skipped: {r.get('id')}")
            continue
        if not start_date <= d <= end_date or rate <= 0:
            continue
        if d in by_date and by_date[d].rate != rate:
            warnings.append(f"CBN duplicate {currency} {d}: {by_date[d].rate} vs {rate}; kept later id")
            if int(r.get("id", 0)) < int(by_date[d].raw_metadata.get("id", 0)):
                continue
        by_date[d] = HistoricalFXRate(
            currency.upper(), "NGN", rate, d, source="cbn_official",
            provider_name="cbn",
            raw_metadata={**r, "rate_field": rate_field, "rate_kind": RATE_KIND},
        )
    rates = [by_date[d] for d in sorted(by_date)]
    return HistoricalFXSeries(
        currency.upper(), "NGN", rates, "cbn", datetime.now(UTC), start_date, end_date,
        warnings,
        {"rate_count": len(rates), "rate_kind": RATE_KIND, "rate_field": rate_field,
         "source_url": CBN_URL},
    )


class CBNReferenceRateProvider:
    provider_name = "cbn"

    def __init__(self, cache: HistoricalDataCache | None = None):
        self.cache = cache or HistoricalDataCache()
        self.cache_paths: list[str] = []
        self.raw_response_paths: list[str] = []
        self.quota_metadata: dict = {}
        self.total_api_call_count = 0
        self.provider_coverage_summary: dict = {}
        self._rows: list[dict] | None = None

    def _all_rows(self, force_refresh: bool) -> list[dict]:
        if self._rows is not None:
            return self._rows
        key = {"provider": "cbn", "endpoint": "GetAllExchangeRates", "day": datetime.now(UTC).date().isoformat()}
        cached = None if force_refresh else self.cache.read("raw", **key)
        if cached:
            self._rows = cached["data"]
        else:
            validate_provider_url(CBN_URL, {"www.cbn.gov.ng"})
            req = urllib.request.Request(CBN_URL, headers={"User-Agent": "collateral-risk-engine"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:  # nosec B310
                    self._rows = json.loads(r.read().decode())
            except Exception as exc:
                raise ProviderError(f"CBN request failed: {exc}", provider="cbn",
                                    code="transport_error") from exc
            self.total_api_call_count += 1
        self.raw_response_paths.append(str(self.cache.write("raw", self._rows, **key)))
        return self._rows or []

    def fetch_fx_history(self, from_currency, to_currency, start_date, end_date,
                         force_refresh=False, rate_field="central"):
        if to_currency.upper() != "NGN":
            raise ProviderError(f"CBN publishes NGN rates only, not {from_currency}/{to_currency}",
                                provider="cbn", code="unsupported_pair")
        series = parse_cbn_rates(self._all_rows(force_refresh), from_currency, start_date,
                                 end_date, rate_field)
        if not series.rates:
            raise ProviderError(f"CBN returned no {from_currency}/NGN rows in the window",
                                provider="cbn", code="empty_coverage")
        from .models import canonical_series_payload

        self.cache_paths.append(str(self.cache.write(
            "normalized", canonical_series_payload(series), provider="cbn",
            pair=f"{from_currency}{to_currency}", start=str(start_date), end=str(end_date),
            field=rate_field)))
        self.provider_coverage_summary = {"requested": 1, "fetched": 1, "api_call_count": 0}
        return series
