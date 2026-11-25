from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, date, datetime

from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.cache import HistoricalDataCache
from app.historical_data.calendars import (
    SessionCalendar,
    fx_daily_calendar,
    fx_weekday_calendar,
    infer_market_closures,
    ngx_calendar,
    nyse_calendar,
)
from app.historical_data.config import load_config
from app.historical_data.coverage import (
    CoverageReport,
    summarise,
    validate_series_coverage,
)
from app.historical_data.manifest import write_manifest
from app.historical_data.models import (
    OHLC_FIELDS,
    PROVENANCE_REPORTED,
    TRADE_STATUS_NO_TRADE,
    HistoricalDatasetManifest,
    HistoricalFXRate,
    HistoricalFXSeries,
    HistoricalSeries,
    canonical_series_payload,
)
from app.historical_data.ngnmarket import (
    NGNMarketHistoricalProvider,
    ngnmarket_supports_fx_pair,
)
from app.historical_data.providers import ProviderError
from app.simulations.config.instrument_reference import reference_for
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
# Pairs each provider is asked for directly. NGNMarket quotes naira pairs only
# (EUR/USD comes back empty), so it supplies EUR/NGN and EUR/USD is derived.
PROVIDER_FX_PAIRS: dict[str, tuple[str, ...]] = {
    "ngnmarket": ("USD/NGN", "EUR/NGN"),
    "alpha_vantage": ("USD/NGN", "EUR/USD"),
}
# Provider precedence per pair when several providers supply it. Provisional:
# which NGN reference rate governs valuation is a Tiva decision (see
# docs/governance/ROADMAP.md). A provider is used only if its series passes
# coverage validation; otherwise the next one is tried. Both series are kept
# and reconciled either way.
FX_SOURCE_PRECEDENCE: dict[str, tuple[str, ...]] = {
    "USD/NGN": ("ngnmarket", "alpha_vantage"),
    "NGN/USD": ("ngnmarket", "alpha_vantage"),
    "EUR/NGN": ("ngnmarket", "alpha_vantage"),
    "NGN/EUR": ("ngnmarket", "alpha_vantage"),
    "EUR/USD": ("alpha_vantage", "ngnmarket"),
    "USD/EUR": ("alpha_vantage", "ngnmarket"),
}
FX_CALENDARS = {"ngnmarket": fx_daily_calendar, "alpha_vantage": fx_weekday_calendar}


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


def derive_fx_set(
    independent: dict[str, HistoricalFXSeries], targets: Iterable[str] = FX_PAIRS
) -> dict[str, HistoricalFXSeries]:
    """Derive every target pair from one provider's independent series.

    Uses inverses and single-hop crosses only, so each derived rate traces to
    at most two provider observations on the same date.
    """
    have = dict(independent)
    targets = list(targets)
    progress = True
    while progress:
        progress = False
        for pair in targets:
            if pair in have:
                continue
            fc, tc = pair.split("/")
            inverse = have.get(f"{tc}/{fc}")
            if inverse is not None:
                have[pair] = _inverse_fx_series(inverse, fc, tc)
                progress = True
                continue
            for mid_pair, first in list(have.items()):
                a, mid = mid_pair.split("/")
                second = have.get(f"{mid}/{tc}")
                if a == fc and second is not None and mid not in (fc, tc):
                    have[pair] = _cross_fx_series(first, second, fc, tc)
                    progress = True
                    break
    return {k: v for k, v in have.items() if k not in independent}


