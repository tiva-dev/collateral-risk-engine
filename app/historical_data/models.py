from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.market_data.identity import InstrumentIdentity


def canonical_series_payload(
    series: HistoricalSeries | HistoricalFXSeries,
) -> dict[str, Any]:
    """Return the provider-independent representation accepted by official replay."""
    from dataclasses import asdict

    payload = asdict(series)
    payload["cache_schema"] = (
        "historical_fx_series/v1"
        if isinstance(series, HistoricalFXSeries)
        else "historical_series/v1"
    )
    return payload


# Field-level provenance labels for OHLC values. "reported" means the provider
# supplied the value; anything else means the adapter filled it.
PROVENANCE_REPORTED = "reported"
PROVENANCE_IMPUTED_FROM_CLOSE = "imputed_from_close"
OHLC_FIELDS = ("open", "high", "low", "close")

# Whether the bar reflects trading on that session.
TRADE_STATUS_TRADED = "traded"
TRADE_STATUS_NO_TRADE = "no_trade"
TRADE_STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class HistoricalBar:
    instrument: str
    timestamp: datetime | date
    open: float
    high: float
    low: float
    close: float
    adjusted_close: float | None = None
    volume: float = 0.0
    value_traded: float | None = None
    currency: str = "USD"
    source: str = "historical_provider"
    provider_name: str = "unknown"
    instrument_identity: InstrumentIdentity | None = None
    data_quality_score: float = 1.0
    warnings: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    # Per-field provenance, e.g. {"open": "reported", "high": "imputed_from_close"}.
    # Empty means the adapter did not record provenance (legacy caches).
    field_provenance: dict[str, str] = field(default_factory=dict)
    trade_status: str = TRADE_STATUS_UNKNOWN

    def imputed_fields(self) -> list[str]:
        return [
            f for f in OHLC_FIELDS
            if self.field_provenance.get(f, PROVENANCE_REPORTED) != PROVENANCE_REPORTED
        ]

    def has_reported_range(self) -> bool:
        """True only when high and low were both supplied by the provider.

        Range-based estimators (Parkinson, Garman-Klass, gap estimators) must
        not consume bars where this is False: an imputed high/low equal to the
        close understates the intraday range.
        """
        return bool(self.field_provenance) and not (
            {"high", "low"} & set(self.imputed_fields())
        )


@dataclass(frozen=True)
class HistoricalFXRate:
    from_currency: str
    to_currency: str
    rate: float
    timestamp: datetime | date
    source: str = "historical_provider"
    provider_name: str = "unknown"
    quality_score: float = 1.0
    warnings: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoricalSeries:
    instrument: str
    bars: list[HistoricalBar]
    provider_name: str
    retrieved_at: datetime
    start_date: date
    end_date: date
    interval: str = "1d"
    warnings: list[str] = field(default_factory=list)
    data_quality_summary: dict[str, Any] = field(default_factory=dict)
    instrument_identity: InstrumentIdentity | None = None


@dataclass(frozen=True)
class HistoricalFXSeries:
    from_currency: str
    to_currency: str
    rates: list[HistoricalFXRate]
    provider_name: str
    retrieved_at: datetime
    start_date: date
    end_date: date
    warnings: list[str] = field(default_factory=list)
    data_quality_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoricalDatasetManifest:
    dataset_id: str
    provider: str
    universe: dict[str, Any]
    instruments: list[str]
    fx_pairs: list[str]
    start_date: date
    end_date: date
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    cache_paths: list[str] = field(default_factory=list)
    raw_response_paths: list[str] = field(default_factory=list)
    checksum: str = ""
    provider_quota_metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)
    earliest_available_date_by_symbol: dict[str, date | str] = field(
        default_factory=dict
    )
    methodology_notes: list[str] = field(default_factory=list)
    missing_symbol_reasons: dict[str, str] = field(default_factory=dict)
    provider_coverage_summary: dict[str, Any] = field(default_factory=dict)
    instrument_identities: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Calendar-aware coverage verdict per series (see coverage.py).
    coverage_validation: dict[str, Any] = field(default_factory=dict)
    # Which provider's series is used for each FX pair, and why.
    fx_source_selection: dict[str, Any] = field(default_factory=dict)
    # Cross-provider comparison where more than one provider supplied a pair.
    fx_reconciliation: dict[str, Any] = field(default_factory=dict)
    # Normalized caches of FX series fetched but not selected for replay.
    alternate_fx_cache_paths: list[str] = field(default_factory=list)
    # Per-series OHLC imputation counts.
    imputation_summary: dict[str, Any] = field(default_factory=dict)
    # Extreme one-day moves and what explains them (corporate_actions.py).
    corporate_action_screen: dict[str, Any] = field(default_factory=dict)


def ohlc_with_provenance(
    row: dict[str, Any], keys: dict[str, str]
) -> tuple[dict[str, float], dict[str, str]] | None:
    """Read OHLC from a provider row, recording field-level provenance.

    ``keys`` maps "open"/"high"/"low"/"close" to the provider's field names.
    Returns None when the close is absent or blank: a bar with no close is
    not an observation and must not be defaulted to zero. Missing open, high
    or low are filled from the close and labelled imputed_from_close.
    """
    raw_close = row.get(keys["close"])
    if raw_close in (None, ""):
        return None
    close = float(raw_close)
    values: dict[str, float] = {"close": close}
    provenance: dict[str, str] = {"close": PROVENANCE_REPORTED}
    for name in ("open", "high", "low"):
        raw = row.get(keys[name])
        if raw in (None, ""):
            values[name] = close
            provenance[name] = PROVENANCE_IMPUTED_FROM_CLOSE
        else:
            values[name] = float(raw)
            provenance[name] = PROVENANCE_REPORTED
    return values, provenance
