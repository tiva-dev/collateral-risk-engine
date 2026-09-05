from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.cache import HistoricalDataCache
from app.historical_data.config import load_config
from app.historical_data.manifest import write_manifest
from app.historical_data.models import (
    HistoricalDatasetManifest,
    HistoricalFXRate,
    HistoricalFXSeries,
    HistoricalSeries,
    canonical_series_payload,
)
from app.historical_data.ngnmarket import NGNMarketHistoricalProvider
from app.historical_data.providers import ProviderError
from app.simulations.config.official_validation_universe import (
    FX_PAIRS,
    NGX_UNIVERSE,
    START_DATE,
    US_UNIVERSE,
    official_universe,
)

DEFAULT_CALL_BUDGETS = {
    "ngnmarket": {"monthly": 3000, "max_per_run": 500},
    "alpha_vantage": {"monthly": None, "max_per_run": 100},
    "alpaca": {"monthly": None, "max_per_run": 500},
}
INDEPENDENT_FX_PAIRS = ("USD/NGN", "EUR/USD")
NGX_RELIABLE_LOOKBACK_DAYS = 370


def _inverse_fx_series(
    source: HistoricalFXSeries,
    from_currency: str,
    to_currency: str,
) -> HistoricalFXSeries:
    source_pair = f"{source.from_currency}/{source.to_currency}"
    rates = [
        HistoricalFXRate(
            from_currency,
            to_currency,
            1.0 / row.rate,
            row.timestamp,
            source="derived_historical_provider",
            provider_name=source.provider_name,
            quality_score=row.quality_score,
            warnings=[f"derived_inverse:{source_pair}"],
            raw_metadata={
                "derivation": "inverse",
                "source_pair": source_pair,
                "source_rate": row.rate,
            },
        )
        for row in source.rates
        if row.rate > 0
    ]
    return HistoricalFXSeries(
        from_currency,
        to_currency,
        rates,
        source.provider_name,
        datetime.now(UTC),
        source.start_date,
        source.end_date,
        [f"Derived {from_currency}/{to_currency} as inverse of {source_pair}"],
        {
            "rate_count": len(rates),
            "derivation": "inverse",
            "source_pairs": [source_pair],
        },
    )


def _cross_fx_series(
    first: HistoricalFXSeries,
    second: HistoricalFXSeries,
    from_currency: str,
    to_currency: str,
) -> HistoricalFXSeries:
    first_pair = f"{first.from_currency}/{first.to_currency}"
    second_pair = f"{second.from_currency}/{second.to_currency}"
    second_by_date = {row.timestamp: row for row in second.rates}
    rates = []
    for row in first.rates:
        other = second_by_date.get(row.timestamp)
        if other is None or row.rate <= 0 or other.rate <= 0:
            continue
        rates.append(
            HistoricalFXRate(
                from_currency,
                to_currency,
                row.rate * other.rate,
                row.timestamp,
                source="derived_historical_provider",
                provider_name=first.provider_name,
                quality_score=min(row.quality_score, other.quality_score),
                warnings=[f"derived_cross:{first_pair}*{second_pair}"],
                raw_metadata={
                    "derivation": "cross",
                    "source_pairs": [first_pair, second_pair],
                    "source_rates": [row.rate, other.rate],
                },
            )
        )
    return HistoricalFXSeries(
        from_currency,
        to_currency,
        rates,
        first.provider_name,
        datetime.now(UTC),
        max(first.start_date, second.start_date),
        min(first.end_date, second.end_date),
        [
            (
                f"Derived {from_currency}/{to_currency} as "
                f"{first_pair} multiplied by {second_pair}"
            )
        ],
        {
            "rate_count": len(rates),
            "derivation": "cross",
            "source_pairs": [first_pair, second_pair],
        },
    )


def _cache_derived_fx(
    cache: HistoricalDataCache,
    series: HistoricalFXSeries,
) -> str:
    return str(
        cache.write(
            "normalized",
            canonical_series_payload(series),
            provider=series.provider_name,
            pair=f"{series.from_currency}{series.to_currency}",
            start=str(series.start_date),
            end=str(series.end_date),
        )
    )


def _raise_missing_derived_fx(pair: str, provider_name: str) -> None:
    raise ProviderError(
        f"Cannot derive {pair}: independent FX source series unavailable",
        provider=provider_name,
        code="source_fx_unavailable",
    )


