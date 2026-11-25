"""Tests for the NGNMarket FX fix, calendar-aware coverage and OHLC provenance.

Payload shapes mirror the structures observed in provider run 45665911547
(25 Nov 2026), reproduced with invented values.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
from app.historical_data.alpha_vantage import AlphaVantageHistoricalProvider
from app.historical_data.cache import HistoricalDataCache
from app.historical_data.calendars import (
    SessionCalendar,
    easter_sunday,
    fx_daily_calendar,
    infer_market_closures,
    ngx_rule_holidays,
    nyse_holidays,
)
from app.historical_data.coverage import validate_series_coverage
from app.historical_data.models import (
    PROVENANCE_IMPUTED_FROM_CLOSE,
    PROVENANCE_REPORTED,
    TRADE_STATUS_NO_TRADE,
    TRADE_STATUS_TRADED,
    HistoricalFXRate,
    HistoricalFXSeries,
)
from app.historical_data.ngnmarket import (
    NGNMarketHistoricalProvider,
    ngnmarket_supports_fx_pair,
)
from app.historical_data.providers import ProviderError
from app.simulations.data_builder import derive_fx_set, reconcile_fx
from app.simulations.evidence_quality import validate_provider_coverage


def _envelope(inner):
    return {
        "success": True,
        "data": inner,
        "meta": {"plan": "hobby", "calls_used": 1, "calls_remaining": 9999},
    }


def _fx_page(rows, page=1, pages=1):
    return _envelope(
        {
            "data": rows,
            "source": "USD",
            "target": "NGN",
            "format": "detailed",
            "period": "30d",
            "start_date": "2026-10-01T00:00:00.000Z",
            "end_date": "2026-10-10T00:00:00.000Z",
            "pagination": {
                "page": page,
                "pages": pages,
                "limit": 3,
                "total": 3 * pages,
                "has_next": page < pages,
                "has_prev": page > 1,
            },
            "statistics": {"latest": 1.0},
        }
    )


def _fx_row(d, rate, src="USD", tgt="NGN"):
    return {
        "rate": rate,
        "rate_date": f"{d.isoformat()}T00:00:00.000Z",
        "source_currency": src,
        "target_currency": tgt,
        "created_at": f"{d.isoformat()}T00:00:06.000Z",
        "timestamp_unix": 0,
    }


@pytest.fixture
def ngn(tmp_path, monkeypatch):
    monkeypatch.setenv("NGNMARKET_API_KEY", "ngm_live_test")
    return NGNMarketHistoricalProvider(cache=HistoricalDataCache(str(tmp_path)))


# --- NGNMarket FX -----------------------------------------------------------


def test_fx_rows_nested_under_data_data_are_parsed_ascending(ngn):
    d0 = date(2026, 10, 1)
    # Newest first, as the provider returns them.
    rows = [_fx_row(d0 + timedelta(days=i), 1350.0 + i) for i in range(4, -1, -1)]
    series = ngn.parse_fx_history("USD", "NGN", _fx_page(rows), d0, d0 + timedelta(days=9))
    assert [r.timestamp for r in series.rates] == [d0 + timedelta(days=i) for i in range(5)]
    assert series.rates[0].rate == 1350.0
    assert series.rates[-1].rate == 1354.0


def test_fx_orientation_is_read_from_source_and_target_currency(ngn):
    d0 = date(2026, 10, 1)
    rows = [_fx_row(d0, 0.0007, src="NGN", tgt="USD")]
    with pytest.raises(ProviderError) as exc:
        ngn.parse_fx_history("USD", "NGN", _fx_page(rows), d0, d0)
    assert exc.value.code == "fx_orientation_mismatch"


def test_fx_duplicate_dates_keep_later_publication_and_warn(ngn):
    d0 = date(2026, 10, 1)
    early = _fx_row(d0, 1350.0)
    late = {**_fx_row(d0, 1351.0), "created_at": "2026-10-01T09:00:00.000Z"}
    series = ngn.parse_fx_history("USD", "NGN", _fx_page([late, early]), d0, d0)
    assert [r.rate for r in series.rates] == [1351.0]
    assert any("conflicting" in w for w in series.warnings)


def test_fx_history_follows_pagination(ngn):
    d0 = date(2026, 10, 1)
    pages = [
        _fx_page([_fx_row(d0 + timedelta(days=i), 1300.0 + i) for i in range(3)], 1, 2),
        _fx_page([_fx_row(d0 + timedelta(days=i), 1300.0 + i) for i in range(3, 6)], 2, 2),
    ]
    with patch.object(ngn, "_request_json", side_effect=pages) as req:
        series = ngn.fetch_fx_history("USD", "NGN", d0, d0 + timedelta(days=9), True)
    assert req.call_count == 2
    assert req.call_args_list[1].args[1]["page"] == 2
    assert len(series.rates) == 6
    assert ngn.provider_coverage_summary["api_call_count"] == 2


def test_non_naira_pair_is_rejected_without_a_call(ngn):
    assert not ngnmarket_supports_fx_pair("EUR", "USD")
    assert ngnmarket_supports_fx_pair("EUR", "NGN")
    with patch.object(ngn, "_request_json") as req, pytest.raises(ProviderError) as exc:
        ngn.fetch_fx_history("EUR", "USD", date(2026, 1, 1), date(2026, 2, 1), True)
    req.assert_not_called()
    assert exc.value.code == "unsupported_pair"


def test_company_list_reads_every_page(ngn):
    def page(n, pages):
        return _envelope(
            {
                "data": [{"symbol": f"S{n}{i}"} for i in range(2)],
                "pagination": {"page": n, "pages": pages, "has_next": n < pages},
            }
        )

    with patch.object(ngn, "_request_json", side_effect=[page(1, 3), page(2, 3), page(3, 3)]):
        companies = ngn.fetch_company_list(force_refresh=True)
    assert len(companies) == 6


# --- OHLC provenance --------------------------------------------------------


def _chart(rows):
    return _envelope({"symbol": "X", "data": rows, "format": "detailed"})


def test_ngnmarket_chart_records_field_level_imputation(ngn):
    rows = [
        {"date": "2026-10-02", "open": 68, "high": None, "low": None, "close": 68,
         "price": 68, "volume": 0, "trade_count": 0},
        {"date": "2026-10-05", "open": 68, "high": 70, "low": 67, "close": 69,
         "price": 69, "volume": 1000, "trade_count": 12},
    ]
    series = ngn.parse_company_chart("X", _chart(rows), date(2026, 10, 1), date(2026, 10, 9))
    no_trade, traded = series.bars
    assert no_trade.field_provenance["high"] == PROVENANCE_IMPUTED_FROM_CLOSE
    assert no_trade.field_provenance["low"] == PROVENANCE_IMPUTED_FROM_CLOSE
    assert no_trade.field_provenance["open"] == PROVENANCE_REPORTED
    assert no_trade.imputed_fields() == ["high", "low"]
    assert not no_trade.has_reported_range()
    assert no_trade.trade_status == TRADE_STATUS_NO_TRADE
    assert traded.has_reported_range()
    assert traded.trade_status == TRADE_STATUS_TRADED


def test_provenance_survives_canonical_cache_round_trip(ngn, tmp_path):
    from app.historical_data.models import canonical_series_payload
    from app.simulations.run_official_validation import _bar_from_payload

    rows = [{"date": "2026-10-02", "open": 68, "high": None, "low": None, "close": 68}]
    series = ngn.parse_company_chart("X", _chart(rows), date(2026, 10, 1), date(2026, 10, 9))
    payload = canonical_series_payload(series)
    bar = _bar_from_payload({**payload["bars"][0], "instrument": "X"})
    assert bar.field_provenance["high"] == PROVENANCE_IMPUTED_FROM_CLOSE
    assert not bar.has_reported_range()


def test_alpaca_missing_close_is_skipped_not_zeroed(tmp_path):
    p = AlpacaTradingHistoricalProvider(cache=HistoricalDataCache(str(tmp_path)))
    payload = {
        "bars": [
            {"t": "2026-10-01T04:00:00Z", "o": 10, "h": 11, "l": 9, "v": 5},
            {"t": "2026-10-02T04:00:00Z", "o": 10, "l": 9, "c": 10.5, "v": 5},
        ]
    }
    series = p.parse_bars("SPY", payload, date(2026, 10, 1), date(2026, 10, 3), "1Day")
    assert len(series.bars) == 1
    assert series.bars[0].close == 10.5
    assert series.bars[0].field_provenance["high"] == PROVENANCE_IMPUTED_FROM_CLOSE
    assert any("missing close" in w for w in series.warnings)


def test_alpha_vantage_missing_close_is_skipped_not_zeroed(tmp_path):
    p = AlphaVantageHistoricalProvider(cache=HistoricalDataCache(str(tmp_path)))
    payload = {
        "Time Series (Daily)": {
            "2026-10-01": {"1. open": "10", "2. high": "11", "3. low": "9", "6. volume": "5"},
            "2026-10-02": {"1. open": "10", "2. high": "11", "3. low": "9",
                           "4. close": "10.2", "6. volume": "0"},
        }
    }
    series = p.parse_daily_adjusted("SPY", payload, date(2026, 10, 1), date(2026, 10, 3))
    assert [b.close for b in series.bars] == [10.2]
    assert series.bars[0].adjusted_close is None
    assert series.bars[0].field_provenance["adjusted_close"] == "missing"
    assert series.bars[0].trade_status == TRADE_STATUS_NO_TRADE


# --- Calendars ----------------------------------------------------------------


def test_easter_and_holiday_rules():
    assert easter_sunday(2024) == date(2024, 3, 31)
    assert easter_sunday(2025) == date(2025, 4, 20)
    nyse = nyse_holidays(2025)
    assert date(2025, 1, 9) in nyse  # Carter day of mourning
    assert date(2025, 4, 18) in nyse  # Good Friday
    assert date(2025, 6, 19) in nyse  # Juneteenth
    assert date(2021, 12, 31) not in nyse_holidays(2021)  # 1 Jan 2022 is a Saturday
    ngx = ngx_rule_holidays(2021)
    # Christmas 2021 is a Saturday: Christmas -> Mon 27, Boxing Day -> Tue 28.
    assert ngx[date(2021, 12, 27)] == "Christmas Day"
    assert ngx[date(2021, 12, 28)] == "Boxing Day"
    assert date(2018, 5, 29) in ngx_rule_holidays(2018)
    assert date(2019, 6, 12) in ngx_rule_holidays(2019)


def test_market_wide_closure_is_inferred_from_cross_section():
    cal = SessionCalendar("T", {})
    start, end = date(2026, 3, 2), date(2026, 3, 13)
    sessions = cal.sessions(start, end)
    eid = date(2026, 3, 10)  # a lunar holiday the rule calendar does not know
    observed = {f"S{i}": [d for d in sessions if d != eid] for i in range(6)}
    closures = infer_market_closures(cal, observed, start, end)
    assert list(closures) == [eid]
    # A single series missing a day is not a market closure.
    observed["S0"] = [d for d in sessions if d not in (eid, date(2026, 3, 4))]
    assert date(2026, 3, 4) not in infer_market_closures(cal, observed, start, end)


# --- Coverage ---------------------------------------------------------------


def _weekdays(start, end):
    return SessionCalendar("T", {}).sessions(start, end)


def test_silent_truncation_fails():
    cal = SessionCalendar("T", {})
    start, end = date(2018, 1, 1), date(2026, 11, 24)
    obs = _weekdays(date(2024, 11, 25), end)  # what a two-year plan returns
    r = validate_series_coverage("X", obs, cal, start, end)
    assert r.status == "fail"
    assert any("silent truncation" in f for f in r.failures)
    assert r.coverage_ratio < 0.3


def test_listing_date_excuses_late_start():
    cal = SessionCalendar("T", {})
    start, end = date(2018, 1, 1), date(2026, 11, 24)
    listing = date(2022, 1, 5)
    r = validate_series_coverage(
        "BUAFOODS", _weekdays(listing, end), cal, start, end,
        listing_date=listing, listing_verified=True,
    )
    assert r.status == "pass"
    assert r.effective_start == listing


def test_unverified_listing_date_warns():
    cal = SessionCalendar("T", {})
    start, end = date(2018, 1, 1), date(2018, 3, 30)
    r = validate_series_coverage(
        "X", _weekdays(date(2018, 2, 1), end), cal, start, end,
        listing_date=date(2018, 2, 1), listing_verified=False,
    )
    assert r.status == "warn"


def test_internal_gap_and_suspension_classification():
    cal = SessionCalendar("T", {})
    start, end = date(2026, 1, 1), date(2026, 6, 30)
    all_days = _weekdays(start, end)
    gap = set(_weekdays(date(2026, 3, 2), date(2026, 3, 27)))  # 20 sessions
    obs = [d for d in all_days if d not in gap]
    r = validate_series_coverage("X", obs, cal, start, end)
    assert r.status == "fail"
    assert r.gaps[0].classification == "unexplained"
    assert r.gaps[0].sessions == 20
    r2 = validate_series_coverage(
        "X", obs, cal, start, end,
        suspensions=[(date(2026, 3, 2), date(2026, 3, 27), "regulatory suspension")],
    )
    assert r2.status == "pass"
    assert r2.gaps[0].classification == "known_suspension"
    assert r2.excused_sessions == 20


def test_stale_end_fails_and_off_calendar_warns():
    cal = SessionCalendar("T", {})
    start, end = date(2026, 1, 1), date(2026, 6, 30)
    r = validate_series_coverage("X", _weekdays(start, date(2026, 5, 29)), cal, start, end)
    assert any("stale series" in f for f in r.failures)
    with_weekend = _weekdays(start, end) + [date(2026, 1, 3)]
    r2 = validate_series_coverage("X", with_weekend, cal, start, end)
    assert r2.status == "warn"
    assert r2.off_calendar_observations == 1


def test_fx_daily_calendar_expects_weekends():
    cal = fx_daily_calendar(date(2026, 10, 1), date(2026, 11, 24))
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(55)]
    r = validate_series_coverage("USD/NGN@ngnmarket", days, cal, days[0], days[-1])
    assert r.expected_sessions == 55
    assert r.status == "pass"


# --- FX derivation, reconciliation, evidence ----------------------------------


def _series(fc, tc, rates, provider="p"):
    d0 = date(2026, 10, 1)
    return HistoricalFXSeries(
        fc, tc,
        [HistoricalFXRate(fc, tc, r, d0 + timedelta(days=i), provider_name=provider)
         for i, r in enumerate(rates)],
        provider, None, d0, d0 + timedelta(days=len(rates)),  # type: ignore[arg-type]
    )


def test_ngnmarket_naira_pairs_derive_the_full_fx_set():
    usd_ngn = _series("USD", "NGN", [1400.0, 1410.0])
    eur_ngn = _series("EUR", "NGN", [1610.0, 1635.0])
    derived = derive_fx_set({"USD/NGN": usd_ngn, "EUR/NGN": eur_ngn})
    assert set(derived) == {"NGN/USD", "NGN/EUR", "EUR/USD", "USD/EUR"}
    assert derived["EUR/USD"].rates[0].rate == pytest.approx(1610.0 / 1400.0)
    assert derived["USD/EUR"].rates[1].rate == pytest.approx(1410.0 / 1635.0)


def test_reconciliation_reports_provider_divergence():
    a = _series("USD", "NGN", [1400.0, 1410.0, 1420.0], "ngnmarket")
    b = _series("USD", "NGN", [1400.0, 1480.0, 1421.0], "alpha_vantage")
    rec = reconcile_fx(a, b)
    assert rec["common_dates"] == 3
    assert rec["days_diff_over_1pct"] == 1
    assert rec["max_diff_date"] == "2026-10-02"


def test_coverage_failure_blocks_official_evidence():
    manifest = {
        "earliest_available_date_by_symbol": {"MTNN": "2024-11-25"},
        "coverage_validation": {
            "series": {
                "MTNN": {
                    "status": "fail",
                    "failures": ["suspected silent truncation: ..."],
                    "warnings": [],
                }
            }
        },
    }
    result = validate_provider_coverage(manifest, ["MTNN"], [])
    assert not result["passed"]
    assert any("silent truncation" in e for e in result["blocking_errors"])


# --- Builder end to end -----------------------------------------------------


class _FakeProvider:
    def __init__(self, name, cache_dir, equities=None, fx=None):
        from app.historical_data.models import HistoricalBar, HistoricalSeries

        self.provider_name = name
        self.cache = HistoricalDataCache(cache_dir)
        self.cache_paths: list[str] = []
        self.raw_response_paths: list[str] = []
        self.quota_metadata: dict = {}
        self.total_api_call_count = 0
        self.provider_coverage_summary: dict = {}
        self._equities = equities or {}
        self._fx = fx or {}
        self._bar = HistoricalBar
        self._series = HistoricalSeries

    def fetch_company_list(self, *a, **k):
        return [{"symbol": s} for s in self._equities]

    def fetch_equity_history(self, symbol, start, end, **k):
        days = self._equities[symbol]
        bars = [self._bar(symbol, d, 1.0, 1.0, 1.0, 1.0, field_provenance={
            "open": "reported", "high": "imputed_from_close",
            "low": "imputed_from_close", "close": "reported"}) for d in days]
        self.provider_coverage_summary = {"fetched": 1, "api_call_count": 1}
        return self._series(symbol, bars, self.provider_name, None, start, end)  # type: ignore[arg-type]

    def fetch_fx_history(self, fc, tc, start, end, **k):
        pair = f"{fc}/{tc}"
        if pair not in self._fx:
            raise ProviderError("no data", provider=self.provider_name, code="empty_coverage")
        days, rate = self._fx[pair]
        series = HistoricalFXSeries(
            fc, tc, [HistoricalFXRate(fc, tc, rate, d, provider_name=self.provider_name)
                     for d in days],
            self.provider_name, None, start, end,  # type: ignore[arg-type]
        )
        self.cache_paths.append(f"{self.provider_name}:{pair}.json")
        self.provider_coverage_summary = {"fetched": 1, "api_call_count": 1}
        return series


def test_builder_selects_fx_sources_and_flags_truncated_equities(tmp_path, monkeypatch):
    from app.simulations import data_builder
    from app.simulations.config.official_validation_universe import NGX_UNIVERSE

    start, end = date(2024, 1, 1), date(2024, 12, 31)
    weekdays = SessionCalendar("T", {}).sessions(start, end)
    every_day = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    late = [d for d in weekdays if d >= date(2024, 7, 1)]
    equities = {s: weekdays for s in NGX_UNIVERSE}
    equities["DANGCEM"] = late  # truncated by the provider
    ngn = _FakeProvider(
        "ngnmarket", str(tmp_path / "n"), equities,
        {"USD/NGN": (every_day, 1500.0), "EUR/NGN": (every_day, 1650.0)},
    )
    av = _FakeProvider(
        "alpha_vantage", str(tmp_path / "a"), {},
        {"USD/NGN": (weekdays, 1515.0), "EUR/USD": (weekdays, 1.09)},
    )
    monkeypatch.setattr(data_builder, "NGNMarketHistoricalProvider", lambda: ngn)
    monkeypatch.setattr(data_builder, "AlphaVantageHistoricalProvider", lambda: av)
    monkeypatch.setattr(data_builder, "AlpacaTradingHistoricalProvider", lambda: _FakeProvider("alpaca", str(tmp_path / "x")))
    m = data_builder.OfficialDatasetBuilder(["ngnmarket", "alpha_vantage"]).build(
        start, end, dry_run=False
    )
    sel = m.fx_source_selection
    assert sel["USD/NGN"]["selected_provider"] == "ngnmarket"
    assert sel["EUR/USD"]["selected_provider"] == "alpha_vantage"
    assert sel["EUR/NGN"]["selected_provider"] == "ngnmarket"
    # The unselected provider's USD/NGN cache is kept aside, not replayed.
    assert "alpha_vantage:USD/NGN.json" in m.alternate_fx_cache_paths
    assert "alpha_vantage:USD/NGN.json" not in m.cache_paths
    assert m.fx_reconciliation["USD/NGN"]["median_abs_rel_diff"] == pytest.approx(0.01)
    cov = m.coverage_validation
    assert cov["series"]["DANGCEM"]["status"] == "fail"
    assert "DANGCEM" in cov["summary"]["failing_series"]
    assert cov["series"]["USD/NGN"]["calendar"] == "FX-DAILY"
    assert m.imputation_summary["MTNN"]["imputed_by_field"]["high"] == len(weekdays)
    assert all(p in m.earliest_available_date_by_symbol for p in
               ["USD/NGN", "NGN/USD", "EUR/USD", "USD/EUR", "EUR/NGN", "NGN/EUR"])
    assert not [k for k in m.earliest_available_date_by_symbol if "@" in k]
    evidence = validate_provider_coverage(m, ["DANGCEM", "MTNN"], ["USD/NGN"])
    assert any("DANGCEM" in e for e in evidence["blocking_errors"])
    assert not any("MTNN" in e for e in evidence["blocking_errors"])


def test_snapshot_round_trip_and_tamper_detection(tmp_path):
    import json

    from app.historical_data.snapshot import (
        create_snapshot,
        ledger_entry,
        verify_snapshot,
    )

    cache = HistoricalDataCache(str(tmp_path / "cache"))
    norm = cache.write("normalized", {"provider_name": "ngnmarket", "bars": []}, k=1)
    raw = cache.write("raw", {"success": True}, k=1)
    manifest = tmp_path / "m.manifest.json"
    manifest.write_text(json.dumps({
        "dataset_id": "ds1", "provider": "ngnmarket", "checksum": "abc",
        "start_date": "2024-01-01", "end_date": "2024-12-31",
        "cache_paths": [str(norm)], "raw_response_paths": [str(raw)],
        "coverage_validation": {"summary": {"status": "pass"}},
    }))
    root = create_snapshot(manifest, tmp_path / "snaps")
    ok = verify_snapshot(root)
    assert ok["ok"], ok["problems"]
    snap = json.loads((root / "SNAPSHOT.json").read_text())
    assert snap["file_count"] == 3
    assert snap["licences"]["ngnmarket"]["status"] == "review_required"
    # Same inputs give the same snapshot id.
    assert create_snapshot(manifest, tmp_path / "snaps2").name == "ds1"
    again = json.loads((tmp_path / "snaps2" / "ds1" / "SNAPSHOT.json").read_text())
    assert again["snapshot_id"] == snap["snapshot_id"]
    entry = ledger_entry(root)
    assert "entries" not in entry and entry["licence_status"] == {"ngnmarket": "review_required"}
    victim = next((root / "files").glob("*.json"))
    victim.write_text("{}")
    bad = verify_snapshot(root)
    assert not bad["ok"] and "checksum mismatch" in bad["problems"][0]