def reconcile_fx(
    a: HistoricalFXSeries, b: HistoricalFXSeries
) -> dict[str, object]:
    """Compare two providers' series for the same pair on common dates."""
    import math
    from itertools import pairwise

    ra = {_day(r.timestamp): r.rate for r in a.rates}
    rb = {_day(r.timestamp): r.rate for r in b.rates}
    common = sorted(set(ra) & set(rb))
    out: dict[str, object] = {
        "providers": [a.provider_name, b.provider_name],
        "common_dates": len(common),
        "only_first": len(set(ra) - set(rb)),
        "only_second": len(set(rb) - set(ra)),
    }
    if not common:
        return out
    diffs = sorted(abs(rb[d] / ra[d] - 1.0) for d in common)
    out["median_abs_rel_diff"] = round(diffs[len(diffs) // 2], 6)
    out["p95_abs_rel_diff"] = round(diffs[min(len(diffs) - 1, int(0.95 * len(diffs)))], 6)
    out["max_abs_rel_diff"] = round(diffs[-1], 6)
    worst = max(common, key=lambda d: abs(rb[d] / ra[d] - 1.0))
    out["max_diff_date"] = worst.isoformat()
    out["days_diff_over_1pct"] = sum(x > 0.01 for x in diffs)
    out["days_diff_over_5pct"] = sum(x > 0.05 for x in diffs)
    rets_a = [math.log(ra[y] / ra[x]) for x, y in pairwise(common)]
    rets_b = [math.log(rb[y] / rb[x]) for x, y in pairwise(common)]
    if len(rets_a) > 2:
        ma, mb = sum(rets_a) / len(rets_a), sum(rets_b) / len(rets_b)
        cov = sum((x - ma) * (y - mb) for x, y in zip(rets_a, rets_b, strict=True))
        va = sum((x - ma) ** 2 for x in rets_a)
        vb = sum((y - mb) ** 2 for y in rets_b)
        out["log_return_correlation"] = (
            round(cov / math.sqrt(va * vb), 6) if va > 0 and vb > 0 else None
        )
    return out


def _day(ts: date | datetime) -> date:
    return ts.date() if isinstance(ts, datetime) else ts


def imputation_summary(series: HistoricalSeries) -> dict[str, object]:
    by_field = {f: 0 for f in OHLC_FIELDS}
    with_provenance = 0
    no_trade = 0
    bars_with_imputed_range = 0
    volume_missing = 0
    first_reported_volume = None
    for bar in series.bars:
        if bar.field_provenance.get("volume") == "missing":
            volume_missing += 1
        elif bar.field_provenance.get("volume") and first_reported_volume is None:
            first_reported_volume = _day(bar.timestamp).isoformat()
        if bar.field_provenance:
            with_provenance += 1
        for f in bar.imputed_fields():
            by_field[f] += 1
        if bar.field_provenance and not bar.has_reported_range():
            bars_with_imputed_range += 1
        if bar.trade_status == TRADE_STATUS_NO_TRADE:
            no_trade += 1
    return {
        "bars": len(series.bars),
        "bars_with_field_provenance": with_provenance,
        "imputed_by_field": by_field,
        "bars_with_imputed_range": bars_with_imputed_range,
        "no_trade_bars": no_trade,
        "volume_missing_bars": volume_missing,
        "first_reported_volume": first_reported_volume,
        "provenance_values": sorted(
            {v for b in series.bars for v in b.field_provenance.values()}
            - {PROVENANCE_REPORTED}
        ),
    }


def _coverage_for(
    name: str,
    dates: list[date],
    calendar: SessionCalendar,
    start: date,
    end: date,
    symbol: str | None = None,
) -> CoverageReport:
    ref = reference_for(symbol) if symbol else None
    return validate_series_coverage(
        name,
        dates,
        calendar,
        start,
        end,
        listing_date=ref.listing_date if ref else None,
        listing_verified=ref.verified if ref else False,
        suspensions=[(s.start, s.end, s.reason) for s in ref.suspensions] if ref else (),
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
        for provider in ("ngnmarket", "alpha_vantage"):
            if provider not in self.provider_names:
                continue
            for pair in PROVIDER_FX_PAIRS[provider]:
                calls.append(
                    {"provider": provider, "operation": "fetch_fx_history", "pair": pair}
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
        if end <= start_date:
            # Run 34000523667 was dispatched with start 2025-09-05 and end
            # 2025-09-04; every provider then returned nothing and the failure
            # surfaced only as "no usable equity histories". Fail fast instead.
            raise ValueError(
                f"Invalid dataset window: end date {end.isoformat()} must be after "
                f"start date {start_date.isoformat()}"
            )
        missing = []
        reasons = {}
        warnings = []
        cache_paths = []
        raw_paths = []
        quota = {}
        earliest = {}
        identities = {}
        coverage = {}
        coverage_validation: dict = {}
        fx_selection: dict = {}
        fx_recon: dict = {}
        alternate_fx_paths: list[str] = []
        imputations: dict = {}
        inferred_closures: dict = {}
        equity_series: dict[str, HistoricalSeries] = {}
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
                        equity_series[symbol] = series
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
                        s, start_date, end, force_refresh=force_refresh
                    ),
                )
            fx_by_provider: dict[str, dict[str, HistoricalFXSeries]] = {}
            fx_cache_path: dict[tuple[str, str], list[str]] = {}

            def fetch_fx(provider_name: str, pair: str, fn, derived=False):
                prov = providers[provider_name]
                before = len(prov.cache_paths)
                series = record(provider_name, f"{pair}@{provider_name}", fn, derived=derived)
                fx_cache_path[(provider_name, pair)] = prov.cache_paths[before:]
                if isinstance(series, HistoricalFXSeries) and series.rates:
                    fx_by_provider.setdefault(provider_name, {})[pair] = series

            for provider_name in ("ngnmarket", "alpha_vantage"):
                if provider_name not in self.provider_names:
                    continue
                for pair in PROVIDER_FX_PAIRS[provider_name]:
                    fc, tc = pair.split("/")
                    if provider_name == "ngnmarket" and not ngnmarket_supports_fx_pair(fc, tc):
                        continue
                    fetch_fx(
                        provider_name,
                        pair,
                        lambda fc=fc, tc=tc, pn=provider_name: providers[
                            pn
                        ].fetch_fx_history(
                            fc, tc, start_date, end, force_refresh=force_refresh
                        ),
                    )
                independent = dict(fx_by_provider.get(provider_name, {}))
                derived_set = derive_fx_set(independent)
                for pair in FX_PAIRS:
                    if pair in independent or pair in PROVIDER_FX_PAIRS[provider_name]:
                        continue
                    series = derived_set.get(pair)
                    if series is None:
                        fetch_fx(
                            provider_name,
                            pair,
                            lambda pair=pair, pn=provider_name: (
                                _raise_missing_derived_fx(pair, pn)
                            ),
                            derived=True,
                        )
                        continue
                    provider = providers[provider_name]
                    provider.cache_paths.append(_cache_derived_fx(provider.cache, series))
                    fetch_fx(provider_name, pair, lambda series=series: series, derived=True)

            # Coverage validation. NGX and NYSE calendars are extended with
            # market-wide closures inferred from the cross-section.
            coverage_reports: dict[str, CoverageReport] = {}
            for market, universe, base in (
                ("ngx", NGX_UNIVERSE, ngx_calendar),
                ("us", US_UNIVERSE, nyse_calendar),
            ):
                observed = {
                    s: [_day(b.timestamp) for b in equity_series[s].bars]
                    for s in universe
                    if s in equity_series
                }
                if not observed:
                    continue
                cal = base(start_date, end)
                closures = infer_market_closures(cal, observed, start_date, end)
                inferred_closures[market] = {d.isoformat(): why for d, why in closures.items()}
                cal = cal.with_closures(closures)
                for s, dates in observed.items():
                    coverage_reports[s] = _coverage_for(s, dates, cal, start_date, end, s)
            for provider_name, pairs in fx_by_provider.items():
                cal = FX_CALENDARS[provider_name](start_date, end)
                for pair, series in pairs.items():
                    key = f"{pair}@{provider_name}"
                    coverage_reports[key] = _coverage_for(
                        key, [_day(r.timestamp) for r in series.rates], cal, start_date, end
                    )

            # FX source selection and reconciliation.
            for pair in FX_PAIRS:
                candidates = [
                    pn for pn in FX_SOURCE_PRECEDENCE.get(pair, ())
                    if pair in fx_by_provider.get(pn, {})
                ]
                passing = [
                    pn for pn in candidates
                    if coverage_reports[f"{pair}@{pn}"].status != "fail"
                ]
                chosen = passing[0] if passing else (candidates[0] if candidates else None)
                fx_selection[pair] = {
                    "selected_provider": chosen,
                    "candidates": candidates,
                    "reason": (
                        "first provider in precedence passing coverage"
                        if passing
                        else "no candidate passes coverage; first available used"
                        if candidates
                        else "no provider supplied this pair"
                    ),
                    "precedence": list(FX_SOURCE_PRECEDENCE.get(pair, ())),
                }
                if chosen is None:
                    missing.append(pair)
                    reasons[pair] = "no provider supplied this pair"
                    continue
                selected_series = fx_by_provider[chosen][pair]
                earliest[pair] = min(r.timestamp for r in selected_series.rates)
                coverage_reports[pair] = coverage_reports[f"{pair}@{chosen}"]
                for pn in candidates:
                    paths = fx_cache_path.get((pn, pair), [])
                    if pn != chosen:
                        alternate_fx_paths.extend(paths)
                if len(candidates) > 1:
                    fx_recon[pair] = reconcile_fx(
                        fx_by_provider[candidates[0]][pair],
                        fx_by_provider[candidates[1]][pair],
                    )
            for k in list(earliest):
                if "@" in k:
                    del earliest[k]
            missing[:] = [m for m in missing if "@" not in m or m.split("@")[0] not in earliest]
            coverage_validation["series"] = {
                k: r.to_dict() for k, r in sorted(coverage_reports.items())
            }
            coverage_validation["summary"] = summarise(
                {k: r for k, r in coverage_reports.items() if "@" not in k}
            )
            coverage_validation["inferred_market_closures"] = inferred_closures
            for s, series in equity_series.items():
                imputations[s] = imputation_summary(series)
            for k, v in providers.items():
                if k in self.provider_names:
                    meta = getattr(v, "quota_metadata", {}) or {}
                    quota[k] = {
                        **budget_summary.get(k, {}),
                        "actual_calls": int(getattr(v, "total_api_call_count", 0)),
                        "provider_metadata": meta,
                    }
                    cache_paths += [
                        cp for cp in getattr(v, "cache_paths", [])
                        if cp not in alternate_fx_paths
                    ]
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
            coverage_validation=coverage_validation,
            fx_source_selection=fx_selection,
            fx_reconciliation=fx_recon,
            alternate_fx_cache_paths=alternate_fx_paths,
            imputation_summary=imputations,
        )

    def write_manifest(self, manifest):
        return write_manifest(manifest, self.output_dir)