def _provider_budget(provider: str) -> dict:
    key = provider.upper()
    defaults = DEFAULT_CALL_BUDGETS[provider]
    monthly = os.getenv(f"{key}_MONTHLY_CALL_BUDGET")
    max_run = os.getenv(f"{key}_MAX_CALLS_PER_RUN")
    return {
        "monthly_call_budget": int(monthly) if monthly else defaults["monthly"],
        "max_calls_per_run": int(max_run) if max_run else defaults["max_per_run"],
    }


class OfficialDatasetBuilder:
    def __init__(
        self, providers: Iterable[str] | None = None, output_dir: str | None = None
    ):
        self.provider_names = set(providers or ["alpaca", "ngnmarket", "alpha_vantage"])
        self.config = load_config()
        self.output_dir = output_dir or self.config.simulation_output_dir
        self.alpaca_feed = os.getenv("ALPACA_DATA_FEED", "iex").strip().lower()
        if self.alpaca_feed not in {"iex", "sip", "boats", "otc"}:
            raise ValueError("ALPACA_DATA_FEED must be one of: iex, sip, boats, otc")

    def plan_calls(self):
        calls = []
        if "alpaca" in self.provider_names:
            calls += [
                {"provider": "alpaca", "operation": "fetch_equity_history", "symbol": s}
                for s in US_UNIVERSE
            ]
        if "ngnmarket" in self.provider_names:
            calls += [
                {
                    "provider": "ngnmarket",
                    "operation": "fetch_equity_history",
                    "symbol": s,
                }
                for s in NGX_UNIVERSE
            ]
        for pair in INDEPENDENT_FX_PAIRS:
            if "ngnmarket" in self.provider_names:
                calls.append(
                    {
                        "provider": "ngnmarket",
                        "operation": "fetch_fx_history",
                        "pair": pair,
                    }
                )
            if "alpha_vantage" in self.provider_names:
                calls.append(
                    {
                        "provider": "alpha_vantage",
                        "operation": "fetch_fx_history",
                        "pair": pair,
                    }
                )
        return calls

    def estimate_call_counts(self):
        counts = {}
        for call in self.plan_calls():
            counts[call["provider"]] = counts.get(call["provider"], 0) + 1
        return counts

    def enforce_call_budgets(
        self, override: bool = False, max_provider_calls: int | None = None
    ):
        planned = self.estimate_call_counts()
        out = {}
        errors = []
        for provider, count in planned.items():
            budget = _provider_budget(provider)
            limit = (
                max_provider_calls
                if max_provider_calls is not None
                else budget["max_calls_per_run"]
            )
            out[provider] = {
                **budget,
                "planned_calls": count,
                "effective_max_calls_per_run": limit,
                "override": override,
            }
            if count > limit and not override:
                errors.append(
                    f"{provider} planned calls {count} exceed max calls per run {limit}"
                )
        if errors:
            raise RuntimeError("; ".join(errors))
        return out

    def build(
        self,
        start_date: date = START_DATE,
        end_date: date | None = None,
        force_refresh: bool = False,
        dry_run: bool = True,
        override_quota: bool = False,
        max_provider_calls: int | None = None,
    ) -> HistoricalDatasetManifest:
        end = end_date or datetime.now(UTC).date()
        missing = []
        reasons = {}
        warnings = []
        cache_paths = []
        raw_paths = []
        quota = {}
        earliest = {}
        identities = {}
        coverage = {}
        planned_counts = self.estimate_call_counts()
        budget_summary = (
            self.enforce_call_budgets(override_quota, max_provider_calls)
            if not dry_run
            else {
                p: {**_provider_budget(p), "planned_calls": c, "actual_calls": 0}
                for p, c in planned_counts.items()
            }
        )
        notes = (
            ["Dry-run mode does not call provider APIs."]
            if dry_run
            else ["Cache-first provider retrieval used unless force_refresh=true."]
        )
        if "alpaca" in self.provider_names:
            notes.append(f"Alpaca market-data feed: {self.alpaca_feed}.")
        ngx_start_date = max(
            start_date,
            end - timedelta(days=NGX_RELIABLE_LOOKBACK_DAYS),
        )
        if "ngnmarket" in self.provider_names:
            notes.append(
                "NGX equity history is limited to the latest 370 calendar days "
                f"({ngx_start_date.isoformat()} to {end.isoformat()}) because "
                "that is the currently validated reliable provider window."
            )
        providers = {}
        ngnmarket_preflight_error: str | None = None
        if not dry_run:
            providers = {
                "alpaca": AlpacaTradingHistoricalProvider(),
                "ngnmarket": NGNMarketHistoricalProvider(),
                "alpha_vantage": AlphaVantageHistoricalProvider(),
            }
            if "ngnmarket" in self.provider_names:
                try:
                    companies = providers["ngnmarket"].fetch_company_list(
                        "NGX", force_refresh=force_refresh
                    )
                    company_symbols = (
                        {
                            str(c.get("symbol") or c.get("ticker") or c.get("code"))
                            for c in companies
                        }
                        if isinstance(companies, list)
                        else set()
                    )
                    for sym in NGX_UNIVERSE:
                        if company_symbols and sym not in company_symbols:
                            missing.append(sym)
                            reasons[sym] = "NGNMarket company list mapping missing"
                            warnings.append(f"NGNMarket mapping missing for {sym}")
                except Exception as exc:  # noqa: BLE001 - provider boundary
                    ngnmarket_preflight_error = str(exc)
                    warnings.append(f"NGNMarket company list validation failed: {exc}")

            def record(name: str, symbol: str, fn, *, derived: bool = False):
                try:
                    if name == "ngnmarket" and ngnmarket_preflight_error:
                        raise ProviderError(
                            ngnmarket_preflight_error,
                            provider="ngnmarket",
                            code="provider_preflight_failed",
                        )
                    series = fn()
                    count = (
                        len(series.bars)
                        if isinstance(series, HistoricalSeries)
                        else len(series.rates)
                        if isinstance(series, HistoricalFXSeries)
                        else 0
                    )
                    coverage.setdefault(
                        name,
                        {
                            "requested": 0,
                            "available": 0,
                            "missing": 0,
                            "cached": 0,
                            "fetched": 0,
                            "page_count": 0,
                            "api_call_count": 0,
                        },
                    )
                    coverage[name]["requested"] += 1
                    pcs = (
                        getattr(providers.get(name), "provider_coverage_summary", {})
                        or {}
                    )
                    if not derived:
                        coverage[name]["cached"] += int(pcs.get("cached", 0))
                        coverage[name]["fetched"] += int(pcs.get("fetched", 0))
                        coverage[name]["page_count"] += int(pcs.get("page_count", 0))
                        coverage[name]["api_call_count"] += int(
                            pcs.get("api_call_count", 0)
                        )
                    if count:
                        coverage[name]["available"] += 1
                    else:
                        coverage[name]["missing"] += 1
                        missing.append(symbol)
                        reasons[symbol] = "provider returned no data"
                    warnings.extend(getattr(series, "warnings", []) or [])
                    if getattr(series, "instrument_identity", None):
                        identities[symbol] = asdict(series.instrument_identity)
                    if isinstance(series, HistoricalSeries) and series.bars:
                        earliest[symbol] = min(b.timestamp for b in series.bars)
                    if isinstance(series, HistoricalFXSeries) and series.rates:
                        earliest[symbol] = min(r.timestamp for r in series.rates)
                    return series
                except Exception as exc:  # noqa: BLE001 - provider boundary
                    coverage.setdefault(
                        name,
                        {
                            "requested": 0,
                            "available": 0,
                            "missing": 0,
                            "cached": 0,
                            "fetched": 0,
                            "page_count": 0,
                            "api_call_count": 0,
                        },
                    )
                    coverage[name]["requested"] += 1
                    coverage[name]["missing"] += 1
                    missing.append(symbol)
                    reasons[symbol] = str(exc)
                    warnings.append(f"{name} failed for {symbol}: {exc}")
                    return None

            for s in US_UNIVERSE if "alpaca" in self.provider_names else []:
                record(
                    "alpaca",
                    s,
                    lambda s=s: providers["alpaca"].fetch_equity_history(
                        s,
                        start_date,
                        end,
                        force_refresh=force_refresh,
                        feed=self.alpaca_feed,
                    ),
                )
            for s in NGX_UNIVERSE if "ngnmarket" in self.provider_names else []:
                record(
                    "ngnmarket",
                    s,
                    lambda s=s: providers["ngnmarket"].fetch_equity_history(
                        s, ngx_start_date, end, force_refresh=force_refresh
                    ),
                )
            provider_fx: dict[tuple[str, str], HistoricalFXSeries] = {}
            for pair in INDEPENDENT_FX_PAIRS:
                fc, tc = pair.split("/")
                if "ngnmarket" in self.provider_names:
                    series = record(
                        "ngnmarket",
                        pair,
                        lambda fc=fc, tc=tc: providers["ngnmarket"].fetch_fx_history(
                            fc, tc, start_date, end, force_refresh=force_refresh
                        ),
                    )
                    if isinstance(series, HistoricalFXSeries):
                        provider_fx[("ngnmarket", pair)] = series
                if "alpha_vantage" in self.provider_names:
                    series = record(
                        "alpha_vantage",
                        pair,
                        lambda fc=fc, tc=tc: providers[
                            "alpha_vantage"
                        ].fetch_fx_history(
                            fc, tc, start_date, end, force_refresh=force_refresh
                        ),
                    )
                    if isinstance(series, HistoricalFXSeries):
                        provider_fx[("alpha_vantage", pair)] = series

            for provider_name in ("ngnmarket", "alpha_vantage"):
                if provider_name not in self.provider_names:
                    continue
                usd_ngn = provider_fx.get((provider_name, "USD/NGN"))
                eur_usd = provider_fx.get((provider_name, "EUR/USD"))
                derived_series: list[HistoricalFXSeries] = []
                if usd_ngn:
                    derived_series.append(
                        _inverse_fx_series(usd_ngn, "NGN", "USD")
                    )
                if eur_usd:
                    derived_series.append(
                        _inverse_fx_series(eur_usd, "USD", "EUR")
                    )
                if usd_ngn and eur_usd:
                    eur_ngn = _cross_fx_series(
                        eur_usd, usd_ngn, "EUR", "NGN"
                    )
                    derived_series.extend(
                        [
                            eur_ngn,
                            _inverse_fx_series(eur_ngn, "NGN", "EUR"),
                        ]
                    )
                derived_by_pair = {
                    f"{series.from_currency}/{series.to_currency}": series
                    for series in derived_series
                }
                for pair in (
                    "NGN/USD",
                    "USD/EUR",
                    "EUR/NGN",
                    "NGN/EUR",
                ):
                    series = derived_by_pair.get(pair)
                    if series is None:
                        record(
                            provider_name,
                            pair,
                            lambda pair=pair, provider_name=provider_name: (
                                _raise_missing_derived_fx(pair, provider_name)
                            ),
                            derived=True,
                        )
                        continue
                    provider = providers[provider_name]
                    provider.cache_paths.append(
                        _cache_derived_fx(provider.cache, series)
                    )
                    record(
                        provider_name,
                        pair,
                        lambda series=series: series,
                        derived=True,
                    )
            for k, v in providers.items():
                if k in self.provider_names:
                    meta = getattr(v, "quota_metadata", {}) or {}
                    quota[k] = {
                        **budget_summary.get(k, {}),
                        "actual_calls": int(getattr(v, "total_api_call_count", 0)),
                        "provider_metadata": meta,
                    }
                    cache_paths += getattr(v, "cache_paths", [])
                    raw_paths += getattr(v, "raw_response_paths", [])
        else:
            quota = {
                p: {**budget_summary.get(p, {}), "actual_calls": 0}
                for p in self.provider_names
            }
            coverage = {
                p: {
                    "planned_calls": sum(
                        1 for c in self.plan_calls() if c["provider"] == p
                    ),
                    "requested": 0,
                    "available": 0,
                    "missing": 0,
                    "cached": 0,
                    "fetched": 0,
                    "page_count": 0,
                    "api_call_count": 0,
                    "cache_paths": "none in dry-run",
                }
                for p in self.provider_names
            }
        return HistoricalDatasetManifest(
            dataset_id="official-validation-"
            + datetime.now(UTC).strftime("%Y%m%d%H%M%S"),
            provider=",".join(sorted(self.provider_names)),
            universe=official_universe(),
            instruments=US_UNIVERSE + NGX_UNIVERSE,
            fx_pairs=FX_PAIRS,
            start_date=start_date,
            end_date=end,
            cache_paths=cache_paths,
            raw_response_paths=raw_paths,
            provider_quota_metadata=quota,
            warnings=warnings,
            missing_symbols=missing,
            earliest_available_date_by_symbol=earliest,
            methodology_notes=notes,
            missing_symbol_reasons=reasons,
            provider_coverage_summary=coverage,
            instrument_identities=identities,
        )

    def write_manifest(self, manifest):
        return write_manifest(manifest, self.output_dir)
